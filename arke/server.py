"""arke server — Claude Code / Codex agents connected to this bus find and message each other.

    python3 server.py               listen on 127.0.0.1:7777
    python3 server.py --tailscale   also listen on this machine's Tailscale IP

Agents talk to it through client.py (CLI + harness hook); people through the web console at /.
Everything is plain JSON over HTTP — see /help. Stdlib only.

An agent is identified by its harness session id (Claude Code / Codex `session_id`).
Recipients: an agent name, `human`, a project path starting with `/` (reaches everyone
subscribed to it or any subproject), or `*` (everyone; use sparingly).

Layout (plain scripts in this folder, imported by name; none of them imports server.py at run time):
    server.py       settings, the Bus (agents, hooks, messages, projects), the background passes (watch_agents,
                    sample_procs), the event log (log, read_events), git history, ntfy, the HTTP handler, main
    transcripts.py  reading Claude Code / Codex transcripts for the console (read_transcript, condense), HttpError
    wake.py         Codex daemon turns, notices typed into tmux panes, busy/idle from transcripts
    native.py       Claude's background wake listener (not mailbox delivery)
    procs.py        processes on this machine, from /proc
    storage.py      the SQLite store, and the Agent / Message record types
    maintain.py     the backup command, run on the bus host (not used by the server)
    client.py       the CLI and harness hooks; roster.py the roster text; settings.py the TOML configuration
"""

from __future__ import annotations

import argparse
import collections
import copy
import fcntl
import gzip
import hashlib
import hmac
import ipaddress
import json
import math
import mimetypes
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import uuid
from bisect import bisect_left
from collections.abc import Callable, Generator, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import zip_longest
from operator import itemgetter
from pathlib import Path
from typing import Any, ClassVar, Literal, Never, TextIO
from urllib.parse import ParseResult, parse_qs, unquote, urlparse

import client  # shared settings (same folder)
import roster
import settings
from client import printable
from procs import cmdline, proc_table, process_info
from storage import Agent, Message, Store
from transcripts import TRANSCRIPT_ROOTS, HttpError, read_transcript, whole_lines
from wake import (
    TMUX_TIMEOUT_S,
    CodexRefused,
    WakeCancelled,
    codex_turn,
    harness_in_pane,
    prompt_box,
    squash,
    tmux_run,
    turn_marker,
    type_notice,
    wake_notice,
)

HERE: Path = Path(__file__).resolve().parent  # <clone>/arke
WEB: Path = HERE.parent / "web"  # the console and analytics pages, read per request
ASSETS: Path = (
    HERE.parent / ".local/web"
)  # downloaded by install-service.py, not checked into Git
SETTINGS: dict[str, Any] = client.SETTINGS.get("server", {})
STATE_DIR: Path = settings.state_dir(SETTINGS)
LOG_FILE: Path = settings.log_file(SETTINGS)
CLIENT: str = client.shell_path(
    HERE / "client.py"
)  # how agents run the client (same layout everywhere)
# which host counts as "this machine" for waking and transcripts
LOCAL_HOST: str = socket.gethostname()
NTFY_URL: str = os.environ.get("ARKE_NTFY", SETTINGS.get("ntfy_url", ""))  # empty = off
# extra Host names browsers may use for the pages (a reverse proxy, a DNS alias); see browser_host
BROWSER_HOSTS: list[str] = [h.lower() for h in SETTINGS.get("browser_hosts", [])]
PORT: int = client.PORT
WAKE_BATCH_S: float = (
    1.5  # wait this long after the first message so a burst becomes one wake-up
)
MAX_WAIT_S: float = 600
ROSTER_COOLDOWN_S: float = 60
ROSTER_CONTEXT_CHARS: int = 6000
# Codex's ~2,500-token hook cap uses UTF-8 bytes / 4; leave room for its wrapper.
# A character budget lets Unicode mail exceed it and be acknowledged without reaching the model.
MAIL_CONTEXT_BYTES: int = 8000
MAX_BODY: int = 10_000_000  # POST bodies over this are refused
MAX_TEXT_BYTES: int = (
    100_000  # message text; every log poll and transcript view returns it in full
)
HOOK_STR: tuple[str, ...] = (
    "session_id",
    "event",
    "kind",
    "host",
    "cwd",
    "branch",
    "tmux",
    "transcript",
    "model",
    "tmux_session",
    "effort",
    "repo",
    "tool",
    "target",
    "desc",
    "process_token",
    "boot",
    "tmux_server",
)  # strings (or null)
HOOK_NUM: tuple[str, ...] = ("pid", "tokens", "context", "window")
HOOK_EVENTS: tuple[str, ...] = (
    "SessionStart",
    "UserPromptSubmit",
    "PostToolUse",
    "Stop",
    "SessionEnd",
)
FILE_TEXT_MAX: int = 64_000  # /fileinfo includes a file's text up to this size
# path parts /fileinfo, /filesizes and /git never touch: env files, ssh/gpg dirs, keys, credential files
# (whole names, any case; see test_secret_names; .git and bare *.git repos: remote URLs can carry tokens).
# Only files that hold secrets themselves: templates (.env.example), public certs (*.pem; file_info drops
# private-key text) and code about credentials (credentials.py) stay readable.
SECRET: re.Pattern[str] = re.compile(
    r"\.env(?!\.(example|sample|template|dist)$)(\..*)?|.*\.env|\.envrc|\.ssh|\.gnupg|\.netrc|\.pgpass"
    r"|.*\.git|\.git-credentials|\.npmrc|\.pypirc|id_(rsa|dsa|ecdsa|ed25519)(_sk)?(\..*)?|.*\.(key|p12|pfx|jks|keystore)"
    r"|\.?credentials(\.(json|ya?ml|toml|ini|txt|csv))?|\..*_history|\.?secrets?(\.(json|ya?ml|toml|env|txt))?"
    r"|token\.json|key|auth\.json|hosts\.ya?ml|\.aws|\.kube|kubeconfig|client_secret.*\.json"
    r"|.*service[-_]account.*\.json|application_default_credentials\.json|.*\.tfstate(\..*)?|\.htpasswd"
    r"|\.vault-token|\.my\.cnf|\.s3cfg|rclone\.conf",
    re.IGNORECASE,
)
# a private key's text (PEM, PGP armor, PuTTY, age): file_info never returns it
PRIVATE_KEY: re.Pattern[str] = re.compile(
    r"PRIVATE KEY( BLOCK)?-----|^PuTTY-User-Key-File-|^AGE-SECRET-KEY-1", re.MULTILINE
)
# (limit, window seconds) per sender: DMs, per-project posts, broadcasts
LIMITS: dict[str, tuple[int, float]] = {
    "dm": (20, 60),
    "project": (6, 600),
    "broadcast": (3, 3600),
}
USAGE_FIELDS: tuple[str, ...] = ("model", "effort", "tokens", "context", "window")
MODES: tuple[str, ...] = ("notify", "board")
NAME_CHARS: str = "A-Za-z0-9._-"  # agent names get typed into terminals by the wake-up
# \Z, not $: $ also matches before a final newline, so "human\n" would pass .match
NAME_RE: re.Pattern[str] = re.compile(f"^[{NAME_CHARS}]{{1,40}}\\Z")
PROJECT_RE: re.Pattern[str] = re.compile(f"^(/[{NAME_CHARS}]+)+\\Z")
# a sub-agent id from the harness (Codex: a thread id; a Claude agent-team teammate: name@team)
HELPER_RE: re.Pattern[str] = re.compile(r"[A-Za-z0-9._@-]{1,100}")
VOLATILE: tuple[str, ...] = ("files", "note")  # in memory only
STALE_S: float = float(SETTINGS.get("stale_seconds", 900))
PANE_CACHE_S: float = (
    5.0  # tmux pane checks are cached briefly; the console polls every few seconds
)
LOG_LOCK: threading.Lock = threading.Lock()
PROC_S: float = float(SETTINGS.get("process_interval", 15))
HORATIO_URL: str = SETTINGS.get("horatio_url", "")  # empty = off


def log(event: str, **fields: Any) -> None:
    """Append a diagnostic event; log I/O failures must not change delivery or wake outcomes."""
    line: str = json.dumps({"ts": time.time(), "event": event, **fields})
    try:
        with LOG_LOCK, LOG_FILE.open("a+b") as f:
            if f.tell():
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    f.write(
                        b"\n"
                    )  # isolate a crash-truncated record; never join a new event onto it
            f.write((line + "\n").encode())
    except OSError:
        traceback.print_exc()


def under(path: str, prefix: str) -> bool:
    """Project `path` is `prefix` or inside it."""
    return path == prefix or path.startswith(prefix + "/")


def clean(s: str, n: int) -> str:
    return re.sub(f"[^{NAME_CHARS}]", "", s)[:n]


PANES: dict[str, tuple[float, bool]] = {}  # tmux "socket:%pane" -> (checked at, alive)
HUNG: dict[str, float] = {}  # tmux socket -> when a check on it timed out


def pane_alive(tmux: str, fresh: bool = False) -> bool:
    """Does this tmux pane (on the server's own host) still exist? `fresh` skips the cache (takeover, wake)."""
    hit: tuple[float, bool] | None = PANES.get(tmux)
    if hit and not fresh and time.time() - hit[0] < PANE_CACHE_S:
        return hit[1]
    sock, pane = tmux.rsplit(":", 1)
    # called with the bus lock held: a hung tmux server costs one timeout per PANE_CACHE_S, not one per check.
    # Its panes count as alive meanwhile: no name is taken over a server we can't check
    if time.time() - HUNG.get(sock, 0) < PANE_CACHE_S:
        return True
    try:
        r: subprocess.CompletedProcess[str] = subprocess.run(
            ["tmux", "-S", sock, "display-message", "-p", "-t", pane, "#{pane_id}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=TMUX_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        HUNG[sock] = time.time()
        return True
    except OSError:  # no tmux on the server's PATH
        return True  # can't tell: unknown counts as alive (not cached, so the next look asks again)
    alive: bool = r.returncode == 0 and r.stdout.strip() == pane
    PANES[tmux] = (time.time(), alive)
    return alive


def is_human(name: str) -> bool:
    """`human` is the person at the console; `Human` or `human_` would pass for them in headers and wake-ups."""
    return re.sub(r"[._-]", "", name.casefold()) == "human"


def where(a: dict[str, Any]) -> str:
    """Where an agent runs, for messages: host, plus tmux session/pane when known."""
    return (
        " ".join(
            x
            for x in (
                a.get("host"),
                a.get("tmux_session") and f"tmux {a['tmux_session']}",
                a.get("tmux") and a["tmux"].rsplit(":", 1)[1],
            )
            if x
        )
        or "unknown host"
    )


def candidates(body: dict[str, Any]) -> list[str]:
    """Names this registrant would like, best first: ARKE_NAME, tmux session, session-window, then <kind>-<dir>."""
    kind: str = clean(body.get("kind") or "agent", 12) or "agent"
    # the directory's end (`...-config-1` vs `-2` must not become the same name)
    tail: str = clean(os.path.basename(body.get("cwd") or ""), 1000)[
        -(39 - len(kind)) :
    ]
    raw: list[str] = [str(n) for n in body.get("names") or []] + [
        f"{kind}-{tail or 'x'}"
    ]
    out: list[str] = []
    for n in raw:
        c: str = clean(n, 1000)
        if (
            c and len(c) <= 40 and not is_human(c) and c not in out
        ):  # too long: skipped, never cut
            out.append(c)
    return out


class Bus:
    def __init__(self, state_file: Path) -> None:
        self.state_file: Path = state_file.with_suffix(".sqlite3")
        self.store: Store = Store(self.state_file)
        self.cond: threading.Condition = (
            threading.Condition()
        )  # guards everything; notified on new mail
        self.dirty: bool = False  # save() was called in this change: commit it
        self.pending_events: list[tuple[str, dict[str, Any]]] | None = None
        self.agents: dict[str, Agent] = {}  # sid -> agent
        self.wake_waiters: dict[
            str, dict[str, Any]
        ] = {}  # live requests only, never persisted
        self.wake_running: set[str] = set()  # external dispatches currently executing
        self.retired: dict[
            str, dict[str, Any]
        ] = {}  # superseded sessions; late hooks must not reclaim their seat
        self.left: dict[
            str, list[str]
        ] = {}  # sid -> process tokens it has left (takeover, or resumed elsewhere)
        self.messages: list[Message] = []
        self.next_id: int = 1
        self.sends: dict[str, list[float]] = collections.defaultdict(
            list
        )  # rate-limit key -> send times
        self.colors: dict[
            str, str
        ] = {}  # agent name or /project -> "#rrggbb" chosen in the console
        self.procs_now: dict[
            str, Any
        ] = {}  # latest sample_procs() totals per agent name
        self.roster_seen: dict[
            str, tuple[str, float]
        ] = {}  # advisory, never persisted or acknowledged
        saved: dict[str, Any] | None = self.store.load()
        if saved is None:
            with self.change():  # a new database
                self.save()
        else:
            self.agents = saved["agents"]
            self.messages = saved["messages"]
            self.next_id = saved["next_id"]
            self.colors = saved.get("colors", {})
            self.retired = saved.get("retired", {})
            self.left = saved.get("left", {})
            print(
                f"loaded {len(self.agents)} agents, {len(self.messages)} messages",
                flush=True,
            )

    def save(self) -> None:
        """Mark this change() for commit; it commits before returning."""
        assert self.pending_events is not None, "save() needs a bus change"
        self.dirty = True

    def record(self, event: str, **fields: Any) -> None:
        """Stage a mutation's event; change() writes it only after the state commit."""
        assert self.pending_events is not None, "events require a bus change"
        self.pending_events.append(
            (event, copy.deepcopy({"ts": time.time(), **fields}))
        )

    def snapshot(self) -> dict[str, Any]:
        """For rollback; runs on every change(), so copy only what is edited in place: `files` lists, retired records
        and rate-limit lists are only ever replaced, never edited; the colors dict is edited, so it's copied whole."""
        refs: dict[str, dict[str, Any]] = dict(self.agents)
        return {
            "agents": {
                sid: {k: v if k == "files" else copy.deepcopy(v) for k, v in a.items()}
                for sid, a in refs.items()
            },
            "retired": dict(self.retired),
            "left": {sid: list(tokens) for sid, tokens in self.left.items()},
            "colors": dict(self.colors),
            "sends": copy.copy(self.sends),
            "refs": refs,
            "messages": list(self.messages),
            "next_id": self.next_id,
        }

    def restore(self, state: dict[str, Any]) -> None:
        # Keep existing agent identities stable for waiting inbox readers.
        for sid, a in state["refs"].items():
            a.clear()
            a.update(state["agents"][sid])
        self.agents = state["refs"]
        for key in ("retired", "left", "colors", "sends", "messages", "next_id"):
            setattr(self, key, state[key])

    @contextmanager
    def change(self, *, messages_changed: bool = True) -> Generator[None]:
        """One mutation at a time, under the bus lock: if save() was called, it commits (changed rows only) before
        anyone else sees it. An error, including a failed write, restores the state from before.
        Agent-only changes may skip comparing immutable message history; rollback still snapshots it.
        """
        with self.cond:
            if self.pending_events is not None:
                raise RuntimeError("nested bus changes are not supported")
            before: dict[str, Any] = self.snapshot()
            events: list[tuple[str, dict[str, Any]]] = []
            self.pending_events, self.dirty = events, False
            try:
                yield
                if self.dirty:
                    state: dict[str, Any] = {
                        k: getattr(self, k)
                        for k in ("messages", "next_id", "colors", "retired", "left")
                    }
                    state["agents"] = {
                        sid: {k: v for k, v in a.items() if k not in VOLATILE}
                        for sid, a in self.agents.items()
                    }
                    if not messages_changed:
                        assert self.next_id == before["next_id"], (
                            "message added in agent-only change"
                        )
                        assert len(self.messages) == len(before["messages"]), (
                            "history size changed in agent-only change"
                        )
                        state.pop("messages")
                    self.store.commit(state)
            except BaseException:
                self.restore(before)
                raise
            finally:
                self.pending_events = None
            self.cond.notify_all()
            for event, fields in events:
                log(
                    event, **fields
                )  # prints a failed write (full disk); the change is already committed

    # --- lookups ---

    def by_name(self, name: str) -> Agent | None:
        return next((a for a in self.agents.values() if a["name"] == name), None)

    def message(self, mid: int) -> Message | None:
        """Caller holds cond. IDs increase on send; loading and trimming preserve order."""
        i: int = bisect_left(self.messages, mid, key=itemgetter("id"))
        if i < len(self.messages) and self.messages[i]["id"] == mid:
            return self.messages[i]
        return None

    def sender(self, sid: str, as_name: str, tmux: str = "") -> str:
        """Who is making this request: a registered session, or `human` / an agent name via X-Agent-As."""
        if sid:
            a: Agent | None = self.agents.get(sid)
            if a is None:
                raise HttpError(
                    401, "unknown session; the hook registers it on the next event"
                )
            if tmux and a.get("tmux") and tmux != a["tmux"]:
                # someone exported another agent's session id by hand: acting on it would mix identities
                raise HttpError(
                    409,
                    f"session {sid[:8]}... belongs to {a['name']} in tmux pane {a['tmux'].rsplit(':', 1)[1]}, "
                    f"but this call comes from pane {tmux.rpartition(':')[2]}. Don't set "
                    "CLAUDE_CODE_SESSION_ID / CODEX_THREAD_ID by hand; unset them and let the harness provide them.",
                )
            return str(a["name"])
        if as_name == "human":
            return as_name
        held: Agent | None = self.by_name(as_name) if as_name else None
        if held is not None:
            if (
                tmux
                and held.get("tmux")
                and tmux != held["tmux"]
                and self.reachable(held)[0] == "yes"
            ):
                raise HttpError(
                    409,
                    f"ARKE_AS={as_name} is a live agent in another tmux pane; you are not it. Unset ARKE_AS "
                    "(it is inherited by child processes) and let the harness identify you.",
                )
            return as_name
        raise HttpError(
            400, "identify with X-Agent-Session (agents) or X-Agent-As: human"
        )

    def agent_for(self, sid: str, as_name: str, tmux: str = "") -> Agent:
        with self.cond:
            name: str = self.sender(sid, as_name, tmux)
            a: Agent | None = self.by_name(name)
            if a is None:
                raise HttpError(400, "this needs an agent, not `human`")
            return a

    def reachable(self, a: Agent) -> tuple[str, str]:
        """("yes" | "gone" | "stale", why). Only what we can actually check; unknown counts as reachable."""
        if a["state"] == "offline":
            return "yes", ""
        if a.get("host") == LOCAL_HOST and a.get("tmux") and not pane_alive(a["tmux"]):
            return (
                "gone",
                f"its tmux pane {a['tmux'].rsplit(':', 1)[1]} no longer exists",
            )
        idle_for: float = time.time() - (a.get("last_active") or time.time())
        if a["state"] == "busy" and idle_for > STALE_S:
            return "stale", f"busy but no hook activity for {int(idle_for / 60)}m"
        return "yes", ""

    def same_seat(self, o: Agent, body: dict[str, Any]) -> bool:
        """The same tmux pane (a restart in it). Pane ids start over when the tmux server does: compare its pid too
        (absent in records from older clients)."""
        return bool(
            body.get("tmux")
            and o.get("tmux") == body["tmux"]
            and o.get("host") == body.get("host")
            and o.get("tmux_server", body.get("tmux_server")) == body.get("tmux_server")
        )

    def replacement_reason(
        self,
        holder: Agent,
        body: dict[str, Any],
        action: Literal["register", "rename", "seat"],
    ) -> str | None:
        """Why this session may inherit the holder's mailbox, or None. Caller holds cond.

        Seat replacement is independent of the requested name. Automatic name claims exclude
        nested sessions and daemon conversations; explicit rename can claim an ended one.
        """
        if action == "seat":
            return (
                "same tmux pane, newer session"
                if not (
                    body.get("nested") or body.get("daemon") or holder.get("daemon")
                )
                and self.same_seat(holder, body)
                else None
            )
        if action == "register" and (
            body.get("nested") or body.get("daemon") or holder.get("daemon")
        ):
            return None
        if holder["state"] == "offline":
            return "previous session ended"
        if self.same_seat(holder, body):
            return "same tmux pane, newer session"
        if (
            body.get("boot")
            and holder.get("boot") not in (None, body["boot"])
            and holder.get("host") == body.get("host")
        ):
            return "previous machine boot"
        if (
            not (body.get("daemon") or holder.get("daemon"))
            and body.get("process_token")
            and holder.get("process_token") == body["process_token"]
        ):
            return "same harness process, different session"
        if (
            holder.get("host") == LOCAL_HOST
            and holder.get("tmux")
            and not pane_alive(holder["tmux"], fresh=True)
        ):
            return "previous tmux pane is gone"
        return None

    def hook_ownership(self, body: dict[str, Any]) -> tuple[str, bool]:
        """Return (rejection reason, switching back in the same process), without mutation.

        Caller holds cond. A legitimate terminal A -> B -> A resume is the exception to
        rejecting a departed process; another thread in a shared daemon is not.
        """
        sid: str = body["session_id"]
        event: str = body["event"]
        token: Any = body.get("process_token")
        a: Agent | None = self.agents.get(sid)
        old: dict[str, Any] | None = self.retired.get(sid)
        switched_back: bool = bool(
            event == "SessionStart"
            and not body.get("daemon")
            and token
            and a is None
            and any(
                not o.get("daemon") and o.get("process_token") == token
                for o in self.agents.values()
            )
        )
        if token and token in self.left.get(sid, []) and not switched_back:
            return "this harness process was superseded", switched_back
        if a is None:
            if old is not None and not switched_back:
                new_process: bool = bool(
                    token
                    and token != old.get("process_token")
                    or body.get("host")
                    and old.get("host")
                    and body["host"] != old["host"]
                    or body.get("pid")
                    and old.get("pid")
                    and body["pid"] != old["pid"]
                )
                occupied: bool = any(
                    o["state"] != "offline"
                    and (
                        o["name"] == old.get("name")
                        or old.get("tmux")
                        and o.get("tmux") == old["tmux"]
                        and o.get("host") == old.get("host")
                    )
                    for o in self.agents.values()
                )
                if event != "SessionStart" or not new_process and occupied:
                    return "this session was superseded", switched_back
            if event == "SessionEnd":
                return "session already ended or unknown", switched_back
        return "", switched_back

    def retire(
        self, old: Agent, new: Agent, why: str, *, continuation: bool = False
    ) -> None:
        """Drop a superseded record; its unread mail and its names (current and earlier) carry over to its successor."""
        if continuation:
            # The destination pane's settings win conflicts when an already-known session resumes here.
            # Keep its unsubscribes and default-project marker, including when it deliberately left that project.
            new["projects"] |= old["projects"]
            if old.get("default_project"):
                new["default_project"] = old["default_project"]
            if old.get("about"):
                new["about"] = old["about"]
            if old.get("named"):
                if new["name"] != old["name"]:
                    new.setdefault("aka", []).append(new["name"])
                new["name"], new["named"] = old["name"], True
        merged: dict[
            int, list[Any]
        ] = {}  # one entry per message (both may hold the same project post)
        for item in old["inbox"] + new.get("inbox", []):
            have: list[Any] | None = merged.get(item[0])
            if have is None:
                merged[item[0]] = list(item)
            else:  # keep the stronger delivery: notify over board, direct over not
                have[1] = "notify" if "notify" in (have[1], item[1]) else have[1]
                have[2] = have[2] or item[2]
        new["inbox"] = sorted(
            merged.values()
        )  # id order: a wake batch is named by its last id
        new["aka"] = [
            n
            for n in dict.fromkeys(
                old.get("aka", []) + [old["name"]] + new.get("aka", [])
            )
            if n != new["name"]
        ]
        self.retired[old["sid"]] = {
            k: old.get(k) for k in ("name", "pid", "host", "tmux", "process_token")
        } | {"at": time.time()}
        self.leave(old["sid"], old.get("process_token"))
        del self.agents[old["sid"]]
        self.cond.notify_all()  # pending inbox readers must recheck their session's ownership
        self.record(
            "takeover",
            agent=old["name"],
            old_sid=old["sid"],
            new_sid=new["sid"],
            new_name=new["name"],
            why=why,
        )

    def leave(self, sid: str, token: str | None) -> None:
        """Hooks still queued in a process this session has left must not act for it any more."""
        if token and token not in self.left.setdefault(sid, []):
            self.left[sid].append(token)

    def resolve(self, to: list[str], sender: str) -> dict[str, Any]:
        """Expand recipients under cond, without external I/O (the same rule /send uses)."""
        reach: dict[str, None] = {}  # ordered set
        unknown: list[str] = []
        unsubscribed: list[
            str
        ] = []  # projects that exist but where every member has unsubscribed
        renamed: list[
            dict[str, str]
        ] = []  # old names in `to`, and who carries them now
        ambiguous: list[
            dict[str, Any]
        ] = []  # names that are (or were) used by more than one agent

        def former(name: str) -> list[Agent]:
            return [a for a in self.agents.values() if name in a.get("aka", [])]

        kinds: list[dict[str, str]] = []
        for t in to:
            if t == "*":
                kinds.append({"name": t, "kind": "broadcast"})
                reach.update(dict.fromkeys(a["name"] for a in self.agents.values()))
            elif t == "human":
                kinds.append({"name": t, "kind": "human"})
                reach["human"] = None
            elif t.startswith("/"):
                kinds.append({"name": t, "kind": "project"})
                hit: list[str] = [
                    a["name"]
                    for a in self.agents.values()
                    if any(sub and under(p, t) for p, sub in a["projects"].items())
                ]
                if hit:
                    reach.update(dict.fromkeys(hit))
                elif any(
                    under(p, t) for a in self.agents.values() for p in a["projects"]
                ):
                    unsubscribed.append(t)
                else:
                    unknown.append(t)
            elif self.by_name(t) or former(t):
                was: list[str] = [
                    a["name"] for a in former(t)
                ]  # agents that used to be called t
                if self.by_name(t):
                    # the current holder wins; say so if the name also belonged to someone else before
                    kinds.append({"name": t, "kind": "agent"})
                    reach[t] = None
                    if was:
                        ambiguous.append(
                            {"name": t, "delivered_to": t, "also_old_name_of": was}
                        )
                elif len(was) == 1:
                    # an old name (renamed, or carried over by the session that replaced it): deliver to the current one
                    kinds.append({"name": was[0], "kind": "agent", "was": t})
                    reach[was[0]] = None
                    renamed.append({"old": t, "new": was[0]})
                else:
                    ambiguous.append(
                        {"name": t, "delivered_to": "", "also_old_name_of": was}
                    )
            else:
                unknown.append(t)
        reach.pop(sender, None)
        return {
            "to": kinds,
            "reach": list(reach),
            "unknown": unknown,
            "unsubscribed": unsubscribed,
            "renamed": renamed,
            "ambiguous": ambiguous,
            "has_project": any(k["kind"] in ("project", "broadcast") for k in kinds),
            "broadcast": "*" in to,
        }

    def unreachable(self, names: list[str]) -> list[dict[str, str]]:
        """Advisory reachability from a snapshot; call after releasing the bus lock."""
        with self.cond:
            agents: list[dict[str, Any]] = [
                dict(a) for name in names if (a := self.by_name(name)) is not None
            ]
        return [
            {"name": a["name"], "reachable": st[0], "why": st[1]}
            for a in agents
            if (st := self.reachable(a))[0] != "yes"
        ]

    # --- hook: one round trip per harness event ---

    def hook(self, body: dict[str, Any]) -> dict[str, Any]:
        # wrong types would be saved and break every later read of the agent (Path(list), str ops on ints)
        for k in HOOK_STR:
            if body.get(k) is not None and not isinstance(body[k], str):
                raise HttpError(400, f"hook field {k!r} must be a string")
            if "\x00" in (
                body.get(k) or ""
            ):  # saved, it would break every path and tmux call on this agent
                raise HttpError(400, f"hook field {k!r} contains a NUL byte")
        for k in HOOK_NUM:
            v: Any = body.get(k)
            if v is not None and (
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not math.isfinite(v)
            ):
                raise HttpError(400, f"hook field {k!r} must be a finite number")
        if body.get("tmux") and not re.fullmatch(
            r".+:%\d+", body["tmux"]
        ):  # $TMUX socket, then the pane id
            raise HttpError(
                400, f"hook field 'tmux' must be SOCKET:%PANE, not {body['tmux']!r}"
            )
        for k in ("nested", "failed", "wake", "receipt", "daemon"):
            if body.get(k) is not None and not isinstance(body[k], bool):
                raise HttpError(400, f"hook field {k!r} must be a boolean")
        if not isinstance(body.get("names") or [], list) or not all(
            isinstance(n, str) for n in body.get("names") or []
        ):
            raise HttpError(400, "hook field 'names' must be a list of strings")
        if not body.get("session_id"):
            raise HttpError(400, "hook needs a session_id")
        if body.get("event") not in HOOK_EVENTS:
            raise HttpError(400, f"unknown hook event {body.get('event')!r}")
        sid: str = body["session_id"]
        event: str = body["event"]
        for k in (
            "host",
            "tmux_session",
        ):  # shown to other agents: one line, like `about`
            if body.get(k):
                body = body | {k: " ".join(body[k].split())}
        # Codex 0.163+ runs every thread's hooks in one background daemon: its tmux pane and session are whichever pane
        # started the daemon, not this thread's. Such a thread is woken through the daemon (codex_wake), never typed into.
        if body.get("daemon"):
            body = body | {"tmux": "", "tmux_session": ""}
        with self.change(messages_changed=False):
            a: Agent | None = self.agents.get(sid)
            token: Any | None = body.get("process_token")
            rejection, switched_back = self.hook_ownership(body)
            if rejection:
                return {"ignored": True, "reason": rejection}
            if a is None:
                a = self.register(sid, body)
                if switched_back:
                    self.left[sid] = [t for t in self.left.get(sid, []) if t != token]
            elif token:
                previous_token: Any | None = a.get("process_token")
                if (
                    not previous_token
                    and a.get("daemon")
                    and a.get("pid_start") is not None
                ):
                    # Existing daemon records kept the observed lifetime even before hooks sent its token.
                    previous_token = f"{a['host']}:{a['pid']}:{a['pid_start']}:{sid}"
                if previous_token and token != previous_token:
                    self.leave(
                        sid, previous_token
                    )  # resumed in a newer process (SessionStart may have been missed)
            if event != "SessionEnd" and not (
                body.get("nested") or body.get("daemon", a.get("daemon"))
            ):
                # A resumed session already exists in agents, so register() alone cannot transfer its new pane.
                # Also handle a missed SessionStart; ownership above rejects hooks from superseded processes.
                for holder in list(self.agents.values()):
                    if holder is not a and self.replacement_reason(
                        holder, body, "seat"
                    ):
                        self.retire(
                            holder,
                            a,
                            "same tmux pane, resumed session",
                            continuation=True,
                        )
            harness_changed: bool = any(
                body.get(k) and body[k] != a.get(k)
                for k in ("pid", "host", "process_token")
            )
            if body.get("transcript") and body["transcript"] != a.get("transcript"):
                a.pop(
                    "turn_offset", None
                )  # offsets belong to this file, not just to the session id
            previous_pid: Any | None = a.get("pid")
            if body.get("branch") is not None:
                a["branch"] = body["branch"]
            for k in (
                "cwd",
                "tmux",
                "transcript",
                "model",
                "host",
                "tmux_session",
                "effort",
                "pid",
                "process_token",
                "boot",
                "tmux_server",
            ):
                if body.get(k):
                    a[k] = body[k]
            if body.get(
                "boot"
            ):  # that machine rebooted: its agents from before never sent SessionEnd
                for o in self.agents.values():
                    if (
                        o is not a
                        and o["state"] != "offline"
                        and o.get("host") == a["host"]
                        and o.get("boot") not in (None, body["boot"])
                    ):
                        self.set_state(o, "offline")
            if body.get("nested"):
                for k in ("tmux", "tmux_session", "pid", "pid_start"):
                    a.pop(k, None)
            if body.get("daemon") is True:
                a["daemon"] = True
                a.pop(
                    "tmux", None
                )  # inherited from before its harness moved into the daemon
                a.pop("tmux_session", None)
            elif body.get("daemon") is False:
                a.pop("daemon", None)
            if a.get("pid") and a.get("host") == LOCAL_HOST:
                proc: dict[str, Any] | None = process_info(a["pid"])
                if token:
                    expected: str = (
                        f"{a['host']}:{a['pid']}:{proc['start']}" if proc else ""
                    )
                    if expected and a.get("daemon"):
                        expected += f":{sid}"
                    if proc or harness_changed or "pid_start" not in a:
                        a["pid_start"] = (
                            proc["start"] if proc and token == expected else None
                        )
                elif a["pid"] != previous_pid or "pid_start" not in a:
                    a["pid_start"] = proc["start"] if proc else None
            usage: dict[str, Any] = {
                k: body[k]
                for k in ("tokens", "context", "window")
                if body.get(k) is not None
            }
            if any(
                a.get(k) != v for k, v in usage.items()
            ):  # every hook sends it: log changes
                a |= usage
                self.record(
                    "usage",
                    agent=a["name"],
                    sid=sid,
                    source="hook",
                    **{k: a.get(k) for k in USAGE_FIELDS},
                )
            a["last_active"] = time.time()
            repo: str = clean(body.get("repo") or "", 40)
            if repo and not a.get("default_project"):
                a["default_project"] = (
                    f"/{repo}"  # once: an agent may leave it later and it won't come back
                )
                if not a["projects"]:
                    a["projects"][f"/{repo}"] = True
                    self.record(
                        "project",
                        agent=a["name"],
                        project=f"/{repo}",
                        action="join",
                        why="default project",
                    )
            if body.get("tool"):
                self.record(
                    "tool",
                    agent=a["name"],
                    sid=sid,
                    tool=body["tool"],
                    target=str(body.get("target", ""))[:4000],
                    desc=str(body.get("desc") or "")[:300],
                    cwd=a.get("cwd"),
                    repo=body.get("repo"),
                    branch=a.get("branch"),
                    host=a.get("host"),
                    projects=list(a["projects"]),
                )
                # a new list, not an edit: snapshot() shares `files` instead of copying it
                a["files"] = [
                    {
                        "ts": time.time(),
                        "tool": body["tool"],
                        "target": str(body.get("target", ""))[:4000],
                        "desc": str(body.get("desc") or "")[:300],
                    },
                    *a.get("files", []),
                ][:20]
            out: dict[str, Any] = {"name": a["name"]}
            if (event == "SessionStart" or harness_changed) and (
                a.get("wake") or {}
            ).get("status") == "failed":
                # A revived harness gets another chance at its existing mail. Keep our rendered notice for a
                # lost Enter, but don't carry an exhausted failure budget into a new session lifetime.
                a["wake"] = {k: v for k, v in a["wake"].items() if k == "typed"}
            ids: list[int] = []
            match event:
                case "SessionStart":
                    # Claude re-runs SessionStart after compacting, auto-compaction included: mid-turn
                    if body.get("source") != "compact":
                        self.set_state(a, "idle")
                    out["context"] = self.intro(a)
                case "UserPromptSubmit":
                    self.record(
                        "prompt", agent=a["name"], sid=sid, wake=bool(body.get("wake"))
                    )
                    if body.get("wake") and a.get("wake"):
                        a["wake"] = a["wake"] | {
                            "status": "picked up",
                            "at": time.time(),
                            "detail": "the agent took the notice",
                        }
                    if not body.get("wake"):
                        a["last_prompt"] = time.time()
                    self.set_state(a, "busy")
                    out["context"], ids = self.prepare_delivery(a, everything=True)
                case "PostToolUse":
                    self.set_state(a, "busy")
                    out["context"], ids = self.prepare_delivery(a, everything=True)
                case "Stop":
                    # only direct mail blocks the stop; project posts wait for the next hook. A stop by API error
                    # (failed) can't be blocked: Claude ignores its output, so the mail waits for a wake-up
                    text: str = ""
                    if not body.get("failed"):
                        text, ids = self.prepare_delivery(a, everything=False)
                    if text:
                        out["block"] = text
                    else:
                        self.set_state(a, "idle")
                case "SessionEnd":
                    self.set_state(a, "offline")
            if ids:
                if body.get("receipt") and (out.get("context") or out.get("block")):
                    out["delivery"] = {"sid": sid, "ids": ids}
                else:
                    # Legacy hooks consume immediately. Obsolete board notices with no output
                    # also need no acknowledgment, matching the existing receipt behavior.
                    consumed: set[int] = set(ids)
                    a["inbox"] = [
                        item for item in a["inbox"] if item[0] not in consumed
                    ]
            # Advisory context must not influence receipt calculation or block a Stop, or crowd out mail.
            if event in ("UserPromptSubmit", "PostToolUse") and not out.get("context"):
                notice: str = self.roster_context(a, force=event == "UserPromptSubmit")
                if notice:
                    out["context"] = "\n\n".join(
                        filter(None, (out.get("context"), notice))
                    )
            self.save()
            return out

    def register(self, sid: str, body: dict[str, Any]) -> Agent:
        """New session: choose a free name or an eligible terminal replacement; supersede our seat."""
        kind: str = clean(body.get("kind") or "agent", 12) or "agent"
        # earlier sessions in this exact tmux pane (a restart); a name chosen with `arke name` sticks to the seat
        seat: list[Agent] = [
            o for o in self.agents.values() if self.replacement_reason(o, body, "seat")
        ]
        wanted: list[str] = [o["name"] for o in seat if o.get("named")] + candidates(
            body
        )
        a: Agent = {
            "sid": sid,
            "name": "",
            "kind": kind,
            "host": body.get("host", ""),
            "state": "idle",
            "started_at": time.time(),
            "about": "",
            "projects": {},
            "inbox": [],
        }
        self.agents[sid] = a
        blocked: list[str] = []
        for c in wanted:
            holder: Agent | None = self.by_name(c)
            if holder is None or self.replacement_reason(holder, body, "register"):
                a["name"] = c
                if holder is not None:
                    a["named"] = holder.get("named", False)
                    self.retire(
                        holder,
                        a,
                        "name taken over from a dead or superseded session",
                        continuation=holder in seat,
                    )
                break
            blocked.append(f"{c} (held by {holder['state']} agent at {where(holder)})")
        if not a["name"]:
            i: int = 2
            while True:
                suffix: str = f"-{i}"
                candidate: str = wanted[-1][: 40 - len(suffix)] + suffix
                if not self.by_name(candidate):
                    a["name"] = candidate
                    break
                i += 1
        assert NAME_RE.match(a["name"]), a["name"]
        for o in seat:
            if o["sid"] in self.agents and o is not a:
                self.retire(o, a, "same tmux pane, newer session", continuation=True)
        if a["name"] != wanted[0]:
            a["note"] = (
                f"You wanted the name {wanted[0]}, but: {'; '.join(blocked)}. You are {a['name']}; `arke name NEW` to change."
            )
        repo: str = clean(body.get("repo") or "", 40)
        if repo and not a.get("default_project"):
            a["default_project"] = f"/{repo}"
            if not a["projects"]:
                a["projects"][f"/{repo}"] = True
        self.record(
            "join",
            agent=a["name"],
            sid=sid,
            kind=kind,
            host=a["host"],
            wanted=wanted,
            projects=list(a["projects"]),
        )
        return a

    def rename(
        self, sid: str, as_name: str, body: dict[str, Any], tmux: str = ""
    ) -> dict[str, Any]:
        """`arke name NEW` renames the calling session; the console (X-Agent-As: human) renames {agent} instead."""
        new: str = str(body.get("name") or "")
        if not NAME_RE.match(new) or is_human(new):
            raise HttpError(
                400, "name must be 1-40 of A-Z a-z 0-9 . _ - and not `human`"
            )
        with self.change(messages_changed=False):
            if sid:
                a = self.agents.get(sid)
                if a is None:
                    raise HttpError(
                        401,
                        "unknown session; renaming needs your own session (run it inside the agent)",
                    )
                self.sender(
                    sid, as_name, tmux
                )  # the borrowed-session check every agent write makes
            elif as_name == "human":
                a = self.by_name(str(body.get("agent") or ""))
                if a is None:
                    raise HttpError(404, f"no agent named {body.get('agent')!r}")
            else:
                raise HttpError(
                    400,
                    "rename yourself from inside the agent (`arke name NEW`), or as human with {agent, name}",
                )
            holder: Agent | None = self.by_name(new)
            if holder is not None and holder is not a:
                if not self.replacement_reason(holder, {}, "rename"):
                    raise HttpError(
                        409, f"{new} is held by a live agent at {where(holder)}"
                    )
                self.retire(holder, a, "name taken over by rename")
            old: str = a["name"]
            if old != new:
                a.setdefault("aka", []).append(old)
            a["aka"] = [
                n for n in a.get("aka", []) if n != new
            ]  # back to an old name: not "also an old name of" itself
            a["name"] = new
            a["named"] = True
            self.record(
                "rename",
                agent=new,
                old=old,
                sid=a["sid"],
                by="human" if not sid else "self",
            )
            if old in self.colors:
                self.colors.setdefault(new, self.colors[old])
            self.save()
            return {"name": new, "old": old}

    def set_color(self, body: dict[str, Any]) -> dict[str, Any]:
        """Console color overrides: {target, color} for one, or {colors: {target: color}} for many at once
        (e.g. "re-roll all"). A null color resets that target to its default. All are checked before any is applied."""
        changes: dict[str, Any] = (
            body["colors"]
            if isinstance(body.get("colors"), dict)
            else {str(body.get("target") or ""): body.get("color")}
        )
        for target, color in changes.items():
            if not (NAME_RE.match(target) or PROJECT_RE.match(target)):
                raise HttpError(
                    400,
                    f"target must be an agent name or /project/path, got {target!r}",
                )
            if color is not None and not re.fullmatch(r"#[0-9a-fA-F]{6}", str(color)):
                raise HttpError(400, f"color for {target} must be #rrggbb or null")
        with self.change(messages_changed=False):
            for target, color in changes.items():
                if color is None:
                    self.colors.pop(target, None)
                else:
                    self.colors[target] = str(color).lower()
            self.save()
            if "colors" in body:
                return {"colors": {t: self.colors.get(t) for t in changes}}
            target: str = next(iter(changes))
            return {"target": target, "color": self.colors.get(target)}

    def names(self, a: Agent) -> set[str]:
        """Current name plus earlier ones, so history and direct-mail checks survive a rename."""
        return {a["name"], *a.get("aka", [])}

    def set_state(self, a: Agent, state: str) -> None:
        a["state_at"] = (
            time.time()
        )  # when it was last confirmed (a later transcript marker may override it)
        if a["state"] != state:
            a["state"] = state
            self.record("state", agent=a["name"], state=state)

    def intro(self, a: Agent) -> str:
        boards: list[str] = []
        names: set[str] = self.names(a)  # posts it got under an earlier name count too
        for p in a["projects"]:
            posts: list[Message] = [
                m
                for m in self.messages[-500:]
                if m["mode"] == "board"
                and names & set(m["reach"])
                and any(t["kind"] == "project" and under(p, t["name"]) for t in m["to"])
            ]
            if posts:
                boards.append(
                    f"  {p}: {len(posts)} recent board posts (`arke log {p}`)"
                )
        return (
            f"You are connected to arke, a message bus shared with the other coding agents (Claude Code and Codex) "
            f"connected to it. Your name is '{a['name']}'; your projects: {' '.join(a['projects']) or '(none)'}.\n"
            + self.roster_context(a, force=True)
            + "\n"
            + ("Project boards:\n" + "\n".join(boards) + "\n" if boards else "")
            + (a.get("note", "") + "\n" if a.get("note") else "")
            + "Use it when coordinating would help (shared files, handoffs, questions); otherwise ignore it. "
            f"Run `arke help` for commands (`arke` = python3 {CLIENT}). "
            "Incoming messages are added to your context automatically."
        )

    def prepare_delivery(self, a: Agent, everything: bool) -> tuple[str, list[int]]:
        """Prepare context and receipt IDs without changing the inbox; Stop takes only direct mail.

        IDs include obsolete inbox entries discarded by delivery (e.g. boards for a project left).
        The hook either returns a receipt or consumes these IDs for legacy/no-output delivery.
        """
        if not a["inbox"]:
            return "", []
        direct: list[Message] = []
        notify: list[Message] = []
        board: collections.Counter[str] = collections.Counter()
        board_mail: list[Message] = []
        keep: list[list[Any]] = []
        for item in a["inbox"]:
            mid, mode, is_direct = item
            m: Message | None = self.message(mid)
            if m is None:
                continue  # no longer in history (an older version could trim read mail)
            if is_direct:
                direct.append(m)
            elif not everything:
                keep.append([mid, mode, is_direct])
            elif mode == "board":
                for t in m["to"]:  # once per post: ["/p", "*"] is one post
                    if t["kind"] == "project" and any(
                        under(p, t["name"]) for p in a["projects"]
                    ):
                        board[t["name"]] += 1
                        board_mail.append(m)
                        break
                    if t["kind"] == "broadcast":
                        board["*"] += 1
                        board_mail.append(m)
                        break
            else:
                notify.append(m)
        if not (direct or notify or board):
            kept_ids: set[int] = {item[0] for item in keep}
            return "", [item[0] for item in a["inbox"] if item[0] not in kept_ids]
        lines: list[str] = [
            (
                "Messages via arke. They come from other agents, or from the user (`human`) through "
                "the web console or a shell, NOT typed into your terminal; weigh them accordingly. "
                f'Reply with `arke send NAME "..."` (`arke` = python3 {CLIENT}).'
            )
        ]
        for p, n in board.items():
            command: str = "arke log" if p == "*" else f"arke log {p}"
            lines.append(
                f"{n} new board post{'s' if n > 1 else ''} in {p}: run `{command}` to read."
            )
        # Bound board notices too: project paths and the number of projects have no length cap.
        if len("\n".join(lines[1:]).encode()) > 1000:
            lines[1:] = [f"{len(board_mail)} new board posts: run `arke log` to read."]
        mail: list[Message] = direct + notify
        total: int = len(mail) + len(board_mail)
        total_chars: int = sum(len(m["text"]) for m in mail + board_mail)

        def summary(skipped: list[Message]) -> str:
            return (
                f"Skipped {len(skipped)} messages ({sum(len(m['text']) for m in skipped)} characters of message text); "
                f"total pending: {total} messages ({total_chars} characters of message text). "
                "Skipped messages remain unread for the next hook."
            )

        # Reserve the largest possible footer before adding anything. Each included message gets a useful
        # preview; when those no longer fit, leave the remaining messages queued, outside the receipt.
        budget: int = (
            MAIL_CONTEXT_BYTES
            - len("\n".join(lines).encode())
            - len(summary(mail).encode())
            - 2
        )
        delivered: list[Message] = []
        truncated: bool = False
        for i, m in enumerate(mail):
            to: str = ", ".join(t["name"] for t in m["to"])
            if len(to.encode()) > 96:
                to = to.encode()[:96].decode("utf-8", "ignore") + "…"
            via: str = (
                {"console": " (web console)", "shell": " (shell)"}.get(
                    m.get("via", ""), ""
                )
                if m["from"] == "human"
                else ""
            )
            header: str = f"[#{m['id']} {m['from']}{via} -> {to}] "
            header_bytes: int = len(header.encode())
            # continuation lines indented: a body can't start a line that reads as another header or as text
            # outside the mail (splitlines: U+2028 and the rest break lines too). Terminal codes are escaped
            # here, before the budget is measured, since Claude shows Stop feedback on screen.
            text: str = "\n    ".join(printable(m["text"]).splitlines())
            raw: bytes = text.encode()
            cut: str = f"… (cut; full text: `arke log {m['from']}`)"
            # Reserve actual body text, separately from recipient names and the history pointer.
            minimum: int = header_bytes + min(len(raw), 256)
            if len(raw) > 256:
                minimum += len(cut.encode())
            if budget < minimum + 1:
                break
            share: int = min(budget - 1, max(minimum, budget // (len(mail) - i) - 1))
            line: str = header + text
            if header_bytes + len(raw) > share:
                line = (
                    header
                    + raw[: share - header_bytes - len(cut.encode())].decode(
                        "utf-8", "ignore"
                    )
                    + cut
                )
                truncated = True
            budget -= len(line.encode()) + 1
            lines.insert(1 + i, line)
            delivered.append(m)
        skipped: list[Message] = mail[len(delivered) :]
        skipped_ids: set[int] = {m["id"] for m in skipped}
        kept_ids: set[int] = skipped_ids | {item[0] for item in keep}
        ids: list[int] = [item[0] for item in a["inbox"] if item[0] not in kept_ids]
        if skipped or truncated:
            lines.append(summary(skipped))
        self.record(
            "delivered",
            agent=a["name"],
            ids=[m["id"] for m in delivered],
            board=dict(board),
        )
        return "\n".join(lines), ids

    # --- messages ---

    def send(
        self,
        sender: str,
        body: dict[str, Any],
        *,
        sid: str = "",
        tmux: str = "",
        helper: str = "",
    ) -> dict[str, Any]:
        recipients: Any | None = body.get("to")
        if not isinstance(recipients, list) or any(
            not isinstance(t, str) for t in recipients
        ):
            raise HttpError(400, "to must be a list of recipient names")
        to: list[str] = [t.strip() for t in recipients if t.strip()]
        if not isinstance(body.get("text", ""), str):
            raise HttpError(400, "text must be a string")
        text: str = (body.get("text") or "").strip()
        mode: str = body.get("mode") or "notify"
        request_id: Any = body.get("request_id", "")
        if not isinstance(request_id, str) or len(request_id) > 100:
            raise HttpError(
                400, "request_id must be a string of at most 100 characters"
            )
        if not to or not text:
            raise HttpError(400, "need at least one recipient and a non-empty message")
        message_text: str = (
            f"Sub-agent {helper} requests that you send the following to {', '.join(to)} "
            f"({mode}). Those recipients have NOT been contacted. Review and forward if appropriate.\n\n{text}"
            if helper
            else text
        )
        if (size := len(message_text.encode())) > MAX_TEXT_BYTES:
            raise HttpError(
                413,
                f"message of {size} bytes; the limit is {MAX_TEXT_BYTES}: send a file path instead",
            )
        if mode not in MODES:
            raise HttpError(400, f"mode must be one of {MODES}")
        if helper and (not sid or not HELPER_RE.fullmatch(helper)):
            raise HttpError(
                400, "helper requests require a parent session and a valid helper ID"
            )
        with self.change():
            sender = self.sender(sid, sender, tmux)
            src: Agent | None = self.by_name(sender)
            owner: str = (
                hashlib.sha256(src["sid"].encode()).hexdigest()
                if src is not None
                else "human"
            )
            if helper:
                owner = hashlib.sha256(json.dumps([sid, helper]).encode()).hexdigest()
            fingerprint: str = hashlib.sha256(
                json.dumps([to, text, mode]).encode()
            ).hexdigest()
            if request_id:
                previous: dict[str, Any] | None = next(
                    (
                        m
                        for m in reversed(self.messages)
                        if m.get("request_id") == request_id
                        and m.get("request_sender") == owner
                    ),
                    None,
                )
                if previous is not None:
                    if previous["request_hash"] != fingerprint:
                        raise HttpError(
                            409, "request_id was already used for a different message"
                        )
                    return {k: previous[k] for k in ("id", "mode", "reach", "to")} | {
                        "replayed": True,
                        "forwarded_to_parent": bool(helper),
                    }
            r: dict[str, Any] = (
                {
                    "to": [{"name": sender, "kind": "agent"}],
                    "reach": [sender],
                    "unknown": [],
                    "unsubscribed": [],
                    "ambiguous": [],
                    "renamed": [],
                    "has_project": False,
                    "broadcast": False,
                    "forwarded_to_parent": True,
                }
                if helper
                else self.resolve(to, sender)
            )
            requested_mode: str = mode
            text = message_text
            if r["unknown"] or r["unsubscribed"]:
                raise HttpError(
                    404,
                    "; ".join(
                        (
                            [
                                f"no agent or project named {', '.join(r['unknown'])} (agents: `arke who`; projects start with /)"
                            ]
                            if r["unknown"]
                            else []
                        )
                        + (
                            [
                                f"nobody is subscribed to {', '.join(r['unsubscribed'])} (its members unsubscribed)"
                            ]
                            if r["unsubscribed"]
                            else []
                        )
                    ),
                )
            stuck: list[dict[str, Any]] = [
                x for x in r["ambiguous"] if not x["delivered_to"]
            ]
            if stuck:
                raise HttpError(
                    409,
                    "; ".join(
                        f"{x['name']} is an old name of several agents ({', '.join(x['also_old_name_of'])}); "
                        "send to one of them by its current name"
                        for x in stuck
                    ),
                )
            if not r["reach"]:
                raise HttpError(400, "nobody else would receive this")
            if not r["has_project"]:
                mode = "notify"  # DMs are always delivered
            # one message counts once per kind (and per project), however many it names
            limits: dict[str, str] = {}
            # counted per session, so renaming doesn't reset it; a parent's helpers share their own budget:
            # a chatty helper must not use up its parent's sends
            base: str = src["sid"] if src is not None else sender
            bucket: str = f"{base}|helper" if helper else base
            for t in r["to"]:
                kind: str = {
                    "agent": "dm",
                    "human": "dm",
                    "project": "project",
                    "broadcast": "broadcast",
                }[t["kind"]]
                limits[f"{bucket}|{kind}|{t['name'] if kind == 'project' else ''}"] = (
                    kind
                )
            for key, kind in limits.items():
                self.rate_limit(key, kind)
            m: Message = {
                "id": self.next_id,
                "ts": time.time(),
                "from": sender,
                "to": r["to"],
                "reach": r["reach"],
                "via": "agent"
                if sender != "human"
                else "console"
                if body.get("via") == "console"
                else "shell",
                "mode": mode,
                "text": text,
                "host": src["host"] if src else "web",
                "cwd": src.get("cwd", "") if src else "",
                "branch": src.get("branch", "") if src else "",
                "projects": list(src["projects"]) if src else [],
            }
            if request_id:
                m |= {
                    "request_id": request_id,
                    "request_sender": owner,
                    "request_hash": fingerprint,
                }
            if helper:
                m |= {
                    "helper": helper,
                    "requested_to": to,
                    "requested_mode": requested_mode,
                }
            self.next_id += 1
            assert not self.messages or self.messages[-1]["id"] < m["id"], (
                "message IDs must increase"
            )
            self.messages.append(m)
            named: set[str] = {t["name"] for t in r["to"] if t["kind"] == "agent"}
            if src is not None:
                src["last_sent"] = m["ts"]
            for name in r["reach"]:
                dst: Agent | None = self.by_name(name)
                if dst is not None:
                    dst["last_human" if sender == "human" else "last_recv"] = m["ts"]
                    their: str = (
                        "notify" if name in named else mode
                    )  # named directly: always delivered + woken
                    dst["inbox"].append(
                        [m["id"], their, name in named]
                    )  # idle agents are woken by wake_pass
            self.record("message", **m)
            self.save()
        ntfy(m)
        return {
            **r,
            "id": m["id"],
            "mode": mode,
            "unreachable": self.unreachable(r["reach"]),
        }

    def rate_limit(self, key: str, kind: str) -> None:
        limit, window = LIMITS[kind]
        now: float = time.time()
        recent: list[float] = [t for t in self.sends[key] if now - t < window]
        if len(recent) >= limit:
            raise HttpError(
                429,
                f"rate limit: {limit} {kind} messages per {int(window / 60)} min"
                + (
                    "; broadcasts are for rare, everyone-needs-this news"
                    if kind == "broadcast"
                    else ""
                ),
            )
        self.sends[key] = recent + [now]

    def inbox(self, a: Agent, wait: float, receipt: bool = False) -> Any:
        """Direct + notify mail for the CLI (`arke inbox`); blocks up to `wait` seconds for some."""
        if not math.isfinite(wait):
            raise HttpError(400, "wait must be finite")
        deadline: float = time.time() + min(wait, MAX_WAIT_S)
        with self.cond:
            while True:
                if self.agents.get(a["sid"]) is not a:
                    raise HttpError(
                        409,
                        "this session was superseded; its mail belongs to its successor",
                    )
                if (
                    any(item[1] == "notify" or item[2] for item in a["inbox"])
                    or time.time() >= deadline
                ):
                    break
                self.cond.wait(max(0.0, deadline - time.time()))
            ids: set[int] = {
                item[0] for item in a["inbox"] if item[1] == "notify" or item[2]
            }  # board posts: `arke log`
            messages: list[dict[str, Any]] = [
                m for mid in sorted(ids) if (m := self.message(mid)) is not None
            ]
            if receipt:
                return {
                    "messages": messages,
                    "delivery": {"sid": a["sid"], "ids": sorted(ids)},
                }
        with self.change(messages_changed=False):
            if self.agents.get(a["sid"]) is not a:
                raise HttpError(
                    409,
                    "this session was superseded; its mail belongs to its successor",
                )
            # Another reader or ACK may have claimed these while we waited for the lock.
            ids.intersection_update(item[0] for item in a["inbox"])
            messages = [m for m in messages if m["id"] in ids]
            # A message arriving between the wait and this transaction must not be consumed unseen.
            a["inbox"] = [item for item in a["inbox"] if item[0] not in ids]
            self.save()
        return messages

    def acknowledge(
        self, sid: str, as_name: str, body: dict[str, Any], tmux: str = ""
    ) -> dict[str, Any]:
        ids: Any | None = body.get("ids")
        if not isinstance(ids, list) or any(type(i) is not int or i < 1 for i in ids):
            raise HttpError(400, "ids must be a list of positive message IDs")
        with self.change(messages_changed=False):
            a: dict[str, Any] = self.agent_for(sid, as_name, tmux)
            received: set[Any] = set(ids)
            a["inbox"] = [item for item in a["inbox"] if item[0] not in received]
            self.save()
            return {"ok": True}

    def log_view(self, q: dict[str, str]) -> list[Message]:
        """History, filtered on the server: by project subtree and/or by agent, the newest `limit` with an id above
        `since` and below `before` (default: no bound), oldest first. Paging back = before=<oldest id you have>."""
        since: int = int(q.get("since", 0))
        before: int = int(q.get("before", 0)) or 1 << 62
        limit: int = min(int(q.get("limit", 200)), 1000)
        project: str = q.get("project", "")
        agent: str = q.get("agent", "")
        inner: bool = q.get("scope") == "inner"

        with self.cond:
            who: Agent | None = (
                (
                    self.by_name(agent)
                    or next(
                        (a for a in self.agents.values() if agent in a.get("aka", [])),
                        None,
                    )
                )
                if agent
                else None
            )  # an old name finds the agent that carries it now
            aliases: set[str] = self.names(who) if who else {agent}
            # earlier names too: their messages from before a rename
            members: set[str] = {
                n
                for a in self.agents.values()
                if any(under(p, project) for p in a["projects"])
                for n in self.names(a)
            }
            out: list[Message] = []
            for m in reversed(self.messages):
                if m["id"] <= since or len(out) >= limit:
                    break
                if m["id"] >= before:
                    continue
                if agent and not (m["from"] in aliases or aliases & set(m["reach"])):
                    continue
                if project:
                    to_project: bool = any(
                        t["kind"] == "project"
                        and (under(t["name"], project) or under(project, t["name"]))
                        for t in m["to"]
                    )
                    reaches_member: bool = not members.isdisjoint(m["reach"])
                    if not (
                        to_project
                        or reaches_member
                        or any(t["kind"] == "broadcast" for t in m["to"])
                        or any(under(p, project) for p in m["projects"])
                    ):
                        continue
                    # inner: only to this project, between its members, or involving the human
                    if inner and not (
                        to_project
                        or m["from"] == "human"
                        or any(t["kind"] == "human" for t in m["to"])
                        or (m["from"] in members and reaches_member)
                    ):
                        continue
                out.append(m)
            return out[::-1]

    def wake_wait(self, sid: str, body: dict[str, Any]) -> dict[str, Any]:
        """A Claude listener offers readiness, not a mailbox read. Only wake_pass can offer it a notice."""
        token, listener = body.get("process_token"), body.get("listener")
        if (
            not sid
            or not isinstance(token, str)
            or not token
            or not isinstance(listener, str)
            or not listener
        ):
            raise HttpError(
                400, "wake wait requires a session, process token and listener ID"
            )
        wait: Any = body.get("wait", 20)
        if (
            isinstance(wait, bool)
            or not isinstance(wait, (int, float))
            or not math.isfinite(wait)
            or not 0 < wait <= 20
        ):
            raise HttpError(400, "wake wait must be between 0 and 20 seconds")
        request: dict[str, Any] = {
            "process_token": token,
            "listener": listener,
            "deadline": time.monotonic() + wait,
        }
        with self.cond:
            a = self.agents.get(sid)
            if a is None:
                if sid in self.retired:
                    raise HttpError(409, "this session was retired")
                return {
                    "status": "unregistered"
                }  # SessionStart hooks execute in parallel
            if not self.listener_current(a, token):
                raise HttpError(
                    409, "this listener no longer owns a live local Claude session"
                )
            # Replace any earlier waiter: a killed listener's request stays here until its deadline.
            self.wake_waiters[sid] = request
            self.cond.notify_all()
            try:
                while True:
                    if self.wake_waiters.get(
                        sid
                    ) is not request or not self.listener_current(a, token):
                        raise HttpError(
                            409, "this listener was replaced or its session ended"
                        )
                    if offer := request.get("offer"):
                        # A receipt, replacement, or user turn may have raced the coordinator's offer.
                        if (
                            self.wake_current(request["snapshot"], request["mail"])
                            and (a.get("wake") or {}).get("attempt") == offer["attempt"]
                        ):
                            return offer
                        w = a.get("wake") or {}
                        if w.get("attempt") == offer["attempt"]:
                            # never delivered: no try spent, no pickup grace, no "didn't pick up" fallback
                            with self.change(messages_changed=False):
                                a["wake"] = w | {
                                    "status": "held",
                                    "at": time.time(),
                                    "tries": max(0, w.get("tries", 0) - 1),
                                    "detail": "waiting: the agent changed before its listener took the notice",
                                }
                                self.save()
                        return {"status": "cancelled"}
                    remaining = request["deadline"] - time.monotonic()
                    if remaining <= 0:
                        return {"status": "timeout"}
                    self.cond.wait(
                        remaining
                    )  # never wait inside change(): it releases the lock
            finally:
                if self.wake_waiters.get(sid) is request:
                    del self.wake_waiters[sid]

    def listener_current(self, a: Agent, token: str) -> bool:
        return bool(
            self.agents.get(a["sid"]) is a
            and a.get("process_token") == token
            and a.get("kind") == "claude"
            and a.get("host") == LOCAL_HOST
            and a.get("state") != "offline"
            and not a.get("nested")
        )

    def native_waiter(self, a: Agent) -> dict[str, Any] | None:
        waiter = self.wake_waiters.get(a["sid"])
        return (
            waiter
            if waiter
            and waiter["deadline"] > time.monotonic()
            and self.listener_current(a, waiter["process_token"])
            and not waiter.get("offer")
            else None
        )

    def wake_attempt_current(self, snapshot: Agent, attempt: str) -> bool:
        """Ownership of a result: unlike dispatch eligibility, it survives busy/read state changes."""
        a = self.agents.get(snapshot["sid"])
        return bool(
            a
            and (a.get("wake") or {}).get("attempt") == attempt
            and all(
                a.get(k) == snapshot.get(k)
                for k in (
                    "name",
                    "host",
                    "tmux",
                    "pid",
                    "pid_start",
                    "process_token",
                    "daemon",
                )
            )
        )

    @staticmethod
    def wake_received(a: Agent, w: dict[str, Any]) -> bool:
        unread = {item[0] for item in a["inbox"] if item[1] == "notify"}
        return not unread or bool(
            w.get("mail_ids") and unread.isdisjoint(w["mail_ids"])
        )

    def wake_pass(self) -> None:
        """Wake local idle agents through native delivery or a verified terminal fallback.
        Claims commit before dispatch; retries and pickup grace survive restart in the agent's wake record."""
        now: float = time.time()
        jobs: list[
            tuple[Agent, list[Message], dict[str, Any]]
        ] = []  # identity snapshot, unread, wake record
        with self.change(messages_changed=False):
            for a in self.agents.values():
                w: dict[str, Any] = a.get("wake") or {}
                unread: list[Message] = [
                    m
                    for mid, mode, _ in a["inbox"]
                    if mode == "notify" and (m := self.message(mid)) is not None
                ]
                if (
                    w.get("attempt")
                    and w.get("status") != "picked up"
                    and self.wake_received(a, w)
                ):
                    w = w | {
                        "status": "picked up",
                        "at": now,
                        "detail": "the agent read its mail",
                    }
                    a["wake"] = w
                    self.save()
                if not unread:
                    if w.get("status") in ("dispatching", "typed", "held", "failed"):
                        a["wake"] = w | {
                            "status": "picked up",
                            "at": now,
                            "detail": "the agent read its mail",
                        }
                        self.save()
                    continue
                waiter = self.native_waiter(
                    a
                )  # once: its deadline can pass between two lookups
                if (
                    a["state"] != "idle"
                    or a.get("host") != LOCAL_HOST
                    or a["sid"] in self.wake_running
                    or not (a.get("tmux") or a.get("daemon") or waiter)
                ):
                    continue
                if w.get("status") == "dispatching":
                    if now - w["at"] < WAKE_ACK_S:
                        continue  # committed before dispatch; a restart cannot cause an immediate duplicate
                    w = w | {"status": "typed", "tries": w.get("tries", 0) + 1}
                ids: str = f"{len(unread)} up to #{unread[-1]['id']}"  # identifies this batch of mail
                if w.get("ids") != ids:  # new mail: start over
                    if w.get("attempt") and w.get("status") in ("typed", "held"):
                        w = w | {
                            "ids": ids
                        }  # an outstanding attempt keeps its grace and retry budget
                    elif w.get("status") == "held":
                        w = w | {
                            "ids": ids,
                            "tries": 0,
                            "retries": 0,
                        }  # a hold keeps its timers: the draft is still there
                    else:
                        # the typed notice may still sit in the box; a just-typed one keeps its WAKE_ACK_S wait
                        w = {"ids": ids, "tries": 0} | {
                            k: w[k]
                            for k in ("typed", "status", "at", "transport")
                            if k in w
                            and (
                                k in ("typed", "transport")
                                or w.get("status") == "typed"
                            )
                        }
                st: str = w.get("status", "")
                if (
                    st == "failed"
                    and now - w["at"] >= WAKE_FAILED_RETRY_S
                    and w.get("retries", 0) < WAKE_TRIES
                ):
                    # after a pause, one more try (up to WAKE_TRIES of these per batch of mail; then it stays failed)
                    w = w | {
                        "status": "",
                        "tries": WAKE_TRIES - 1,
                        "retries": w.get("retries", 0) + 1,
                    }
                    st = ""
                if now - min(m["ts"] for m in unread) < WAKE_BATCH_S:
                    continue  # let a burst of mail become one wake-up
                waiting_ack: bool = st == "typed" and now - w["at"] < WAKE_ACK_S
                waiting_hold: bool = st == "held" and now - w["at"] < WAKE_RETRY_S
                if waiting_ack or waiting_hold or st == "failed":
                    continue
                if st == "typed" and w["tries"] >= WAKE_TRIES:
                    a["wake"] = w | {
                        "status": "failed",
                        "at": now,
                        "detail": f"tried {w['tries']} times, the agent didn't pick it up",
                    }
                    self.record(
                        "wake",
                        agent=a["name"],
                        sender=", ".join(dict.fromkeys(m["from"] for m in unread)),
                        ok=False,
                        error=a["wake"]["detail"],
                        via=w.get("transport", ""),
                        fallback=w.get("fallback", ""),
                    )
                    self.save()
                    continue
                if (
                    not waiter
                    and not a.get("daemon")
                    and w.get("transport") == "claude hook"
                    and now - a.get("state_at", 0) < LISTENER_REARM_S
                ):
                    continue  # its last wake-up used the listener, which re-arms on Stop
                transport = (
                    "codex turn/start"
                    if a.get("daemon")
                    else (
                        "claude hook"
                        if waiter
                        and (
                            w.get("transport") != "claude hook"
                            or w.get("status") != "typed"
                            or not a.get("tmux")
                        )
                        else "tmux"
                    )
                )
                # how often Claude falls back to typing into tmux, and why (the `wake` events say)
                fallback: str = (
                    ""
                    if transport != "tmux" or a.get("kind") != "claude"
                    else "listener didn't pick up"
                    if w.get("transport") == "claude hook"
                    and w.get("status") == "typed"
                    else "no listener"
                )
                claimed = dict(
                    w,
                    ids=ids,
                    fallback=fallback,
                    attempt=uuid.uuid4().hex,
                    transport=transport,
                    mail_ids=[m["id"] for m in unread],
                    status="dispatching",
                    at=now,
                )
                a["wake"] = claimed
                self.save()  # commit before any external action or listener response
                jobs.append(
                    (
                        dict(a),
                        unread,
                        dict(
                            w,
                            ids=ids,
                            attempt=claimed["attempt"],
                            transport=transport,
                            fallback=fallback,
                            mail_ids=claimed["mail_ids"],
                        ),
                    )
                )
        down: list[
            str
        ] = []  # skip queued Codex calls after a failure; in-flight calls finish with their own timeout

        def wake_one(snapshot: Agent, unread: list[Message], w: dict[str, Any]) -> None:
            mail: set[Any] = {m["id"] for m in unread}

            def action(
                *args: str,
                stdin: str | None = None,
                expect: str | None = None,
                snapshot: Agent = snapshot,
                mail: set[int] = mail,
            ) -> str:
                # Every terminal mutation, including the final Enter, checks the actual foreground process and the
                # prompt box (tmux and /proc, so outside the bus lock), then the current session under the lock,
                # which it keeps while typing: no hook can change the state between that check and the keys.
                sock, pane = snapshot["tmux"].rsplit(":", 1)
                if not harness_in_pane(snapshot, sock, pane):
                    raise WakeCancelled(
                        "its registered harness is no longer foreground in this pane"
                    )
                kind, now_box = prompt_box(sock, pane)
                if kind != "box":
                    raise WakeCancelled("its prompt box changed or a dialog opened")
                # `expect`: exactly what the box showed when we last read it (whitespace too: a space changes a
                # command); someone starting to type must never get our notice added to their words and sent
                if expect is not None and now_box != expect:
                    raise WakeCancelled("someone is typing in its prompt box")
                with self.cond:
                    if not self.wake_current(
                        snapshot, mail
                    ) or not self.wake_attempt_current(snapshot, w["attempt"]):
                        raise WakeCancelled(
                            "the session, state, pane or unread mail changed"
                        )
                    return tmux_run(sock, *args, stdin=stdin)

            with self.cond:
                if not self.wake_attempt_current(snapshot, w["attempt"]):
                    return
                if not self.wake_current(snapshot, mail):
                    # nothing was dispatched: no try spent, no pickup grace to wait out
                    with self.change(messages_changed=False):
                        self.agents[snapshot["sid"]]["wake"] = w | {
                            "status": "held",
                            "at": time.time(),
                            "detail": "waiting: the agent changed before the wake-up",
                        }
                        self.save()
                    return
                self.wake_running.add(snapshot["sid"])
            try:
                if w["transport"] == "claude hook":
                    with self.cond:
                        a = self.agents[snapshot["sid"]]
                        waiter = self.native_waiter(a)
                        if (
                            waiter
                            and self.wake_current(snapshot, mail)
                            and self.wake_attempt_current(snapshot, w["attempt"])
                        ):
                            w = w | {
                                "status": "typed",
                                "at": time.time(),
                                "tries": w.get("tries", 0) + 1,
                                "detail": "offered a wake notice to its Claude hook",
                            }
                            with self.change(messages_changed=False):
                                a["wake"] = w
                                self.save()
                                self.record(
                                    "wake",
                                    agent=a["name"],
                                    sender=", ".join(
                                        dict.fromkeys(m["from"] for m in unread)
                                    ),
                                    ok=True,
                                    via="claude hook",
                                    error="",
                                )
                            # Commit before publishing; the outer lock keeps the waiter from returning early.
                            waiter.update(
                                snapshot=snapshot,
                                mail=mail,
                                offer={
                                    "attempt": w["attempt"],
                                    "notice": wake_notice(unread),
                                },
                            )
                            self.cond.notify_all()
                        else:
                            w = w | {
                                "status": "held",
                                "at": time.time(),
                                "detail": "waiting: its Claude listener disconnected",
                            }
                elif snapshot.get("daemon"):
                    w = self.codex_wake(snapshot, unread, w, down)
                elif snapshot.get("tmux"):
                    w = self.try_wake(
                        snapshot["name"],
                        snapshot["tmux"],
                        unread,
                        w,
                        action=action,
                        harness=snapshot,
                    )
                else:
                    w = wake_failed(
                        w, snapshot["name"], "", "no verified tmux pane for fallback"
                    )
            except Exception as e:  # noqa: BLE001 — printed with its traceback; one agent's bug mustn't stop the others' wake-ups
                traceback.print_exc()
                w = w | {
                    "status": "failed",
                    "at": time.time(),
                    "detail": f"wake-up error: {type(e).__name__}: {e}",
                }
            try:
                w = w | {
                    k: snapshot["wake"][k] for k in ("attempt", "transport", "mail_ids")
                }
                with self.change(messages_changed=False):
                    if self.wake_attempt_current(snapshot, snapshot["wake"]["attempt"]):
                        a = self.agents[snapshot["sid"]]
                        if (
                            w["transport"] == "claude hook"
                            and a["wake"].get("status") == "held"
                        ):
                            # The waiter cancelled after publication; its returned try and hold must survive.
                            w = dict(a["wake"])
                        if self.wake_received(a, w):
                            w = w | {
                                "status": "picked up",
                                "detail": "the agent read its mail",
                            }
                        elif (a.get("wake") or {}).get("status") == "picked up":
                            w = w | {
                                "status": "picked up",
                                "detail": a["wake"]["detail"],
                            }
                        a["wake"] = w
                        self.save()  # retry text and draft timers survive an ordinary restart
            finally:
                with self.cond:
                    self.wake_running.discard(snapshot["sid"])

        # Outside the lock, several at once: each can take seconds of prompt polling, and each pane is its own.
        # Each Codex turn has its own connection. Alternate the two kinds so neither queues behind all the other.
        daemon = (j for j in jobs if j[0].get("daemon"))
        terminal = (j for j in jobs if not j[0].get("daemon"))
        with ThreadPoolExecutor(max_workers=WAKE_PARALLEL) as pool:
            running: list[Future[Any]] = [
                pool.submit(wake_one, *j)
                for pair in zip_longest(daemon, terminal)
                for j in pair
                if j is not None
            ]
            for f in running:
                f.result()

    def codex_wake(
        self, a: Agent, unread: list[Message], w: dict[str, Any], down: list[str]
    ) -> dict[str, Any]:
        """A Codex daemon thread: start a turn with the notice through the daemon (no pane, nothing typed). `down`:
        why the daemon didn't answer earlier in this pass (skip calls not yet started: each try can take seconds)."""
        now: float = time.time()
        senders: str = ", ".join(dict.fromkeys(m["from"] for m in unread))
        mail: set[Any] = {m["id"] for m in unread}

        def before_start() -> None:
            # The socket handshake and thread/read can take time. Check again immediately before submission,
            # without holding either bus lock while waiting for any daemon response.
            with self.cond:
                if not self.wake_current(a, mail) or (
                    w.get("attempt") and not self.wake_attempt_current(a, w["attempt"])
                ):
                    raise WakeCancelled(
                        "the session, state, pane or unread mail changed"
                    )

        def held(why: str, **extra: Any) -> dict[str, Any]:
            if w.get("detail") != f"waiting: {why}":
                log("wake_held", agent=a["name"], why=why)
            # down_since only while the daemon keeps not answering
            return (
                {k: v for k, v in w.items() if k != "down_since"}
                | extra
                | {"status": "held", "at": now, "detail": f"waiting: {why}"}
            )

        try:
            if down:
                raise OSError(down[0])
            status: str = codex_turn(
                a["sid"], wake_notice(unread), before_start=before_start
            )
        except WakeCancelled as e:
            return held(str(e))
        except CodexRefused as e:
            return held(f"its Codex daemon refused the turn ({e})")
        except OSError as e:  # no daemon (Codex restarting, updating) or it hangs
            down[:] = [str(e)]
            since: float = w.get("down_since", now)
            if now - since < CODEX_DOWN_S:
                return held(f"its Codex daemon isn't answering ({e})", down_since=since)
            return wake_failed(
                w,
                a["name"],
                senders,
                f"its Codex daemon hasn't answered for {int(now - since)}s ({e})",
                error=f"codex daemon: {e}",
            )
        except Exception as e:  # noqa: BLE001 — logged with its traceback; one odd reply mustn't stop the other wake-ups
            return wake_failed(
                w,
                a["name"],
                senders,
                f"unexpected reply from its Codex daemon ({type(e).__name__}: {e})",
                error=f"codex daemon: {type(e).__name__}: {e}",
                trace=traceback.format_exc()[-2000:],
            )
        if status != "started":
            # notLoaded: no Codex has it open; starting a turn would run it with nobody watching
            if status == "notLoaded":
                return wake_failed(
                    w, a["name"], senders, "its Codex thread is notLoaded"
                )
            return held(f"its Codex thread is {status}")
        log(
            "wake",
            agent=a["name"],
            sender=senders,
            ok=True,
            error="",
            via="codex turn/start",
        )
        return {
            "ids": w["ids"],
            "tries": w.get("tries", 0) + 1,
            "retries": w.get("retries", 0),
            "status": "typed",
            "at": now,
            "detail": "started a turn in its Codex thread",
        }

    def wake_current(self, snapshot: Agent, mail: set[int]) -> bool:
        """Dispatch eligibility, with cond held. Result ownership instead uses wake_attempt_current."""
        a: dict[str, Any] | None = self.agents.get(snapshot["sid"])
        return bool(
            a is not None
            and a["state"] == "idle"
            and a.get("host") == LOCAL_HOST
            and all(
                a.get(k) == snapshot.get(k)
                for k in (
                    "name",
                    "tmux",
                    "pid",
                    "pid_start",
                    "state_at",
                    "daemon",
                    "process_token",
                )
            )
            and mail.issubset({item[0] for item in a["inbox"] if item[1] == "notify"})
        )

    def try_wake(
        self,
        name: str,
        tmux: str,
        unread: list[Message],
        w: dict[str, Any],
        *,
        action: Callable[..., str],
        harness: Agent,
    ) -> dict[str, Any]:
        now: float = time.time()
        sock, pane = tmux.rsplit(":", 1)
        senders: str = ", ".join(dict.fromkeys(m["from"] for m in unread))
        notice: str = wake_notice(unread)
        if not pane_alive(tmux, fresh=True):
            return wake_failed(
                w,
                name,
                senders,
                f"its tmux pane {pane} no longer exists",
                error=f"pane {pane} no longer exists",
            )
        kind: str = "unknown"
        typed: str = w.get(
            "typed", ""
        )  # the whole box as our last paste left it (or as found still in it)
        try:
            if not harness_in_pane(harness, sock, pane):
                raise WakeCancelled(
                    "its registered harness is not foreground in this pane"
                )
            kind, box = prompt_box(sock, pane)
            if kind == "dialog":
                return w | {
                    "status": "held",
                    "at": now,
                    "detail": f"waiting: {box} in its pane",
                }
            if kind == "box" and not box and harness.get("kind") == "claude":
                # Claude's blank software cursor is visually identical to a real
                # space under that cursor. Move to the end before assuming the
                # input is empty; a space-only draft then becomes visible.
                action("send-keys", "-t", pane, "End", expect=box)
                time.sleep(0.2)
                if prompt_box(sock, pane) != (kind, box):
                    raise WakeCancelled(
                        "the prompt box changed while checking empty input"
                    )
            # Retry a lost Enter only when the whole rendered box is unchanged, including whitespace.
            # A notice prefix alone says nothing about whether someone has edited the draft since our paste.
            ours: bool = bool(box) and box == w.get("typed")
            if kind == "unknown" or box and not ours:
                # someone's draft (or a screen this can't read): wait while it changes; a draft left alone gets ours
                # added and sent; an unknown screen (a shell, an editor) is never typed into
                key: str = box if kind == "box" else "(no prompt box found)"
                # Enter on a draft like "/clear" runs that command with the notice as its arguments; "!ls" in Codex
                # runs a shell command
                command: bool = kind == "box" and box.lstrip().startswith(("/", "!"))
                # a paste that landed but failed its check stays in the box; never stack another copy on it
                stuck: bool = kind == "box" and "[arke]" in squash(box)
                if w.get("held_text") != key:
                    w = w | {"held_text": key, "held_since": now}
                if now - w["held_since"] < WAKE_PUSH_S:
                    why: str = (
                        "unsent text in its prompt box"
                        if kind == "box"
                        else "can't find its prompt box"
                    )
                    if w.get("status") != "held":
                        log("wake_held", agent=name, why=why)
                    return w | {
                        "status": "held",
                        "at": now,
                        "detail": f"waiting: {why}"
                        + (
                            f" (sent anyway after {int(WAKE_PUSH_S)}s unchanged)"
                            if kind == "box" and not command and not stuck
                            else ""
                        ),
                    }
                if kind == "unknown" or command or stuck:
                    why = (
                        "a command in its prompt box; not running it"
                        if command
                        else "an earlier notice in its prompt box; not adding another"
                        if stuck
                        else "no prompt box on its screen; not typing blind"
                    )
                    return wake_failed(w, name, senders, why, error=why.split(";")[0])
                # a draft left alone: the notice goes after the whole draft, never at the cursor (which may sit
                # mid-word): down to its last line, then to its end; if that changed the text (e.g. stepped through
                # prompt history), give up and wait again
                for key in ["Down"] * box.count("\n") + ["End"]:
                    action("send-keys", "-t", pane, key, expect=box)
                time.sleep(0.2)
                if prompt_box(sock, pane) != ("box", box):
                    return w | {
                        "status": "held",
                        "at": now,
                        "held_since": now,
                        "held_text": None,
                        "detail": "waiting: the prompt box changed while moving to the end of the draft",
                    }
                added: str = f"\n[user message cut off]\n{notice}"
                err, typed = type_notice(sock, pane, added, before=box, action=action)
            elif ours:
                err, typed = type_notice(
                    sock, pane, box, paste=False, before="", action=action
                )
            else:
                err, typed = type_notice(sock, pane, notice, before=box, action=action)
        except (
            WakeCancelled
        ) as e:  # a change in the box restarts the wait for it to sit unchanged
            if (
                w.get("status") != "held" or w.get("detail") != f"waiting: {e}"
            ):  # once per reason, not every pass
                log("wake_held", agent=name, why=str(e))
            return w | {
                "status": "held",
                "at": now,
                "held_since": now,
                "held_text": None,
                "detail": f"waiting: {e}",
            }
        except (OSError, subprocess.TimeoutExpired) as e:
            err = f"tmux: {e}"
        log(
            "wake",
            agent=name,
            sender=senders,
            ok=not err,
            error=err,
            via="tmux",
            fallback=w.get("fallback", ""),
        )
        return {
            "ids": w["ids"],
            "tries": w.get("tries", 0) + 1,
            "retries": w.get("retries", 0),
            "typed": typed,
            "status": "failed" if err else "typed",
            "at": now,
            "detail": err or "typed into its pane",
        }

    def turn_pass(self) -> None:
        """Busy/idle from the transcripts of agents on this machine, for when a hook didn't arrive (see turn_marker):
        a turn start or end written after the last state change wins."""
        with self.cond:
            todo: list[dict[str, Any]] = [
                dict(a)
                for a in self.agents.values()
                if a.get("host") == LOCAL_HOST
                and a.get("transcript")
                and a["state"] != "offline"
            ]
        for snapshot in todo:
            sid, path, offset = (
                snapshot["sid"],
                Path(snapshot["transcript"]),
                snapshot.get("turn_offset", 0),
            )
            try:
                if not any(path.resolve().is_relative_to(r) for r in TRANSCRIPT_ROOTS):
                    continue
                end, found = turn_marker(path, offset)
            except OSError:
                continue  # transcript gone or unreadable: the hooks are all there is
            except Exception as e:  # noqa: BLE001 — logged; one odd transcript mustn't stop the others' wake-ups
                log(
                    "turn_error",
                    sid=sid,
                    path=str(path),
                    error=f"{type(e).__name__}: {e}",
                    trace=traceback.format_exc()[-2000:],
                )
                try:
                    end = (
                        path.stat().st_size
                    )  # skip past it rather than fail on it every pass
                except OSError:  # it vanished meanwhile: start over if it comes back
                    end = 0
                found = None
            with (
                self.cond
            ):  # no change() can start between this check and the one below
                a: Agent | None = self.agents.get(sid)
                if a is None or any(
                    a.get(k) != snapshot.get(k)
                    for k in (
                        "transcript",
                        "turn_offset",
                        "host",
                        "pid",
                        "pid_start",
                        "process_token",
                        "state_at",
                        "started_at",
                    )
                ):
                    continue
                if not (
                    found
                    and found[0] > a.get("state_at", 0)
                    and a["state"] != "offline"
                    and a["state"] != found[1]
                ):
                    a["turn_offset"] = (
                        end  # nothing else to change: the next commit saves it
                    )
                    continue
                with self.change(messages_changed=False):
                    a["turn_offset"] = end
                    self.record(
                        "state_from_transcript",
                        agent=a["name"],
                        state=found[1],
                        was=a["state"],
                    )
                    self.set_state(a, found[1])
                    self.save()

    # --- projects and agents ---

    def roster_snapshot(
        self,
        observer: Agent | None,
        project: str = "",
        show_all: bool = False,
    ) -> dict[str, Any]:
        """Compact, detached rows from committed state (or a hook's staged state). Caller holds cond."""
        projects: list[str] = (
            [project] if project else sorted(observer["projects"]) if observer else []
        )
        selected: list[Agent] = []
        for a in self.agents.values():
            if project and not any(under(p, project) for p in a["projects"]):
                continue
            if observer is not None and (
                a is observer
                or not any(
                    under(p, q) or under(q, p) for p in a["projects"] for q in projects
                )
            ):
                continue
            selected.append(a)
        now: float = time.time()
        ended: int = sum(a["state"] == "offline" for a in selected)
        return {
            "scope": "project" if project else "peers" if observer else "global",
            "projects": projects,
            "as_of": now,
            "stale_seconds": STALE_S,
            "include_ended": show_all,
            "ended": ended,
            "total": len(selected),
            "agents": [
                roster.describe(a, now, STALE_S)
                for a in sorted(selected, key=itemgetter("name"))
                if show_all or a["state"] != "offline"
            ],
        }

    def roster(
        self,
        sid: str,
        as_name: str,
        tmux: str = "",
        *,
        project: str = "",
        global_scope: bool = False,
        show_all: bool = False,
    ) -> dict[str, Any]:
        if project and global_scope:
            raise HttpError(400, "choose a project or global scope, not both")
        if project:
            project = project.rstrip("/")
            if not PROJECT_RE.fullmatch(project):
                raise HttpError(400, "project must look like /name or /name/sub")
        with self.cond:
            observer: Agent | None = None
            if (
                not project
                and not global_scope
                and (sid or as_name not in ("", "human"))
            ):
                observer = self.agent_for(sid, as_name, tmux)
            return self.roster_snapshot(observer, project, show_all)

    def roster_context(self, a: Agent, *, force: bool = False) -> str:
        """Best-effort snapshots on safe hooks, never mail. Lost responses refresh at the next prompt."""
        now: float = time.monotonic()
        previous: tuple[str, float] | None = self.roster_seen.get(a["sid"])
        if not force and previous and now - previous[1] < ROSTER_COOLDOWN_S:
            return ""
        snapshot: dict[str, Any] = self.roster_snapshot(a)
        # Time passing and busy/idle transitions must not generate roster chatter.
        digest: str = hashlib.sha256(
            json.dumps(
                [
                    snapshot["projects"],
                    snapshot["ended"],
                    [
                        (row["name"], sorted(row["projects"]))
                        for row in snapshot["agents"]
                    ],
                ]
            ).encode()
        ).hexdigest()
        self.roster_seen[a["sid"]] = (digest, now)
        if not force and previous and digest == previous[0]:
            return ""
        return roster.render(snapshot, ROSTER_CONTEXT_CHARS)

    def projects(
        self, sid: str, as_name: str, body: dict[str, Any], tmux: str = ""
    ) -> dict[str, Any]:
        project: str = str(body.get("project") or "").rstrip("/")
        action: str = str(body.get("action") or "")
        if not PROJECT_RE.match(project):
            raise HttpError(
                400,
                f"project must look like /name or /name/sub (letters, digits, . _ -), got {project!r}",
            )
        with self.change(messages_changed=False):
            a: Agent = self.agent_for(sid, as_name, tmux)
            match action:
                case "join" | "subscribe":
                    a["projects"][project] = True
                case "unsubscribe":
                    if project not in a["projects"]:
                        raise HttpError(404, f"not in {project}")
                    a["projects"][project] = False
                case "leave":
                    if a["projects"].pop(project, None) is None:
                        raise HttpError(404, f"not in {project}")
                case _:
                    raise HttpError(
                        400, "action must be join, leave, subscribe or unsubscribe"
                    )
            self.record("project", agent=a["name"], project=project, action=action)
            self.save()
            return {"name": a["name"], "projects": a["projects"]}

    def project_tree(self) -> list[dict[str, Any]]:
        with self.cond:
            paths: set[str] = set()
            for a in self.agents.values():
                for p in a["projects"]:
                    parts: list[str] = p.split("/")[1:]
                    paths.update(
                        "/" + "/".join(parts[: i + 1]) for i in range(len(parts))
                    )
            return [
                {
                    "path": p,
                    "members": sum(
                        any(under(q, p) for q in a["projects"])
                        for a in self.agents.values()
                    ),
                    "subscribed": sum(
                        any(s and under(q, p) for q, s in a["projects"].items())
                        for a in self.agents.values()
                    ),
                    "color": self.colors.get(p),
                }
                for p in sorted(paths)
            ]

    def public(self, a: Agent) -> dict[str, Any]:
        """Enrich a private agent snapshot; callers must release cond before the external I/O. No lock for colors
        and procs_now: each is one dict read, which is atomic; procs_now's values are replaced, never edited."""
        color: str | None = self.colors.get(a["name"])
        procs: Any | None = copy.deepcopy(self.procs_now.get(a["name"]))
        if a.get("context") and not a.get("window") and a.get("kind") == "claude":
            # Claude transcripts don't record the window: 1M if the model says so or we're already past 200k
            a["window"] = (
                1_000_000
                if "[1m]" in (a.get("model") or "") or a["context"] > 200_000
                else 200_000
            )
            a["window_est"] = True
        st: tuple[str, str] = self.reachable(a)
        return {
            k: v
            for k, v in a.items()
            if k
            not in (
                "sid",
                "inbox",
                "transcript",
                "note",
                "turn_offset",
                "process_token",
                "pid_start",
            )
        } | {
            "unread": len(a["inbox"]),
            "unread_ids": [item[0] for item in a["inbox"]],
            "local": a.get("host") == LOCAL_HOST,
            "reachable": st[0],
            "reachable_why": st[1],
            "presence": roster.presence(a, time.time(), STALE_S),
            "color": color,
            "procs": procs,
            # recent activity, trimmed: the console polls /agents every few seconds (full targets are in the event log)
            "files": [
                {**f, "target": f["target"][:300]} for f in a.get("files", [])[:12]
            ],
        }

    def list_agents(self, project: str) -> list[dict[str, Any]]:
        with self.cond:
            agents: list[dict[str, Any]] = [
                copy.deepcopy(a)
                for a in self.agents.values()
                if not project or any(under(p, project) for p in a["projects"])
            ]
        return [self.public(a) for a in agents]

    def forget_old(self) -> None:
        """Mark dead local agents offline. They are never removed automatically."""
        with self.change(messages_changed=False):
            for a in self.agents.values():
                if (
                    a["state"] != "offline"
                    and a.get("host") == LOCAL_HOST
                    and a.get("pid")
                    and a.get("pid_start") is not None
                ):
                    try:
                        proc: dict[str, Any] | None = process_info(
                            a["pid"], strict=True
                        )
                    except (OSError, ValueError, IndexError):
                        continue  # unavailable evidence is not death; retry this agent on the next pass
                    if proc is None or proc["start"] != a["pid_start"]:
                        self.record("gone", agent=a["name"], pid=a["pid"])
                        self.set_state(a, "offline")
                        self.save()
            self.roster_seen = {
                sid: seen
                for sid, seen in self.roster_seen.items()
                if sid in self.agents
            }

    def update(
        self, sid: str, as_name: str, body: dict[str, Any], tmux: str = ""
    ) -> dict[str, Any]:
        with self.change(messages_changed=False):
            a: Agent = self.agent_for(sid, as_name, tmux)
            if "about" in body:
                a["about"] = str(body["about"])[:200]
                self.record("about", agent=a["name"], about=a["about"])
            self.save()
            return {"name": a["name"]}

    def local_roots(self) -> list[str]:
        """Local agents' working directories, resolved outside the lock. Agents report their own cwd, so working
        dirs of /, ~ or anything above ~ don't count."""
        with self.cond:
            cwds: set[str] = {
                a["cwd"]
                for a in self.agents.values()
                if a.get("host") == LOCAL_HOST and a.get("cwd")
            }
        home: Path = Path.home().resolve()
        return [str(r) for c in cwds if not home.is_relative_to(r := Path(c).resolve())]

    def local_path(self, p: str, roots: list[str] | None = None) -> Path | None:
        """`p` resolved, if it's an absolute path at or inside some local agent's working directory (`roots`: from
        local_roots(), for many paths at once). Kernel trees and secret-looking files never count."""
        f: Path = Path(p).resolve()
        if not Path(p).is_absolute() or not any(
            under(str(f), r) for r in (self.local_roots() if roots is None else roots)
        ):
            return None
        if any(
            f.is_relative_to(k) for k in (Path("/proc"), Path("/sys"), Path("/dev"))
        ) or any(map(SECRET.fullmatch, f.parts)):
            return None
        return f

    def local_file(self, p: str, roots: list[str] | None = None) -> Path | None:
        f: Path | None = self.local_path(p, roots)
        return f if f and f.is_file() else None

    def file_info(self, p: str) -> dict[str, Any]:
        """One file for the analytics side pane: size, mtime, type, and its text when small and UTF-8."""
        f: Path | None = self.local_file(p)
        if f is None:
            return {
                "path": p,
                "available": False,
                "reason": f"not a file inside a working directory of an agent on {LOCAL_HOST} (or it no longer exists)",
            }
        try:
            st: os.stat_result = f.stat()
            raw: bytes = f.read_bytes() if st.st_size <= FILE_TEXT_MAX else b""
        except OSError as e:  # gone or unreadable since the check above
            return {"path": p, "available": False, "reason": f"{type(e).__name__}: {e}"}
        out: dict[str, Any] = {
            "path": p,
            "resolved": str(f),
            "available": True,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "type": mimetypes.guess_type(f.name)[0]
            or (f.suffix.lstrip(".") or "unknown"),
        }
        if st.st_size <= FILE_TEXT_MAX:
            try:
                out["text"] = raw.decode("utf-8")
            except UnicodeDecodeError:
                out["binary"] = True
            if PRIVATE_KEY.search(out.get("text", "")):  # certificates stay readable
                del out["text"]
        return out

    def file_sizes(self, paths: list[str]) -> dict[str, int | None]:
        """Sizes in bytes of files on this machine, for the analytics city map. Only absolute paths inside some
        local agent's working directory are looked at; anything else (or missing, or not a file) is null."""
        out: dict[str, int | None] = {}
        roots: list[str] = self.local_roots()
        for p in paths[:500]:
            f: Path | None = self.local_file(p, roots)
            try:
                out[p] = f.stat().st_size if f else None
            except OSError:
                out[p] = None  # deleted or unreadable since the check; same as missing
        return out

    def transcript(
        self, name: str, cursor: str, limit: int, before: str
    ) -> dict[str, Any]:
        with self.cond:
            a: Agent | None = self.by_name(name)
            if a is None:
                raise HttpError(404, f"no agent named {name!r}")
            path: str = a.get("transcript") or ""
            host: str = a.get("host") or "?"
            names: set[str] = self.names(a)
        if host != LOCAL_HOST:
            return {
                "available": False,
                "reason": f"{name} runs on {host}; transcripts are only read for agents on {LOCAL_HOST}.",
            }
        if not path:  # Path("") would be the server's cwd
            return {"available": False, "reason": "no transcript reported yet"}

        def msgs(after: float) -> list[dict[str, Any]]:
            """The messages this agent sent or got with id > after (newest are last, so polls stop early)."""
            out: list[dict[str, Any]] = []
            with self.cond:
                for m in reversed(self.messages):
                    if m["id"] <= after:
                        break
                    if m["from"] in names or names & set(m["reach"]):
                        out.append(
                            {
                                "id": m["id"],
                                "ts": m["ts"],
                                "kind": "bus",
                                "from": m["from"],
                                "text": m["text"],
                                "to": [t["name"] for t in m["to"]],
                                "outgoing": m["from"] in names,
                                # a board post it wasn't named in: it only got a one-line note, not the text
                                "board": m["mode"] == "board"
                                and not names & {t["name"] for t in m["to"]},
                            }
                        )
            return out[::-1]

        return read_transcript(Path(path), msgs, cursor, limit, before)


def watch_agents(bus: Bus) -> None:
    """Every WAKE_PASS_S: busy/idle from transcripts, then wake-ups for idle agents with mail. A failure is logged
    and the loop carries on (it is what keeps agents from sitting on unread mail)."""
    while True:
        time.sleep(WAKE_PASS_S)
        for step in (
            bus.forget_old,
            bus.turn_pass,
            bus.wake_pass,
        ):  # one failing step mustn't starve the others
            try:
                step()
            except Exception as e:  # noqa: BLE001 — printed with its traceback, not hidden; the next pass tries again
                print(f"watch_agents: {traceback.format_exc()}", flush=True)
                log(
                    "watch_error",
                    step=step.__name__,
                    error=f"{type(e).__name__}: {e}",
                    trace=traceback.format_exc()[-2000:],
                )


def sample_procs(bus: Bus) -> None:
    """Every PROC_S seconds: each local agent's process tree (its harness pid and descendants) -> one `procs` event:
    per-agent totals and busiest processes, plus processes that started or ended since the last sample."""
    hz: int = os.sysconf("SC_CLK_TCK")
    page_mb: float = os.sysconf("SC_PAGE_SIZE") / 2**20
    boot: float = time.time() - float(Path("/proc/uptime").read_text().split()[0])
    prev: dict[int, tuple[int, int]] = {}  # pid -> (start, ticks) last time
    known: dict[tuple[int, int], str] = {}  # (pid, start) -> agent, for "ended"
    last: float = time.time()
    while True:
        time.sleep(PROC_S)
        try:
            procs: dict[int, dict[str, Any]] = proc_table()
        except OSError as e:  # e.g. a transient /proc permission error: printed, and the next sample tries again
            print(f"sample_procs: {e}", flush=True)
            continue
        now: float = time.time()
        dt: float = max(now - last, 1e-3)
        last = now
        kids: dict[int, list[int]] = collections.defaultdict(list)
        for pid, row0 in procs.items():
            kids[row0["ppid"]].append(pid)
        with bus.cond:
            roots: dict[str, int] = {
                a["name"]: a["pid"]
                for a in bus.agents.values()
                if a.get("host") == LOCAL_HOST
                and a.get("pid")
                and a["state"] != "offline"
                and not a.get("daemon")
            }  # (every thread of a Codex daemon has the daemon's pid)
        agents: dict[str, Any] = {}
        started: list[dict[str, Any]] = []
        seen: set[tuple[int, int]] = set()
        for name, root in roots.items():
            if root not in procs:
                continue
            tree: list[int] = [root]
            for pid in tree:  # breadth-first; the list grows as we go
                tree.extend(kids.get(pid, []))
            rows: list[dict[str, Any]] = []
            for pid in tree:
                p: dict[str, Any] = procs[pid]
                key: tuple[int, int] = (pid, p["start"])
                seen.add(key)
                before: tuple[int, int] | None = prev.get(pid)
                cpu: float = (
                    100 * (p["ticks"] - before[1]) / hz / dt
                    if before and before[0] == p["start"]
                    else 0.0
                )
                row: dict[str, Any] = {
                    "pid": pid,
                    "cpu": round(cpu, 1),
                    "rss_mb": round(p["rss"] * page_mb, 1),
                }
                rows.append(row)
                if key not in known:
                    known[key] = name
                    if pid != root:
                        started.append(
                            {
                                "agent": name,
                                "pid": pid,
                                "ppid": p["ppid"],
                                "cmd": cmdline(pid),
                                "start": round(boot + p["start"] / hz, 2),
                            }
                        )

            busy: list[dict[str, Any]] = sorted(
                (r for r in rows if r["cpu"] >= 1 or r["rss_mb"] >= 200),
                key=itemgetter("cpu"),
                reverse=True,
            )[:5]
            for r in busy:
                r["cmd"] = cmdline(r["pid"])[:120]
            agents[name] = {
                "cpu": round(sum(r["cpu"] for r in rows), 1),
                "rss_mb": round(sum(r["rss_mb"] for r in rows), 1),
                "n": len(rows),
                "top": busy,
            }
        ended: list[dict[str, Any]] = [
            {"agent": a, "pid": k[0]} for k, a in known.items() if k not in seen
        ]
        for k in [k for k in known if k not in seen]:
            del known[k]
        prev = {pid: (q["start"], q["ticks"]) for pid, q in procs.items()}
        with bus.cond:
            bus.procs_now = agents
        if agents or ended:
            try:
                log("procs", agents=agents, started=started, ended=ended)
            except (
                OSError
            ) as e:  # e.g. a full disk: printed, and the next sample tries again
                print(f"sample_procs: can't log: {e}", flush=True)


def git_log(cwd: Path, since: float, limit: int) -> dict[str, Any]:
    """Resolve the working tree root, then return commits with paths relative to that root."""
    try:
        top: subprocess.CompletedProcess[str] = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        if top.returncode != 0:
            raise HttpError(
                400, f"git root in {cwd} failed: {top.stderr.strip()[:300]}"
            )
        root: Path = Path(top.stdout.rstrip("\n"))
        r: subprocess.CompletedProcess[bytes] = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "log",
                *([f"--since=@{int(since)}"] if since > 0 else []),
                f"--max-count={limit}",
                "--no-show-signature",
                "--numstat",
                "-z",
                "--format=%x00%H%x00%ct%x00%an%x00%ae%x00%s",
            ],
            capture_output=True,
            check=False,
            timeout=20,
        )  # binary output preserves carriage returns in filenames
    except subprocess.TimeoutExpired as e:
        raise HttpError(
            504,
            f"git history in {cwd} took over 20 s; try a smaller limit or a later since",
        ) from e
    if r.returncode != 0:
        raise HttpError(
            400,
            f"git log failed in {root}: {r.stderr.decode('utf-8', 'replace').strip()[:300]}",
        )
    out: list[dict[str, Any]] = []
    records: Iterator[str] = iter(r.stdout.decode("utf-8", "replace").split("\0"))
    for record in records:
        if (
            not record
        ):  # empty field introduces a commit header; every field is NUL-separated
            h: str | None = next(records, None)
            if h is None:
                break  # trailing NUL
            ts, an, ae, subj = (next(records) for _ in range(4))
            out.append(
                {
                    "hash": h,
                    "ts": int(ts),
                    "author": an,
                    "email": ae,
                    "subject": subj,
                    "files": [],
                }
            )
            continue
        added, deleted, path = record.lstrip("\n").split(
            "\t", 2
        )  # -z leaves the actual path unquoted
        if not path:  # rename/copy: counts<TAB><NUL>old path<NUL>new path<NUL>
            next(records)
            path: str = next(records)
        out[-1]["files"].append(
            {
                "path": path,
                "add": None if added == "-" else int(added),
                "del": None if deleted == "-" else int(deleted),
            }
        )
    return {"repo": str(root), "commits": out}


def read_events(
    since: int,
    types: set[str],
    limit: int,
    generation: str = "",
    procs_totals: bool = False,
) -> dict[str, Any]:
    """Raw event log from byte offset `since` (optionally only some event types). `next` is where to continue.
    procs_totals: procs events keep only each agent's cpu and rss_mb (their top/started/ended lists are most of
    the log's bytes, and the 2D page draws only the totals)."""
    if not LOG_FILE.exists():
        return {"events": [], "next": 0, "generation": "missing"}
    events: list[dict[str, Any]] = []
    pos: int = since
    with LOG_FILE.open("rb") as f:
        stat: os.stat_result = os.fstat(f.fileno())
        current: str = f"{stat.st_dev:x}:{stat.st_ino:x}"
        if generation and generation != current:
            raise HttpError(
                400, "event log was rotated; start from 0 with a new generation"
            )
        if since > stat.st_size:
            raise HttpError(
                400,
                f"since={since} is past the end of the log ({stat.st_size} bytes); was it rotated? start from 0",
            )
        # `since` should be a line start (a previous `next`); if not, this resumes at the next whole line
        for start, line in whole_lines(f, since, stat.st_size):
            pos = start + len(line)
            try:
                e: Any = json.loads(line)
                if not isinstance(e, dict):
                    raise TypeError("event must be an object")
                if not isinstance(e.get("event"), str):
                    raise TypeError("event name must be a string")
            except (ValueError, TypeError) as err:
                print(
                    f"arke: skipping damaged event at byte {start}: {err}",
                    file=sys.stderr,
                )
                continue
            if not types or e.get("event") in types:
                if procs_totals and e["event"] == "procs":
                    agents: Any = e.get("agents")
                    e = {
                        "ts": e.get("ts"),
                        "event": "procs",
                        "agents": {
                            name: {"cpu": v.get("cpu"), "rss_mb": v.get("rss_mb")}
                            for name, v in agents.items()
                            if isinstance(v, dict)
                        }
                        if isinstance(agents, dict)
                        else {},
                    }
                events.append(e)
                if len(events) >= limit:
                    break
    return {"events": events, "next": pos, "generation": current}


# wake-up timing, read by Bus.wake_pass and watch_agents (the waking itself is in wake.py)
WAKE_PASS_S: float = 2.0  # how often waiting mail and busy/idle are checked
WAKE_PARALLEL: int = (
    8  # simultaneous wake attempts, across terminal panes and Codex daemon threads
)
WAKE_RETRY_S: float = 10.0  # a held wake-up is looked at again this often
WAKE_PUSH_S: float = (
    60.0  # a draft left unchanged this long gets the notice added under it and is sent
)
WAKE_ACK_S: float = 30.0  # wait for pickup after dispatch before retrying or using the terminal fallback
LISTENER_REARM_S: float = 10.0  # a listener that just woke its Claude restarts at Stop; tmux waits this long for it
WAKE_TRIES: int = 3
WAKE_FAILED_RETRY_S: float = 300.0  # a failed wake-up (no prompt box, notice never picked up) gets one more try this often, 3 times


def wake_failed(
    w: dict[str, Any],
    name: str,
    senders: str,
    detail: str,
    error: str = "",
    **extra: Any,
) -> dict[str, Any]:
    """Log a failed wake-up and give the wake record that says so (the console shows `detail`)."""
    via: dict[str, str] = {
        "via": w.get("transport", ""),
        "fallback": w.get("fallback", ""),
    }
    log(
        "wake",
        agent=name,
        sender=senders,
        ok=False,
        error=error or detail,
        **(via | extra),
    )
    return w | {"status": "failed", "at": time.time(), "detail": detail}


CODEX_DOWN_S: float = 60.0  # a daemon that doesn't answer (restarting, updating) is retried this long before failed


def ntfy(m: Message) -> None:
    """Push to the self-hosted ntfy: messages to you loudly, broadcasts normally, everything else silently."""
    if not NTFY_URL:
        return
    kinds: set[str] = {t["kind"] for t in m["to"]}
    topic: str = "arke-human" if "human" in kinds else "arke"
    priority: str = (
        "high" if "human" in kinds else "default" if "broadcast" in kinds else "min"
    )
    to: str = ", ".join(t["name"] for t in m["to"])

    def post() -> None:
        # in the thread: the message is already committed, so a bad ntfy_url only gets printed
        try:
            req: urllib.request.Request = urllib.request.Request(
                f"{NTFY_URL}/{topic}",
                data=m["text"].encode(),
                method="POST",
                headers={
                    "Title": f"{m['from']} -> {to}",
                    "Priority": priority,
                    "Tags": m["mode"],
                },
            )
            urllib.request.urlopen(req, timeout=5).close()
        except (OSError, ValueError) as e:
            print(f"NTFY FAILED: {e}", flush=True)

    threading.Thread(target=post, daemon=True).start()


# the pages and their scripts (/vendor/: from ASSETS, the rest from WEB)
PAGES: dict[str, str] = {
    "/": "console.html",
    "/analytics": "analytics.html",
    "/analytics3d": "analytics3d.html",
    **{
        f"/{n}": n
        for n in (
            "arke.css",
            "auth.js",
            "base.js",
            "graph-layout.js",
            "analytics-shared.js",
            "analytics-page.js",
        )
    },
    **{
        f"/vendor/{n}": n
        for n in ("three.module.min.js", "OrbitControls.js", "highlight.min.js")
    },
}


class BusHTTPServer(ThreadingHTTPServer):
    # Hooks have short deadlines; let a burst connect while the accept loop starts its handlers.
    request_queue_size: int = 128


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0, the default protocol_version: one request per connection, so the unread body of a refused
    # request is never read as the next one. Keep it unless every early reply drains or closes.
    bus: Bus
    key: str = ""  # experimental opt-in; empty preserves unauthenticated operation
    timeout: ClassVar[float | None] = (
        30  # socket reads/writes (a client that never sends its body frees its thread); not /inbox?wait
    )

    def reply(self, status: int, obj: Any, ctype: str = "application/json") -> None:
        data: bytes = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store" if self.key else "no-cache")
        self.send_header("Vary", "Origin, Sec-Fetch-Site, Accept-Encoding")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            self.send_header("X-Frame-Options", "DENY")
        # Browsers always accept gzip; JSON shrinks ~10x. Level 1: a 30 MB events page compresses in well under a second.
        if len(data) > 1024 and accepts_gzip(self.headers.get("Accept-Encoding", "")):
            data = gzip.compress(data, compresslevel=1)
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        # The 30 s limit covers the whole send (Python 3.5+), so a big answer to a slow link was cut off and a
        # browser read the truncated JSON as complete. Add 1 s per 100 kB: 30 MB gets 330 s.
        if self.timeout is not None:
            self.connection.settimeout(self.timeout + len(data) / 100_000)
        self.wfile.write(data)

    def route(self, method: str) -> None:
        sid: str = self.headers.get("X-Agent-Session", "")
        as_name: str = self.headers.get("X-Agent-As", "")
        helper: str = self.headers.get("X-Agent-Helper", "")
        tmux: str = self.headers.get(
            "X-Agent-Tmux", ""
        )  # caller's pane; must match the session's (see sender)
        bus: Bus = self.bus
        try:
            url: ParseResult = urlparse(self.path)
            page: str | None = PAGES.get(url.path) if method == "GET" else None
            public: bool = page is not None or method == "GET" and url.path == "/help"
            site: str = self.headers.get("Sec-Fetch-Site", "")
            origin: str | None = self.headers.get("Origin")
            host: str = self.headers.get("Host", "")
            # Every caller must name this bus in Host: a DNS-rebinding page sends neither Fetch Metadata nor
            # Origin on its same-origin GETs, only its own domain. Fetch Metadata covers GETs too; without it,
            # check Origin; null origins are never trusted.
            # HTTPS termination works when the proxy preserves Host and the browser's same-origin metadata.
            if not public and not browser_host(host):
                return self.reply(
                    403,
                    {
                        "error": f"this bus does not answer to the name {host!r}; list it in [server] browser_hosts"
                    },
                )
            if not public and (
                site not in ("", "same-origin", "none")
                or origin is not None
                and origin != f"http://{host}"
                and not (site == "same-origin" and origin == f"https://{host}")
            ):
                return self.reply(
                    403,
                    {
                        "error": "cross-origin website requests are disabled; open the console from this bus"
                    },
                )
            if self.key and not public:
                supplied: list[str] | list[Any] = self.headers.get_all(
                    "Authorization", []
                )
                scheme, _, value = (
                    supplied[0].partition(" ") if len(supplied) == 1 else ("", "", "")
                )
                if scheme.lower() != "bearer" or not hmac.compare_digest(
                    value.encode(), self.key.encode()
                ):
                    return self.reply(
                        401,
                        {
                            "error": "experimental key required or incorrect; configure client.url and client.key_file"
                        },
                    )
            query: dict[str, list[str]] = parse_qs(url.query)
            q: dict[str, str] = {k: v[0] for k, v in query.items()}
            if helper:
                if not sid or not HELPER_RE.fullmatch(helper):
                    raise HttpError(
                        400,
                        "helper requests require a parent session and a valid helper ID",
                    )
                if method != "GET" and not (
                    method == "POST" and url.path in ("/send", "/helper/send")
                ):
                    raise HttpError(
                        403,
                        "sub-agents have read-only access; sends go only to their parent",
                    )
            body: dict[str, Any] = {}
            if method == "POST":
                # Forms and no-cors fetches can submit without a preflight. Require a non-simple content type.
                if (
                    self.headers.get("Content-Type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                    != "application/json"
                ):
                    raise HttpError(
                        415, "POST requests must use Content-Type: application/json"
                    )
                if (
                    "Transfer-Encoding" in self.headers
                ):  # unread, a chunked body would be taken as {}
                    raise HttpError(411, "send the body with a Content-Length")
                n: int = int(self.headers.get("Content-Length") or 0)
                if not 0 <= n <= MAX_BODY:
                    raise HttpError(
                        413, f"request body of {n} bytes; the bus takes 0 to {MAX_BODY}"
                    )
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except RecursionError:
                    raise ValueError("JSON nested too deeply") from None
                if not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
                # JSON can spell a lone surrogate ("\\ud800"); it can't be saved, logged or served, so refuse it here
                json.dumps(body, ensure_ascii=False).encode()
            parts: list[str] = [unquote(p) for p in url.path.strip("/").split("/")]
        except HttpError as e:
            return self.reply(e.status, {"error": str(e)})
        except ValueError as e:
            return self.reply(400, {"error": f"bad request: {e}"})
        try:
            if (
                page is not None
            ):  # read per request: iterate on the pages without restarting
                f: Path = (ASSETS if url.path.startswith("/vendor/") else WEB) / page
                if not f.is_file():
                    raise HttpError(
                        404,
                        f"{page} missing (vendored files: run python3 install-service.py --assets-only)",
                    )
                return self.reply(
                    200,
                    f.read_bytes(),
                    f"{mimetypes.guess_type(page)[0]}; charset=utf-8",
                )
            match method, parts:
                case "GET", ["help"]:
                    text: str = (
                        (HERE / "help.md")
                        .read_text()
                        .replace("{client}", "<clone>/arke/client.py")
                        .replace("{dir}", "<clone>/arke")
                    )
                    return self.reply(200, text.encode(), "text/plain; charset=utf-8")
                case "GET", ["git"]:
                    cwd: Path | None = bus.local_path(q.get("cwd", ""))
                    if cwd is None or not cwd.is_dir():
                        raise HttpError(
                            400,
                            f"cwd must be a local agent's working directory (or inside one) on {LOCAL_HOST}",
                        )
                    out = git_log(
                        cwd,
                        float(q.get("since", 0)),
                        max(1, min(int(q.get("limit", 200)), 2000)),
                    )
                case "GET", [
                    "machine"
                ]:  # the bus host's size, for scaling per-process shares in analytics
                    mem_kb: int = int(
                        Path("/proc/meminfo").read_text().split()[1]
                    )  # first line: MemTotal: N kB
                    out = {
                        "host": LOCAL_HOST,
                        "cores": os.cpu_count(),
                        "mem_total_mb": mem_kb // 1024,
                    }
                case "GET", ["horatio", "history"]:
                    if not HORATIO_URL:
                        raise HttpError(
                            404,
                            "horatio is not configured (set [server].horatio_url)",
                        )
                    try:
                        with urllib.request.urlopen(
                            f"{HORATIO_URL}/api/history?hours={float(q.get('hours', 6))}",
                            timeout=20,
                        ) as r:
                            if r.headers.get_content_type() != "application/json":
                                raise HttpError(
                                    502,
                                    f"{HORATIO_URL} answered {r.headers.get_content_type()}, not horatio's JSON",
                                )
                            return self.reply(200, r.read(), "application/json")
                    except OSError as e:
                        raise HttpError(
                            502, f"horatio at {HORATIO_URL} unreachable: {e}"
                        ) from e
                case "GET", ["fileinfo"]:
                    out = bus.file_info(q.get("path", ""))
                case "GET", ["filesizes"]:
                    out = bus.file_sizes(query.get("path", []))
                case "GET", ["events"]:
                    if q.get("procs", "") not in ("", "totals"):
                        raise HttpError(400, "procs must be totals or absent")
                    out = read_events(
                        max(0, int(q.get("since", 0))),
                        set(filter(None, q.get("type", "").split(","))),
                        max(1, min(int(q.get("limit", 5000)), 50000)),
                        q.get("generation", ""),
                        q.get("procs") == "totals",
                    )
                case "POST", ["hook"]:
                    out = bus.hook(body)
                case "GET", ["agents"]:
                    out = bus.list_agents(q.get("project", ""))
                case "GET", ["roster"]:
                    if any(
                        q.get(flag, "0") not in ("0", "1") for flag in ("global", "all")
                    ):
                        raise HttpError(400, "global and all must be 0 or 1")
                    out = bus.roster(
                        sid,
                        as_name,
                        tmux,
                        project=q.get("project", ""),
                        global_scope=q.get("global") == "1",
                        show_all=q.get("all") == "1",
                    )
                case "GET", ["agents", name]:
                    with bus.cond:
                        a: Agent | None = bus.by_name(name)
                        if a is None:
                            raise HttpError(404, f"no agent named {name!r}")
                        snapshot: dict[str, Any] = copy.deepcopy(a)
                    out = bus.public(snapshot)
                case "GET", ["agents", name, "transcript"]:
                    out = bus.transcript(
                        name,
                        q.get("cursor", ""),
                        max(1, min(int(q.get("limit", 40)), 500)),
                        q.get("before", ""),
                    )
                case "GET", ["projects"]:
                    out = bus.project_tree()
                case "POST", ["projects"]:
                    out = bus.projects(sid, as_name, body, tmux)
                case "GET", ["resolve"]:
                    to: list[str] = [
                        t.strip() for t in q.get("to", "").split(",") if t.strip()
                    ]
                    with bus.cond:
                        out = bus.resolve(to, bus.sender(sid, as_name or "human"))
                    out["unreachable"] = bus.unreachable(out["reach"])
                case "POST", ["send"] | ["helper", "send"]:
                    if parts == ["helper", "send"] and not helper:
                        raise HttpError(400, "helper sends require X-Agent-Helper")
                    out = bus.send(
                        as_name,
                        body | {"via": self.headers.get("X-Agent-Via", "")},
                        sid=sid,
                        tmux=tmux,
                        helper=helper,
                    )
                case "GET", ["inbox"]:
                    out = bus.inbox(
                        bus.agent_for(sid, as_name, tmux),
                        float(q.get("wait", 0)),
                        bool(helper) or q.get("receipt") == "1",
                    )
                    if helper:
                        out.pop("delivery", None)
                case "POST", ["ack"]:
                    out = bus.acknowledge(sid, as_name, body, tmux)
                case "POST", ["wake", "wait"]:
                    out = bus.wake_wait(sid, body)
                case "GET", ["log"]:
                    out = bus.log_view(q)
                case "POST", ["about"]:
                    out = bus.update(sid, as_name, body, tmux)
                case "POST", ["name"]:
                    out = bus.rename(sid, as_name, body, tmux)
                case "POST", ["color"]:
                    out = bus.set_color(body)
                case _:
                    raise HttpError(404, f"no route {method} {url.path} (see /help)")
        except HttpError as e:
            return self.reply(e.status, {"error": str(e)})
        except (KeyError, ValueError, OverflowError) as e:
            # missing or malformed fields in the request, or a server bug that raised one: printed either way
            traceback.print_exc()
            return self.reply(400, {"error": f"bad request: {e!r}"})
        except ConnectionError:
            raise  # the client went away
        except Exception as e:  # noqa: BLE001 — a server bug: printed with its traceback, and answered (not a dropped connection)
            traceback.print_exc()
            return self.reply(500, {"error": f"server error: {type(e).__name__}: {e}"})
        self.reply(200, out)

    def do_GET(self) -> None:
        self.route("GET")

    def do_POST(self) -> None:
        self.route("POST")

    def do_OPTIONS(self) -> None:
        """The console uses its own origin; no browser preflight needs permission."""
        self.reply(
            403,
            {
                "error": "open the console from this bus; cross-origin access is disabled"
            },
        )

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        # hooks and console polling are constant; only log those when they fail
        if (
            code == 200
            and urlparse(self.path).path
            in (
                "/hook",
                "/agents",
                "/roster",
                "/log",
                "/projects",
                "/resolve",
                "/inbox",
                "/events",
                "/ack",
                "/wake/wait",
                "/",
            )
            or code == 200
            and "/transcript" in self.path
        ):
            return
        print(
            # Malformed requests may have no path, and either field can contain terminal controls.
            f"{time.strftime('%H:%M:%S')} {self.client_address[0]} {ascii(str(self.command))[1:-1]} "
            f"{ascii(getattr(self, 'path', self.requestline))[1:-1]} {code}",
            flush=True,
        )


def accepts_gzip(header: str) -> bool:
    """Whether an Accept-Encoding header allows gzip: listed, and not with q=0."""
    for part in header.split(","):
        name, _, params = part.partition(";")
        if name.strip().lower() != "gzip":
            continue
        weight: str = params.strip()
        try:
            return not weight.startswith("q=") or float(weight[2:]) > 0
        except ValueError:
            return False  # a malformed weight: send it uncompressed, which every client can read
    return False


def browser_host(host: str) -> bool:
    """Whether a browser's Host names this bus: an IP, localhost, this machine (or its tailnet name), or one of
    server.browser_hosts. A DNS-rebinding page sends its own domain, which otherwise looks same-origin."""
    name: str = (urlparse("//" + host).hostname or "").rstrip(".")
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        me: str = LOCAL_HOST.lower()
        return (
            name in ("localhost", me, *BROWSER_HOSTS)
            or name.startswith(me + ".")
            and name.endswith(".ts.net")
        )


def private_state() -> None:
    """Mail, tool targets and backups are private: files made from here on are owner-only, and a state dir (or log)
    made before, with the default umask, is closed to other local users too."""
    os.umask(0o077)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.chmod(0o700)
    if LOG_FILE.exists():  # it may live outside the state dir (ARKE_LOG / log_file)
        LOG_FILE.chmod(0o600)


def tailscale_ip() -> str:
    return subprocess.run(
        ["tailscale", "ip", "-4"], capture_output=True, text=True, check=True
    ).stdout.split()[0]


def refuse_empty_start() -> None:
    """Exit instead of quietly starting an empty bus next to existing state (a forgotten migration or env rename,
    a database moved aside): new message ids would collide with the old ones."""
    stale: list[str] = sorted(k for k in os.environ if k.startswith("AGENTBUS_"))
    if stale:
        raise SystemExit(
            f"{', '.join(stale)} set: remove legacy settings after migrating state; "
            "do not carry old AGENTBUS_DIR/AGENTBUS_LOG paths into the new service"
        )
    database: Path = STATE_DIR / "state.sqlite3"
    if (
        database.exists() and database.stat().st_size
    ):  # an emptied file is not a database
        return
    old: Path = (
        Path.home() / ".local/state/agentbus"
    )  # an explicit ARKE_DIR is a new bus on purpose
    found: list[Path] = [
        p
        for p in [LOG_FILE, STATE_DIR / "state.json"]
        + (
            []
            if os.environ.get("ARKE_DIR") or "state_dir" in SETTINGS
            else [old / "state.sqlite3", old / "state.json"]
        )
        if p.exists()
    ]
    if found:
        raise SystemExit(
            f"no database at {STATE_DIR / 'state.sqlite3'}, but {', '.join(map(str, found))} exists, so this bus "
            "has run before and a new one would reuse its message ids. Moving from agentbus: run its migration "
            "script (migrate_from_agentbus.py). Otherwise restore a backup (see DOCS.md), "
            "or move those files aside to start an empty bus."
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument(
        "--tailscale",
        action="store_true",
        help="also listen on this machine's Tailscale IP ([server] tailscale = true does the same)",
    )
    args: argparse.Namespace = parser.parse_args()
    try:
        Handler.key = settings.server_key(SETTINGS)
    except (OSError, ValueError) as e:
        raise SystemExit(f"arke key configuration: {e}") from None
    refuse_empty_start()
    private_state()
    state_lock: TextIO = (STATE_DIR / "server.lock").open("a")
    try:
        fcntl.flock(state_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(f"another arke server already owns {STATE_DIR}") from None
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    Handler.bus = Bus(STATE_DIR / "state.sqlite3")
    port: int = int(os.environ.get("ARKE_PORT") or PORT)
    hosts: list[str] = ["127.0.0.1"] + (
        [tailscale_ip()] if args.tailscale or SETTINGS.get("tailscale", False) else []
    )
    servers: list[BusHTTPServer] = [BusHTTPServer((h, port), Handler) for h in hosts]
    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    print(
        f"arke on {', '.join(f'http://{h}:{port}' for h in hosts)} (local host: {LOCAL_HOST}), logging to {LOG_FILE}, "
        f"ntfy {NTFY_URL or 'off (ntfy_url empty)'}",
        flush=True,
    )
    print(
        "experimental key requirement: "
        + ("enabled (no guarantees)" if Handler.key else "off (no authentication)"),
        flush=True,
    )
    log("start", hosts=hosts, port=port)
    threading.Thread(target=sample_procs, args=(Handler.bus,), daemon=True).start()
    threading.Thread(target=watch_agents, args=(Handler.bus,), daemon=True).start()

    def stop_server(*_: object) -> Never:
        return sys.exit(0)

    signal.signal(
        signal.SIGTERM, stop_server
    )  # systemd/kill stop -> run the finally below
    try:
        servers[0].serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        log("stop")
        print("arke stopped", flush=True)


if __name__ == "__main__":
    main()

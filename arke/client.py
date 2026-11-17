"""arke — the arke client: message other coding agents on a local or private network.

    arke who [/PROJECT | --global] [--all]
                                       project peers; --global: all projects; --all: list ended agents too
    arke projects                      project tree
    arke log [/PROJECT | AGENT] [--before ID]
                                       message history, newest 200 (a project's: posts to it and messages among its members);
                                       --before ID pages back to older messages
    arke send TO[,TO...] TEXT... [--board]
                                       TO: agent name, human, /project/path, or *
                                       --board: post without interrupting anyone (projects only)
                                       --request-id ID: retry an uncertain send without creating a duplicate
    arke broadcast TEXT...             to every agent; rare, everyone-needs-this news only
    arke inbox [--wait SECONDS]        unread mail; --wait blocks until some arrives (at most 600 s)
    arke join|leave|subscribe|unsubscribe /PROJECT
    arke about TEXT...                 what you're working on (shown to everyone)
    arke name NEW                      rename yourself (sticks to your tmux pane across restarts)
    arke server [URL | --reset]        show or set which bus server to talk to
    arke hooks claude|codex            print hook configuration for this clone
    arke hook claude|codex             harness hook; reads the event JSON on stdin
    arke help                          this, plus the bus's guide for agents

`arke` = python3 {client}. Server: $ARKE_URL, `arke server URL`, TOML client.url, then localhost.
Settings: .local/config.toml in the clone, or $ARKE_CONFIG. Localhost uses server.port (default 7777).
Run inside Claude Code / Codex, you're identified by your session; elsewhere set ARKE_AS=human.
Agent names: an `arke name` from this tmux pane, else $ARKE_NAME, else the tmux session name (#S), else
#S-<window>, else <kind>-<dir>. Codex daemon threads are always codex-<dir> (use `arke name`).
"""

from __future__ import annotations

import io
import json
import re  # json imports it anyway
import shlex
import sys

TYPE_CHECKING = False  # typing.TYPE_CHECKING, without importing typing before the quick hook answers below
if TYPE_CHECKING:
    import http.client
    import urllib.request
    from collections.abc import Iterator
    from typing import IO, Any


def mark_helper(event: dict[str, Any]) -> dict[str, Any] | None:
    """PreToolUse output marking a Claude sub-agent's Bash or Monitor command that calls Arke as its parent's
    helper, or None. Other commands stay untouched: the added `export` line makes Claude's permission check see two
    commands, so allow rules and its read-only list stop matching (auto mode doesn't mind)."""
    inp: Any = event.get("tool_input")
    if not (
        event.get("agent_id")
        and event.get("session_id")
        and event.get("tool_name") in ("Bash", "Monitor")
        and isinstance(inp, dict)
        and isinstance(inp.get("command"), str)
        and re.search(r"\barke\b|client\.py|\bab\.py\b", inp["command"])
    ):
        return None
    prefix: str = (
        "export "
        + shlex.join(
            [
                f"ARKE_HELPER_ID={event['agent_id']}",
                f"ARKE_PARENT_SESSION={event['session_id']}",
            ]
        )
        + "\n"
    )
    if inp["command"].startswith(prefix):
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": inp | {"command": prefix + inp["command"]},
        }
    }


# Claude's PreToolUse (on every Bash and Monitor call) and a Claude sub-agent's hooks never reach the bus: answer them
# before importing the rest of the client, which takes longer than the answer itself. run_hook does the same for
# callers that import this module.
if __name__ == "__main__" and sys.argv[1:] == ["hook", "claude"]:
    raw: bytes = sys.stdin.buffer.read()
    # for run_hook, which reads it again
    sys.stdin = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8")
    try:
        quick: Any = json.loads(raw)
    except ValueError:  # undecodable or not JSON: run_hook reports it
        quick = None
    if isinstance(quick, dict) and quick.get("hook_event_name") == "PreToolUse":
        if out := mark_helper(quick):
            print(json.dumps(out), flush=True)
        sys.exit(0)
    if isinstance(quick, dict) and quick.get("agent_id"):
        sys.exit(0)

# The network stack (urllib, http.client, email: ~100 ms) is imported where it's used: most hooks never reach the bus.
import contextlib
import hashlib
import os
import socket
import subprocess
import time
from pathlib import Path

import roster
import settings

try:
    SETTINGS: dict[str, Any] = settings.load()
    PORT: int = int(
        os.environ.get("ARKE_PORT") or SETTINGS.get("server", {}).get("port", 7777)
    )
    STATE: Path = (
        Path(os.environ["ARKE_CLIENT_DIR"])
        if os.environ.get("ARKE_CLIENT_DIR")
        else settings.path(SETTINGS.get("client", {}).get("state_dir", ".local/client"))
    )
except (OSError, ValueError) as e:
    if __name__ == "__main__" and sys.argv[1:2] == ["hook"]:
        print(json.dumps({"systemMessage": f"arke configuration error: {e}"}))
        sys.exit(0)  # bad settings must not block an agent's prompt or turn
    raise SystemExit(f"arke configuration error: {e}") from e

HOST: str = socket.gethostname()
# one line: the server URL (`arke server URL`); all state lives in the clone
CONFIG: Path = Path(__file__).resolve().parent.parent / ".local/server-url"
HOOK_TIMEOUT_S: float = 1.5  # a hook runs on every tool call; never stall the agent
PREP_TIMEOUT_S: float = 1.0  # each git/tmux call a hook makes before its HTTP call
# start/end of a turn: once per turn, and the bus's busy/idle depends on them
TURN_HOOK_TIMEOUT_S: float = 5.0
TURN_HOOKS: tuple[str, ...] = (
    "SessionStart",
    "UserPromptSubmit",
    "Stop",
    "StopFailure",
    "SessionEnd",
)
# after a failed hook call, routine (tool) hooks skip the bus this long
DOWN_BACKOFF_S: float = 10.0
HARNESSES: tuple[str, ...] = ("claude", "codex")


BOOT: str = ""
try:  # changes on every boot: the server can tell this machine's agents from before a reboot (no SessionEnd)
    BOOT = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
except OSError:
    pass


def shell_path(path: Path) -> str:
    """Quote an advertised path, keeping installs beneath home portable."""
    try:
        return "~/" + shlex.quote(str(path.relative_to(Path.home())))
    except ValueError:
        return shlex.quote(str(path))


USAGE: str = str(__doc__).replace("{client}", shell_path(Path(__file__).resolve()))


class BusError(Exception):
    pass


class Down(BusError):
    """No usable answer (unreachable, timed out, 429, 5xx): hooks back off. Any other 4xx is a definite no."""


def normalize(addr: str) -> str:
    """`bus-host`, `100.1.2.3:8000` or `http://host:port` -> `http://host:port`."""
    import urllib.parse

    addr = addr.strip()
    if any(ord(c) < 32 or ord(c) == 127 for c in addr):
        raise BusError("server address contains a control character")
    if "://" not in addr:
        addr = "http://" + addr
    try:
        u: urllib.parse.SplitResult = urllib.parse.urlsplit(addr)
    except ValueError:
        raise BusError("server address is not a valid URL") from None
    if (
        u.scheme not in ("http", "https")
        or not u.hostname
        or u.username is not None
        or u.password is not None
        or u.path not in ("", "/")
        or u.query
        or u.fragment
    ):
        raise BusError(
            "server address must be an http(s) origin without credentials, path, query or fragment"
        )
    host: str = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
    try:
        # a bare host means our server.port; https:// without one is a TLS proxy on 443
        port: int = (
            u.port if u.port is not None else 443 if u.scheme == "https" else PORT
        )
    except ValueError:
        raise BusError("server address has an invalid port") from None
    if not 1 <= port <= 65535:
        raise BusError("server port must be between 1 and 65535")
    return f"{u.scheme}://{host}:{port}"


def server() -> tuple[str, str]:
    """(URL, source). Environment, saved CLI override, TOML, then localhost."""
    if os.environ.get("ARKE_URL"):
        return normalize(os.environ["ARKE_URL"]), "$ARKE_URL"
    if CONFIG.exists() and CONFIG.read_text().strip():
        return normalize(CONFIG.read_text()), str(CONFIG)
    if url := SETTINGS.get("client", {}).get("url"):
        return normalize(url), f"{settings.config_file()} [client].url"
    return f"http://127.0.0.1:{PORT}", "default"


def open_bus(req: urllib.request.Request, timeout: float) -> http.client.HTTPResponse:
    """Shared by the CLI, hooks and maintenance; credentials belong to one configured origin."""
    import urllib.parse
    import urllib.request

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(
            self,
            req: urllib.request.Request,
            fp: IO[bytes],
            code: int,
            msg: str,
            headers: http.client.HTTPMessage,
            newurl: str,
        ) -> None:
            return None  # a bus API never redirects: show a proxy's 301 as such, not as a bodiless GET's 404

    values: dict[str, Any] = SETTINGS.get("client", {})
    if "key_file" in values:
        try:
            if not values.get("url"):
                raise ValueError(
                    "client.key_file requires client.url to bind the key to its server"
                )
            target: urllib.parse.SplitResult = urllib.parse.urlsplit(req.full_url)
            # every caller builds its URL from normalize(); anything else (no port, credentials) is refused
            if f"{target.scheme}://{target.netloc}" != normalize(values["url"]):
                raise ValueError(
                    "selected server differs from client.url; refusing to send its key"
                )
            key: str = settings.read_key(values["key_file"])
        except (OSError, ValueError) as e:
            raise BusError(f"key configuration: {e}") from None
        req.add_unredirected_header("Authorization", "Bearer " + key)
    # never via $http_proxy: the bus is a direct peer, keyed or not
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect()
    ).open(req, timeout=timeout)


def explain(e: BaseException) -> str:
    """A connection failure in plain words, with what to check and how to point elsewhere."""
    import urllib.parse

    url, source = server()
    host: str = urllib.parse.urlsplit(url).hostname or url
    reason: Any = getattr(e, "reason", e)  # URLError wraps the socket error
    if isinstance(reason, ConnectionRefusedError):
        why = f"{host} is reachable but nothing is listening on that port. Is server.py running there?"
    elif isinstance(reason, socket.gaierror):
        why = f"can't resolve '{host}'. Check the hostname and network connection (including Tailscale if you use it)."
    elif isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(
        reason
    ):
        why = f"no answer from {host}. Is it online and reachable from this machine?"
    else:
        why = str(reason)
    return (
        f"can't reach the arke server at {url} ({source}): {why}\n"
        f"  Point at a different server with `arke server HOST[:PORT]` or ARKE_URL."
    )


def identity() -> tuple[str, str]:
    """(Registered ancestor session, helper ID). Never stop at an unregistered intermediate helper."""
    sid: str = nearest_session()
    helper: str = ""
    parent_sid: str = os.environ.get("ARKE_PARENT_SESSION", "")
    if os.environ.get("ARKE_HELPER_ID") and sid == parent_sid:
        return parent_sid, os.environ["ARKE_HELPER_ID"]
    thread: str = os.environ.get("CODEX_THREAD_ID", "")
    if sid and thread:
        if sid != thread:
            # Same-kind nesting selects the inner harness's PID map, which names the parent even
            # in its helpers' shells. Trust the thread mapping only if it reaches that harness.
            found: list[tuple[int, str]] = harnesses()
            if not found or found[0][1] != "codex":
                return sid, ""
        ancestor: str = thread
        seen: set[str] = set()
        while ancestor:
            if ancestor in seen or len(seen) >= 100:
                raise BusError("cyclic or excessively deep sub-agent parent mapping")
            seen.add(ancestor)
            try:
                parent_sid = (STATE / "thread" / ancestor).read_text().strip()
            except FileNotFoundError:
                break
            except (OSError, ValueError) as e:
                raise BusError(f"cannot read sub-agent parent mapping: {e}") from e
            if (
                not parent_sid
                or Path(parent_sid).name != parent_sid
                or parent_sid in (".", "..")
            ):
                raise BusError("invalid sub-agent parent mapping")
            helper = helper or thread
            ancestor = parent_sid
        if sid in seen:
            return ancestor, helper
        # An independent inner harness may inherit an outer helper's environment.
    return sid, ""


def nearest_session() -> str:
    """Use the nearest harness's environment or pid mapping, never an outer harness's inherited identity."""
    found: list[tuple[int, str]] = harnesses()
    if found:
        pid, kind = found[0]
        # a shared daemon's pid map would name whichever thread hooked last
        if kind == "codex" and is_daemon(pid):
            return os.environ.get("CODEX_THREAD_ID", "")
        recorded: str = ""
        # missing, or corrupt (a directory, undecodable bytes): no mapping
        try:
            recorded = (STATE / "pid" / str(pid)).read_text().strip()
        except (OSError, ValueError):
            pass
        # another instance of the same harness may have exported this very variable:
        # wait for the inner hook rather than trust an ambiguous inherited id
        if any(k == kind for _, k in found[1:]):
            return recorded
        env: str = "CLAUDE_CODE_SESSION_ID" if kind == "claude" else "CODEX_THREAD_ID"
        return os.environ.get(env) or recorded  # never the parent agent's mapping
    # No harness among our ancestors: we're in a sandbox whose namespace hides it. Codex's sandbox runs as PID 1
    # (codex-linux-sandbox); there the Codex thread id is ours, and a CLAUDE_CODE_SESSION_ID was inherited from
    # an outer Claude that launched this Codex
    if os.environ.get("CODEX_THREAD_ID") and "codex" in sandbox_init():
        return os.environ["CODEX_THREAD_ID"]
    # (A pid map left by some earlier harness at a now-reused pid is no identity: ARKE_AS applies.)
    return (
        os.environ.get("CLAUDE_CODE_SESSION_ID")
        or os.environ.get("CODEX_THREAD_ID")
        or ""
    )


def sandbox_init() -> str:
    """argv[0] of PID 1 in this process namespace ("" if unreadable)."""
    try:
        return (
            Path("/proc/1/cmdline")
            .read_bytes()
            .split(b"\0")[0]
            .decode("utf-8", "replace")
        )
    except OSError:
        return ""


def call(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    timeout: float = 30,
    sid: str = "",
) -> Any:
    """`sid`: the hook already knows its session from the event, so it passes it (Codex hook processes get no
    $CODEX_THREAD_ID and may predate the pid mapping); everything else works it out with identity()."""
    import http.client
    import urllib.error
    import urllib.request
    import uuid

    parent_sid, helper = identity()
    sid = parent_sid if helper else sid or parent_sid
    as_name: str = os.environ.get("ARKE_AS", "")
    if not sid and harnesses():
        raise BusError(
            "the nearest agent harness has no session identity yet; its next hook will register it. "
            "An inherited parent session or ARKE_AS cannot identify this child agent."
        )
    if not sid and not as_name:
        raise BusError(
            "can't tell which agent you are: no $CLAUDE_CODE_SESSION_ID or $CODEX_THREAD_ID, and no harness "
            f"pid recorded under {STATE / 'pid'} (that's written at session start). Inside an agent, check "
            "`env | grep -E 'CLAUDE_CODE_SESSION_ID|CODEX_THREAD_ID'`; don't set them by hand (identities collide). "
            "As a person or script, set ARKE_AS=human."
        )
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if helper:
        if method != "GET" and not (method == "POST" and path == "/send"):
            raise BusError(
                "sub-agents have read-only Arke access; sends go only to their parent"
            )
        headers["X-Agent-Helper"] = helper
    headers["X-Agent-Session" if sid else "X-Agent-As"] = sid or as_name
    if tmux_pane()[0]:  # lets the server catch a borrowed identity
        headers["X-Agent-Tmux"] = tmux_pane()[0]
    sending: bool = method == "POST" and path == "/send" and body is not None
    if sending:
        body = body | {"request_id": body.get("request_id") or uuid.uuid4().hex}
        if helper:
            # An older server must reject this, rather than ignore the helper header and send directly.
            path = "/helper/send"
    req: urllib.request.Request = urllib.request.Request(
        server()[0] + path,
        method=method,
        headers=headers,
        data=json.dumps(body).encode() if body is not None else None,
    )
    try:
        try:
            with open_bus(req, timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                raw: bytes = e.read()
            try:
                reason: Any = json.loads(raw).get("error")
            except (ValueError, AttributeError):
                reason = raw.decode("utf-8", "replace")[:300]
            if helper and sending and e.code == 404:
                reason = f"{reason}; helper delivery requires an updated Arke server"
            retry: str = (  # a 4xx other than 429 is a definite no; retrying won't help
                f"; retry the same message with --request-id {body['request_id']}"
                if sending and body and (e.code == 429 or e.code >= 500)
                else ""
            )
            raise (Down if e.code == 429 or e.code >= 500 else BusError)(
                f"HTTP {e.code}: {reason}{retry}"
            ) from None
    except (OSError, ValueError, http.client.HTTPException) as e:
        if sending and body is not None:
            raise Down(
                f"send outcome unknown: {e}. Retry the same message with --request-id {body['request_id']}"
            ) from e
        raise Down(explain(e)) from e


def tmux_pane() -> tuple[str, str]:
    """(socket:pane, tmux server pid), or ("", "") outside tmux. $TMUX = socket,server pid,idx."""
    sock, _, rest = os.environ.get("TMUX", "").partition(",")
    pane: str = os.environ.get("TMUX_PANE", "")
    return (f"{sock}:{pane}", rest.partition(",")[0]) if sock and pane else ("", "")


def printable(s: str) -> str:
    """Bus text as shown in a terminal: control and format characters (ESC sequences can retitle, clear or write the
    clipboard; bidi and invisible marks hide text) as \\xNN / \\uNNNN; newlines, tabs and the joiners and soft hyphen
    that scripts and emoji need stay. CRLF becomes a newline; a lone CR stays escaped."""
    import unicodedata

    return "".join(
        c
        if c in "\n\t\u200c\u200d\u00ad" or unicodedata.category(c) not in ("Cc", "Cf")
        else f"\\x{ord(c):02x}"
        if ord(c) < 256
        else f"\\u{ord(c):04x}"
        if ord(c) < 65536
        else f"\\U{ord(c):08x}"
        for c in s.replace("\r\n", "\n")
    )


def fmt(msgs: list[dict[str, Any]]) -> str:
    """Continuation lines are indented, as in hook mail, so a body can't pass for another message."""
    return "\n".join(
        f"[#{m['id']} {time.strftime('%H:%M', time.localtime(m['ts']))} {m['from']} -> "
        f"{', '.join(t['name'] for t in m['to'])}{' (board)' if m['mode'] == 'board' else ''}] "
        + printable("\n    ".join(m["text"].splitlines()))
        for m in msgs
    )


def acknowledge(delivery: dict[str, Any] | None) -> None:
    """Only called after stdout is flushed. A failed ack leaves mail queued for another delivery."""
    if not delivery:
        return
    if not isinstance(delivery, dict) or not isinstance(delivery.get("sid"), str):
        print(
            f"arke: mail output, but its receipt is malformed; it may be delivered again: {delivery!r:.200}",
            file=sys.stderr,
        )
        return
    if not delivery.get("ids"):
        return
    try:
        call(
            "POST",
            "/ack",
            {"ids": delivery["ids"]},
            sid=delivery["sid"],
            timeout=TURN_HOOK_TIMEOUT_S,  # a timed-out ack hands the same mail over again next hook
        )
    except BusError as e:
        print(
            f"arke: mail output, but acknowledgment failed; it may be delivered again: {e}",
            file=sys.stderr,
        )


def print_inbox(wait: float = 0) -> None:
    out: Any = call("GET", f"/inbox?receipt=1&wait={wait}", timeout=max(0, wait) + 30)
    print(fmt(out["messages"]) or "(no new messages)", flush=True)
    acknowledge(out.get("delivery"))


# --- hook ---


def git(cwd: str, *args: str) -> str | None:
    """git's output ("" if it fails, e.g. not a repo), or None if it timed out (worth asking again next hook)."""
    try:  # hooks have a deadline; a slow repo (network fs, huge index) just means no branch/repo this time
        r: subprocess.CompletedProcess[str] = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=PREP_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        print(f"arke: git {args[0]}: {e}", file=sys.stderr)
        return None
    except OSError as e:  # no git on PATH: no repo, and that won't change next hook
        print(f"arke: git {args[0]}: {e}", file=sys.stderr)
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def load_cache(f: Path) -> dict[str, Any] | None:
    """A cache file, or None if missing or unreadable (a write cut off by a full disk): it's rebuilt, never fatal."""
    try:
        out: Any = json.loads(f.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        print(f"arke: rebuilding unreadable cache {f}: {e}", file=sys.stderr)
        return None
    return out if isinstance(out, dict) else None


def save_cache(f: Path, obj: dict[str, Any] | str) -> None:
    """Caches (and pid files) are rebuilt when missing, so a failed write (full disk, read-only state dir) is
    reported and the hook still reaches the bus."""
    tmp: Path = f.with_suffix(f".tmp{os.getpid()}")
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(obj if isinstance(obj, str) else json.dumps(obj))
        os.replace(tmp, f)  # whole or not at all
    except OSError as e:
        with contextlib.suppress(
            OSError
        ):  # cleanup only; the error itself is printed below
            tmp.unlink(missing_ok=True)
        print(f"arke: couldn't write {f}: {e}", file=sys.stderr)


def parent(pid: int) -> int:
    # field 4 = ppid (after "(comm)", which may contain spaces)
    return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])


def ancestors(start: int | None = None) -> Iterator[int]:
    """Live process ancestry, nearest first. A parent may disappear while a hook is exiting."""
    pid: int = os.getppid() if start is None else start
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        yield pid
        try:
            pid = parent(pid)
        except (OSError, ValueError, IndexError):
            break


def harness_kind(pid: int) -> str:
    try:
        comm: str = Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return ""
    # `codex`, or an install-suffixed `codex-x86_64-un`; never a `run-claude` wrapper (as the server's wake check)
    return next((h for h in HARNESSES if comm.partition("-")[0] == h), "")


def harnesses() -> list[tuple[int, str]]:
    """Harness ancestors as (pid, kind), nearest first."""
    return [(pid, kind) for pid in ancestors() for kind in [harness_kind(pid)] if kind]


def seat(sid: str, cwd: str) -> dict[str, Any]:
    """Per-session facts computed once and cached: wanted names, repo, and whether we're a nested harness."""
    found: list[tuple[int, str]] = harnesses()
    pid: int = found[0][0] if found else 0
    f: Path = STATE / "seat" / f"{sid}.json"
    cached: dict[str, Any] | None = load_cache(f)
    # 4: v3 took a 0.164 Codex daemon for nested
    if cached and cached.get("version") == 4 and cached.get("pid") == pid:
        return cached
    session_name: str = ""
    timed_out: bool = False
    # a harness started from inside another agent (claude -p / codex exec in its Bash tool) inherits the parent's
    # TMUX_PANE; it must not claim that pane or its names, or it would replace the parent and wake into its terminal
    # Codex 0.163+ runs every thread's hooks in one shared `codex app-server` daemon (0.164: the child of another
    # codex process); its tmux pane and session (so the names from them) are the daemon's, not this thread's
    daemon: bool = found[:1] == [(pid, "codex")] and is_daemon(pid)
    # inside Codex's sandbox (PID 1 = codex-linux-sandbox) the outer Codex is hidden, but it's there: we're nested
    nested: bool = not daemon and (len(found) > 1 or "codex" in sandbox_init())
    names: list[str] = (
        [os.environ["ARKE_NAME"]]
        if os.environ.get("ARKE_NAME") and not nested and not daemon
        else []
    )
    if tmux_pane()[0] and not nested and not daemon:
        try:
            r: subprocess.CompletedProcess[str] | None = subprocess.run(
                [
                    "tmux",
                    "display-message",
                    "-p",
                    "-t",
                    os.environ["TMUX_PANE"],
                    "#S\t#I",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=PREP_TIMEOUT_S,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            print(f"arke: tmux: {e}", file=sys.stderr)
            r, timed_out = None, True  # no session names this time
        if r is not None and r.returncode == 0 and "\t" in r.stdout:
            sess, win = r.stdout.strip().split("\t")
            names += [sess, f"{sess}-{win}"]
            session_name = sess
    top: str | None = git(cwd, "rev-parse", "--show-toplevel")
    out: dict[str, Any] = {
        "version": 4,
        "pid": pid,
        "names": names,
        "session": session_name,
        "repo": os.path.basename(top or ""),
        "nested": nested,
        "daemon": daemon,
    }
    # a timeout isn't cached, so the next hook asks again
    if not timed_out and top is not None:
        save_cache(f, out)
    return out


def is_daemon(pid: int) -> bool:
    """Whether a Codex process is its shared app-server daemon."""
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[1:2] == [
            b"app-server"
        ]
    except OSError:
        return False


def process_token(pid: int) -> str:
    """Identify this actual harness lifetime (pid from harnesses()), including nested agents that must not
    expose a sampling pid. pid 0 (none) has no /proc entry, so no token."""
    try:
        start: int = int(
            Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        )
    except (OSError, ValueError, IndexError):
        return ""
    return f"{HOST}:{pid}:{start}"


USAGE_FRESH: dict[str, Any] = {
    "offset": 0,
    "tokens": 0,
    "context": 0,
    "model": "",
    "effort": "",
    "window": 0,
}


def usage(sid: str, transcript: str) -> dict[str, Any]:
    """Model, effort, tokens out, context size (and Codex's window), read incrementally; state cached per session."""
    f: Path = STATE / "usage" / f"{sid}.json"
    u: dict[str, Any] = read_usage(load_cache(f) or USAGE_FRESH, Path(transcript))
    save_cache(f, u)
    return u


def read_usage(u: dict[str, Any], path: Path) -> dict[str, Any]:
    """Advance usage through complete lines, counting Claude's cumulative usage once per response id."""

    def count(value: Any) -> int:
        if type(value) is not int or value < 0:
            raise ValueError("usage counts must be non-negative integers")
        return value

    u = dict(u)
    if not path.is_file():
        return u
    u = USAGE_FRESH | u
    with path.open("rb") as fh:
        size: int = os.fstat(fh.fileno()).st_size
        source: str = str(path.resolve())
        offset: int = u["offset"]
        if (
            size < offset or u.get("source", source) != source
        ):  # truncated, or another file
            u = dict(USAGE_FRESH)
            offset = 0
        outputs: dict[str, int] = dict(u.get("claude_outputs") or {})
        u["claude_outputs"] = outputs
        end: int = offset
        fh.seek(offset)
        while fh.tell() < size:
            line: bytes = fh.readline(size - fh.tell())
            if not line.endswith(b"\n"):
                break  # only complete lines; the partial one is read next time
            end = fh.tell()
            try:
                rec: Any = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            try:
                pl: Any = rec.get("payload")
                # Codex: running totals in token_count events, model in turn_context
                if isinstance(pl, dict):
                    if rec.get("type") == "turn_context":
                        u["model"] = pl.get("model") or u["model"]
                        u["effort"] = pl.get("effort") or u["effort"]
                    info: Any = (
                        pl.get("info") if pl.get("type") == "token_count" else None
                    )
                    if isinstance(info, dict):
                        # Validate the whole record before publishing any of its counts.
                        window = count(info.get("model_context_window") or u["window"])
                        tokens = count(
                            (info.get("total_token_usage") or {}).get(
                                "output_tokens", u["tokens"]
                            )
                        )
                        context = count(
                            (info.get("last_token_usage") or {}).get(
                                "input_tokens", u["context"]
                            )
                        )
                        u.update(window=window, tokens=tokens, context=context)
                    continue
                msg: Any = rec.get("message")
                if (
                    rec.get("type") == "assistant"
                    and isinstance(msg, dict)
                    and isinstance(msg.get("usage"), dict)
                ):
                    us: dict[str, Any] = msg["usage"]
                    total: int = count(us.get("output_tokens", 0))
                    context = sum(
                        count(us.get(k, 0))
                        for k in (
                            "input_tokens",
                            "cache_read_input_tokens",
                            "cache_creation_input_tokens",
                        )
                    )
                    key: str = (
                        f"message:{msg['id']}"
                        if msg.get("id")
                        else f"record:{rec['uuid']}"
                        if rec.get("uuid")
                        else ""
                    )
                    if key:
                        previous: int = outputs.get(key, 0)
                        u["tokens"] += max(0, total - previous)
                        outputs[key] = max(total, previous)
                    else:  # old transcripts without response or record ids can only be counted per line
                        u["tokens"] += total
                    u["context"] = context
                    u["model"] = msg.get("model") or u["model"]
                    u["effort"] = rec.get("effort") or u["effort"]
            except (TypeError, AttributeError, ValueError) as e:
                # A complete bad record must not pin the cursor and hide every later update.
                print(f"arke: usage from {path}: {e}", file=sys.stderr)
        u |= {"offset": end, "source": source}
    return u


def hook(
    kind: str, event: dict[str, Any], quick: bool = False
) -> dict[str, Any] | None:
    """One /hook call -> harness output plus private receipt metadata consumed by run_hook."""
    name: str = event["hook_event_name"]
    # a turn ended by an API error: Claude ignores this hook's output, so no mail
    failed: bool = name == "StopFailure"
    if failed:
        name = "Stop"  # still a turn end (older servers know only Stop)
    sid: str = event["session_id"]
    cwd: str = event.get("cwd") or os.getcwd()
    body: dict[str, Any] = {
        "event": name,
        "session_id": sid,
        "kind": kind,
        "host": HOST,
        "cwd": cwd,
        "transcript": event.get("transcript_path", ""),
        "model": event.get("model", ""),
        # SessionStart: startup, resume, clear, compact
        "source": str(event.get("source") or ""),
        "failed": failed,
        "receipt": True,
    }
    st: dict[str, Any] = seat(sid, cwd)
    pid: int = st["pid"]
    nested: bool = st["nested"]
    # A daemon thread has no pane or names of its own. Its process token rejects late hooks after a daemon
    # restart; the server scopes that protection to the thread and never uses it to merge daemon threads.
    daemon: bool = st["daemon"]
    token: str = process_token(pid)
    if daemon and token:
        token += f":{sid}"  # distinct threads must never claim the same session identity, even during a server update
    body |= {
        "daemon": daemon,
        "names": [] if daemon else st["names"],
        "repo": st["repo"],
        "tmux_session": "" if nested or daemon else st.get("session", ""),
        "nested": nested,
        "process_token": token,
        "boot": BOOT,
    }
    tmux, tmux_server = tmux_pane()
    if tmux and not nested and not daemon:
        # pane ids start over at %0 when the tmux server does
        body |= {"tmux": tmux, "tmux_server": tmux_server}
    if pid and not nested:
        # the server samples this process tree's CPU/memory (agents on its own host)
        body["pid"] = pid
    if name in ("SessionStart", "UserPromptSubmit"):
        branch: str | None = git(cwd, "branch", "--show-current")
        if branch is not None:
            body["branch"] = branch
    # map the harness pid to this session so `arke` run from the agent's shell can find it; on any event, so sessions
    # that started before the hooks were installed (or before a restart) get it on their next tool call
    pid_file: Path = STATE / "pid" / str(pid)
    if pid and not daemon:
        # missing, or corrupt (a directory, undecodable bytes): rewrite it
        try:
            recorded: str = pid_file.read_text()
        except (OSError, ValueError):
            recorded = ""
        if recorded != sid:
            save_cache(pid_file, sid)
    if name == "PostToolUse":
        inp: Any = event.get("tool_input") or {}
        # some tools send a bare string (or list) as their input
        if not isinstance(inp, dict):
            inp = {"command": inp}
        body["tool"] = event.get("tool_name", "?")
        body["target"] = str(
            inp.get("file_path")
            or inp.get("cmd")
            or inp.get("command")
            or inp.get("pattern")
            or inp.get("url")
            or ""
        )[:4000]
        # Claude's Bash says what the command is for
        body["desc"] = str(inp.get("description") or "")[:300]
    if name == "UserPromptSubmit":
        # typed by the bus's wake-up, or Claude's background listener waking it (a task notification), not a person
        prompt: str = str(event.get("prompt") or "")
        body["wake"] = prompt.startswith("[arke]") or (
            prompt.lstrip().startswith("<task-notification>") and "[arke]" in prompt
        )
    u: dict[str, Any] | None = None
    # live usage, incremental so cheap; not SessionStart (a resumed session's first read is its whole file) or
    # SessionEnd (1.5 s)
    if body["transcript"] and name not in ("SessionStart", "SessionEnd"):
        try:
            u = usage(sid, body["transcript"])
        # an unreadable transcript or cache only costs this turn's numbers
        except (OSError, TypeError, AttributeError, ValueError) as e:
            print(f"arke: usage from {body['transcript']}: {e}", file=sys.stderr)
    if u:  # a Claude window of 0 is left to the server's estimate
        body |= {k: u[k] for k in ("tokens", "context", "window", "effort")}
        body["model"] = u["model"] or body["model"]
    out: dict[str, Any] = call(
        "POST",
        "/hook",
        body,
        # SessionEnd must finish inside the harness's short SessionEnd limit
        timeout=TURN_HOOK_TIMEOUT_S
        if name in TURN_HOOKS and name != "SessionEnd" and not quick
        else HOOK_TIMEOUT_S,
        sid=sid,
    )
    if not isinstance(out, dict):
        raise Down(f"unexpected reply from the bus: a JSON {type(out).__name__}")
    if out.get("block") and not failed:
        return {
            "decision": "block",
            "reason": printable(out["block"]),  # Claude shows it on screen
            "_delivery": out.get("delivery"),
        }
    if out.get("context"):
        return {
            "hookSpecificOutput": {
                "hookEventName": name,
                "additionalContext": printable(out["context"]),
            },
            "_delivery": out.get("delivery"),
        }
    return None


def run_hook(kind: str) -> None:
    try:
        # undecodable bytes raise UnicodeDecodeError, a ValueError
        event: Any = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise TypeError(f"expected a JSON object, got {type(event).__name__}")
    except (ValueError, TypeError) as e:
        print(
            json.dumps({"systemMessage": f"arke hook: unreadable event: {e}"}),
            flush=True,
        )
        return
    if kind == "claude" and event.get("hook_event_name") == "PreToolUse":
        if out := mark_helper(event):
            print(json.dumps(out), flush=True)
        return
    if event.get("agent_id"):
        # a sub-agent (its hooks carry the parent's session_id) is invisible to the bus and gets no output. A Codex
        # sub-agent's shell has its own CODEX_THREAD_ID (= agent_id): map it to the parent for identity()
        if kind == "codex" and isinstance(event.get("session_id"), str):
            save_cache(STATE / "thread" / str(event["agent_id"]), event["session_id"])
        return
    try:
        down: Path = (
            STATE
            / f"down-until-{hashlib.sha256(server()[0].encode()).hexdigest()[:12]}"
        )
    except (OSError, ValueError, BusError) as e:
        print(
            json.dumps({"systemMessage": f"arke configuration error: {e}"}), flush=True
        )
        return
    # after a failure, routine tool hooks skip the bus for a while so a dead server doesn't slow every tool call;
    # start/end-of-turn hooks always try, since a missed one leaves the agent shown busy (and never woken), but
    # with the short timeout
    marked_down: bool = False
    try:
        marked_down = float(down.read_text()) > time.time()
    except (OSError, ValueError):
        pass  # no back-off, or another hook is writing it right now: try the server
    if marked_down and event.get("hook_event_name") not in TURN_HOOKS:
        return
    try:
        out = hook(kind, event, quick=marked_down)
    except Down as e:  # only the bus call: a local failure or a 4xx mustn't mute every agent's tool hooks
        warned: bool = down.exists()
        save_cache(down, str(time.time() + DOWN_BACKOFF_S))
        out = (
            None
            if warned
            else {
                "systemMessage": f"arke: {e}\n  Tool hooks skip the bus for {int(DOWN_BACKOFF_S)}s at a time until it's back."
            }
        )
    except BusError as e:  # the bus answered no; it is up
        out = {"systemMessage": f"arke: {e}"}
    except Exception as e:  # noqa: BLE001 — a hook must never break the agent's turn; surface it instead
        out = {"systemMessage": f"arke hook error: {type(e).__name__}: {e}"}
    else:
        try:  # the bus answered; a stale marker we can't remove (read-only state dir) mustn't hide that
            down.unlink(missing_ok=True)
        except OSError as e:
            print(f"arke: couldn't clear {down}: {e}", file=sys.stderr)
    if out:
        delivery: Any = out.pop("_delivery", None)
        print(json.dumps(out), flush=True)
        acknowledge(delivery)


# --- CLI ---


def flags(args: list[str]) -> tuple[list[str], dict[str, str]]:
    """Split off --board/--all and --before/--request-id VALUE. After a message's text only the command's own
    trailing flags count, so a sentence that mentions --all or ends in --before 12 keeps it."""
    switches: tuple[str, ...] = ("--board", "--all")
    valued: tuple[str, ...] = ("--before", "--request-id")
    found: dict[str, str] = {}
    text_flags: dict[str, tuple[str, ...]] = {
        "send": ("--board", "--request-id"),
        "broadcast": ("--request-id",),
        "about": (),
    }
    if args[:1] and args[0] in text_flags:
        own: tuple[str, ...] = text_flags[args[0]]
        while args:
            if (
                args[-1] == "--request-id" and args[-1] in own
            ):  # its value forgotten: don't send it as text
                raise SystemExit(f"arke: {args[-1]} needs a value")
            if args[-1] == "--board" and args[-1] in own:
                found[args.pop()] = ""
            elif len(args) > 2 and args[-2] == "--request-id" and args[-2] in own:
                found[args[-2]] = args[-1]
                del args[-2:]
            else:
                break
        return args, found
    rest: list[str] = []
    i: int = 0
    while i < len(args):
        if args[i] in switches:
            found[args[i]] = ""
        elif args[i] in valued:
            if i + 1 == len(args):
                raise SystemExit(f"arke: {args[i]} needs a value")
            found[args[i]] = args[i + 1]
            i += 1
        else:
            rest.append(args[i])
        i += 1
    return rest, found


def main() -> None:
    args, opts = flags(sys.argv[1:])
    board: bool = "--board" in opts
    show_all: bool = "--all" in opts
    before: str = opts.get("--before", "")
    request_id: str = opts.get("--request-id", "")
    # for the commands below; a hook imports only what its event needs
    if args[:1] != ["hook"]:
        import urllib.error
        import urllib.parse
        import urllib.request
    if args[:1] == ["inbox"] and len(args) == 2 and args[1].startswith("--wait="):
        args = ["inbox", "--wait", args[1].removeprefix("--wait=")]
    try:
        match args:
            case ["hooks", kind] if kind in HARNESSES:
                script: str = shlex.quote(str(Path(__file__).resolve()))
                words: list[str] = [shlex.quote(sys.executable), script, "hook", kind]
                if os.environ.get("ARKE_CONFIG"):
                    words = [
                        "env",
                        shlex.quote(f"ARKE_CONFIG={settings.config_file()}"),
                        *words,
                    ]
                # a missing script makes python3 exit 2, which Claude takes as "block": every tool call, prompt
                # and stop of a running session refused once the clone moves. Exit 1 is a plain hook error.
                command: str = (
                    f"[ -f {script} ] || {{ echo arke: missing {script} >&2; exit 1; }}; exec "
                    + " ".join(words)
                )
                native_hook: list[dict[str, Any]] = []
                if kind == "claude":
                    import native

                    if native.supported():
                        native_hook = [
                            {
                                "type": "command",
                                "command": f"[ -f {script} ] || exit 1; exec "
                                + " ".join(words[:-2] + ["listen", "claude"]),
                                # asyncRewake alone can block single-shot `claude -p`.
                                "async": True,
                                "asyncRewake": True,
                                "timeout": int(native.LIFETIME_S),
                            }
                        ]
                events: list[str] = [
                    "SessionStart",
                    "UserPromptSubmit",
                    "PostToolUse",
                    "Stop",
                    "SessionEnd",
                ]
                # StopFailure: Claude only; SubagentStart: maps a Codex sub-agent's thread to its parent
                events.append("StopFailure" if kind == "claude" else "SubagentStart")
                if kind == "claude":
                    events.append("PreToolUse")
                print(
                    json.dumps(
                        {
                            "hooks": {
                                event: [
                                    {
                                        **(
                                            {"matcher": "Bash|Monitor"}
                                            if event == "PreToolUse"
                                            else {}
                                        ),
                                        "hooks": [
                                            {"type": "command", "command": command}
                                            # SessionEnd defaults: Claude 1.5 s, Codex 1 s; we need startup + HOOK_TIMEOUT_S
                                            | (
                                                {
                                                    "timeout": 5
                                                    if kind == "claude"
                                                    else 3
                                                }
                                                if event == "SessionEnd"
                                                else {}
                                            )
                                        ]
                                        + (
                                            native_hook
                                            if event
                                            in ("SessionStart", "Stop", "StopFailure")
                                            else []
                                        ),
                                    }
                                ]
                                for event in events
                            }
                        },
                        indent=2,
                    )
                )
            case ["hook", kind] if kind in HARNESSES:
                run_hook(kind)
            case ["listen", "claude"]:
                import native

                raise SystemExit(native.listen())
            case ["who", *who_args]:
                global_scope: bool = "--global" in who_args
                paths: list[str] = [arg for arg in who_args if arg != "--global"]
                if len(paths) > 1 or any(not path.startswith("/") for path in paths):
                    raise BusError("usage: arke who [/PROJECT | --global] [--all]")
                query: dict[str, str] = {}
                if paths:
                    query["project"] = paths[0]
                if global_scope:
                    query["global"] = "1"
                if show_all:
                    query["all"] = "1"
                print(
                    printable(
                        roster.render(
                            call("GET", "/roster?" + urllib.parse.urlencode(query))
                        )
                    )
                )
            case ["projects"]:
                for p in call("GET", "/projects"):
                    depth: int = p["path"].count("/") - 1
                    print(
                        f"{'  ' * depth}{p['path']:<{30 - 2 * depth}} {p['subscribed']}/{p['members']} subscribed"
                    )
            case ["log"] | ["log", _]:
                key: str = (
                    "project" if len(args) > 1 and args[1].startswith("/") else "agent"
                )
                query = {key: args[1]} if len(args) > 1 else {}
                if before:
                    query["before"] = str(int(before))
                msgs: list[dict[str, Any]] = call(
                    "GET", "/log?" + urllib.parse.urlencode(query)
                )
                print(fmt(msgs) or "(nothing yet)")
                if len(msgs) >= 200:  # a full page: there may be older ones
                    print(
                        f"(older: arke log {' '.join(args[1:])} --before {msgs[0]['id']})".replace(
                            "  ", " "
                        )
                    )
            case ["send", to, *text] if text:
                r: dict[str, Any] = call(
                    "POST",
                    "/send",
                    {
                        "to": to.split(","),
                        "text": " ".join(text),
                        "mode": "board" if board else "notify",
                        "request_id": request_id,
                    },
                )
                print(
                    f"#{r['id']} request sent only to parent {', '.join(r['reach'])}; intended recipients were not contacted"
                    if r.get("forwarded_to_parent")
                    else f"#{r['id']} {'posted to board' if r['mode'] == 'board' else 'sent'}: reaches {', '.join(r['reach'])}"
                )
                for x in r.get("ambiguous", []):
                    print(
                        f"note: {x['name']} is a current agent's name but also an old name of {', '.join(x['also_old_name_of'])}; "
                        f"delivered to the current {x['name']}. If you meant the other, send to its current name."
                    )
                for rn in r.get("renamed", []):
                    print(
                        f"note: {rn['old']} is now called {rn['new']}; delivered to {rn['new']} (use the new name)"
                    )
                for u in r.get("unreachable", []):
                    print(
                        f"note: {u['name']} looks unreachable ({u['why']}); the message is stored for it"
                    )
            case ["broadcast", *text] if text:
                r = call(
                    "POST",
                    "/send",
                    {"to": ["*"], "text": " ".join(text), "request_id": request_id},
                )
                print(
                    f"#{r['id']} broadcast request sent only to parent {', '.join(r['reach'])}"
                    if r.get("forwarded_to_parent")
                    else f"#{r['id']} broadcast to {len(r['reach'])} agents"
                )
            case ["inbox"]:
                print_inbox()
            case ["inbox", "--wait", secs]:
                if not abs(float(secs)) < float("inf"):  # inf, nan
                    raise BusError("--wait takes a number of seconds")
                print_inbox(float(secs))
            case [("join" | "leave" | "subscribe" | "unsubscribe") as action, project]:
                r = call("POST", "/projects", {"project": project, "action": action})
                print(
                    "projects: "
                    + " ".join(
                        f"{p}{'' if s else ' (unsubscribed)'}"
                        for p, s in r["projects"].items()
                    )
                )
            case ["server"]:
                url, source = server()
                print(f"{url}  (from {source})")
                try:
                    open_bus(urllib.request.Request(url + "/agents"), 5).close()
                except (
                    urllib.error.HTTPError
                ) as e:  # it answered: not a reachability problem
                    raise BusError(
                        f"reachable, but /agents answered HTTP {e.code} {e.reason}"
                        + (" (check the key)" if e.code == 401 else "")
                    ) from None
                print(
                    "reachable"
                    + (
                        " (key sent)"
                        if SETTINGS.get("client", {}).get("key_file")
                        else " (no key configured)"
                    )
                )
            case ["server", "--reset"]:
                if identity()[1]:
                    raise BusError("sub-agents cannot change the bus address")
                CONFIG.unlink(missing_ok=True)
                print(f"using {server()[0]} ({server()[1]})")
            case ["server", addr] if not addr.startswith(
                "-"
            ):  # `server --help` isn't an address
                if identity()[1]:
                    raise BusError("sub-agents cannot change the bus address")
                url: str = normalize(addr)
                CONFIG.parent.mkdir(parents=True, exist_ok=True)
                CONFIG.write_text(url + "\n")
                print(
                    f"saved {url} to {CONFIG}"
                    + (
                        "  (but $ARKE_URL overrides it)"
                        if os.environ.get("ARKE_URL")
                        else ""
                    )
                )
            case ["name", new]:
                r = call("POST", "/name", {"name": new})
                print(f"{r['old']} -> {r['name']}")
            case ["about", *text] if text:
                call("POST", "/about", {"about": " ".join(text)})
            case ["help"]:
                print(USAGE)
                # the bus's own guide
                with open_bus(urllib.request.Request(server()[0] + "/help"), 5) as resp:
                    print(resp.read().decode())
            case _:
                sys.exit(USAGE)
    except (BusError, ValueError) as e:  # ValueError: a malformed number (--before abc)
        sys.exit(f"arke: {e}")
    except OSError as e:
        sys.exit(f"arke: {explain(e)}")


if __name__ == "__main__":
    main()

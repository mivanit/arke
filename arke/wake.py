"""Waking idle agents through the Codex daemon or a terminal notice.

Codex daemon conversations use turn/start (codex_turn). Terminal agents receive a notice typed into their tmux pane
(type_notice). The terminal path reads the pane's prompt box first (prompt_box): a dialog or someone's draft holds the
wake-up (a draft left unchanged for WAKE_PUSH_S gets the notice added under it, with a note, and is sent). The notice
goes in as one bracketed paste, Enter is pressed once it shows, and the box must clear; a wake-up the agent hasn't
picked up after WAKE_ACK_S is typed again, up to WAKE_TRIES times. Every step is in the agent's `wake` record.
When to wake whom, and those timings, are in server.py (Bus.wake_pass); turn_marker reads busy/idle from a transcript.
"""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

if __package__:
    from .procs import cmdline, process_info
else:
    from procs import cmdline, process_info
if __package__:
    from .storage import Agent, Message
else:
    from storage import Agent, Message
if __package__:
    from .transcripts import (
        TRANSCRIPT_TAIL_BYTES,
        parse_ts,
        transcript_record,
        whole_lines,
    )
else:
    from transcripts import (
        TRANSCRIPT_TAIL_BYTES,
        parse_ts,
        transcript_record,
        whole_lines,
    )

TMUX_TIMEOUT_S: float = 2.0  # pane checks run under the bus lock


class WakeCancelled(Exception):
    """The queued wake is no longer safe to type into its original harness."""


class CodexRefused(Exception):
    """The Codex daemon answered a request with an error (it is up; the thread can't take a turn right now)."""


ANSI: re.Pattern[str] = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
DIM: re.Pattern[str] = re.compile(
    r"\x1b\[(?:0;)?2m.*?(?=\x1b\[(?:0|22)?m|$)"
)  # dim text: the box's placeholder ("Try ...")
# Claude draws a placeholder's first letter inverted (the cursor sits on it) and the rest dim; tmux captures it as
# ESC[7mT ESC[0;2mry ... Turn that letter dim too, so the whole placeholder reads as empty, not as a draft.
PLACEHOLDER: re.Pattern[str] = re.compile(r"\x1b\[7m.\x1b\[(?:27m\x1b\[)?(?:0;)?2m")
CURSOR: re.Pattern[str] = re.compile(
    r"\x1b\[7m \x1b\[(?:0|27)m(?=(?:\x1b\[[0-9;?]*[A-Za-z])*$)"
)  # Ambiguous with a real trailing space under the cursor; wake checks End before treating it as empty.
BORDER: re.Pattern[str] = re.compile(
    r"^[\s│]*[─━═╭╮╰╯┌┐└┘][\s─━═╭╮╰╯┌┐└┘│]*$"
)  # a rule under the box (Claude)
# Claude's top rule can carry text (fast mode, the prompt-history position, a renamed session's banner,
# which keeps one rule char after its label): "──── text ────", "──── name ─"
LABELED: re.Pattern[str] = re.compile(r"^[\s│]*[─━═╭┌]{2,}.*[─━═╮┐][\s│]*$")


# Claude's key hints under a menu (its binary pairs Enter/Esc with these actions) and its history search
MENU: re.Pattern[str] = re.compile(
    r"\b(?:Enter|Esc) to (?:confirm|select|continue|save|close|go back|exit)\b|search prompts:|no matching prompt:"
)


def tmux_run(sock: str, *args: str, stdin: str | None = None) -> str:
    r: subprocess.CompletedProcess[str] = subprocess.run(
        ["tmux", "-S", sock, *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        timeout=TMUX_TIMEOUT_S,
    )
    if r.returncode != 0:
        raise OSError(f"tmux {args[0]} failed: {r.stderr.strip()}")
    return r.stdout


def wake_notice(unread: list[Message]) -> str:
    senders: list[str] = list(dict.fromkeys(m["from"] for m in unread))
    what: str = (
        f"new message #{unread[0]['id']} from {senders[0]}"
        if len(unread) == 1
        else f"{len(unread)} new messages ({', '.join(f'#{m["id"]} {m["from"]}' for m in unread[:6])}"
        f"{', ...' if len(unread) > 6 else ''})"
    )
    return f"[arke] {what}. Read them and respond as appropriate."


CODEX_SOCK: Path = (
    Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    / "app-server-control"
    / "app-server-control.sock"
)
CODEX_TIMEOUT_S: float = (
    5.0  # one whole wake request, including notifications and fragmented replies
)


def codex_turn(
    thread: str, text: str, *, before_start: Callable[[], None] | None = None
) -> str:
    """Start a turn in a Codex thread through its app-server daemon, the way its own TUI does (JSON-RPC over a
    WebSocket on a unix socket; experimental in Codex 0.163/0.164), if the daemon says the thread is idle. Returns
    "started", or the thread's status ("active", "notLoaded", ...). OSError: no daemon to talk to; CodexRefused:
    it answered with an error."""
    with socket.socket(socket.AF_UNIX) as s:
        deadline: float = time.monotonic() + CODEX_TIMEOUT_S

        def budget() -> None:
            remaining: float = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex daemon wake request timed out")
            s.settimeout(remaining)

        budget()
        s.connect(str(CODEX_SOCK))
        key: str = base64.b64encode(os.urandom(16)).decode()
        budget()
        s.sendall(
            f"GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        buf: bytes = b""
        while b"\r\n\r\n" not in buf:
            budget()
            chunk: bytes = s.recv(4096)
            if not chunk:
                raise OSError("codex daemon closed the connection")
            buf += chunk
        head, buf = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise OSError(f"codex daemon refused the websocket: {head[:200]!r}")

        def send(
            obj: dict[str, Any],
        ) -> None:  # a client's frames are masked (RFC 6455)
            data: bytes = json.dumps(obj).encode()
            mask: bytes = os.urandom(4)
            n: int = len(data)
            size: bytes = (
                bytes([0x80 | n])
                if n < 126
                else bytes([0x80 | 126]) + n.to_bytes(2, "big")
            )
            budget()
            s.sendall(
                bytes([0x81])
                + size
                + mask
                + bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            )

        def read(k: int) -> bytes:
            nonlocal buf
            while len(buf) < k:
                budget()
                chunk: bytes = s.recv(65536)
                if not chunk:
                    raise OSError("codex daemon closed the connection")
                buf += chunk
            out, buf = buf[:k], buf[k:]
            return out

        def call(rid: int, method: str, params: dict[str, Any]) -> Any:
            send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            message: bytes = b""
            while True:  # frames until our reply; notifications in between are skipped
                b0, b1 = read(2)
                n: int = b1 & 0x7F
                if n >= 126:
                    n = int.from_bytes(read(2 if n == 126 else 8), "big")
                payload: bytes = read(n)
                if b0 & 0x0F == 8:
                    raise OSError("codex daemon closed the websocket")
                if b0 & 0x0F not in (0, 1):
                    continue  # ping/pong
                message += payload
                if not b0 & 0x80:
                    continue  # fragment: more follows
                reply: Any = json.loads(message)
                message = b""
                if reply.get("id") == rid:
                    if "error" in reply:
                        raise CodexRefused(
                            f"codex {method}: {reply['error'].get('message')}"
                        )
                    return reply["result"]

        call(1, "initialize", {"clientInfo": {"name": "arke", "version": "1"}})
        send({"jsonrpc": "2.0", "method": "initialized"})
        status: str = call(2, "thread/read", {"threadId": thread})["thread"]["status"][
            "type"
        ]
        if status != "idle":
            return status
        if before_start is not None:
            before_start()
        call(
            3,
            "turn/start",
            {"threadId": thread, "input": [{"type": "text", "text": text}]},
        )
        return "started"


def harness_in_pane(a: Agent, sock: str, pane: str) -> bool:
    """The registered process (or its native child behind a wrapper) owns the foreground of the pane's tty: verify the
    process, its start time and that it runs in this pane (prompt glyphs also occur in shells)."""
    kind: Any | None = a.get("kind")
    if kind not in ("claude", "codex"):
        return False
    info: dict[str, Any] | None = process_info(a.get("pid"))
    if not info or a.get("pid_start") != info["start"]:
        return False
    try:
        pane_pid, tty = (
            tmux_run(
                sock, "display-message", "-p", "-t", pane, "#{pane_pid}\t#{pane_tty}"
            )
            .strip()
            .split("\t")
        )
        device: int = os.stat(tty).st_rdev
        seen: set[int] = set()
        root: int = a["pid"]
        ancestor: int = root
        while ancestor != int(pane_pid):  # the registered process runs in this pane
            if ancestor in seen or ancestor <= 1:
                return False
            seen.add(ancestor)
            parent: dict[str, Any] | None = process_info(ancestor)
            if not parent:
                return False
            ancestor = parent["ppid"]
        seen.clear()
        todo: list[int] = [root]
        while todo and len(seen) < 256:
            pid: int = todo.pop()
            if pid in seen:
                continue
            seen.add(pid)
            proc: dict[str, Any] | None = process_info(pid)
            if not proc:
                continue
            native: Any = proc["comm"] == kind or proc["comm"].startswith(kind + "-")
            # Older Claude/Codex distributions run their CLI through node/bun; a generic runtime name
            # alone is insufficient, but its installed CLI path identifies the actual harness.
            if not native and proc["comm"] in ("node", "bun"):
                command: str = cmdline(pid)
                native = (
                    kind == "claude"
                    and "/@anthropic-ai/claude-code/" in command
                    or kind == "codex"
                    and "/@openai/codex/" in command
                )
            if (
                native
                and proc["tty"] == device
                and proc["pgrp"] == proc["foreground"]
                and proc["foreground"] > 0
            ):
                return True
            try:
                todo.extend(
                    int(p)
                    for p in Path(f"/proc/{pid}/task/{pid}/children")
                    .read_text()
                    .split()
                )
            except OSError:
                continue
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False
    return False


def prompt_box(sock: str, pane: str) -> tuple[str, str]:
    """What the pane's prompt box holds: ("box", text) ("" when empty), ("dialog", what) when a menu or permission
    question is up, ("unknown", "") when no box is recognisable (another program, a layout this doesn't know)."""
    shown: list[str] = tmux_run(
        sock, "capture-pane", "-p", "-e", "-t", pane
    ).splitlines()
    while shown and not ANSI.sub("", shown[-1]).strip():
        shown.pop()  # a new or cleared harness draws at the top of a tall pane, above empty rows
    shown = shown[-40:]
    plain: list[str] = [
        ANSI.sub(
            "", DIM.sub("", CURSOR.sub("", PLACEHOLDER.sub("\x1b[2m", ln)))
        ).replace("\u00a0", " ")
        for ln in shown
    ]  # Claude puts a no-break space after ❯
    while plain and not plain[-1].strip():
        plain.pop()  # a short screen leaves empty rows below what's drawn
    # Prompts start at column zero; Claude's menus add one space (or a box edge).
    # Both harnesses indent draft continuations by two spaces: a copied ❯ or › there is draft text.
    at: int | None = next(
        (
            i
            for i in range(len(plain) - 1, -1, -1)
            if plain[i].removeprefix("│").removeprefix(" ").startswith("❯")
            or plain[i].startswith(("›", "!"))
        ),
        None,
    )
    first: str = plain[at].lstrip(" │") if at is not None else ""
    # Keep the shell marker so the command guard refuses Enter. Dropping it would turn a command into a draft.
    if first.startswith(("❯", "›")):
        first = first[1:]
    lines: list[str] = [first, *plain[at + 1 :]] if at is not None else plain[-15:]
    top: str = (
        plain[at - 1].strip()
        if at and (BORDER.match(plain[at - 1]) or LABELED.match(plain[at - 1]))
        else ""
    )
    # the box ends at the rule matching the top one: equal, or for a labeled top a plain rule as wide
    end: int | None = (
        next(
            (
                i
                for i in range(1, len(lines))
                if lines[i].strip() == top
                or (
                    BORDER.match(lines[i])
                    and len(lines[i].strip()) == len(top)
                    and not BORDER.match(top)
                )
            ),
            None,
        )
        if top
        else None
    )
    # Claude's menus (/model) and Ctrl-R history search draw its bordered box: Enter picks an option or runs an old
    # prompt. Their hints are dim (read with dim text kept), below the box's bottom rule (a draft may mention them).
    if (
        at is not None
        and end is not None
        and any(
            MENU.search(ANSI.sub("", ln))
            for ln in shown[at + end + 1 :]  # lines[i] is shown[at + i]
        )
    ):
        return "dialog", "a menu or search is open"
    # Codex's footer is the final block below a blank spacer, not the first status-like line in a draft.
    # Identify it before looking for dialog words: numbered lists and quoted menu hints are ordinary input.
    footer: int | None = None
    if at is not None and plain[at].startswith(("›", "!")) and not top:
        spacer: int | None = next(
            (i for i in range(len(lines) - 1, 0, -1) if not lines[i].strip()),
            None,
        )
        # An empty composer can put its single footer row directly under the prompt.
        if spacer is None and first in ("", " ") and len(lines) == 2:
            spacer = 0
        if (
            spacer is not None
            and any(
                re.search(
                    r"\?\s+for shortcuts|\d+%\s+(?:context\s+)?left|^\s*(?:gpt-[\w.-]+|o\d[\w.-]*|codex[\w.-]*)\b.*·",
                    ln,
                    re.IGNORECASE,
                )
                for ln in lines[spacer + 1 :]
            )
            and not any(BORDER.match(ln) for ln in lines[1:])
        ):
            footer = spacer + 1
    # Only controls below a recognized composer count as a dialog. Without one, inspect the choices themselves.
    tail: str = "\n".join(
        lines[end + 1 :]
        if end is not None
        else lines[footer:]
        if footer is not None
        else lines
    )
    if "Esc to cancel" in tail or "Do you want to" in tail:
        return "dialog", "a question or menu is open"
    if at is None:
        return "unknown", ""
    if end is not None:
        lines = lines[:end]  # Claude: matching rules enclose its draft.
    else:
        if footer is None:
            if re.match(r"\s*\d+\.", first):  # a numbered choice without a composer
                return "dialog", "a choice is open"
            return "unknown", ""
        if any("Shell mode" in ln for ln in lines[footer:]) and not first.startswith(
            "!"
        ):
            # The shell prompt is clipped or a glyph inside the command fooled the scan.
            return "unknown", ""
        lines = lines[:footer]
        if len(lines) > 1 and lines[-1] == "":
            lines.pop()  # Codex's single spacer before its footer, not draft blank lines
    # Keep the rendered draft, including indentation and trailing spaces, for change detection.
    # Only the one separator after the prompt glyph is UI padding. Continuation-line padding
    # stays too: guessing how much is UI versus user text would hide edits.
    lines[0] = lines[0].removeprefix(" ")
    if PLACEHOLDER.search(shown[at]) and not "".join(lines).strip():
        return "box", ""  # a placeholder wrapped onto more rows in a narrow pane
    return "box", "\n".join(lines)


def squash(s: str) -> str:
    return "".join(s.split())  # compare text across the box's line wrapping


def type_notice(
    sock: str,
    pane: str,
    text: str,
    paste: bool = True,
    *,
    before: str,
    action: Callable[..., str],
) -> tuple[str, str]:
    """Paste and submit; return (error or "", verified rendered text for retrying a lost Enter).
    `before`: the box as we read it; any other change cancels (WakeCancelled)."""
    typed: str = text if not paste else ""
    end: str = squash(text)[-30:]
    if paste:
        buf: str = f"arke-{os.getpid()}-{threading.get_ident()}"
        tmux_run(sock, "load-buffer", "-b", buf, "-", stdin=text)
        try:
            action(
                "paste-buffer", "-p", "-d", "-b", buf, "-t", pane, expect=before
            )  # bracketed paste arrives whole
        except Exception:
            tmux_run(sock, "delete-buffer", "-b", buf)
            raise
        for _ in range(
            50
        ):  # wait until it shows (then a short pause, so Enter isn't taken as part of the paste)
            time.sleep(0.1)
            kind, box = prompt_box(sock, pane)
            if kind == "box" and end in squash(box):
                time.sleep(0.15)
                kind, shown = prompt_box(sock, pane)
                # The paste action checked the draft exactly before inserting.
                # tmux omits trailing spaces once a line is completed by the
                # appended newline, although those spaces remain in the input.
                # Only account for that known rendering change at this boundary.
                prefix: str = (
                    before.rstrip(" ") + "\n" if text.startswith("\n") else before
                )
                if (
                    kind != "box"
                    or squash(shown) != squash(before + text)
                    or not shown.startswith((before, prefix))
                ):
                    raise WakeCancelled("someone is typing in its prompt box")
                before = typed = shown
                break
        else:
            return "the notice never showed in the prompt box", typed
    for _ in range(2):  # Enter; once more if the first was taken as part of the paste
        kind, box = prompt_box(sock, pane)
        if kind != "box" or end not in squash(box):
            return "the prompt box changed before submission", typed
        action("send-keys", "-t", pane, "Enter", expect=before if paste else text)
        for _ in range(30):
            time.sleep(0.1)
            kind, box = prompt_box(sock, pane)
            if kind != "box" or end not in squash(box):
                return "", typed
    return "Enter didn't submit it; the notice is still in the prompt box", typed


TURN_MARKS: tuple[bytes, ...] = (
    b'"user"',
    b'"assistant"',
    b"stop_hook_summary",
    b"task_started",
    b"task_complete",
    b"turn_aborted",
)


def turn_marker(path: Path, offset: int) -> tuple[int, tuple[float, str] | None]:
    """Read a transcript from `offset` for the last start or end of a turn: (new offset, (time, "busy"|"idle") or None).
    Claude: a prompt starts one, and so does any reply (turns started by a background task or another session's
    message have no prompt); stop-hook summaries and interrupts mark idle. If a hook continues the turn,
    the next reply marks it busy again. A turn's last reply is written before its Stop hook, so it never
    outdates that hook's idle. Codex: task_started / task_complete / turn_aborted. The bus's hooks can be missed; these can't."""
    size: int = path.stat().st_size
    if size < offset:
        offset = 0  # rewritten
    end: int = max(
        offset, size - TRANSCRIPT_TAIL_BYTES
    )  # a first look at a big file reads its tail
    found: tuple[float, str] | None = None
    # Keep the offset at unfinished Stop feedback so the next poll (or a restart)
    # reads it together with its summary. No extra persisted parser state is needed.
    feedback_at: int | None = None
    with path.open("rb") as f:
        for pos, raw in whole_lines(f, end, size):
            end = pos + len(raw)
            if not any(m in raw for m in TURN_MARKS):
                continue
            try:
                rec: Any = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            ts: float = parse_ts(rec.get("timestamp"))
            p: Any = rec.get("payload")
            kind: str = p.get("type", "") if isinstance(p, dict) else ""
            if kind == "task_started":
                found = (ts, "busy")
            elif kind in ("task_complete", "turn_aborted"):
                found = (ts, "idle")
            elif rec.get("subtype") == "stop_hook_summary":
                # A blocking Stop hook (ours handing over mail) continues the turn, unless one forced a stop
                # (preventedContinuation, continue:false). hookErrors alone doesn't say: a hook that failed
                # (exit 1, a timeout) is listed there too, and the turn has ended.
                found = (
                    ts,
                    "busy"
                    if feedback_at is not None and not rec.get("preventedContinuation")
                    else "idle",
                )
                feedback_at = None
            elif (
                rec.get("type") == "user"
                and rec.get("isMeta")
                and not rec.get("isSidechain")
            ):
                m: Any = rec.get("message")
                feedback_at = (
                    pos
                    if isinstance(m, dict)
                    and str(m.get("content")).startswith("Stop hook feedback:")
                    else None
                )
            elif rec.get("type") == "user" and not rec.get("isSidechain"):
                feedback_at = None
                # transcript_record: an odd record becomes an error notice, not an exception
                for e in transcript_record(raw, pos)[1]:
                    if e["kind"] == "prompt" and not e.get(
                        "command"
                    ):  # /model, /status run no turn; one that does replies
                        found = (ts, "busy")
                    elif e["kind"] == "notice" and e["text"] == "interrupted":
                        found = (ts, "idle")
            elif (
                rec.get("type") == "assistant"
                and not rec.get("isSidechain")
                and not rec.get("isApiErrorMessage")
                and not (
                    isinstance(rec.get("message"), dict)
                    and rec["message"].get("model") == "<synthetic>"
                )
            ):  # API errors, harness-made replies
                found = (ts, "busy")
    return feedback_at if feedback_at is not None else end, found

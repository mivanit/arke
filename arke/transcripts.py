"""Transcripts: reading Claude Code and Codex session files for the console.

read_transcript condenses a session (prompts, replies, tool calls, notices) into entries, merges in the bus
messages the agent sent or got, and pages through it with cursors that notice a rewritten or replaced file.
condense / condense_codex turn one transcript record into entries. Only files under TRANSCRIPT_ROOTS are read.
HttpError lives here because it is the lowest module that raises it (a bad cursor); server.py turns it into a status.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import client  # shared settings (same folder)
import settings

TRANSCRIPT_TAIL_BYTES: int = 2_000_000
MAX_TEXT: int = (
    20_000  # per transcript text/command/output; expanded tool calls show this much
)
# only transcripts under these folders are ever read; resolved, since transcript paths are resolved before the
# check (a symlinked ~/.claude)
TRANSCRIPT_ROOTS: tuple[Path, ...] = tuple(
    settings.path(p).resolve()
    for p in client.SETTINGS.get("server", {}).get(
        "transcript_roots", ["~/.claude/projects", "~/.codex/sessions"]
    )
)


class HttpError(Exception):
    def __init__(self, status: int, msg: str) -> None:
        super().__init__(msg)
        self.status: int = status


SESSION_GRACE_S: float = (
    60.0  # bus messages this long before a transcript's first entry still show in it
)
TAIL_CHUNK: int = (
    64_000  # first read size when looking for the last entries (grows 4x until enough)
)
TAIL_BYTES: int = 256  # a cursor also hashes this many bytes before its offset
FP_BYTES: int = 1024  # a transcript is identified by a hash of its first KB (fewer while it's shorter; see `mint`)


def file_fp(f: Any, n: int, start: int = 0) -> str:
    f.seek(start)
    return hashlib.sha256(f.read(n)).hexdigest()[:16]


def tail_fp(f: Any, offset: int) -> str:
    """Hash of the TAIL_BYTES before `offset`: a rewrite that keeps the file's head still changes this."""
    back: int = min(offset, TAIL_BYTES)
    return file_fp(f, back, offset - back)


def whole_lines(f: Any, start: int, end: int) -> Iterator[tuple[int, bytes]]:
    """(offset, line) for each complete line in [start, end). A line `start` cuts into is skipped; one still being
    written at `end` is left for the next read. Don't seek `f` until the loop is done."""
    f.seek(max(0, start - 1))
    if start and f.read(1) != b"\n":
        f.readline()
    pos: int = f.tell()
    while pos < end:
        raw: bytes = f.readline(end - pos)
        if not raw.endswith(b"\n"):
            return
        yield pos, raw
        pos += len(raw)


def mint(kind: str, f: Any, size: int, offset: int, extra: float) -> str:
    """An opaque transcript cursor: kind (n = poll from here, p = page back from here), file fingerprint and how many
    bytes it covers, a byte offset that is always a line start, a hash of the bytes just before it (a rewrite that
    keeps the first KB is still caught), and the last bus message id (n) or the page's start time (p).
    Clients store it and hand it back; only the server reads it."""
    fplen: int = min(
        size, FP_BYTES
    )  # a young file's fingerprint grows with it, after `check` saw its old prefix intact
    return f"{kind}.{file_fp(f, fplen)}.{fplen}.{offset}.{tail_fp(f, offset)}.{extra!r}"  # repr: exact round trip


def check(f: Any, size: int, cursor: str, kind: str) -> tuple[int, float] | None:
    """(offset, extra) if `cursor` still points into this file at a line start, else None: the file was replaced,
    rewritten or truncated, and the client must start over."""
    parts: list[str] = cursor.split(".", 5)
    if (
        len(parts) != 6
        or parts[0] != kind
        or not parts[2].isdigit()
        or not parts[3].isdigit()
    ):
        raise HttpError(
            400,
            f"bad transcript cursor {cursor!r} (expected one this server gave out as {'next' if kind == 'n' else 'prev'})",
        )
    fplen: int = int(parts[2])
    offset: int = int(parts[3])
    if (
        offset > size
        or fplen > size
        or file_fp(f, fplen) != parts[1]
        or tail_fp(f, offset) != parts[4]
    ):
        return None
    if offset:
        f.seek(offset - 1)
        if f.read(1) != b"\n":
            return None
    try:
        extra: float = float(parts[5])
    except ValueError:
        raise HttpError(
            400, f"bad transcript cursor {cursor!r} (its last field must be a number)"
        ) from None
    # nan would match every bus message on every poll; inf is only an open upper bound, which a page back passes on
    # from a page with no timestamps
    if math.isnan(extra) or (math.isinf(extra) and kind != "p"):
        raise HttpError(
            400, f"bad transcript cursor {cursor!r} (its last field must be finite)"
        )
    return offset, extra


def transcript_record(
    raw: bytes, pos: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Decode one record; damaged JSON or an unexpected record shape must not hide the rest of the file."""
    try:
        rec: Any = json.loads(raw)
        return (rec, condense(rec)) if isinstance(rec, dict) else ({}, [])
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError) as e:
        return {}, [
            {
                "ts": 0.0,
                "kind": "notice",
                "level": "error",
                "text": f"unreadable transcript line at byte {pos}",
                "detail": f"{type(e).__name__}: {e}\n"
                + raw[:2000].decode("utf-8", "replace"),
            }
        ]


class FileContext:
    """What a page of a transcript needs from outside itself, kept per file and extended as the file grows:
    when the session started (bus messages before it belong to earlier sessions) and, for Codex, which byte ranges
    a later `thread_rolled_back` undid (Codex's own screen drops those turns)."""

    def __init__(self, fp: str) -> None:
        self.fp: str = fp
        self.scanned: int = 0  # whole lines read up to here
        self.start: float | None = None  # time of the session's first entry
        self.turns: list[
            int
        ] = []  # offsets of the live (not undone) Codex turn starts, in order
        self.undone: list[
            tuple[int, int]
        ] = []  # [from, to) byte ranges a rollback removed (`to` = the rollback's line)
        self.tail: str = hashlib.sha256(b"").hexdigest()[
            :16
        ]  # hash of the TAIL_BYTES before `scanned` (a rewrite that keeps the head)

    def update(self, f: Any, size: int) -> None:
        for pos, raw in whole_lines(f, self.scanned, size):
            # cheap substring tests first: only a few record kinds matter here, most lines are skipped unparsed
            if (
                self.start is None
                or b'"task_started"' in raw
                or b'"thread_rolled_back"' in raw
            ):
                rec, entries = transcript_record(raw, pos)
                if rec:
                    if self.start is None:
                        self.start = next((e["ts"] for e in entries if e["ts"]), None)
                    p: Any = rec.get("payload")
                    if isinstance(p, dict) and rec.get("type") == "event_msg":
                        if p.get("type") == "task_started":
                            self.turns.append(pos)
                        elif p.get("type") == "thread_rolled_back":
                            n: int = int(p.get("num_turns") or 0)
                            if n > 0 and self.turns:
                                cut: int = self.turns[max(0, len(self.turns) - n)]
                                self.undone.append((cut, pos))
                                del self.turns[max(0, len(self.turns) - n) :]
            self.scanned = pos + len(raw)
        self.tail = tail_fp(f, self.scanned)

    def is_undone(self, offset: int) -> bool:
        return any(a <= offset < b for a, b in self.undone)


CONTEXTS: dict[
    str, FileContext
] = {}  # resolved path -> context (rebuilt when the file's fingerprint changes)
CONTEXTS_LOCK: threading.Lock = threading.Lock()


def file_context(path: Path, f: Any, size: int) -> FileContext:
    fp: str = file_fp(f, min(size, FP_BYTES))
    with CONTEXTS_LOCK:
        c: FileContext | None = CONTEXTS.get(str(path))
        if (
            c is None
            or c.fp != fp
            or c.scanned > size
            or tail_fp(f, c.scanned) != c.tail
        ):
            c = CONTEXTS[str(path)] = FileContext(fp)
        c.update(f, size)
        return c


def said_before(f: Any, start: int, ctx: FileContext | None) -> str:
    """What a tool call at byte `start` borrows as its description: summarize_tools' state after the lines just
    before it (up to 64 KB back), so a call reads the same whichever poll or page it lands in."""
    entries: list[dict[str, Any]] = []
    for pos, raw in whole_lines(f, max(0, start - 64_000), start):
        if not (ctx and ctx.is_undone(pos)):
            entries.extend(transcript_record(raw, pos)[1])
    return summarize_tools(entries)


def read_lines(
    f: Any, start: int, end: int, ctx: FileContext | None = None
) -> tuple[list[dict[str, Any]], int]:
    """Entries from the whole lines in [start, end) (start is a line start); also where the last whole line ends.
    Entry ids are `offset:k` (stable for this file); a record without a time gets the previous record's.
    Turns a later Codex rollback undid are left out (`ctx`)."""
    entries: list[dict[str, Any]] = []
    last: int = start
    ts: float = 0.0
    for pos, raw in whole_lines(f, start, end):
        last = pos + len(raw)
        if ctx and ctx.is_undone(pos):
            continue
        for k, e in enumerate(transcript_record(raw, pos)[1]):
            if (
                e["kind"] == "rollback"
            ):  # tell the client which rows to drop: the range this rollback undid
                e["undo_from"] = (
                    next((a for a, b in ctx.undone if b == pos), pos) if ctx else pos
                )
                e["undo_to"] = pos
            ts = e["ts"] = e["ts"] or ts
            e["id"] = f"{pos}:{k}"
            entries.append(e)
    summarize_tools(entries, said_before(f, start, ctx) if start else "")
    return entries, last


def read_transcript(
    path: Path,
    msgs: Callable[[float], list[dict[str, Any]]],
    cursor: str = "",
    limit: int = 40,
    before: str = "",
) -> dict[str, Any]:
    """Condensed transcript of a Claude Code or Codex session (prompts, replies, tool calls, notices) with the bus
    messages this agent sent or got merged in. Every entry has a stable `id`, so clients can merge reads idempotently.

    cursor (a previous `next`): what was appended since, plus bus messages newer than the last one included.
    otherwise the last `limit` entries, ending at `before` (a previous `prev`) when paging back.
    `next` is the cursor to poll with, `prev` the one to page back with (null at the start of the file).
    If a cursor no longer fits the file (new session file, rewritten, truncated) the reply is a fresh tail with
    `reset: true`: the client drops what it has and starts over."""
    resolved: Path = path.resolve()
    if not any(resolved.is_relative_to(root) for root in TRANSCRIPT_ROOTS):
        return {
            "available": False,
            "reason": "transcript path is outside the harness session folders; not reading it.",
        }
    if not resolved.is_file():
        return {
            "available": False,
            "reason": "transcript file not found (session ended or moved).",
        }
    try:
        f: Any = resolved.open("rb")
    except OSError as e:  # gone or unreadable since the check above
        return {
            "available": False,
            "reason": f"transcript can't be opened: {type(e).__name__}: {e}",
        }
    with f:
        size: int = os.fstat(f.fileno()).st_size
        ctx: FileContext = file_context(resolved, f, size)
        # bus messages from before the session (its first entry, less a grace for a wake-up sent just before the
        # agent wrote anything) belong to earlier sessions and stay on the messages tab
        session: float = (
            ctx.start - SESSION_GRACE_S if ctx.start is not None else float("inf")
        )
        if cursor:
            at: tuple[int, float] | None = check(f, size, cursor, "n")
            if at is not None and at[0] > 0 and ctx.start is not None:
                entries, end = read_lines(f, at[0], size, ctx)
                new: list[dict[str, Any]] = msgs(at[1])
                bus: list[dict[str, Any]] = [
                    m | {"id": f"bus:{m['id']}"} for m in new if m["ts"] >= session
                ]
                return {
                    "available": True,
                    "entries": entries + bus,
                    "next": mint(
                        "n", f, size, end, max([at[1]] + [m["id"] for m in new])
                    ),
                }
            reset: bool = (
                at is None
            )  # else nothing shown yet (no session start): this is still the first read
        else:
            reset = False
        hi: float = float("inf")
        end: int = size
        if before:
            at = check(f, size, before, "p")
            if at is None:
                reset = True
            else:
                end, hi = at
        # the last `limit` entries before `end`: read backwards in growing chunks so a big file isn't parsed whole
        chunk: int = TAIL_CHUNK
        while True:
            start: int = max(0, end - chunk)
            if start:
                f.seek(start - 1)
                if (
                    f.read(1) != b"\n"
                ):  # mid-line: the page and its cursor begin at the next line
                    start += len(f.readline())
            entries, last = read_lines(f, start, end, ctx)
            # past the size cap, keep growing only while no whole line fits (one huge line before `end`)
            if (
                sum(e["kind"] != "result" for e in entries) >= limit
                or start == 0
                or (chunk >= TRANSCRIPT_TAIL_BYTES and entries)
            ):
                break
            chunk *= 4
        if hi == float("inf"):
            end = last  # a tail read stops before a line still being written; polling resumes there
        # keep the last `limit` entries, never splitting one line's entries or a tool call from its result
        keep: int = len(entries)
        n: int = 0
        while keep > 0 and n < limit:
            keep -= 1
            n += entries[keep]["kind"] != "result"
        while (
            keep > 0
            and entries[keep - 1]["id"].split(":")[0]
            == entries[keep]["id"].split(":")[0]
        ):
            keep -= 1
        if keep > 0:
            start = int(entries[keep]["id"].split(":")[0])
            entries: list[dict[str, Any]] = entries[keep:]
        # bus messages in [this page's first entry time, hi): consecutive pages' windows meet exactly, none is missed.
        # The file's first page starts at the session start (an empty file has none yet: no messages until its first
        # entry, then they come as a first read).
        # (a page with no entries passes its upper bound on, so the next older page's window starts where this one would)
        lo: float = (
            session if not start else next((e["ts"] for e in entries if e["ts"]), hi)
        )
        every: list[dict[str, Any]] = msgs(0)
        # before the session's first entry no message is shown, so none counts as seen yet
        last_bus: int = (
            max((m["id"] for m in every), default=0) if ctx.start is not None else 0
        )
        bus = [m | {"id": f"bus:{m['id']}"} for m in every if lo <= m["ts"] < hi]
        out: dict[str, Any] = {
            "available": True,
            "entries": entries + bus,
            "prev": mint("p", f, size, start, lo) if start else None,
        }
        if not before or reset:
            out["next"] = mint("n", f, size, end, last_bus)
        if reset:
            out["reset"] = True
        return out


def summarize_tools(entries: list[dict[str, Any]], said: str = "") -> str:
    """Codex records no description for a tool call (Claude's Bash has one). Use the sentence the agent wrote just
    before the call ("I'll check the bus inbox."), else the command with paths cut to their last part.
    `said`: the agent's text right before these entries (see said_before)."""
    for e in entries:
        if e["kind"] == "assistant":
            said = e["text"]
        elif (
            e["kind"] == "tool"
            and not e.get("desc")
            and not e.get("edits")
            and e.get("command")
        ):
            # the last sentence before the call is usually the intent ("Now I'll run the tests.")
            first: str = (
                re.split(r"(?<=[.!?:])\s|\n", said.strip())[-1] if said.strip() else ""
            )
            # e.g. "python3 /home/x/client.py inbox" -> "python3 client.py inbox"; [^\s'"]* keeps quoted args intact
            short: str = re.sub(
                r"(?<![\w'\">])/[^\s'\"]*/([^\s/'\"]+)", r"\1", e.get("target") or ""
            )
            e["desc"] = re.sub(r"[*_`]", "", first or short)[
                :200
            ]  # plain text: drop markdown emphasis
            said = ""  # one sentence describes one call; later calls in a batch use their command
        elif e["kind"] == "prompt":
            said = ""
    return said  # what a call right after these entries would borrow


def unwrap_exec(text: str) -> str:
    """Codex's `exec` tool wraps output as 'Script completed\nWall time ..\nOutput:\n\n{"output": ..}'; keep the output."""
    m: re.Match[str] | None = re.match(
        r"Script completed\s.*?Output:\s*(\{.*\})\s*$", text, re.DOTALL
    )
    if not m:
        return text
    try:
        j: Any = json.loads(m.group(1))
    except ValueError:
        return text
    if not isinstance(j, dict) or "output" not in j:
        return text
    code: Any = j.get("exit_code")
    return str(j["output"]) + (f"\n[exit {code}]" if code not in (0, None) else "")


def nlines(t: Any) -> int:
    return len(str(t or "").splitlines())


def claude_edits(tool: str, inp: dict[str, Any]) -> list[dict[str, Any]]:
    """File changes a Claude tool call made, as [{path, add, del}] (line counts; Write's del is unknown -> null)."""
    path: str = str(inp.get("file_path") or inp.get("notebook_path") or "")
    match tool:
        case "Edit":
            return [
                {
                    "path": path,
                    "add": nlines(inp.get("new_string")),
                    "del": nlines(inp.get("old_string")),
                }
            ]
        case "MultiEdit":
            eds: list[Any] = [e for e in inp.get("edits") or [] if isinstance(e, dict)]
            return [
                {
                    "path": path,
                    "add": sum(nlines(e.get("new_string")) for e in eds),
                    "del": sum(nlines(e.get("old_string")) for e in eds),
                }
            ]
        case "Write":
            return [{"path": path, "add": nlines(inp.get("content")), "del": None}]
        case "NotebookEdit":
            return [{"path": path, "add": nlines(inp.get("new_source")), "del": None}]
    return []


def patch_edits(raw: str) -> list[dict[str, Any]]:
    """Codex apply_patch text ("*** Update File: p" then +/- lines) -> [{path, add, del}]. `raw` may be a JSON- or
    JS-escaped string inside an exec script, so unescape \\n first."""
    if "*** Begin Patch" not in raw:
        return []
    text: str = (
        raw.replace("\\n", "\n").replace('\\"', '"')
        if "\n" not in raw.split("*** Begin Patch", 1)[1][:200]
        else raw
    )
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for line in text.splitlines():
        # header lines: "*** Update File: path", "*** Add File: path", "*** Delete File: path"
        h: re.Match[str] | None = re.match(
            r"\*\*\* (Update|Add|Delete) File: (.+)", line
        )
        if h:
            cur = {
                "path": h.group(2).strip(),
                "add": 0,
                "del": 0,
                "op": h.group(1).lower(),
            }
            out.append(cur)
        elif cur is not None and line.startswith("+"):
            cur["add"] += 1
        elif cur is not None and line.startswith("-"):
            cur["del"] += 1
    return out


def tag(name: str, text: str) -> str | None:
    """The inside of <name>...</name> in `text`, if it's there."""
    m: re.Match[str] | None = re.search(rf"<{name}>(.*?)</{name}>", text, re.DOTALL)
    return m.group(1).strip() if m else None


def notice(ts: float, text: str, detail: str = "", level: str = "") -> dict[str, Any]:
    return {
        "ts": ts,
        "kind": "notice",
        "text": text[:300],
        "detail": detail[:MAX_TEXT],
        "level": level,
    }


def cut(text: str) -> dict[str, Any]:
    """`text` capped at MAX_TEXT, saying so when it was cut."""
    return (
        {"text": text[:MAX_TEXT], "truncated": True}
        if len(text) > MAX_TEXT
        else {"text": text}
    )


# spans Claude Code wraps around what the person typed (reminders, the IDE's open file / selection); dropped in place
INJECTED: re.Pattern[str] = re.compile(
    r"<(system-reminder|ide_opened_file|ide_selection)>.*?</\1>", re.DOTALL
)


def claude_user_text(ts: float, text: str) -> list[dict[str, Any]]:
    """One text block of a Claude user record -> what the TUI shows for it (rules follow claude-code-log's).
    Harness blocks start with their tag; a prompt that merely contains one (pasted logs) stays a prompt."""
    lead: str = m.group(1) if (m := re.match(r"\s*<([\w-]+)>", text)) else ""
    if (
        lead in ("command-message", "command-name")
        and (name := tag("command-name", text)) is not None
    ):  # a slash command: "/name args"
        return [
            {
                "ts": ts,
                "kind": "prompt",
                "command": True,
                "text": f"{name} {tag('command-args', text) or ''}".strip(),
            }
        ]
    if lead in ("local-command-stdout", "local-command-stderr"):
        out: str = tag(lead, text) or ""
        return [{"ts": ts, "kind": "output", **cut(out)}] if out else []
    if (
        lead == "bash-input" and (cmd := tag("bash-input", text)) is not None
    ):  # "!" shell mode
        return [
            {
                "ts": ts,
                "kind": "tool",
                "tool": "shell",
                "target": cmd[:300],
                "desc": "",
                "command": cmd[:MAX_TEXT],
                "input": "",
                "edits": [],
            }
        ]
    if lead in ("bash-stdout", "bash-stderr"):
        both: str = "\n".join(
            filter(None, [tag("bash-stdout", text), tag("bash-stderr", text)])
        )
        return [{"ts": ts, "kind": "result", "for": "shell", **cut(both)}]
    if lead == "local-command-caveat":
        return []
    if (
        lead == "task-notification"
        and (n := tag("task-notification", text)) is not None
    ):
        return [
            notice(
                ts,
                (tag("summary", n) or re.sub(r"<[^>]+>", " ", n))
                .strip()
                .split("\n")[0],
                n,
            )
        ]
    t: str = INJECTED.sub("", text)
    t = re.sub(
        r"</?pasted_content[^>]*>", "", t
    ).strip()  # the TUI's wrapper around pasted text; keep the text
    if not t:
        return []
    if t.startswith("[Request interrupted"):
        return [notice(ts, "interrupted")]
    return [{"ts": ts, "kind": "prompt", **cut(t)}]


def text_of(content: Any, sep: str = "\n") -> str:
    """A message's text: a string as is, or the text of a list of content blocks."""
    if isinstance(content, list):
        return sep.join(b.get("text", "") for b in content if isinstance(b, dict))
    return content if isinstance(content, str) else ""


def condense(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """One transcript record (Claude Code or Codex) -> zero or more display entries."""
    if "payload" in rec:
        return condense_codex(rec)
    ts: float = parse_ts(rec.get("timestamp"))
    if rec.get("isSidechain"):
        return []  # subagent records; their tool call in the main thread stands for them
    if rec.get("type") == "system":
        match rec.get("subtype"):
            case "compact_boundary":
                meta: Any = rec.get("compactMetadata") or {}
                pre: Any = meta.get("preTokens")
                return [
                    notice(
                        ts,
                        "context compacted"
                        + (
                            f" ({meta.get('trigger')}, {pre // 1000}k tokens before)"
                            if isinstance(pre, int)
                            else ""
                        ),
                    )
                ]
            case "api_error":
                err: Any = rec.get("error")
                return [
                    notice(
                        ts,
                        "API error: "
                        + str(
                            (err.get("message") if isinstance(err, dict) else err)
                            or rec.get("content")
                            or ""
                        ),
                        level="error",
                    )
                ]
            case "local_command":
                return [
                    e
                    for c in [str(rec.get("content") or "")]
                    for e in claude_user_text(ts, c)
                ]
        return []
    queued: Any = rec.get("attachment")
    if (
        rec.get("type") == "attachment"
        and isinstance(queued, dict)
        and queued.get("type") == "queued_command"
    ):
        # a prompt typed (or a task notification arriving) while Claude is busy is recorded only here
        return claude_user_text(ts, text_of(queued.get("prompt")))
    msg: Any = rec.get("message")
    if rec.get("type") not in ("user", "assistant") or not isinstance(msg, dict):
        return []
    if rec.get("isCompactSummary"):
        return [
            notice(
                ts,
                "summary of the conversation so far (after compaction)",
                str(msg.get("content") or ""),
            )
        ]
    content: Any = msg.get("content")
    if rec.get(
        "isMeta"
    ):  # a skill's body or other injected instructions: one line, the text on expand
        body: str = text_of(content)
        skill: re.Match[str] | None = re.match(
            r"Base directory for this skill: \S*?([^/\s]+)/?\s", body
        )
        first: str = next(
            (ln.strip("# ").strip() for ln in body.splitlines() if ln.strip()), ""
        )
        if not body.strip() or body.lstrip().startswith("<local-command-caveat>"):
            return []  # the caveat Claude Code adds before a local command's output says nothing to a reader
        return [notice(ts, f"skill: {skill.group(1)}" if skill else first[:120], body)]
    if rec["type"] == "assistant" and rec.get("isApiErrorMessage"):
        return [notice(ts, "API error: " + text_of(content, " "), level="error")]
    blocks: list[Any] = (
        [{"type": "text", "text": content}]
        if isinstance(content, str)
        else content
        if isinstance(content, list)
        else []
    )
    out: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        match block.get("type"):
            case "text" if str(block.get("text") or "").strip():
                if rec["type"] == "user":
                    out.extend(claude_user_text(ts, str(block["text"])))
                else:
                    out.append(
                        {"ts": ts, "kind": "assistant", **cut(str(block["text"]))}
                    )
            case "thinking" if str(block.get("thinking") or "").strip():
                out.append({"ts": ts, "kind": "thinking", **cut(block["thinking"])})
            case "tool_use":
                inp: Any = (
                    block.get("input") if isinstance(block.get("input"), dict) else {}
                )
                target: str = str(
                    inp.get("file_path")
                    or inp.get("command")
                    or inp.get("pattern")
                    or inp.get("description")
                    or ""
                )
                out.append(
                    {
                        "ts": ts,
                        "kind": "tool",
                        "call": block.get("id"),
                        "tool": block.get("name", "?"),
                        "target": target[:300],
                        "desc": str(inp.get("description") or "")[:1000],
                        "command": str(inp.get("command") or "")[:MAX_TEXT],
                        "input": json.dumps(inp, indent=1)[:MAX_TEXT],
                        "edits": claude_edits(block.get("name", ""), inp),
                    }
                )
            case "tool_result":
                out.append(
                    {
                        "ts": ts,
                        "kind": "result",
                        "for": block.get("tool_use_id"),
                        "error": bool(block.get("is_error")),
                        **cut(text_of(block.get("content"))),
                    }
                )
    return out


def codex_prompt(t: str) -> str:
    """What the person typed in a Codex user turn: injected context blocks (<environment_context>, skills) are
    dropped, and the IDE extension's "# Context from my IDE setup" keeps only what follows "## My request..."."""
    if t.lstrip().startswith("# Context from my IDE"):
        parts: list[str] = re.split(
            r"^## My request[^\n]*\n", t, maxsplit=1, flags=re.MULTILINE
        )
        return parts[1] if len(parts) == 2 else ""
    if t.startswith("# AGENTS.md instructions for ") and t.rstrip().endswith(
        "</INSTRUCTIONS>"
    ):
        return ""  # the project's AGENTS.md, which Codex sends as a user message
    # Only known harness blocks are context: arbitrary XML/HTML may be the person's prompt. Some carry attributes
    # (<hook_prompt hook_run_id="...">: a hook's text, such as arke's mail).
    tag: re.Match[str] | None = re.match(
        r"\s*<(environment_context|user_instructions|skills_instructions|skill|codex_internal_context|hook_prompt)[\s>]",
        t,
    )
    return (
        "" if tag and t.rstrip().endswith(f"</{tag[1]}>") else t
    )  # no regex over the body: long prompts must not cause quadratic backtracking


def codex_user_text(ts: float, text: str) -> list[dict[str, Any]]:
    """Keep user shell commands and their results together, using the console's existing shell pairing."""
    wrapped: str = text.strip()
    if wrapped.startswith("<user_shell_command>") and wrapped.endswith(
        "</user_shell_command>"
    ):
        body: str = (
            wrapped.removeprefix("<user_shell_command>")
            .removesuffix("</user_shell_command>")
            .strip()
        )
        # Codex surrounds the raw command and result with newlines; neither body is XML-escaped.
        command, end, result = body.removeprefix("<command>\n").partition(
            "\n</command>"
        )
        result = result.strip()
        if (
            body.startswith("<command>\n")
            and end
            and (
                not result
                or (result.startswith("<result>\n") and result.endswith("</result>"))
            )
        ):
            entries: list[dict[str, Any]] = [
                {
                    "ts": ts,
                    "kind": "tool",
                    "tool": "shell",
                    "target": command[:300],
                    "desc": command.split("\n", 1)[0][:300],
                    "command": command[:MAX_TEXT],
                    "input": "",
                    "edits": [],
                }
            ]
            if result:
                output: str = (
                    result.removeprefix("<result>\n")
                    .removesuffix("</result>")
                    .removesuffix("\n")
                )
                entries.append(
                    {"ts": ts, "kind": "result", "for": "shell", **cut(output)}
                )
            return entries
    prompt: str = codex_prompt(text)
    return [{"ts": ts, "kind": "prompt", **cut(prompt)}] if prompt.strip() else []


def condense_codex(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """Codex rollout line {timestamp, type, payload}. Messages and tool calls are `response_item`s in every history
    mode; interruptions, rollbacks, compaction and readable reasoning come from `event_msg`s (legacy events or the
    newer item_completed), the way `codex resume` rebuilds its screen."""
    p: Any = rec.get("payload")
    if not isinstance(p, dict):
        return []
    ts: float = parse_ts(rec.get("timestamp"))
    if rec.get("type") == "event_msg":
        item: Any = p.get("item") if p.get("type") == "item_completed" else None
        it: str = item.get("type", "") if isinstance(item, dict) else ""
        match p.get("type"):
            case "turn_aborted":
                return [
                    notice(
                        ts,
                        "interrupted"
                        if p.get("reason") == "interrupted"
                        else f"turn aborted ({p.get('reason')})",
                    )
                ]
            case "thread_rolled_back":
                return [
                    {
                        "ts": ts,
                        "kind": "rollback",
                        "turns": int(p.get("num_turns") or 0),
                    }
                ]
            case "context_compacted":
                return [notice(ts, "context compacted")]
            case "agent_reasoning" if str(p.get("text") or "").strip():
                return [{"ts": ts, "kind": "thinking", **cut(str(p["text"]))}]
            case "item_completed" if it == "ContextCompaction":
                return [notice(ts, "context compacted")]
            case "item_completed" if (
                it == "Reasoning"
                and "".join(map(str, item.get("summary_text") or [])).strip()
            ):
                return [
                    {
                        "ts": ts,
                        "kind": "thinking",
                        **cut("\n\n".join(map(str, item["summary_text"]))),
                    }
                ]
        return []
    if rec.get("type") != "response_item":
        return []
    match p.get("type"):
        case "message" if p.get("role") in ("user", "assistant"):
            texts: list[str] = [
                c.get("text", "") for c in p.get("content") or [] if isinstance(c, dict)
            ]
            if p["role"] == "user":
                return [entry for t in texts for entry in codex_user_text(ts, t)]
            return [
                {
                    "ts": ts,
                    "kind": "assistant",
                    **cut(t),
                }
                for t in texts
                if t.strip()
            ]
        case "agent_message":  # between Codex's own sub-agents; the body is encrypted, the header is readable
            head: str = " ".join(
                c.get("text", "")
                for c in p.get("content") or []
                if isinstance(c, dict) and c.get("type") != "encrypted_content"
            )
            return [
                notice(
                    ts,
                    f"agent message {p.get('author', '?')} → {p.get('recipient', '?')}",
                    head.strip(),
                )
            ]
        case "function_call" | "custom_tool_call" | "local_shell_call":
            given: Any = p.get("arguments") or p.get("input") or p.get("action") or ""
            raw: str = (
                given if isinstance(given, str) else json.dumps(given)
            )  # local_shell_call: an action object
            try:
                args: Any = json.loads(raw)
            except ValueError:
                args = raw
            target: Any = (
                (
                    args.get("cmd")
                    or args.get("command")
                    or args.get("path")
                    or args.get("target")
                    or args.get("message")
                    or args.get("name")
                    or ""
                )
                if isinstance(args, dict)
                else args
            )
            # Codex's `exec` tool runs a script like `await tools.exec_command({"cmd": "..."})`: show the command
            cmd: re.Match[str] | None = (
                re.search(r'"?\bcmd"?\s*:\s*("(?:[^"\\]|\\.)*")', raw)
                if isinstance(target, str)
                else None
            )
            if cmd:
                target = json.loads(cmd.group(1))
            shown: str = (
                " ".join(map(str, target)) if isinstance(target, list) else str(target)
            )
            lines: list[str] = shown.strip().splitlines()
            command: str = (
                shown
                if isinstance(target, list)
                or cmd
                or (isinstance(args, dict) and (args.get("cmd") or args.get("command")))
                else ""
            )
            return [
                {
                    "ts": ts,
                    "kind": "tool",
                    "call": p.get("call_id"),
                    "tool": p.get("name") or p["type"],
                    "target": (lines[0] if lines else "")[:300],
                    "desc": "",
                    "command": command[:MAX_TEXT],
                    "input": (
                        json.dumps(args, indent=1) if isinstance(args, dict) else raw
                    )[:MAX_TEXT],
                    "edits": patch_edits(raw),
                }
            ]
        case (
            "function_call_output"
            | "custom_tool_call_output"
            | "local_shell_call_output"
        ):
            out: Any = p.get("output")
            if isinstance(out, str):
                try:
                    out = json.loads(out)
                except ValueError:
                    pass  # plain-text output (most tools); used as is
            text: str = unwrap_exec(
                text_of(out)
                if isinstance(out, list)
                else str(out.get("output", out) if isinstance(out, dict) else out)
            )
            return [
                {
                    "ts": ts,
                    "kind": "result",
                    "for": p.get("call_id"),
                    **cut(text),
                    **(
                        {"truncated": True}
                        if "truncated for persistence" in text
                        else {}
                    ),
                }
            ]
    return []


def parse_ts(s: Any) -> float:
    """Transcript times are UTC ISO; 0.0 if missing or odd (the reader then uses the previous record's time)."""
    try:
        return datetime.fromisoformat(str(s)).timestamp()
    except ValueError:
        return 0.0

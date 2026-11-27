"""Daily checkpoints of the event log, so the analytics pages can start reading at a recent UTC midnight
instead of byte 0, while the log itself stays whole.

A checkpoint for time T is the log before the last UTC midnight before T, compacted: the events the pages still
need from then (identity, every state and message, a few per-session samples), verbatim and in log order. The
page runs its usual model code over checkpoint + everything after it. Rules never key on agent names (names
are reused, renamed and merged; resolving them is the page's job): they keep everything of a type, or the
first / last event per session id.

Each UTC day's own compaction is saved once, in STATE_DIR/checkpoints/<day>.json, covering the log bytes
[cut(day), cut(next day)) where a cut is the first complete line stamped at or after that midnight. A file is
trusted only while it was built by this code and still describes the log: same file (generation), long enough, starting where the
previous day ended, and the bytes before its end unchanged. Anything else is rebuilt from the log.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

if __package__:
    from .client import load_cache, save_cache
else:
    from client import load_cache, save_cache
if __package__:
    from .transcripts import tail_fp, whole_lines
else:
    from transcripts import tail_fp, whole_lines

DAY: float = 86400.0
# Every event of these types is kept: identity (names, sessions, projects) plus the small types whose whole
# history the pages read (state at the window start and how long it has lasted, who owes a reply).
KEEP_ALL: frozenset[str] = frozenset(
    {
        "join",
        "rename",
        "takeover",
        "forget",
        "project",
        "start",
        "state",
        "update",
        "message",
    }
)
# Day files record which code built them: a change to what is kept (or anything else in this file) rebuilds them.
RULES: str = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]
# Building writes files; one at a time (two tabs opening at once would otherwise build the same days twice).
LOCK: threading.Lock = threading.Lock()


def parse_event(line: bytes, start: int) -> dict[str, Any] | None:
    """One log line as an event, or None (with a warning) if it is damaged: shared with read_events."""
    try:
        e: Any = json.loads(line)
        if not isinstance(e, dict):
            raise TypeError("event must be an object")
        if not isinstance(e.get("event"), str):
            raise TypeError("event name must be a string")
    except (ValueError, TypeError) as err:
        print(f"arke: skipping damaged event at byte {start}: {err}", file=sys.stderr)
        return None
    return e


def stamp(e: dict[str, Any]) -> float | None:
    """The event's time, if it has a usable one."""
    ts: Any = e.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, int | float) or not math.isfinite(ts):
        return None
    return float(ts)


def number(v: Any) -> float:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else 0.0


def later(e: dict[str, Any], old: dict[str, Any] | None) -> bool:
    """Whether `e` is at least as new as `old` by time, then log position (buildModel sorts usage by ts)."""
    return old is None or (stamp(e) or 0.0, e["o"]) >= (stamp(old) or 0.0, old["o"])


def rules(e: dict[str, Any]) -> list[tuple[str, str]]:
    """(mode, key) for each reason to keep `e`: mode "all" (always), "first" (the earliest per key), "last"
    (the latest by log position) or "newest" (the latest by time, then position: buildModel sorts usage by ts)."""
    ev: str = e["event"]
    if ev in KEEP_ALL:
        return [("all", "")]
    out: list[tuple[str, str]] = [
        ("first", f"type|{ev}")
    ]  # the page's t0 is the earliest loaded event
    sid: Any = e.get("sid")
    if not isinstance(sid, str) or not sid:
        # Sessions always carry a sid in logs this server writes; older logs' usage/tool events have none and
        # can't be keyed safely (names get reused), so they are all kept. That history doesn't grow.
        return [("all", "")] if ev in ("usage", "tool") else out
    # a session is bound to a name at its first sighting of each type the page asks for
    out.append(("first", f"seen|{sid}|{e.get('agent')}|{ev}"))
    if ev == "usage":
        source: str = str(e.get("source", ""))
        out.append(("newest", f"usage|{sid}|{source}"))  # the tokens-out baseline
        context: float = number(e.get("context"))
        # the context gauge's starting value
        if context > 0:
            out.append(("newest", f"context|{sid}|{source}"))
        # the window guess: 1M once a session went past 200k
        if context > 200000:
            out.append(("first", f"big|{sid}"))
    elif ev == "tool":
        # the lane's last tool call and "silent" in the now table (the log's first one is kept as "type|tool")
        out.append(("last", f"tool|{sid}"))
        projects: Any = e.get("projects")
        for p in projects if isinstance(projects, list) else []:
            out.append(("first", f"project|{sid}|{p}"))  # the project filter
    return out


def first_day(f: BinaryIO, size: int) -> float | None:
    """The UTC midnight starting the log's first day: that of its first line with a usable time."""
    for offset, line in whole_lines(f, 0, size):
        e: dict[str, Any] | None = parse_event(line, offset)
        ts: float | None = stamp(e) if e else None
        if ts is not None:
            return ts // DAY * DAY
    return None


def build_day(
    f: BinaryIO, start: int, size: int, gen: str, day: float
) -> dict[str, Any] | None:
    """The compaction of UTC day `day` (its midnight), read from byte `start` (its cut) to the next midnight's
    cut; None if no line has been logged after that midnight yet (the day is still open). Events get "o", their
    byte offset."""
    rec: dict[str, Any] = {
        "rules": RULES,
        "gen": gen,
        "start": start,
        "all": [],
        "first": {},
        "last": {},
        "newest": {},
        "hours": {},
    }
    for offset, line in whole_lines(f, start, size):
        e: dict[str, Any] | None = parse_event(line, offset)
        if e is None:
            continue
        ts: float | None = stamp(e)
        if (
            ts is not None and ts >= day + DAY
        ):  # the next day's cut: this line belongs to it
            rec |= {"day": day, "end": offset, "next": ts // DAY * DAY}
            rec["fp"] = tail_fp(f, offset)  # iteration is over, so seeking is fine
            return rec
        e["o"] = offset
        for mode, key in rules(e):
            if mode == "all":
                rec["all"].append(e)
            elif mode == "first":
                rec["first"].setdefault(key, e)
            elif mode == "last":
                rec["last"][key] = e
            elif later(e, rec["newest"].get(key)):
                rec["newest"][key] = e
        if e["event"] == "tool" and ts is not None:
            who: str = (
                f"s:{e['sid']}"
                if isinstance(e.get("sid"), str) and e["sid"]
                else f"a:{e.get('agent')}"
            )
            cell: str = f"{int(ts // 3600)}|{who}"
            rec["hours"][cell] = rec["hours"].get(cell, 0) + 1
    return None


def before(
    f: BinaryIO, size: int, gen: str, t: float, folder: Path
) -> dict[str, Any] | None:
    """The checkpoint for time `t` from the open log `f` (its size and generation from one fstat): events in log
    order, the cut to continue reading from, the UTC midnight it stands for, hourly tool counts. None if the log
    has no closed day before t."""
    until: float = t // DAY * DAY
    days: list[dict[str, Any]] = []
    with LOCK:
        start: int = 0
        day: float | None = first_day(f, size)
        while day is not None and day + DAY <= until:
            path: Path = folder / f"{datetime.fromtimestamp(day, UTC):%Y-%m-%d}.json"
            rec: dict[str, Any] | None = load_cache(path)
            if not (
                rec
                and rec.get("rules") == RULES
                and rec.get("gen") == gen
                and rec.get("start") == start
                and isinstance(rec.get("end"), int)
                and rec["end"] <= size
                and rec.get("fp") == tail_fp(f, rec["end"])
            ):
                rec = build_day(f, start, size, gen, day)
                if rec is None:
                    break  # that day is still open: no line after its midnight yet
                save_cache(path, rec)
            if rec["next"] > until:
                break
            days.append(rec)
            start, day = rec["end"], rec["next"]
    if not days:
        return None
    kept: dict[int, dict[str, Any]] = {}
    first: dict[str, dict[str, Any]] = {}
    last: dict[str, dict[str, Any]] = {}
    newest: dict[str, dict[str, Any]] = {}
    hours: dict[str, int] = {}
    for rec in days:
        for e in rec["all"]:
            kept[e["o"]] = e
        for key, e in rec["first"].items():
            first.setdefault(key, e)
        last |= rec["last"]
        for key, e in rec["newest"].items():
            if later(e, newest.get(key)):
                newest[key] = e
        for cell, n in rec["hours"].items():
            hours[cell] = hours.get(cell, 0) + n
    for e in [*first.values(), *last.values(), *newest.values()]:
        kept[e["o"]] = e
    events: list[dict[str, Any]] = [
        {k: v for k, v in e.items() if k != "o"} for _, e in sorted(kept.items())
    ]
    return {
        "events": events,
        "cut": days[-1]["end"],
        "day": days[-1]["next"],
        "hours": hours,
    }

"""Daily checkpoints: /events?from=T starts with the compacted log before T's UTC midnight, then continues at
that midnight's cut; per-day files are written once and rebuilt whenever they no longer describe the log."""

from __future__ import annotations

import json
import os
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import checkpoints
import pytest
import server

from tests.test_transfer import get, port  # noqa: F401  (port is a fixture)

DAY: float = checkpoints.DAY
D0: float = 1_795_000_000 // DAY * DAY  # a UTC midnight


@pytest.fixture
def folder(bus: server.Bus, tmp_path: Path) -> Generator[Path]:
    with patch.object(server, "STATE_DIR", tmp_path / "state"):
        yield tmp_path / "state" / "checkpoints"


def write(rows: list[dict[str, Any]], mode: str = "a") -> None:
    """Log lines the way log() writes them: ts and event first."""
    with server.LOG_FILE.open(mode) as f:
        for r in rows:
            f.write(json.dumps({"ts": r.pop("ts"), "event": r.pop("event"), **r}) + "\n")


def three_days() -> list[dict[str, Any]]:
    """Two closed days and an open third, with the cases the rules exist for."""
    rows: list[dict[str, Any]] = []
    for d in range(3):
        t: float = D0 + d * DAY
        rows += [
            {"ts": t + 10, "event": "join", "agent": f"a{d}", "sid": f"s{d}", "projects": ["/p"]},
            {"ts": t + 20, "event": "state", "agent": f"a{d}", "state": "busy"},
            {"ts": t + 30, "event": "tool", "agent": f"a{d}", "sid": f"s{d}", "tool": "Read", "projects": ["/p", "/q"]},
            {"ts": t + 40, "event": "tool", "agent": f"a{d}", "sid": f"s{d}", "tool": "Edit", "projects": ["/p"]},
            {"ts": t + 50, "event": "usage", "agent": f"a{d}", "sid": f"s{d}", "source": "hook", "tokens": 10, "context": 300000},
            {"ts": t + 60, "event": "usage", "agent": f"a{d}", "sid": f"s{d}", "source": "hook", "tokens": 20, "context": None},
            {"ts": t + 70, "event": "prompt", "agent": f"a{d}", "sid": f"s{d}"},
            {"ts": t + 80, "event": "procs", "agents": {f"a{d}": {"cpu": 1, "rss_mb": 2, "top": ["x"]}}},
            {"ts": t + 90, "event": "message", "from": f"a{d}", "to": [{"name": "human", "kind": "human"}], "reach": ["human"], "text": "hi"},
            {"ts": t + 100, "event": "wake", "agent": f"a{d}", "sender": "human"},
            {"ts": t + 80000, "event": "state", "agent": f"a{d}", "state": "idle"},
        ]
    rows.insert(-3, {"ts": D0 + 2 * DAY - 2, "event": "state", "agent": "late", "state": "busy"})  # stamped before midnight, written after it
    return rows


def reference(t: float) -> list[dict[str, Any]]:
    """What a checkpoint for t must hold: the rules applied once to every line before the cut, in log order."""
    with server.LOG_FILE.open("rb") as f:
        size: int = os.fstat(f.fileno()).st_size
        lines: list[tuple[int, bytes]] = list(checkpoints.whole_lines(f, 0, size))
    until: float = t // DAY * DAY
    cut: int = next((o for o, line in lines if json.loads(line)["ts"] >= until), size)
    kept: dict[int, dict[str, Any]] = {}
    first: dict[str, int] = {}
    last: dict[str, int] = {}
    newest: dict[str, tuple[float, int]] = {}
    for o, line in lines:
        if o >= cut:
            break
        e: dict[str, Any] = json.loads(line)
        for mode, key in checkpoints.rules(e):
            if mode == "all":
                kept[o] = e
            elif mode == "first":
                first.setdefault(key, o)
            elif mode == "last":
                last[key] = o
            else:
                newest[key] = max(newest.get(key, (-1.0, -1)), (e["ts"], o))
    events: dict[int, bytes] = dict(lines)
    for o in [*first.values(), *last.values(), *(o for _, o in newest.values())]:
        kept[o] = json.loads(events[o])
    return [kept[o] for o in sorted(kept)]


def test_from_returns_the_checkpoint_then_everything_after_its_cut(folder: Path) -> None:
    write(three_days())
    t: float = D0 + 2 * DAY + 500
    r: dict[str, Any] = server.read_events(0, set(), 50000, from_ts=t)
    whole: list[dict[str, Any]] = server.read_events(0, set(), 50000)["events"]
    cp: list[dict[str, Any]] = reference(t)
    assert r["from"]["day"] == D0 + 2 * DAY
    assert r["events"][: len(cp)] == cp
    # after the checkpoint: every line from the cut on, including the late-stamped one
    after: list[dict[str, Any]] = r["events"][len(cp) :]
    assert after == whole[len(whole) - len(after) :]
    assert after[0] == {"ts": D0 + 2 * DAY + 10, "event": "join", "agent": "a2", "sid": "s2", "projects": ["/p"]}
    assert {"ts": D0 + 2 * DAY - 2, "event": "state", "agent": "late", "state": "busy"} in after
    assert r["next"] == server.LOG_FILE.stat().st_size
    # dropped before the cut: prompts, wakes, procs after the first, the middle tool and usage samples
    before: list[dict[str, Any]] = r["events"][: len(cp)]
    assert [e["event"] for e in before].count("wake") == 1  # only the first of its type
    assert [e["tool"] for e in before if e["event"] == "tool"] == ["Read", "Edit", "Read", "Edit"]
    assert sorted(r["from"]["hours"].values()) == [2, 2]


def test_compacting_day_by_day_equals_compacting_the_whole_prefix(folder: Path) -> None:
    write(three_days())
    for t in (D0 + DAY + 1, D0 + 2 * DAY + 1, D0 + 9 * DAY):
        r: dict[str, Any] = server.read_events(0, set(), 50000, from_ts=t)
        cp: list[dict[str, Any]] = reference(min(t, D0 + 2 * DAY))  # the third day is still open
        assert r["events"][: len(cp)] == cp, t


def test_type_filter_and_procs_totals_apply_to_the_checkpoint(folder: Path) -> None:
    write(three_days())
    r: dict[str, Any] = server.read_events(0, {"procs", "state"}, 50000, procs_totals=True, from_ts=D0 + 2 * DAY + 1)
    assert {e["event"] for e in r["events"]} == {"procs", "state"}
    assert all("top" not in v for e in r["events"] if e["event"] == "procs" for v in e["agents"].values())


def test_no_from_or_nothing_closed_reads_from_byte_zero(folder: Path) -> None:
    write(three_days())
    plain: dict[str, Any] = server.read_events(0, set(), 50000)
    assert "from" not in plain
    early: dict[str, Any] = server.read_events(0, set(), 50000, from_ts=D0 + 5)
    assert "from" not in early and early["events"] == plain["events"]


def test_day_files_are_written_once_and_reused(folder: Path) -> None:
    write(three_days())
    t: float = D0 + 2 * DAY + 1
    first: dict[str, Any] = server.read_events(0, set(), 50000, from_ts=t)
    assert sorted(p.name for p in folder.iterdir()) == [
        f"{checkpoints.datetime.fromtimestamp(D0 + d * DAY, checkpoints.UTC):%Y-%m-%d}.json" for d in (0, 1)
    ]
    with patch.object(checkpoints, "build_day", side_effect=AssertionError("rebuilt")):
        assert server.read_events(0, set(), 50000, from_ts=t) == first
        write([{"ts": D0 + 2 * DAY + 900, "event": "state", "agent": "a2", "state": "busy"}])
        grown: dict[str, Any] = server.read_events(0, set(), 50000, from_ts=t)  # growth keeps the files valid
    assert grown["events"][-1]["ts"] == D0 + 2 * DAY + 900


@pytest.mark.parametrize("change", ["truncate", "rewrite in place", "replace the file"])
def test_day_files_are_rebuilt_when_the_log_changed_under_them(folder: Path, change: str) -> None:
    write(three_days())
    t: float = D0 + 2 * DAY + 1
    server.read_events(0, set(), 50000, from_ts=t)
    rows: list[dict[str, Any]] = three_days()
    rows[0]["agent"] = "renamed"  # same length class of change: the first day's bytes differ
    if change == "truncate":
        server.LOG_FILE.write_text("")
        write(rows)
    elif change == "rewrite in place":
        with server.LOG_FILE.open("r+") as f:  # same inode, same size, different first day
            f.write("".join(json.dumps({"ts": r.pop("ts"), "event": r.pop("event"), **r}) + "\n" for r in rows))
    else:
        server.LOG_FILE.unlink()
        write(rows)
    assert server.read_events(0, set(), 50000, from_ts=t)["events"][: len(reference(t))] == reference(t)


def test_a_quiet_night_uses_the_newest_closed_day(folder: Path) -> None:
    write([r for r in three_days() if r["ts"] < D0 + 2 * DAY - 10])  # nothing logged after the second midnight
    r: dict[str, Any] = server.read_events(0, set(), 50000, from_ts=D0 + 2 * DAY + 600)
    assert r["from"]["day"] == D0 + DAY  # day 2 is still open: the checkpoint stops at day 1
    assert r["events"][-1]["ts"] == D0 + DAY + 80000


def test_from_must_be_a_finite_time(port: int) -> None:  # noqa: F811
    for bad in ("nan", "inf", "-inf"):
        body: bytes = get(port, f"/events?from={bad}")[1]
        assert json.loads(body)["error"] == "from must be a finite time"

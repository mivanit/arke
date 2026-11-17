"""Processes on this machine, read from /proc: one process's stat line, the whole table, a command line.

Used to tell which harness owns a tmux pane (wake.py), to check an agent's pid, and to sample agents' process trees.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def proc_table() -> dict[int, dict[str, Any]]:
    """Every process on this machine (see process_info); one that exits while we look is left out."""
    return {
        int(d.name): info
        for d in Path("/proc").iterdir()
        if d.name.isdigit() and (info := process_info(int(d.name)))
    }


def cmdline(pid: int) -> str:
    try:
        return (
            Path(f"/proc/{pid}/cmdline")
            .read_bytes()
            .replace(b"\0", b" ")
            .decode("utf-8", "replace")
            .strip()
            or Path(f"/proc/{pid}/comm").read_text().strip()
        )[:300]
    except (
        OSError
    ):  # gone, or not ours to read: the sample just goes without its command line
        return ""


def process_info(pid: Any, *, strict: bool = False) -> dict[str, Any] | None:
    """/proc/PID/stat: comm, ppid, pgrp, tty, foreground pgrp, cpu ticks (utime+stime), start (ticks since boot), rss
    (pages). Strict callers distinguish disappearance (None) from unavailable evidence (raised)."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return None
    try:
        # TODO: read as bytes and decode with errors="replace" (here and comm in cmdline): a process name that
        # isn't valid UTF-8 raises, which drops it and its children from proc_table and the samples
        raw: str = Path(f"/proc/{pid}/stat").read_text()
        comm, fields = raw.split("(", 1)[1].rsplit(")", 1)
        row: list[str] = fields.split()  # row[0] is field 3 (state)
        return {
            "comm": comm,
            "ppid": int(row[1]),
            "pgrp": int(row[2]),
            "tty": int(row[4]),
            "foreground": int(row[5]),
            "ticks": int(row[11]) + int(row[12]),
            "start": int(row[19]),
            "rss": int(row[21]),
        }
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (OSError, ValueError, IndexError):
        if strict:
            raise
        return None

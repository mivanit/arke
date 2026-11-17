"""Incremental, synchronous SQLite commits and online snapshots for arke.

Only the bus writes this database, and only under its lock (commits and checkpoints);
backup connections are read-only. Never copy a live WAL database with shutil.copy.
"""

from __future__ import annotations

import json
import os
import sqlite3
import weakref
from contextlib import closing
from pathlib import Path
from typing import Any

# the bus's two records, which this database stores; here (the lowest module) so server.py and wake.py can both name them
# {sid, name, kind, host, cwd, branch, tmux, tmux_session, transcript, model, effort, about, state, started_at,
#  tokens, context, window (context window size; context/window = % filled), window_est (window guessed),
#  projects: {path: subscribed}, default_project, inbox: [[message id, mode, direct]], named (set via rename), aka (old names),
#  process: pid, pid_start, process_token, boot, tmux_server, daemon (a Codex daemon thread); wake (the wake-up record),
#  turn_offset (transcript read position), times: last_active, state_at, last_prompt, last_sent, last_recv, last_human,
#  volatile (not saved): files (recent tool calls), note (naming note for intro)}
type Agent = dict[str, Any]
# {id, ts, from, to: [{name, kind, was?}], reach: [names], mode, text, via (console / shell / agent), host, cwd, branch,
#  projects, request_id / request_sender / request_hash (retry dedup), helper / requested_to / requested_mode (a
#  sub-agent's request forwarded to its parent)}
type Message = dict[str, Any]


def sync_dir(path: Path) -> None:
    fd: int = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_file(path: Path) -> None:
    with path.open("rb") as f:
        os.fsync(f.fileno())


class Store:
    def __init__(self, path: Path) -> None:
        self.path: Path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db: sqlite3.Connection = sqlite3.connect(
            path, timeout=30, check_same_thread=False
        )
        self.finalizer: weakref.finalize = weakref.finalize(self, self.db.close)
        self.db.execute("PRAGMA auto_vacuum=INCREMENTAL")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA journal_size_limit=4194304")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS agents (sid TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, data TEXT NOT NULL);
        """)
        self.agent_rows: dict[str, str] = {}
        self.meta_rows: dict[str, str] = {}
        self.message_rows: dict[int, dict[str, Any]] = {}
        sync_dir(path.parent)

    def close(self) -> None:
        self.finalizer()

    def load(self) -> dict[str, Any] | None:
        self.meta_rows = dict(self.db.execute("SELECT key, value FROM meta"))
        if "next_id" not in self.meta_rows:
            # Never initialized (missing file, or a crash before the first commit): empty tables.
            blank: bool = not self.db.execute(
                "SELECT 1 FROM agents UNION ALL SELECT 1 FROM messages UNION ALL SELECT 1 FROM meta"
            ).fetchone()
            if blank and not self.path.with_suffix(".json").exists():
                return None
            raise ValueError(
                f"{self.path} has no committed initialization metadata; complete the original "
                "state.json import with the old server or restore a backup before starting arke"
            )
        self.agent_rows = dict(self.db.execute("SELECT sid, data FROM agents"))
        self.message_rows = {
            mid: json.loads(raw)
            for mid, raw in self.db.execute("SELECT id, data FROM messages ORDER BY id")
        }
        return {k: json.loads(v) for k, v in self.meta_rows.items()} | {
            "agents": {sid: json.loads(raw) for sid, raw in self.agent_rows.items()},
            "messages": list(self.message_rows.values()),
        }

    def commit(self, state: dict[str, Any]) -> None:
        """Write changed rows; omitted messages leave committed history untouched.

        Message dictionaries are immutable after publication. Agent-only changes can omit
        them to avoid scanning all retained history just to save a heartbeat or receipt.
        """
        agents: dict[str, str] = {
            sid: json.dumps(a) for sid, a in state["agents"].items()
        }
        meta: dict[str, str] = {
            k: json.dumps(v)
            for k, v in state.items()
            if k not in ("agents", "messages")
        }
        messages: dict[int, dict[str, Any]] = self.message_rows
        added: list[tuple[Any, str]] = []
        removed_messages: list[tuple[int]] = []
        if "messages" in state:
            messages = {m["id"]: m for m in state["messages"]}
            added = [
                (mid, json.dumps(m))
                for mid, m in messages.items()
                if self.message_rows.get(mid) is not m
            ]
            removed_messages = [
                (mid,) for mid in self.message_rows.keys() - messages.keys()
            ]
        changed_agents: list[tuple[Any, str]] = [
            (sid, data)
            for sid, data in agents.items()
            if self.agent_rows.get(sid) != data
        ]
        changed_meta: list[tuple[str, str]] = [
            (k, v) for k, v in meta.items() if self.meta_rows.get(k) != v
        ]
        removed_agents: list[tuple[str]] = [
            (sid,) for sid in self.agent_rows.keys() - agents.keys()
        ]
        if not any(
            (changed_agents, changed_meta, added, removed_agents, removed_messages)
        ):
            return
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.executemany(
                "INSERT OR REPLACE INTO agents VALUES (?, ?)", changed_agents
            )
            self.db.executemany(
                "INSERT OR REPLACE INTO meta VALUES (?, ?)", changed_meta
            )
            self.db.executemany("INSERT OR REPLACE INTO messages VALUES (?, ?)", added)
            self.db.executemany("DELETE FROM agents WHERE sid=?", removed_agents)
            self.db.executemany("DELETE FROM messages WHERE id=?", removed_messages)
            self.db.commit()  # synchronous=FULL: success waits for the WAL to reach disk
        except BaseException:
            self.db.rollback()
            raise
        self.agent_rows, self.meta_rows, self.message_rows = agents, meta, messages

    def backup(self, target: Path) -> None:
        """A standalone consistent database, including committed WAL data; writes may continue."""
        with target.open("xb"):
            pass
        with (
            closing(
                sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
            ) as source,
            closing(sqlite3.connect(target)) as dest,
        ):
            # Pin one WAL snapshot so continuous hooks cannot restart the copy indefinitely.
            source.execute("BEGIN")
            source.execute("SELECT value FROM meta WHERE key='next_id'").fetchone()
            source.backup(dest, pages=128, sleep=0.01)
            source.rollback()
            dest.execute("PRAGMA journal_mode=DELETE")
            if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise OSError("backup failed SQLite integrity_check")
        sync_file(target)
        sync_dir(target.parent)

    def compact(self, pages: int = 128) -> int:
        """Bounded work between bus mutations; no whole-database VACUUM pause."""
        before: int = int(self.db.execute("PRAGMA freelist_count").fetchone()[0])
        self.db.execute(f"PRAGMA incremental_vacuum({int(pages)})").fetchall()
        self.db.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchall()
        after: int = int(self.db.execute("PRAGMA freelist_count").fetchone()[0])
        return before - after

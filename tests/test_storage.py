"""Durability, online maintenance, and visibility during slow/failed commits."""

from __future__ import annotations

import gzip
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Generator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack, closing
from pathlib import Path
from typing import Any, BinaryIO
from unittest.mock import patch

import maintain
import pytest
import server


class TestStorage:
    @pytest.fixture(autouse=True)
    def setup(self, bus: server.Bus, tmp_path: Path) -> Generator[None]:
        self.bus: server.Bus = bus
        self.root: Path = tmp_path
        with ExitStack() as self.stack:
            self.bus.hook(
                {
                    "session_id": "a",
                    "event": "SessionStart",
                    "names": ["alice"],
                    "host": "remote",
                }
            )
            yield

    def message(self, text: str, age: float = 0) -> int:
        with patch.object(server.time, "time", return_value=time.time() - age):
            return self.bus.send("human", {"to": ["alice"], "text": text})["id"]

    def job(self, trim: bool = True) -> dict[str, Any]:
        job: dict[str, Any] = self.bus.maintenance.start(7, trim)
        deadline: float = time.monotonic() + 10
        while job["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
            job = self.bus.maintenance.status(job["id"])
        assert job["status"] != "running", job
        return job

    def restored_database(self, job: dict[str, Any]) -> Path:
        path: Path = self.root / f"restored-{job['id']}.sqlite3"
        with gzip.open(Path(job["backup"]) / "state.sqlite3.gz", "rb") as archived:
            path.write_bytes(archived.read())
        return path

    def archived_log(self, job: dict[str, Any]) -> bytes:
        with gzip.open(Path(job["backup"]) / "log.jsonl.gz", "rb") as archived:
            return archived.read()

    def test_hook_updates_no_historical_message_rows(self) -> None:
        self.message("retained history")
        statements: list[str] = []
        self.bus.store.db.set_trace_callback(statements.append)
        self.bus.hook({"session_id": "a", "event": "PostToolUse", "receipt": True})
        assert any(s == "COMMIT" for s in statements)
        assert not any(
            "INTO messages" in s or "DELETE FROM messages" in s for s in statements
        )
        assert self.bus.store.db.execute("PRAGMA synchronous").fetchone()[0] == 2

    def test_an_existing_uninitialized_database_does_not_reset_history_or_ids(
        self,
    ) -> None:
        broken: Path = self.root / "incomplete.sqlite3"
        with closing(sqlite3.connect(broken)) as db:
            db.executescript(
                "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT); "
                "CREATE TABLE messages(id INTEGER PRIMARY KEY, data TEXT); "
                "CREATE TABLE agents(sid TEXT PRIMARY KEY, data TEXT);"
            )
            db.execute(
                "INSERT INTO messages VALUES(25, ?)", ('{"text":"preserve this row"}',)
            )
            db.commit()
        with pytest.raises(ValueError, match="no committed initialization metadata"):
            server.Bus(broken)
        with closing(sqlite3.connect(broken)) as db:
            assert db.execute("SELECT * FROM messages").fetchall() == [
                (25, '{"text":"preserve this row"}')
            ]
            assert db.execute("SELECT * FROM meta").fetchall() == []

    def test_crash_before_the_first_commit_does_not_brick_a_new_bus(self) -> None:
        fresh: Path = self.root / "fresh.sqlite3"
        result: subprocess.CompletedProcess[bytes] = subprocess.run(
            [
                sys.executable,
                "-c",
                f"import os, storage, pathlib; storage.Store(pathlib.Path({str(fresh)!r})); os._exit(0)",
            ],
            cwd=server.HERE,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        bus: server.Bus = server.Bus(fresh)
        self.stack.callback(bus.store.close)
        assert bus.next_id == 1

    def test_unbounded_keep_days_are_refused(self) -> None:
        for days in (1e308, float("inf"), float("nan"), -1):
            with pytest.raises(ValueError, match="keep_days"):
                self.bus.maintenance.start(days)

    def test_successful_commit_survives_process_exit_without_cleanup(
        self,
    ) -> None:
        script: str = """
import os,sys
from pathlib import Path
import server
server.LOG_FILE=Path(sys.argv[1])/'crash-log.jsonl'
server.NTFY_URL=''
b: server.Bus=server.Bus(Path(sys.argv[1])/'crash.json')
b.hook({'session_id':'a','event':'SessionStart','names':['alice'],'host':'remote'})
b.send('human',{'to':['alice'],'text':'survives abrupt exit','request_id':'crash-send'})
os._exit(0)
"""
        subprocess.run(
            [sys.executable, "-c", script, str(self.root)],
            cwd=server.HERE,
            check=True,
            capture_output=True,
        )
        recovered: server.Bus = server.Bus(self.root / "crash.json")
        self.stack.callback(recovered.store.close)
        assert recovered.agents["a"]["inbox"] == [[1, "notify", True]]
        assert recovered.messages[0]["text"] == "survives abrupt exit"
        assert recovered.send(
            "human",
            {
                "to": ["alice"],
                "text": "survives abrupt exit",
                "request_id": "crash-send",
            },
        )["replayed"]

    def test_sql_failure_rolls_back_already_written_agent_and_id_rows(
        self,
    ) -> None:
        self.bus.store.db.executescript("""
            CREATE TRIGGER reject_send BEFORE INSERT ON messages
            BEGIN SELECT RAISE(ABORT, 'injected failure after metadata writes'); END;
        """)
        with pytest.raises(sqlite3.IntegrityError):
            self.message("failed")
        assert self.bus.next_id == 1
        assert self.bus.agents["a"]["inbox"] == []
        # read through another connection: store.load() would also reset the store's row cache
        with closing(sqlite3.connect(self.bus.state_file)) as db:
            assert (
                db.execute("SELECT value FROM meta WHERE key='next_id'").fetchone()[0]
                == "1"
            )
            inbox: str = db.execute("SELECT data FROM agents WHERE sid='a'").fetchone()[
                0
            ]
            assert json.loads(inbox)["inbox"] == []
        self.bus.store.db.execute("DROP TRIGGER reject_send")
        assert self.message("failed") == 1  # the retry is durable
        self.bus.store.close()
        reloaded: server.Bus = server.Bus(self.bus.state_file)
        assert reloaded.next_id == 2
        assert reloaded.agents["a"]["inbox"] == [[1, "notify", True]]
        assert [m["text"] for m in reloaded.messages] == ["failed"]
        reloaded.store.close()

    def test_incremental_trim_reclaims_database_pages(self) -> None:
        # Build a retained history without live traffic or rate-limit workarounds.
        row: dict[str, float | str | list[Any]] = {
            "ts": time.time() - 30 * 86400,
            "from": "human",
            "to": [],
            "reach": [],
            "text": "x" * 8192,
            "mode": "notify",
        }
        with self.bus.change():
            self.bus.messages = [row | {"id": n} for n in range(1, 301)]
            self.bus.next_id = 301
            self.bus.save()
        before: Any = self.bus.store.db.execute("PRAGMA page_count").fetchone()[0]
        job: dict[str, Any] = self.job()
        assert job["status"] == "complete", job
        after: Any = self.bus.store.db.execute("PRAGMA page_count").fetchone()[0]
        assert after < before // 2
        assert self.bus.next_id == 301
        restored: Path = self.restored_database(job)
        assert (
            Path(job["backup"]) / "state.sqlite3.gz"
        ).stat().st_size < restored.stat().st_size // 10
        assert not (Path(job["backup"]) / "state.sqlite3").exists()
        with closing(sqlite3.connect(restored)) as db:
            assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 300

    def test_a_trim_is_one_transaction(self) -> None:
        row: dict[str, Any] = {"ts": 0, "from": "human", "to": [], "reach": []}
        with self.bus.change():
            self.bus.messages = [row | {"id": n} for n in range(1, 1201)]
            self.bus.next_id = 1201
            self.bus.save()
        with patch.object(
            self.bus.store, "commit", wraps=self.bus.store.commit
        ) as commit:
            assert self.bus.trim_backed_up(set(range(1, 1201))) == 1200
        assert commit.call_count == 1  # a crash can't leave half a trim

    def test_backup_before_trim_preserves_unread_recent_and_later_writes(
        self,
    ) -> None:
        old_read: int = self.message("old read", 30 * 86400)
        old_unread: int = self.message("old unread", 30 * 86400)
        recent: int = self.message("recent")
        self.bus.acknowledge(
            "a", "", {"ids": [old_read, recent]}
        )  # recent read mail stays
        server.LOG_FILE.write_bytes(
            (
                json.dumps({"event": "join", "agent": "alice", "ts": 1})
                + "\n"
                + json.dumps({"event": "tool", "target": "old", "ts": 1})
                + "\n"
                + json.dumps({"event": "tool", "target": "recent", "ts": time.time()})
                + "\n"
                + "damaged-but-preserved\n"
            ).encode()
        )
        original_log: bytes = server.LOG_FILE.read_bytes()
        write: Callable[..., None] = maintain.write_json
        late: list[int] = []

        def after_backup(path: Path, data: dict[str, Any]) -> None:
            write(path, data)
            if path.name == "manifest.json":
                late.append(
                    self.message(
                        "created after backup with an old timestamp", 30 * 86400
                    )
                )
                server.log("tool", target="appended after backup", ts=1)

        with patch.object(maintain, "write_json", side_effect=after_backup):
            job: dict[str, Any] = self.job()
        assert job["status"] == "complete", job
        assert [m["id"] for m in self.bus.messages] == [old_unread, recent, *late]
        backup: Path = Path(job["backup"])
        assert self.archived_log(job) == original_log
        restored: Path = self.restored_database(job)
        with closing(sqlite3.connect(restored)) as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert [
                r[0] for r in db.execute("SELECT id FROM messages ORDER BY id")
            ] == [old_read, old_unread, recent]
        manifest: Any = json.loads((backup / "manifest.json").read_text())
        assert manifest["format"] == 2
        assert manifest["compression"] == "gzip"
        assert manifest["uncompressed_sha256"]["state.sqlite3"] == maintain.digest(
            restored
        )
        for name, sha in manifest["sha256"].items():
            assert maintain.digest(backup / name) == sha
        active: str = server.LOG_FILE.read_text()
        assert '"target": "old"' not in active
        for kept in (
            '"event": "join"',
            '"target": "recent"',
            "damaged-but-preserved",
            "appended after backup",
        ):
            assert kept in active
        assert job["messages_removed"] == 1
        assert job["events_removed"] == 1

    def test_trim_sync_does_not_block_hooks_and_preserves_concurrent_appends(
        self,
    ) -> None:
        original: bytes = (
            json.dumps({"event": "tool", "ts": 0})
            + "\n"
            + (
                json.dumps({"event": "tool", "ts": time.time(), "target": "x" * 1024})
                + "\n"
            )
            * 1024
        ).encode()
        server.LOG_FILE.write_bytes(original)
        entered, release = threading.Event(), threading.Event()
        sync_locks: list[bool] = []
        real_fsync: Callable[..., None] = os.fsync

        def slow_sync(fd: int) -> None:
            if Path(os.readlink(f"/proc/self/fd/{fd}")).name.startswith(".arke-trim-"):
                sync_locks.append(server.LOG_LOCK.locked())
                if len(sync_locks) == 1:
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError("test did not release log sync")
            real_fsync(fd)

        with (
            patch.object(maintain.os, "fsync", side_effect=slow_sync),
            ThreadPoolExecutor(max_workers=3) as pool,
        ):
            maintenance: Future[dict[str, Any]] = pool.submit(self.job)
            try:
                assert entered.wait(2), "trim never reached log sync"
                hook: Future[dict[str, Any]] = pool.submit(
                    self.bus.hook,
                    {
                        "session_id": "a",
                        "event": "PostToolUse",
                        "tool": "Read",
                        "target": "written during sync",
                        "receipt": True,
                    },
                )
                reader: Future[list[dict[str, Any]]] = pool.submit(
                    self.bus.log_view, {}
                )
                hook.result(timeout=1)
                reader.result(timeout=1)
            finally:
                release.set()
                job: dict[str, Any] = maintenance.result(timeout=5)
        assert job["status"] == "complete", job
        assert not sync_locks[0], "bulk log sync held the append lock"
        assert sync_locks[-1], "final tail was not synced under the append lock"
        assert self.archived_log(job) == original
        active: bytes = server.LOG_FILE.read_bytes()
        assert b"written during sync" in active
        assert b'"ts": 0' not in active
        assert job["events_removed"] == 1

    def test_failed_trim_sync_leaves_active_log_and_archive_intact(
        self, subtests: pytest.Subtests
    ) -> None:
        original: bytes = b'{"event":"tool","ts":0}\n'
        real_fsync: Callable[..., None] = os.fsync
        for locked in (False, True):

            def fail_sync(fd: int, locked: bool = locked) -> None:
                if (
                    Path(os.readlink(f"/proc/self/fd/{fd}")).name.startswith(
                        ".arke-trim-"
                    )
                    and server.LOG_LOCK.locked() == locked
                ):
                    raise OSError("trim sync failed")
                real_fsync(fd)

            with (
                subtests.test(locked=locked),
                patch.object(maintain.os, "fsync", side_effect=fail_sync),
            ):
                server.LOG_FILE.write_bytes(original)
                job: dict[str, Any] = self.job()
                assert job["status"] == "failed", job
                assert "trim sync failed" in job["error"]
                assert server.LOG_FILE.read_bytes() == original
                assert self.archived_log(job) == original
                assert list(self.root.glob(".arke-trim-*")) == []

    def test_malformed_event_names_do_not_abort_maintenance(self) -> None:
        mid: int = self.message("old read", 30 * 86400)
        self.bus.acknowledge("a", "", {"ids": [mid]})
        records: list[
            dict[str, int] | dict[str, list[Any] | dict[str, Any] | None | int]
        ] = [{"event": name, "ts": 0} for name in ([], {})] + [{"ts": 0}]
        retained: bytes = (
            b"".join((json.dumps(row) + "\n").encode() for row in records)
            + b'{"event":"tool","ts\n'
        )  # cut short by a crash
        original: bytes = retained + b'{"event":"tool","ts":0}\n'
        server.LOG_FILE.write_bytes(original)
        job: dict[str, Any] = self.job()
        assert job["status"] == "complete", job
        assert job["messages_removed"] == 1
        assert job["events_removed"] == 1
        assert server.LOG_FILE.read_bytes() == retained
        assert self.archived_log(job) == original

    def test_failed_backup_never_trims_active_data(self) -> None:
        mid: int = self.message("old read", 30 * 86400)
        self.bus.acknowledge("a", "", {"ids": [mid]})
        before: bytes = server.LOG_FILE.read_bytes()
        with patch.object(
            maintain, "write_json", side_effect=OSError("backup disk full")
        ):
            job: dict[str, Any] = self.job()
        assert job["status"] == "failed"
        assert [m["id"] for m in self.bus.messages] == [mid]
        assert server.LOG_FILE.read_bytes() == before

    def test_a_retried_start_is_the_same_job_and_a_second_job_waits(self) -> None:
        release: threading.Event = threading.Event()
        with patch.object(maintain.Maintenance, "run", lambda _: release.wait(5)):
            job: dict[str, Any] = self.bus.maintenance.start(7, True, "r1")
            assert self.bus.maintenance.start(7, True, "r1") == job
            with pytest.raises(ValueError, match="already running"):
                self.bus.maintenance.start(7, True, "r2")
            release.set()

    def test_backup_only_leaves_active_history_unchanged(self) -> None:
        mid: int = self.message("old read", 30 * 86400)
        self.bus.acknowledge("a", "", {"ids": [mid]})
        before: bytes = server.LOG_FILE.read_bytes()
        job: dict[str, Any] = self.job(trim=False)
        assert job["status"] == "complete", job
        assert [m["id"] for m in self.bus.messages] == [mid]
        assert server.LOG_FILE.read_bytes() == before
        first: Path = Path(job["backup"])
        archived_hash: str = maintain.digest(first / "log.jsonl.gz")
        second: dict[str, Any] = self.job()
        assert second["status"] == "complete", second
        assert job["backup"] != second["backup"]
        assert maintain.digest(first / "log.jsonl.gz") == archived_hash
        assert self.archived_log(job) == before

    def test_failed_compression_or_verification_never_trims(self) -> None:
        mid: int = self.message("old read", 30 * 86400)
        self.bus.acknowledge("a", "", {"ids": [mid]})
        before: bytes = server.LOG_FILE.read_bytes()
        compress: Callable[..., dict[str, str]] = maintain.gzip_snapshot

        def fail_log(source: BinaryIO, target: Path, count: int) -> dict[str, str]:
            if target.name == "log.jsonl.gz":
                raise OSError("backup disk full")
            return compress(source, target, count)

        for failure in (
            patch.object(maintain, "gzip_snapshot", side_effect=fail_log),
            patch.object(
                maintain.gzip, "open", side_effect=gzip.BadGzipFile("damaged archive")
            ),
        ):
            with failure:
                job: dict[str, Any] = self.job()
            assert job["status"] == "failed", job
            assert [m["id"] for m in self.bus.messages] == [mid]
            assert server.LOG_FILE.read_bytes() == before
            # the uncompressed copy of all mail isn't left behind (only the verified gzip is meant to stay)
            assert not (Path(job["backup"]) / "state.sqlite3").exists()

    def test_the_server_closes_its_state_to_other_local_users(self) -> None:
        """A state dir made with the default umask kept mail and backups world-readable, key or not."""
        state: Path = self.root / "older-state"
        state.mkdir(mode=0o755)
        log: Path = state / "log.jsonl"
        log.write_text("")
        log.chmod(0o644)
        umask: int = os.umask(0o022)
        try:
            with (
                patch.object(server, "STATE_DIR", state),
                patch.object(server, "LOG_FILE", log),
            ):
                server.private_state()
            assert os.umask(0o022) == 0o077  # files made from here on are owner-only
        finally:
            os.umask(umask)
        assert (state.stat().st_mode & 0o777, log.stat().st_mode & 0o777) == (
            0o700,
            0o600,
        )

    def test_pruning_keeps_an_offline_agent_with_unread_direct_mail(self) -> None:
        for sid, name in (("b", "bob"), ("c", "carol")):
            self.bus.hook(
                {
                    "session_id": sid,
                    "event": "SessionStart",
                    "names": [name],
                    "host": "remote",
                }
            )
            self.bus.hook({"session_id": sid, "event": "SessionEnd"})
        self.bus.send("human", {"to": ["bob"], "text": "still waiting for you"})
        with self.bus.change():
            for sid in ("b", "c"):
                self.bus.agents[sid]["last_active"] = (
                    time.time() - server.OFFLINE_DROP_S - 100
                )
            self.bus.save()
        assert self.job()["status"] == "complete"
        assert "b" in self.bus.agents and "c" not in self.bus.agents

    def test_session_cleanup_waits_for_archive_and_keeps_active_guards(
        self,
    ) -> None:
        self.bus.hook(
            {
                "session_id": "b",
                "event": "SessionStart",
                "names": ["bob"],
                "host": "remote",
            }
        )
        self.bus.hook({"session_id": "b", "event": "SessionEnd"})
        with self.bus.change():
            old: float = time.time() - server.OFFLINE_DROP_S - 100
            self.bus.agents["b"]["last_active"] = old
            self.bus.retired = {"a": {"at": old}, "gone": {"at": old}}
            self.bus.left = {"a": ["still-needed"], "gone": ["old-process"]}
            self.bus.save()
        self.bus.forget_old()
        assert "b" in self.bus.agents
        assert "gone" in self.bus.retired
        with patch.object(
            maintain, "write_json", side_effect=OSError("archive incomplete")
        ):
            failed: dict[str, Any] = self.job()
        assert failed["status"] == "failed"
        assert "b" in self.bus.agents
        assert "gone" in self.bus.retired

        job: dict[str, Any] = self.job()
        assert job["status"] == "complete", job
        assert "b" not in self.bus.agents
        assert self.bus.retired == {}
        assert self.bus.left == {"a": ["still-needed"]}
        with closing(sqlite3.connect(self.restored_database(job))) as db:
            assert {sid for (sid,) in db.execute("SELECT sid FROM agents")} == {
                "a",
                "b",
            }
            meta: dict[Any, Any] = {
                k: json.loads(v) for k, v in db.execute("SELECT key, value FROM meta")
            }
        assert set(meta["retired"]) == {"a", "gone"}
        assert meta["left"]["gone"] == ["old-process"]

    def test_session_changes_after_archive_are_not_pruned(self) -> None:
        self.bus.hook(
            {
                "session_id": "b",
                "event": "SessionStart",
                "names": ["bob"],
                "host": "remote",
            }
        )
        self.bus.hook({"session_id": "b", "event": "SessionEnd"})
        with self.bus.change():
            self.bus.agents["b"]["last_active"] = (
                time.time() - server.OFFLINE_DROP_S - 100
            )
            self.bus.save()
        write: Callable[..., None] = maintain.write_json

        def revive(path: Path, data: dict[str, Any]) -> None:
            write(path, data)
            if path.name == "manifest.json":
                self.bus.hook({"session_id": "b", "event": "SessionStart"})
                self.bus.send("human", {"to": ["bob"], "text": "mail after archive"})

        with patch.object(maintain, "write_json", side_effect=revive):
            job: dict[str, Any] = self.job()
        assert job["status"] == "complete", job
        assert self.bus.agents["b"]["state"] == "idle"
        assert self.bus.agents["b"]["inbox"] == [[1, "notify", True]]

    def test_resumed_session_still_rejects_stale_hooks_after_archiving(
        self,
    ) -> None:
        def hook(sid: str, pid: int, event: str = "SessionStart") -> dict[str, Any]:
            return self.bus.hook(
                {
                    "session_id": sid,
                    "event": event,
                    "names": ["alice"],
                    "kind": "claude",
                    "host": "remote",
                    "pid": pid,
                    "process_token": f"remote:{pid}:{pid * 10}",
                    "tmux": "/private:%1",
                    "tmux_server": "9",
                    "receipt": True,
                }
            )

        with patch.object(server.time, "time", return_value=1000.0) as clock:
            hook("a", 100)
            hook("b", 200)
            clock.return_value = 1000 + server.OFFLINE_DROP_S - 2
            hook("a", 300)
            hook("a", 400)
            mid: int = self.message("mail for the current process")
            clock.return_value = 1000 + server.OFFLINE_DROP_S + 1
            job: dict[str, Any] = self.job()
            assert job["status"] == "complete", job
            assert "a" not in self.bus.retired
            assert hook("a", 300, "SessionEnd")["ignored"]
            current: dict[str, Any] = hook("a", 400, "PostToolUse")
        assert current["delivery"] == {"sid": "a", "ids": [mid]}
        assert "mail for the current process" in current["context"]
        assert self.bus.agents["a"]["pid"] == 400
        assert self.bus.agents["a"]["state"] == "busy"

    def test_crash_after_archive_leaves_full_recoverable_copies(self) -> None:
        mid: int = self.message("recover this archived mail", 30 * 86400)
        self.bus.acknowledge("a", "", {"ids": [mid]})
        original: bytes = server.LOG_FILE.read_bytes()
        script: str = """
import os,sys,time
from pathlib import Path
import server
root: Path=Path(sys.argv[1]); server.LOG_FILE=root/'log.jsonl'; server.NTFY_URL=''
b: server.Bus=server.Bus(root/'state.sqlite3')
b.maintenance.job={'id':'crash','status':'running','started_at':time.time(),'keep_days':7,'trim':True}
def crash_after_backup(ids: set[int]) -> None: os._exit(23)
b.trim_backed_up=crash_after_backup
b.maintenance.run()
"""
        result: subprocess.CompletedProcess[bytes] = subprocess.run(
            [sys.executable, "-c", script, str(self.root)],
            cwd=server.HERE,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 23, result.stderr
        backup: Path = next((self.root / "backups").iterdir())
        job: dict[str, str] = {"id": "crash", "backup": str(backup)}
        assert (backup / "manifest.json").is_file()
        assert self.archived_log(job) == original
        with closing(sqlite3.connect(self.restored_database(job))) as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert db.execute("SELECT id FROM messages").fetchall() == [(mid,)]
        assert server.LOG_FILE.read_bytes() == original
        saved: dict[str, Any] | None = self.bus.store.load()
        assert saved is not None
        assert saved["messages"][0]["id"] == mid

    def test_online_backup_does_not_block_a_send(self) -> None:
        entered, release = threading.Event(), threading.Event()

        class PausedBackup(sqlite3.Connection):
            def backup(self, target: sqlite3.Connection, **kwargs: object) -> None:
                def progress(*args: object) -> None:
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError("backup test was not released")

                super().backup(target, pages=128, progress=progress)

        connect: Callable[..., sqlite3.Connection] = sqlite3.connect

        def reader_connection(
            database: str | Path, *, uri: bool = False
        ) -> sqlite3.Connection:
            return connect(database, uri=uri, factory=PausedBackup)

        with patch.object(maintain.sqlite3, "connect", side_effect=reader_connection):
            job: dict[str, Any] = self.bus.maintenance.start(7, trim=False)
            try:
                assert entered.wait(2)
                assert self.message("sent while backup is paused") == 1
                assert self.bus.maintenance.status(job["id"])["status"] == "running"
            finally:
                release.set()
            deadline: float = time.monotonic() + 5
            while time.monotonic() < deadline:
                job = self.bus.maintenance.status(job["id"])
                if job["status"] != "running":
                    break
                time.sleep(0.01)
        assert job["status"] == "complete", job
        assert self.bus.agents["a"]["inbox"] == [[1, "notify", True]]

    def test_compressing_database_or_log_does_not_block_messages(
        self, subtests: pytest.Subtests
    ) -> None:
        compress: Callable[..., dict[str, str]] = maintain.gzip_snapshot
        for archive in ("state.sqlite3.gz", "log.jsonl.gz"):
            with subtests.test(archive=archive):
                old: int = self.message("old read", 30 * 86400)
                self.bus.acknowledge("a", "", {"ids": [old]})
                entered, release, sent = (
                    threading.Event(),
                    threading.Event(),
                    threading.Event(),
                )
                failures: list[Exception] = []

                def paused(
                    source: BinaryIO,
                    target: Path,
                    count: int,
                    archive: str = archive,
                    entered: threading.Event = entered,
                    release: threading.Event = release,
                ) -> dict[str, str]:
                    if target.name == archive:
                        entered.set()
                        if not release.wait(5):
                            raise TimeoutError("test did not release compression")
                    return compress(source, target, count)

                def send(
                    archive: str = archive,
                    sent: threading.Event = sent,
                    failures: list[Exception] = failures,
                ) -> None:
                    try:
                        self.message(f"sent during {archive}")
                        self.bus.hook(
                            {"session_id": "a", "event": "PostToolUse", "receipt": True}
                        )
                        sent.set()
                    except Exception as e:  # noqa: BLE001 — relay errors from the sender thread
                        failures.append(e)

                with patch.object(maintain, "gzip_snapshot", side_effect=paused):
                    job: dict[str, Any] = self.bus.maintenance.start(7)
                    sender: threading.Thread = threading.Thread(target=send)
                    try:
                        assert entered.wait(2)
                        sender.start()
                        assert sent.wait(1), "compression blocked a send or hook"
                    finally:
                        release.set()
                        if sender.ident is not None:
                            sender.join(3)
                    deadline: float = time.monotonic() + 5
                    while job["status"] == "running" and time.monotonic() < deadline:
                        time.sleep(0.01)
                        job = self.bus.maintenance.status(job["id"])
                assert failures == []
                assert job["status"] == "complete", job
                assert f"sent during {archive}" in server.LOG_FILE.read_text()
                assert any(
                    m["text"] == f"sent during {archive}" for m in self.bus.messages
                )

    def test_rotated_log_rejects_old_generation_even_after_regrowth(self) -> None:
        server.log("tool", target="old")
        first: dict[str, Any] = server.read_events(0, set(), 1000)
        replacement: Path = self.root / "new-log"
        replacement.write_text(
            (json.dumps({"event": "tool", "ts": time.time(), "target": "new"}) + "\n")
            * 20
        )
        os.replace(replacement, server.LOG_FILE)
        with pytest.raises(server.HttpError):
            server.read_events(first["next"], set(), 1000, first["generation"])
        fresh: dict[str, Any] = server.read_events(0, set(), 1000)
        assert fresh["generation"] != first["generation"]
        assert len(fresh["events"]) == 20

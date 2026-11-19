"""Durability, backups beside a live bus, and visibility during slow/failed commits."""

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

    # --- backups: maintain.py reads the files beside the live bus; nothing in the server can start one ---

    def backup(self) -> Path:
        return maintain.backup(self.bus.state_file, server.LOG_FILE)

    def restored(self, folder: Path, name: str) -> bytes:
        with gzip.open(folder / (name + ".gz"), "rb") as archived:
            return archived.read()

    def test_backup_matches_its_manifest_and_changes_no_history(self) -> None:
        old: int = self.message("old read", 30 * 86400)
        unread: int = self.message("unread")
        self.bus.acknowledge("a", "", {"ids": [old]})
        log: bytes = server.LOG_FILE.read_bytes()
        folder: Path = self.backup()
        assert folder.parent == self.root / "backups"
        assert sorted(p.name for p in folder.iterdir()) == [
            "log.jsonl.gz",
            "manifest.json",
            "state.sqlite3.gz",
        ]  # the uncompressed copy of all mail isn't left behind
        manifest: Any = json.loads((folder / "manifest.json").read_text())
        assert manifest["format"] == 3
        assert manifest["messages"] == 2
        assert manifest["log_bytes"] == len(log)
        for name, sha in manifest["sha256"].items():
            assert maintain.digest(folder / name) == sha
        database: Path = self.root / "restored.sqlite3"
        database.write_bytes(self.restored(folder, "state.sqlite3"))
        assert manifest["uncompressed_sha256"]["state.sqlite3"] == maintain.digest(
            database
        )
        with closing(sqlite3.connect(database)) as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert [
                r[0] for r in db.execute("SELECT id FROM messages ORDER BY id")
            ] == [old, unread]
        assert self.restored(folder, "log.jsonl") == log
        # active history is untouched, however old, and a second backup is a separate folder
        assert [m["id"] for m in self.bus.messages] == [old, unread]
        assert server.LOG_FILE.read_bytes() == log
        assert self.backup() != folder

    def test_backup_stops_at_the_last_whole_log_line(self) -> None:
        whole: bytes = server.LOG_FILE.read_bytes()
        assert whole.endswith(b"\n")
        with server.LOG_FILE.open("ab") as f:
            f.write(b'{"ts": 1, "event": "tool", "targ')  # an append still in progress
        folder: Path = self.backup()
        assert self.restored(folder, "log.jsonl") == whole
        assert json.loads((folder / "manifest.json").read_text())["log_bytes"] == len(
            whole
        )

    def test_backup_without_an_event_log_archives_an_empty_one(self) -> None:
        server.LOG_FILE.unlink()
        folder: Path = self.backup()
        assert self.restored(folder, "log.jsonl") == b""

    def test_failed_backup_has_no_manifest_and_changes_nothing(self) -> None:
        mid: int = self.message("old read", 30 * 86400)
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
            patch.object(maintain, "write_json", side_effect=OSError("disk full")),
        ):
            with failure, pytest.raises(OSError, match="full|damaged"):
                self.backup()
        folders: list[Path] = list((self.root / "backups").iterdir())
        assert len(folders) == 3
        for folder in folders:
            assert not (folder / "manifest.json").exists()
            assert not (folder / "state.sqlite3").exists()
        assert [m["id"] for m in self.bus.messages] == [mid]
        assert server.LOG_FILE.read_bytes() == before

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

        with (
            patch.object(maintain.sqlite3, "connect", side_effect=reader_connection),
            ThreadPoolExecutor(1) as pool,
        ):
            running: Future[Path] = pool.submit(self.backup)
            try:
                assert entered.wait(2)
                assert self.message("sent while backup is paused") == 1
                assert not running.done()
            finally:
                release.set()
            folder: Path = running.result(timeout=5)
        assert (folder / "manifest.json").is_file()
        assert self.bus.agents["a"]["inbox"] == [[1, "notify", True]]
        # the snapshot was pinned before the send
        database: Path = self.root / "restored.sqlite3"
        database.write_bytes(self.restored(folder, "state.sqlite3"))
        with closing(sqlite3.connect(database)) as db:
            assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0

    def test_backup_command_runs_beside_the_open_bus(self) -> None:
        mid: int = self.message("in the backup")
        config: Path = self.root / "config.toml"
        config.write_text("")
        env: dict[str, str] = os.environ | {
            "ARKE_CONFIG": str(config),
            "ARKE_DIR": str(self.root),
            "ARKE_LOG": str(server.LOG_FILE),
        }
        script: str = str(server.HERE / "maintain.py")
        result: subprocess.CompletedProcess[str] = subprocess.run(
            [sys.executable, script],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        folder: Path = Path(result.stdout.strip())
        assert folder.parent == self.root / "backups"
        assert (folder / "manifest.json").is_file()
        database: Path = self.root / "restored.sqlite3"
        database.write_bytes(self.restored(folder, "state.sqlite3"))
        with closing(sqlite3.connect(database)) as db:
            assert db.execute("SELECT id FROM messages").fetchall() == [(mid,)]
        assert self.message("the bus still writes") == mid + 1
        # the old trim flags are refused, not ignored; and no database is an error, not an empty backup
        for args, where in (
            (["--days", "7"], self.root),
            (["--backup-only"], self.root),
            ([], self.root / "nothing-here"),
        ):
            failed: subprocess.CompletedProcess[str] = subprocess.run(
                [sys.executable, script, *args],
                env=env | {"ARKE_DIR": str(where)},
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            assert failed.returncode != 0, failed.stdout
        assert len(list((self.root / "backups").iterdir())) == 1
        assert not (self.root / "nothing-here").exists()

"""Back up and trim a running arke: python3 maintain.py --days 7.

Work runs on the server; closing this client doesn't cancel it. Backups are
under the server's state directory, not on the machine running this command.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import http.client
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from _thread import LockType
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

from storage import sync_dir

if TYPE_CHECKING:
    from _hashlib import HASH

    from server import Bus

CHUNK: int = 1024 * 1024
IDENTITY_EVENTS: set[str] = {"join", "rename", "takeover", "forget", "project", "start"}


def copy_bytes(source: BinaryIO, target: BinaryIO, count: int) -> None:
    while count:
        data: bytes = source.read(min(CHUNK, count))
        if not data:
            raise OSError("event log was truncated during backup")
        target.write(data)
        count -= len(data)


def digest(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def gzip_snapshot(source: BinaryIO, target: Path, count: int) -> dict[str, str]:
    """Archive a fixed prefix, sync it, then decompress it again to verify every original byte."""
    original: HASH = hashlib.sha256()
    with target.open("xb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            while count:
                data: bytes = source.read(min(CHUNK, count))
                if not data:
                    raise OSError("source was truncated during compressed backup")
                compressed.write(data)
                original.update(data)
                count -= len(data)
        raw.flush()
        os.fsync(raw.fileno())
    with gzip.open(target, "rb") as restored:
        if hashlib.file_digest(restored, "sha256").hexdigest() != original.hexdigest():
            raise OSError("compressed backup does not match its source")
    sync_dir(target.parent)
    return {"sha256": digest(target), "uncompressed_sha256": original.hexdigest()}


def write_json(path: Path, data: dict[str, Any]) -> None:
    with path.open("x") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    sync_dir(path.parent)


def keep_event(line: bytes, cutoff: float) -> bool:
    try:
        event: Any = json.loads(line)
        return event["event"] in IDENTITY_EVENTS or event["ts"] >= cutoff
    except (ValueError, KeyError, TypeError):
        return True  # a line cut short by a crash: kept in both the backup and the active log


class Maintenance:
    def __init__(self, bus: Bus, log_path: Path, log_lock: threading.Lock) -> None:
        self.bus, self.log_path, self.log_lock = bus, log_path, log_lock
        self.lock: LockType = threading.Lock()
        self.job: dict[
            str, Any
        ] = {}  # the latest job; status is reset on server restart

    def start(
        self, days: float, trim: bool = True, request_id: str = ""
    ) -> dict[str, Any]:
        if (
            isinstance(days, bool)
            or not isinstance(days, (int, float))
            or not 0 <= days <= 100_000
        ):  # also rejects NaN; an infinite cutoff is not valid JSON
            raise ValueError("keep_days must be a number between 0 and 100000")
        if not isinstance(trim, bool):
            raise ValueError("trim must be boolean")  # noqa: TRY004 — API validation returns a bad request
        if not isinstance(request_id, str) or len(request_id) > 100:
            raise ValueError("request_id must be a string of at most 100 characters")
        with self.lock:
            if request_id and self.job.get("id") == request_id:
                return dict(self.job)  # a retried request
            if self.job.get("status") == "running":
                raise ValueError(
                    "a maintenance job is already running; inspect GET /maintenance"
                )
            self.job = {
                "id": request_id or uuid.uuid4().hex,
                "status": "running",
                "phase": "queued",
                "keep_days": days,
                "trim": trim,
                "started_at": time.time(),
            }
            threading.Thread(
                target=self.run, name="arke-maintenance", daemon=True
            ).start()
            return dict(self.job)

    def status(self, key: str = "") -> dict[str, Any]:
        with self.lock:
            if key and self.job.get("id") != key:
                raise ValueError(
                    "unknown maintenance job (only the latest is kept, until server restart)"
                )
            return dict(self.job)

    def update(self, **fields: Any) -> None:
        with self.lock:
            self.job.update(fields)

    def run(self) -> None:
        source: BinaryIO | None = None
        replacement: Path | None = None
        database: Path | None = (
            None  # the uncompressed SQLite copy: a workspace, gone once its gzip is verified
        )
        error: str = ""
        try:
            job: dict[str, Any] = self.status()
            cutoff: float = job["started_at"] - job["keep_days"] * 86400
            root: Path = self.bus.state_file.parent / "backups"
            root.mkdir(parents=True, exist_ok=True)
            sync_dir(root.parent)
            backup: Path = Path(
                tempfile.mkdtemp(prefix=time.strftime("%Y%m%d-%H%M%S-"), dir=root)
            )
            sync_dir(root)
            self.update(phase="backing up database", backup=str(backup))
            database = backup / "state.sqlite3"
            self.bus.store.backup(database)
            with closing(
                sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
            ) as db:
                old_ids: set[int] = {
                    mid
                    for mid, raw in db.execute("SELECT id, data FROM messages")
                    if json.loads(raw)["ts"] < cutoff
                }
                backed_up: int = db.execute("SELECT count(*) FROM messages").fetchone()[
                    0
                ]
                saved: dict[str, Any] = {
                    k: json.loads(v)
                    for k, v in db.execute(
                        "SELECT key, value FROM meta WHERE key IN ('retired', 'left')"
                    )
                }
                saved["agents"] = {
                    sid: json.loads(raw)
                    for sid, raw in db.execute("SELECT sid, data FROM agents")
                }

            self.update(phase="compressing database")
            with database.open("rb") as db_file:
                db_hashes: dict[str, str] = gzip_snapshot(
                    db_file, backup / "state.sqlite3.gz", database.stat().st_size
                )

            self.update(phase="backing up event log")
            # Capture a byte boundary under the append lock; copy the immutable prefix outside it.
            with self.log_lock:
                if self.log_path.exists():
                    source = self.log_path.open("rb")
                    original: os.stat_result = os.fstat(source.fileno())
                    boundary: int = original.st_size
                else:
                    boundary = 0
            saved_log: Path = backup / "log.jsonl.gz"
            log_hashes: dict[str, str] = gzip_snapshot(
                source or io.BytesIO(), saved_log, boundary
            )
            write_json(
                backup / "manifest.json",
                {
                    "format": 2,
                    "compression": "gzip",
                    "created_at": time.time(),
                    "keep_days": job["keep_days"],
                    "cutoff": cutoff,
                    "source_database": str(self.bus.state_file),
                    "source_log": str(self.log_path),
                    "messages": backed_up,
                    "log_bytes": boundary,
                    "sha256": {
                        "state.sqlite3.gz": db_hashes["sha256"],
                        "log.jsonl.gz": log_hashes["sha256"],
                    },
                    "uncompressed_sha256": {
                        "state.sqlite3": db_hashes["uncompressed_sha256"],
                        "log.jsonl": log_hashes["uncompressed_sha256"],
                    },
                    "note": "Database is a consistent online snapshot; log is a later byte-prefix snapshot.",
                },
            )  # both backups and their manifest are durable before anything is removed
            database.unlink()  # the verified gzip is the permanent copy; this was only the SQLite backup workspace
            sync_dir(backup)
            if not job["trim"]:
                self.update(
                    status="complete",
                    phase="backup complete",
                    finished_at=time.time(),
                )
                return

            self.update(phase="trimming messages")
            removed: int = self.bus.trim_backed_up(old_ids)
            self.update(messages_removed=removed, phase="trimming event log")
            dropped: int = 0
            if source:
                fd, name = tempfile.mkstemp(
                    prefix=".arke-trim-", dir=self.log_path.parent
                )
                replacement = Path(name)
                with (
                    os.fdopen(fd, "wb") as target,
                    gzip.open(saved_log, "rb") as archived_log,
                ):
                    for line in archived_log:
                        if keep_event(line, cutoff):
                            target.write(line)
                        else:
                            dropped += 1
                    if dropped:
                        # Sync and catch up outside the append lock; only the final short tail and swap hold it.
                        while True:
                            target.flush()
                            os.fsync(target.fileno())
                            current: int = os.fstat(source.fileno()).st_size
                            pending: int = current - source.tell()
                            if pending < 0:
                                raise OSError(
                                    "event log was truncated during maintenance"
                                )
                            if pending <= CHUNK:
                                break
                            copy_bytes(source, target, pending)
                        with self.log_lock:
                            live: os.stat_result = self.log_path.stat()
                            if (live.st_dev, live.st_ino) != (
                                original.st_dev,
                                original.st_ino,
                            ) or live.st_size < source.tell():
                                raise OSError(
                                    "event log was replaced during maintenance; active log left untouched"
                                )
                            copy_bytes(source, target, live.st_size - source.tell())
                            target.flush()
                            os.fsync(target.fileno())
                            os.replace(replacement, self.log_path)
                            sync_dir(self.log_path.parent)
            self.update(phase="trimming inactive sessions", events_removed=dropped)
            pruned: dict[str, int] = self.bus.prune_backed_up(saved, job["started_at"])
            self.update(phase="reclaiming database space", **pruned)
            self.bus.compact()
            result: dict[str, float] = {
                "messages_removed": removed,
                "events_removed": dropped,
                **pruned,
                "finished_at": time.time(),
            }
            write_json(backup / "result.json", result)
            self.update(**result, status="complete", phase="complete")
        except Exception as e:  # noqa: BLE001 — failures are exposed through the job status endpoint
            error = f"{type(e).__name__}: {e}"
        finally:
            if source:
                source.close()
            if replacement:
                replacement.unlink(missing_ok=True)
            if database:
                database.unlink(missing_ok=True)
        if error:  # only once the temporary files are gone, so "failed" means nothing is left half-done
            self.update(status="failed", error=error, finished_at=time.time())


def main() -> None:
    import client

    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--server",
        help="bus URL (default: configured arke server)",
    )
    parser.add_argument(
        "--days",
        type=float,
        default=7,
        help="days of read history to keep; unread mail is always kept (default: 7)",
    )
    parser.add_argument(
        "--backup-only",
        action="store_true",
        help="gzip the database and log into a backup without trimming",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="print the job status and return immediately",
    )
    parser.add_argument(
        "--status",
        metavar="JOB",
        help="follow an existing job instead of starting another",
    )
    args: argparse.Namespace = parser.parse_args()
    key: str = args.status or uuid.uuid4().hex
    try:
        if not args.status and client.identity()[1]:
            raise client.BusError("sub-agents cannot start maintenance")
        base: str = client.normalize(args.server) if args.server else client.server()[0]
    except client.BusError as e:
        raise SystemExit(f"maintenance: {e}") from None

    def request(path: str, body: dict[str, Any] | None = None) -> Any:
        req: urllib.request.Request = urllib.request.Request(
            base + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-Agent-As": "human"},
        )
        with client.open_bus(req, 10) as response:
            return json.load(response)

    try:
        if not args.status:
            print(f"Job {key}; follow with --status {key}", flush=True)
            job: Any = request(
                "/maintenance",
                {
                    "keep_days": args.days,
                    "trim": not args.backup_only,
                    "request_id": key,
                },
            )
            if args.no_wait:
                print(json.dumps(job, indent=2))
                return
        previous: str = ""
        while True:
            job = request("/maintenance?id=" + urllib.parse.quote(key, safe=""))
            if job["phase"] != previous:
                print(job["phase"], flush=True)
                previous = job["phase"]
            if job["status"] != "running" or args.no_wait:
                print(json.dumps(job, indent=2))
                if job["status"] == "failed":
                    raise SystemExit(1)
                return
            time.sleep(1)
    except (OSError, ValueError, client.BusError, http.client.HTTPException) as e:
        raise SystemExit(
            f"maintenance: {e}; check --status {key} before starting another job"
        ) from e
    except KeyboardInterrupt:
        raise SystemExit(
            f"Client stopped; the server job continues. Follow with --status {key}"
        ) from None


if __name__ == "__main__":
    main()

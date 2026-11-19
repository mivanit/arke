"""Back up arke's database and event log: python3 maintain.py

Run it on the machine that holds the bus's state, as the user the server runs as. It reads the
files directly, whether or not the server is running, and never changes or removes active history.
The server has no part in it and no request can start it.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

import settings
import storage
from storage import sync_dir

if TYPE_CHECKING:
    from _hashlib import HASH

CHUNK: int = 1024 * 1024


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


def whole_lines(source: BinaryIO) -> int:
    """Bytes up to the last newline. The server may be appending: a line still being written waits for the next backup."""
    end: int = os.fstat(source.fileno()).st_size
    while end:
        start: int = max(0, end - CHUNK)
        source.seek(start)
        found: int = source.read(end - start).rfind(b"\n")
        if found >= 0:
            return start + found + 1
        end = start
    return 0


def backup(state_file: Path, log_path: Path) -> Path:
    """Write a new folder under <state folder>/backups: state.sqlite3.gz, log.jsonl.gz and manifest.json.

    The manifest is written last, after both archives are verified: a folder without one is a failed backup.
    """
    if not state_file.is_file():
        raise OSError(f"no database at {state_file}")
    root: Path = state_file.parent / "backups"
    root.mkdir(parents=True, exist_ok=True)
    sync_dir(root.parent)
    folder: Path = Path(
        tempfile.mkdtemp(prefix=time.strftime("%Y%m%d-%H%M%S-"), dir=root)
    )
    sync_dir(root)
    # the uncompressed SQLite copy: a workspace, gone once its gzip is verified
    database: Path = folder / "state.sqlite3"
    try:
        storage.backup(state_file, database)
        with closing(
            sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        ) as db:
            messages: int = db.execute("SELECT count(*) FROM messages").fetchone()[0]
        with database.open("rb") as db_file:
            db_hashes: dict[str, str] = gzip_snapshot(
                db_file, folder / "state.sqlite3.gz", database.stat().st_size
            )
        with log_path.open("rb") if log_path.exists() else io.BytesIO() as log:
            log_bytes: int = (
                whole_lines(log) if isinstance(log, io.BufferedReader) else 0
            )
            log.seek(0)
            log_hashes: dict[str, str] = gzip_snapshot(
                log, folder / "log.jsonl.gz", log_bytes
            )
        write_json(
            folder / "manifest.json",
            {
                "format": 3,
                "compression": "gzip",
                "created_at": time.time(),
                "source_database": str(state_file),
                "source_log": str(log_path),
                "messages": messages,
                "log_bytes": log_bytes,
                "sha256": {
                    "state.sqlite3.gz": db_hashes["sha256"],
                    "log.jsonl.gz": log_hashes["sha256"],
                },
                "uncompressed_sha256": {
                    "state.sqlite3": db_hashes["uncompressed_sha256"],
                    "log.jsonl": log_hashes["uncompressed_sha256"],
                },
                "note": "Database is a consistent online snapshot; log is a later byte-prefix snapshot ending at a line break.",
            },
        )
    finally:
        database.unlink(missing_ok=True)
    sync_dir(folder)
    return folder


def main() -> None:
    argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    ).parse_args()
    os.umask(0o077)  # as the server does: mail is in these files
    try:
        values: dict[str, Any] = settings.load().get("server", {})
        folder: Path = backup(
            settings.state_dir(values) / "state.sqlite3", settings.log_file(values)
        )
    except (OSError, ValueError, sqlite3.Error) as e:
        raise SystemExit(f"backup failed: {type(e).__name__}: {e}") from e
    print(folder)


if __name__ == "__main__":
    main()

"""Offline owner repair for an unused shared binding left by rejected setup.

No established conversation can be cleared. The service must be stopped, and
all private conversations and requests remain untouched.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import sqlite3
from pathlib import Path


def clear_empty_binding(path: Path) -> bool:
    path = path.resolve()
    if not path.is_file() or path.stat().st_uid != os.geteuid():
        raise ValueError("choose an existing conversation database owned by this user")
    with path.with_suffix(".lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(
                "stop the conversation service before repairing sharing"
            ) from None
        db = sqlite3.connect(path)
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                shared = db.execute(
                    "SELECT value FROM metadata WHERE key='shared_conversation'"
                ).fetchone()
                if shared is None:
                    return False
                cid = shared[0]
                row = db.execute(
                    "SELECT native_id FROM conversations WHERE id=?", (cid,)
                ).fetchone()
                if row is None:
                    raise ValueError(
                        "shared conversation is missing; inspect the database before repairing"
                    )
                if row[0] is not None or any(
                    db.execute(
                        f"SELECT 1 FROM {table} WHERE conversation=? LIMIT 1", (cid,)
                    ).fetchone()
                    for table in ("requests", "history")
                ):
                    raise ValueError(
                        "shared conversation has been used; refusing to clear its binding"
                    )
                db.execute(
                    "DELETE FROM metadata WHERE key='shared_conversation' AND value=?",
                    (cid,),
                )
                db.execute("DELETE FROM conversations WHERE id=?", (cid,))
                return True
        finally:
            db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    changed = clear_empty_binding(args.database)
    print(
        "Cleared the empty shared binding; private conversations preserved."
        if changed
        else "No shared binding exists."
    )


if __name__ == "__main__":
    main()

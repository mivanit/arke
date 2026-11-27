"""Small, transactional inventory. No Arke, MCP, web, or model dependency."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class Inventory:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL, location TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK(quantity >= 0), notes TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    updated TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    request_id TEXT PRIMARY KEY, payload TEXT NOT NULL, result TEXT NOT NULL
                );
            """)

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def text(value: str, field: str, maximum: int, *, empty: bool = False) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{field} must be text")  # noqa: TRY004 — tool input validation
        value = value.strip()
        if (not empty and not value) or len(value) > maximum:
            raise ValueError(
                f"{field} must contain {'0' if empty else '1'} to {maximum} characters"
            )
        return value

    @staticmethod
    def integer(value: int, field: str, minimum: int, maximum: int) -> int:
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"{field} must be an integer from {minimum} to {maximum}")
        return value

    @staticmethod
    def item(db: sqlite3.Connection, item_id: int) -> dict[str, Any]:
        row = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise ValueError("item not found")
        return dict(row)

    def get_item(self, item_id: int) -> dict[str, Any]:
        self.integer(item_id, "item_id", 1, 2**63 - 1)
        with self.connection() as db:
            return self.item(db, item_id)

    def list_items(
        self, query: str = "", after_id: int = 0, limit: int = 25
    ) -> dict[str, Any]:
        query = self.text(query, "query", 200, empty=True)
        self.integer(after_id, "after_id", 0, 2**63 - 1)
        self.integer(limit, "limit", 1, 100)
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM items WHERE id>? AND instr(lower(name || ' ' || location || ' ' || notes), lower(?))>0 ORDER BY id LIMIT ?",
                (after_id, query, limit + 1),
            ).fetchall()
        items = [dict(row) for row in rows[:limit]]
        return {
            "items": items,
            "next_after_id": items[-1]["id"] if len(rows) > limit else None,
        }

    def add_item(
        self,
        request_id: str,
        name: str,
        location: str,
        quantity: int = 1,
        notes: str = "",
    ) -> dict[str, Any]:
        return self.save(request_id, name, location, quantity, notes)

    def update_item(
        self,
        request_id: str,
        item_id: int,
        expected_version: int,
        name: str,
        location: str,
        quantity: int,
        notes: str = "",
    ) -> dict[str, Any]:
        self.integer(item_id, "item_id", 1, 2**63 - 1)
        self.integer(expected_version, "expected_version", 1, 2**63 - 2)
        return self.save(
            request_id, name, location, quantity, notes, item_id, expected_version
        )

    def save(
        self,
        request_id: str,
        name: str,
        location: str,
        quantity: int,
        notes: str,
        item_id: int | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        request_id = self.text(request_id, "request_id", 100)
        values = (
            self.text(name, "name", 200),
            self.text(location, "location", 200),
            self.integer(quantity, "quantity", 0, 1_000_000),
            self.text(notes, "notes", 2000, empty=True),
        )
        payload = json.dumps([item_id, expected_version, *values])
        with self.connection() as db:
            # Lock before inspecting either a receipt or a version. Concurrent
            # tools cannot both apply the same write or silently lose an edit.
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT payload,result FROM receipts WHERE request_id=?", (request_id,)
            ).fetchone()
            if old:
                if old["payload"] != payload:
                    raise ValueError("request_id already used for different content")
                return json.loads(old["result"])
            if item_id is None:
                cursor = db.execute(
                    "INSERT INTO items(name,location,quantity,notes) VALUES (?,?,?,?)",
                    values,
                )
                item_id = cursor.lastrowid
                assert item_id is not None
            else:
                current = self.item(db, item_id)
                if current["version"] != expected_version:
                    raise ValueError(
                        "item changed; get it again and review the latest version before updating"
                    )
                db.execute(
                    "UPDATE items SET name=?,location=?,quantity=?,notes=?,version=version+1,updated=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                    (*values, item_id),
                )
            result = self.item(db, item_id)
            db.execute(
                "INSERT INTO receipts VALUES (?,?,?)",
                (request_id, payload, json.dumps(result)),
            )
            return result


def serve(path: Path) -> None:
    # Only the tool server needs the MCP SDK; Inventory itself is stdlib-only.
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    inventory = Inventory(path)
    server = MCPServer("inventory")
    read = ToolAnnotations(read_only_hint=True, open_world_hint=False)

    @server.tool(annotations=read)
    def list_items(
        query: str = "", after_id: int = 0, limit: int = 25
    ) -> dict[str, Any]:
        """Search names, locations, and notes. Continue with next_after_id until null."""
        return inventory.list_items(query, after_id, limit)

    @server.tool(annotations=read)
    def get_item(item_id: int) -> dict[str, Any]:
        """Read one item, including its current version for an update."""
        return inventory.get_item(item_id)

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    def add_item(
        request_id: str, name: str, location: str, quantity: int = 1, notes: str = ""
    ) -> dict[str, Any]:
        """Add an inventory record. Choose a unique request_id and reuse it for an identical retry. Quantity is an absolute count, not an increment."""
        return inventory.add_item(request_id, name, location, quantity, notes)

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    def update_item(
        request_id: str,
        item_id: int,
        expected_version: int,
        name: str,
        location: str,
        quantity: int,
        notes: str = "",
    ) -> dict[str, Any]:
        """Replace an item's fields using the version from get_item. Preserve fields the human did not change. Choose a new request_id; reuse it only for an identical retry. On a version conflict, read again and review the change. Use quantity zero for items no longer present."""
        return inventory.update_item(
            request_id, item_id, expected_version, name, location, quantity, notes
        )

    server.run()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Example inventory tools over MCP stdio"
    )
    parser.add_argument("--database", type=Path, required=True)
    serve(parser.parse_args().database)

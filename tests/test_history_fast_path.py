"""Agent-only saves preserve history, receipts, and transaction rollback."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import server


def seed(bus: server.Bus) -> None:
    bus.hook(
        {
            "session_id": "a",
            "event": "SessionStart",
            "names": ["alice"],
            "host": "remote",
        }
    )
    bus.send("human", {"to": ["alice"], "text": "pending"})


@pytest.mark.parametrize(
    "operation",
    [
        "SessionStart",
        "UserPromptSubmit",
        "PostToolUse",
        "Stop",
        "SessionEnd",
        "ack",
        "wake",
    ],
)
def test_empty_inbox_does_not_inspect_retained_history(
    bus: server.Bus, operation: str
) -> None:
    seed(bus)
    bus.acknowledge("a", "", {"ids": [1]})

    class GuardedMessage(dict[str, Any]):
        guard = False

        def __getitem__(self, key: str) -> Any:
            assert not (self.guard and key == "id"), "inspected retained history"
            return super().__getitem__(key)

    row = GuardedMessage(bus.messages[0])
    with bus.change():
        bus.messages[0] = row
        bus.agents["a"]["wake"] = {"status": "held"}
        bus.save()
    cached = bus.store.message_rows
    row.guard = True
    if operation == "ack":
        bus.acknowledge("a", "", {"ids": [1]})
    elif operation == "wake":
        bus.wake_pass()
        assert (
            json.loads(
                bus.store.db.execute(
                    "SELECT data FROM agents WHERE sid='a'"
                ).fetchone()[0]
            )["wake"]["status"]
            == "picked up"
        )
    else:
        bus.hook({"session_id": "a", "event": operation, "receipt": True})
    assert bus.store.message_rows is cached
    assert bus.store.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


@pytest.mark.parametrize("operation", ["hook", "ack"])
def test_failed_agent_save_restores_receipt_and_cache(
    bus: server.Bus, operation: str
) -> None:
    seed(bus)
    cached = bus.store.agent_rows.copy()
    bus.store.db.execute(
        "CREATE TRIGGER reject_save BEFORE INSERT ON agents "
        "BEGIN SELECT RAISE(ABORT, 'failed save'); END"
    )

    def consume() -> None:
        if operation == "hook":
            bus.hook({"session_id": "a", "event": "PostToolUse"})
        else:
            bus.acknowledge("a", "", {"ids": [1]})

    with pytest.raises(sqlite3.IntegrityError, match="failed save"):
        consume()
    assert bus.agents["a"]["inbox"] == [[1, "notify", True]]
    assert bus.store.agent_rows == cached
    assert json.loads(
        bus.store.db.execute("SELECT data FROM agents WHERE sid='a'").fetchone()[0]
    )["inbox"] == [[1, "notify", True]]
    bus.store.db.execute("DROP TRIGGER reject_save")
    consume()
    assert bus.agents["a"]["inbox"] == []


def test_agent_save_then_send_trim_and_backup(bus: server.Bus, tmp_path: Path) -> None:
    seed(bus)
    bus.hook({"session_id": "a", "event": "PostToolUse"})
    assert bus.send("human", {"to": ["alice"], "text": "new"})["id"] == 2
    assert bus.trim_backed_up({1, 2}) == 1
    target = tmp_path / "snapshot.sqlite3"
    bus.store.backup(target)
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT id FROM messages").fetchall() == [(2,)]
        assert json.loads(
            db.execute("SELECT data FROM agents WHERE sid='a'").fetchone()[0]
        )["inbox"] == [[2, "notify", True]]


@pytest.mark.parametrize("mutation", ["append", "trim", "append-and-trim"])
def test_agent_only_scope_rejects_history_changes(
    bus: server.Bus, mutation: str
) -> None:
    seed(bus)
    with pytest.raises(AssertionError), bus.change(messages_changed=False):
        if mutation in ("append", "append-and-trim"):
            bus.messages.append(dict(bus.messages[0], id=bus.next_id))
            bus.next_id += 1
        if mutation in ("trim", "append-and-trim"):
            bus.messages = bus.messages[1:]
        bus.save()
    assert [m["id"] for m in bus.messages] == [1]
    assert bus.next_id == 2
    assert bus.store.db.execute("SELECT id FROM messages").fetchall() == [(1,)]


def test_wake_looks_up_only_notifications_and_persists_result(bus: server.Bus) -> None:
    seed(bus)
    with bus.change():
        bus.agents["a"].update(
            host=server.LOCAL_HOST,
            state="idle",
            tmux="/fake:%1",
            inbox=[[1, "notify", True], [1, "board", False], [99, "notify", True]],
        )
        bus.save()
    cached = bus.store.message_rows
    with (
        patch.object(bus, "message", wraps=bus.message) as lookup,
        patch.object(bus, "try_wake", return_value={"status": "held"}) as wake,
    ):
        bus.wake_pass()
    assert [call.args[0] for call in lookup.call_args_list] == [1, 99]
    assert wake.call_args.args[2] == [bus.messages[0]]
    assert bus.store.message_rows is cached
    restored = server.Bus(bus.state_file)
    try:
        assert restored.agents["a"]["wake"]["status"] == "held"
        assert restored.messages == bus.messages
    finally:
        restored.store.close()

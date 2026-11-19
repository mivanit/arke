"""Mail preparation and ownership boundaries; temporary state only."""

from __future__ import annotations

import copy
from typing import Any

import pytest
import server


def start(
    bus: server.Bus, sid: str = "a", name: str = "alice", **extra: Any
) -> server.Agent:
    bus.hook(
        {
            "session_id": sid,
            "event": "SessionStart",
            "names": [name],
            "host": "remote",
            "repo": "p",
            **extra,
        }
    )
    return bus.agents[sid]


@pytest.mark.parametrize("everything", [False, True])
def test_preparing_mail_preserves_the_inbox(bus: server.Bus, everything: bool) -> None:
    agent: server.Agent = start(bus)
    bus.send("human", {"to": ["/p"], "text": "board", "mode": "board"})
    bus.send("human", {"to": ["/p"], "text": "project notification"})
    bus.send("human", {"to": ["alice"], "text": "direct task"})
    pending: list[Any] = agent["inbox"]
    before: server.Agent = copy.deepcopy(agent)
    with bus.change():
        text, ids = bus.prepare_delivery(agent, everything)
        assert bus.prepare_delivery(agent, everything) == (text, ids)
        assert agent == before
        assert agent["inbox"] is pending
    assert "direct task" in text
    assert ("project notification" in text) == everything
    assert ("new board post" in text) == everything
    assert ids == ([1, 2, 3] if everything else [3])
    bus.acknowledge("a", "", {"ids": ids})
    assert agent["inbox"] == (
        [] if everything else [[1, "board", False], [2, "notify", False]]
    )


@pytest.mark.parametrize("visible_mail", [False, True])
@pytest.mark.parametrize("receipt", [False, True])
def test_obsolete_board_entries_keep_existing_receipt_behavior(
    bus: server.Bus, visible_mail: bool, receipt: bool
) -> None:
    agent: server.Agent = start(bus)
    bus.send("human", {"to": ["/p"], "text": "old board", "mode": "board"})
    if visible_mail:
        bus.send("human", {"to": ["alice"], "text": "direct task"})
    agent["projects"] = {}
    pending: list[Any] = copy.deepcopy(agent["inbox"])
    with bus.change():
        text, ids = bus.prepare_delivery(agent, True)
        assert agent["inbox"] == pending
    assert bool(text) == visible_mail
    assert ids == ([1, 2] if visible_mail else [1])
    out: dict[str, Any] = bus.hook(
        {"session_id": "a", "event": "PostToolUse", "receipt": receipt}
    )
    if receipt and visible_mail:
        # The old receipt includes obsolete entries when there is visible mail alongside them.
        assert out["delivery"] == {"sid": "a", "ids": [1, 2]}
        assert agent["inbox"] == pending
    else:
        assert "delivery" not in out
        assert agent["inbox"] == []


@pytest.mark.parametrize("replacement", ["seat", "rename", "reboot"])
def test_old_hook_receipt_cannot_acknowledge_inherited_mail(
    bus: server.Bus, replacement: str
) -> None:
    start(bus, process_token="remote:10:1", boot="boot-1", tmux="/fake:%1")
    bus.send("human", {"to": ["alice"], "text": "pending task"})
    receipt: dict[str, Any] = bus.hook(
        {"session_id": "a", "event": "PostToolUse", "receipt": True}
    )["delivery"]
    if replacement == "rename":
        bus.hook({"session_id": "a", "event": "SessionEnd"})
        start(bus, "b", "bob", process_token="remote:20:2")
        bus.rename("b", "", {"name": "alice"})
    else:
        start(
            bus,
            "b",
            process_token="remote:20:2",
            boot="boot-2" if replacement == "reboot" else "boot-1",
            tmux="/fake:%2" if replacement == "reboot" else "/fake:%1",
        )
    before: server.Agent = copy.deepcopy(bus.agents["b"])
    with pytest.raises(server.HttpError, match="unknown session"):
        bus.acknowledge(receipt["sid"], "", {"ids": receipt["ids"]})
    for event in ("SessionStart", "PostToolUse", "Stop", "SessionEnd"):
        out: dict[str, Any] = bus.hook(
            {"session_id": "a", "event": event, "process_token": "remote:10:1"}
        )
        assert out["ignored"]
    assert bus.agents["b"] == before
    assert before["inbox"] == [[1, "notify", True]]
    bus.acknowledge("b", "", {"ids": receipt["ids"]})
    assert bus.agents["b"]["inbox"] == []


def test_mail_lookup_handles_trimmed_gaps_restart_and_new_sends(
    bus: server.Bus,
) -> None:
    agent: server.Agent = start(bus)
    for n in range(1, 6):
        bus.send("human", {"to": ["alice"], "text": f"task {n}"})
    bus.acknowledge("a", "", {"ids": [1, 3, 5]})
    with bus.change():  # history with gaps: an older version could trim read mail
        bus.messages = [m for m in bus.messages if m["id"] not in {1, 3, 5}]
        bus.save()
    assert [m["id"] for m in bus.inbox(agent, 0, receipt=True)["messages"]] == [2, 4]
    bus.store.close()
    restored: server.Bus = server.Bus(bus.state_file)
    try:
        restored.send("human", {"to": ["alice"], "text": "task 6"})
        agent = restored.agents["a"]
        # Exercise both sides of the list and a trimmed gap, without shifting returned IDs.
        agent["inbox"].extend(
            [[1, "notify", True], [3, "notify", True], [99, "notify", True]]
        )
        assert [
            m["id"] for m in restored.inbox(agent, 0, receipt=True)["messages"]
        ] == [2, 4, 6]
        out: dict[str, Any] = restored.hook(
            {"session_id": "a", "event": "PostToolUse", "receipt": True}
        )
        for n in (2, 4, 6):
            assert f"task {n}" in out["context"]
        for n in (1, 3, 5):
            assert f"task {n}" not in out["context"]
        restored.acknowledge("a", "", {"ids": out["delivery"]["ids"]})
        assert agent["inbox"] == []
    finally:
        restored.store.close()

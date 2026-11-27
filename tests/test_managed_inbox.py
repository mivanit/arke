"""Managed mail pages fit both directions of the worker/MCP wire contract."""

from __future__ import annotations

import json
from typing import Any

import pytest
import server
from mcp.server.mcpserver.utilities.func_metadata import func_metadata

from arke.managed_mail import encoded_size, mailbox_page
from arke.wire import MAX_FRAME, encode_frame


def mcp_event(page: dict[str, Any]) -> dict[str, Any]:
    def inbox() -> Any:
        return page

    result = (
        func_metadata(inbox).convert_result(page).model_dump(mode="json", by_alias=True)
    )
    return {
        "event": {
            "kind": "tool",
            "data": {"details": {"type": "mcpToolCall", "result": result}},
        }
    }


@pytest.mark.parametrize("character", ["x", "\0", '"', "\\", "😀"])
def test_complete_pages_fit_nested_mcp_encoding(character: str) -> None:
    text = character * (100_000 // len(character.encode()))
    remaining = [{"id": n, "text": text} for n in range(1, 22)]
    received = []
    while remaining:
        page = mailbox_page("managed-fixture", remaining)
        assert page["messages"] and not page["oversized"]
        assert len(encode_frame({"bus_reply": 1, "result": page})) <= MAX_FRAME
        actual = len(encode_frame(mcp_event(page)))
        assert actual < encoded_size(page) <= MAX_FRAME
        ids = page["delivery"]["ids"]
        assert ids == [item["id"] for item in page["messages"]]
        assert all(item["text"] == text for item in page["messages"])
        received.extend(ids)
        remaining = [item for item in remaining if item["id"] not in ids]
        assert page["more"] == bool(remaining)
    assert received == list(range(1, 22))


def test_oversized_records_are_bounded_unacknowledged_and_do_not_loop() -> None:
    oversized = [{"id": n, "text": "x" * MAX_FRAME} for n in range(20)]
    page = mailbox_page("managed-fixture", oversized + [{"id": 21, "text": "small"}])
    assert page["delivery"]["ids"] == [21]
    assert not page["more"]
    assert page["oversized_count"] == 20
    assert len(page["oversized"]) == 8
    assert len(json.dumps(page)) < 5000
    page = mailbox_page("managed-fixture", oversized)
    assert not page["messages"] and not page["delivery"]["ids"] and not page["more"]


def test_managed_pages_preserve_receipts_and_new_mail(bus: server.Bus) -> None:
    with bus.change():
        agent = bus.register("managed-test", {"names": ["reader"], "kind": "claude"})
        agent["managed"] = "fixture"
        bus.save()
    for number in range(12):
        bus.send("human", {"to": ["reader"], "text": str(number) + "x" * 99_990})
    legacy = bus.inbox(agent, 0, True)
    assert len(legacy["messages"]) == 12
    first = bus.managed_inbox(agent)
    assert first["more"] and len(first["messages"]) < 12
    # Reading again (including a disconnected caller) never acknowledges by itself.
    assert bus.managed_inbox(agent) == first
    bus.send("human", {"to": ["reader"], "text": "arrived after read"})
    bus.acknowledge("managed-test", "", {"ids": first["delivery"]["ids"]})
    seen = list(first["delivery"]["ids"])
    while agent["inbox"]:
        page = bus.managed_inbox(agent)
        ids = page["delivery"]["ids"]
        assert ids and not set(ids).intersection(seen)
        seen.extend(ids)
        bus.acknowledge("managed-test", "", {"ids": ids})
    assert len(seen) == 13
    assert not bus.managed_inbox(agent)["more"]


def test_shutdown_wakes_existing_native_listener(bus: server.Bus) -> None:
    from tests.test_native_wake import agent, waiter

    agent(bus)
    with waiter(bus, wait=20) as replies:
        bus.begin_shutdown()
    assert len(replies) == 1
    assert isinstance(replies[0], server.HttpError) and replies[0].status == 503

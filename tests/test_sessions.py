"""Conversation ordering, crash recovery, and permission races without model calls."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from arke.harnesses import Event, Launch
from arke.sessions import Conflict, Conversations


class FakeAdapter:
    def __init__(self, launch: Launch) -> None:
        self.launch = launch
        self.sent: list[str] = []
        self.events: asyncio.Queue[Event | Exception] = asyncio.Queue()
        self.interrupts = 0
        self.answers: list[tuple[str, dict[str, Any]]] = []
        self.closed = False
        self.opened: list[str | None] = []

    async def open(self, native_id: str | None) -> str:
        self.opened.append(native_id)
        return native_id or "native-session"

    async def run(self, text: str) -> AsyncIterator[Event]:
        self.sent.append(text)
        yield Event("started")
        while True:
            event = await self.events.get()
            if isinstance(event, Exception):
                raise event
            yield event
            if event.kind == "completed":
                return

    async def interrupt(self) -> None:
        self.interrupts += 1
        self.events.put_nowait(Event("completed", {"status": "interrupted"}))

    async def respond(self, request_id: str, answer: dict[str, Any]) -> None:
        self.answers.append((request_id, answer))

    async def close(self) -> None:
        self.closed = True

    def finish(self) -> None:
        self.events.put_nowait(Event("message", {"text": "done"}))
        self.events.put_nowait(Event("completed", {"status": "completed"}))


async def until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(3):
        while not condition():
            await asyncio.sleep(0.001)


def test_serial_queue_redirect_and_receipts(tmp_path: Path) -> None:
    async def scenario() -> None:
        fake = FakeAdapter(Launch("codex", tmp_path))
        manager = Conversations(
            tmp_path / "sessions.db", {"code": fake.launch}, lambda _: fake
        )
        cid = manager.create("code")
        try:
            manager.submit(cid, "one", "first")
            manager.submit(cid, "two", "older queued")
            await until(lambda: fake.sent == ["first"])
            assert manager.submit(cid, "one", "first")["status"] == "running"
            with pytest.raises(Conflict):
                manager.submit(cid, "one", "different")
            with pytest.raises(Conflict):
                manager.submit(cid, "bad", "redirect", redirect=True, target="stale")
            manager.submit(cid, "three", "new direction", redirect=True, target="one")
            await until(lambda: fake.sent == ["first", "new direction"])
            assert fake.interrupts == 1
            fake.finish()
            await until(lambda: manager.snapshot(cid)["active"] is None)
            assert manager.snapshot(cid)["paused"]
            assert fake.sent == ["first", "new direction"]
            assert (
                manager.submit(
                    cid, "three", "new direction", redirect=True, target="one"
                )["status"]
                == "completed"
            )
            manager.resume(cid)
            await until(lambda: len(fake.sent) == 3)
            assert fake.sent[-1] == "older queued"
            fake.finish()
            await until(lambda: manager.snapshot(cid)["active"] is None)
        finally:
            await manager.close()
        assert fake.closed

    asyncio.run(scenario())


def test_restart_pauses_queue_and_does_not_retry_uncertain_send(tmp_path: Path) -> None:
    async def scenario() -> None:
        launch = Launch("codex", tmp_path)
        first = FakeAdapter(launch)
        manager = Conversations(
            tmp_path / "state.db", {"code": launch}, lambda _: first
        )
        cid = manager.create("code")
        manager.submit(cid, "one", "possibly delivered")
        manager.submit(cid, "two", "waiting")
        await until(lambda: bool(first.sent))
        first.events.put_nowait(ConnectionError("lost acknowledgement"))
        await until(lambda: manager.snapshot(cid)["error"] == "lost acknowledgement")
        await manager.close()
        second = FakeAdapter(launch)
        manager = Conversations(
            tmp_path / "state.db", {"code": launch}, lambda _: second
        )
        try:
            assert manager.snapshot(cid)["paused"]
            manager.submit(cid, "three", "new input after restart")
            await asyncio.sleep(0)
            assert not second.sent and not second.opened
            with pytest.raises(Conflict):
                manager.resume(cid)
            assert (
                manager.submit(cid, "one", "possibly delivered")["status"]
                == "uncertain"
            )
            manager.resolve(cid, "one")
            manager.discard(cid, "two")
            manager.resume(cid)
            await until(lambda: bool(second.sent))
            assert second.sent == ["new input after restart"]
            assert second.opened == ["native-session"]
            second.finish()
            await until(lambda: manager.snapshot(cid)["active"] is None)
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_late_approvals_cannot_authorize_next_turn(tmp_path: Path) -> None:
    async def scenario() -> None:
        fake = FakeAdapter(Launch("claude", tmp_path))
        manager = Conversations(
            tmp_path / "state.db", {"code": fake.launch}, lambda _: fake
        )
        cid = manager.create("code")
        try:
            manager.submit(cid, "one", "first")
            await until(lambda: bool(fake.sent))
            fake.events.put_nowait(
                Event(
                    "approval",
                    {
                        "request_id": "native-request",
                        "kind": "permission",
                        "details": {"command": "write"},
                    },
                )
            )
            await until(lambda: bool(manager.snapshot(cid)["approvals"]))
            aid = manager.snapshot(cid)["approvals"][0]["id"]
            manager.submit(cid, "two", "redirect", redirect=True, target="one")
            await until(lambda: len(fake.sent) == 2)
            with pytest.raises(Conflict):
                await manager.answer(cid, aid, {"allow": True})
            assert fake.answers == []
            fake.finish()
            await until(lambda: manager.snapshot(cid)["active"] is None)
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_single_owner_and_changed_workspace(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = Conversations(
            tmp_path / "state.db", {"code": Launch("codex", tmp_path)}
        )
        cid = manager.create("code")
        with pytest.raises(RuntimeError, match="another"):
            Conversations(tmp_path / "state.db", {})
        await manager.close()
        other = tmp_path / "other"
        other.mkdir()
        fake = FakeAdapter(Launch("codex", other))
        manager = Conversations(
            tmp_path / "state.db", {"code": fake.launch}, lambda _: fake
        )
        try:
            manager.submit(cid, "one", "hello")
            manager.resume(cid)
            await until(lambda: bool(manager.snapshot(cid)["error"]))
            assert "workspace changed" in manager.snapshot(cid)["error"]
            assert fake.opened == []
        finally:
            await manager.close()

    asyncio.run(scenario())

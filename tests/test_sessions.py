"""Conversation ordering, crash recovery, and permission races without model calls."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

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
            assert manager.snapshot(cid)["held"]
            assert not manager.snapshot(cid)["paused"]
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
            with pytest.raises(ValueError, match="binding changed"):
                manager.resume(cid)
            assert manager.snapshot(cid)["paused"]
            assert fake.opened == []
        finally:
            await manager.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("older", [False, True])
def test_redirect_holds_only_older_messages(tmp_path: Path, older: bool) -> None:
    async def scenario() -> None:
        fake = FakeAdapter(Launch("codex", tmp_path))
        manager = Conversations(
            tmp_path / "state.db", {"code": fake.launch}, lambda _: fake
        )
        cid = manager.create("code")
        try:
            manager.submit(cid, "first", "first")
            await until(lambda: fake.sent == ["first"])
            if older:
                manager.submit(cid, "old", "old")
            manager.submit(cid, "redirect", "redirect", redirect=True, target="first")
            manager.submit(cid, "during", "during")
            await until(lambda: fake.sent == ["first", "redirect"])
            assert manager.snapshot(cid)["held"] is older
            assert not manager.snapshot(cid)["paused"]
            fake.finish()
            await until(lambda: fake.sent == ["first", "redirect", "during"])
            fake.finish()
            await until(lambda: manager.snapshot(cid)["active"] is None)
            manager.submit(cid, "after", "after")
            await until(lambda: fake.sent[-1] == "after")
            fake.finish()
            await until(lambda: manager.snapshot(cid)["active"] is None)
            if older:
                assert manager.snapshot(cid)["queue"][0]["status"] == "held"
                manager.discard(cid, "old")
                assert not manager.snapshot(cid)["held"]
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_full_queue_still_accepts_redirect(tmp_path: Path) -> None:
    async def scenario() -> None:
        fake = FakeAdapter(Launch("codex", tmp_path))
        manager = Conversations(
            tmp_path / "state.db", {"code": fake.launch}, lambda _: fake
        )
        cid = manager.create("code")
        try:
            manager.submit(cid, "first", "first")
            await until(lambda: bool(fake.sent))
            for index in range(100):
                manager.submit(cid, str(index), "older")
            with pytest.raises(Conflict, match="queue is full"):
                manager.submit(cid, "extra", "extra")
            manager.submit(cid, "redirect", "urgent", redirect=True, target="first")
            await until(lambda: fake.sent == ["first", "urgent"])
            assert (
                sum(r["status"] == "held" for r in manager.snapshot(cid)["queue"])
                == 100
            )
            fake.finish()
            await until(lambda: manager.snapshot(cid)["active"] is None)
        finally:
            await manager.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("acknowledged", [True, False])
def test_unfinished_interrupt_pauses_without_running_redirect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, acknowledged: bool
) -> None:
    from arke import sessions

    monkeypatch.setattr(sessions, "INTERRUPT_TIMEOUT", 0.02)
    monkeypatch.setattr(sessions, "INTERRUPT_COMPLETION_TIMEOUT", 0.02)

    class StuckAdapter(FakeAdapter):
        async def interrupt(self) -> None:
            self.interrupts += 1
            if not acknowledged:
                await asyncio.Event().wait()

    async def scenario() -> None:
        fake = StuckAdapter(Launch("codex", tmp_path))
        manager = Conversations(
            tmp_path / "state.db", {"code": fake.launch}, lambda _: fake
        )
        cid = manager.create("code")
        try:
            manager.submit(cid, "first", "first")
            await until(lambda: bool(fake.sent))
            manager.submit(cid, "redirect", "redirect", redirect=True, target="first")
            await until(lambda: fake.closed)
            assert manager.snapshot(cid)["paused"]
            assert "Could not confirm interruption" in manager.snapshot(cid)["error"]
            assert fake.sent == ["first"]
            queue = {r["id"]: r["status"] for r in manager.snapshot(cid)["queue"]}
            assert queue == {"first": "uncertain", "redirect": "queued"}
            # Adapter.close returning is earlier than the actor's done callback,
            # which owns publishing readiness for Resume.
            await until(lambda: manager._rt(cid).task is None)
            with pytest.raises(Conflict, match="uncertain"):
                manager.resume(cid)
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_held_requests_survive_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        fake = FakeAdapter(Launch("codex", tmp_path))
        manager = Conversations(
            tmp_path / "state.db", {"code": fake.launch}, lambda _: fake
        )
        cid = manager.create("code")
        manager.submit(cid, "first", "first")
        await until(lambda: bool(fake.sent))
        manager.submit(cid, "old", "old")
        manager.submit(cid, "redirect", "redirect", redirect=True, target="first")
        await until(lambda: len(fake.sent) == 2)
        fake.finish()
        await until(lambda: manager.snapshot(cid)["active"] is None)
        await manager.close()
        new = FakeAdapter(fake.launch)
        manager = Conversations(
            tmp_path / "state.db", {"code": new.launch}, lambda _: new
        )
        try:
            assert manager.snapshot(cid)["paused"] and manager.snapshot(cid)["held"]
            manager.submit(cid, "fresh", "fresh")
            await asyncio.sleep(0)
            assert not new.sent
            manager.resume(cid)
            await until(lambda: new.sent == ["old"])
            new.finish()
            await until(lambda: new.sent == ["old", "fresh"])
            new.finish()
            await until(lambda: manager.snapshot(cid)["active"] is None)
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_sharing_creates_fresh_persisted_identity(tmp_path: Path) -> None:
    async def scenario() -> None:
        launch = Launch("codex", tmp_path)
        profiles = {"code": launch, "other": launch}
        manager = Conversations(tmp_path / "state.db", profiles)
        private = manager.create("code", "Private")
        manager._record(
            private, "secret", Event("message", {"text": "private fixture"})
        )
        shared = manager.get_or_create_shared("code", "Household")
        assert private != shared
        assert manager.history(shared) == []
        assert manager.get_or_create_shared("code") == shared
        await manager.close()
        manager = Conversations(tmp_path / "state.db", profiles)
        try:
            assert manager.get_or_create_shared("code") == shared
            with pytest.raises(ValueError, match="shared conversation"):
                manager.get_or_create_shared("other")
            assert len(manager.list_conversations()) == 2
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_cancelled_close_caller_does_not_abandon_cleanup(tmp_path: Path) -> None:
    class SlowClose(FakeAdapter):
        async def close(self) -> None:
            await asyncio.sleep(0.02)
            await super().close()

    async def scenario() -> None:
        fake = SlowClose(Launch("codex", tmp_path))
        manager = Conversations(
            tmp_path / "state.db", {"code": fake.launch}, lambda _: fake
        )
        cid = manager.create("code")
        manager.submit(cid, "first", "first")
        await until(lambda: bool(fake.sent))
        closing = asyncio.create_task(manager.close())
        await asyncio.sleep(0)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert fake.closed, "caller cancellation must wait for owned cleanup"
        await manager.close()
        await manager.close()
        assert fake.closed
        replacement = Conversations(tmp_path / "state.db", {"code": fake.launch})
        await replacement.close()

    asyncio.run(scenario())


def test_shutdown_cancels_open_and_releases_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arke import sessions

    monkeypatch.setattr(sessions, "SHUTDOWN_GRACE", 0.01)

    class StuckOpen(FakeAdapter):
        async def open(self, native_id: str | None) -> str:
            self.opened.append(native_id)
            await asyncio.Event().wait()
            return "unreachable"

    async def scenario() -> None:
        fake = StuckOpen(Launch("codex", tmp_path))
        manager = Conversations(
            tmp_path / "state.db", {"code": fake.launch}, lambda _: fake
        )
        cid = manager.create("code")
        manager.submit(cid, "first", "first")
        await until(lambda: bool(fake.opened))
        await manager.close()
        assert fake.closed and not fake.sent
        replacement = Conversations(tmp_path / "state.db", {"code": fake.launch})
        try:
            assert replacement.snapshot(cid)["queue"][0]["status"] == "queued"
        finally:
            await replacement.close()

    asyncio.run(scenario())


def test_claude_cancelled_consumer_cancels_receiver(tmp_path: Path) -> None:
    from claude_agent_sdk import ClaudeSDKClient

    from arke.harnesses.claude import Claude

    class Client:
        async def query(self, text: str) -> None:
            pass

        async def receive_response(self) -> AsyncIterator[Any]:
            await asyncio.Event().wait()
            yield None

    async def scenario() -> None:
        native = Claude(Launch("claude", tmp_path))
        native.client = cast(ClaudeSDKClient, Client())

        async def consume() -> None:
            async for _ in native.run("hello"):
                pass

        consumer = asyncio.create_task(consume())
        await until(lambda: native.receiver is not None)
        receiver = native.receiver
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(consumer, 1)
        assert receiver is not None and receiver.done()
        assert native.receiver is None

    asyncio.run(scenario())


def test_failed_cleanup_keeps_exclusive_ownership(tmp_path: Path) -> None:
    class FailedClose(FakeAdapter):
        async def close(self) -> None:
            raise RuntimeError("native process could not be stopped")

    async def scenario() -> None:
        fake = FailedClose(Launch("codex", tmp_path))
        manager = Conversations(
            tmp_path / "state.db", {"code": fake.launch}, lambda _: fake
        )
        cid = manager.create("code")
        manager.submit(cid, "first", "first")
        await until(lambda: bool(fake.sent))
        try:
            with pytest.raises(RuntimeError, match="could not be stopped"):
                await manager.close()
            with pytest.raises(RuntimeError, match="another"):
                Conversations(tmp_path / "state.db", {})
            with pytest.raises(RuntimeError, match="could not be stopped"):
                await manager.close()
            assert manager.snapshot(cid)["paused"]
        finally:
            # This fake owns no OS process; release the deliberately retained lock.
            manager.db.close()
            manager._lock.close()

    asyncio.run(scenario())


def test_turn_finishing_during_redirect_does_not_interrupt_next_turn(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fake = FakeAdapter(Launch("codex", tmp_path))
        manager = Conversations(
            tmp_path / "state.db", {"code": fake.launch}, lambda _: fake
        )
        cid = manager.create("code")
        try:
            manager.submit(cid, "first", "first")
            await until(lambda: bool(fake.sent))
            # Wake the native consumer before the interrupt task gets scheduled.
            fake.finish()
            manager.submit(cid, "redirect", "redirect", redirect=True, target="first")
            await until(lambda: fake.sent == ["first", "redirect"])
            assert fake.interrupts == 0
            assert not manager.snapshot(cid)["paused"]
            fake.finish()
            await until(lambda: manager.snapshot(cid)["active"] is None)
        finally:
            await manager.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["runner", "native_home"])
def test_execution_binding_cannot_change_on_resume_or_sharing(
    tmp_path: Path, change: str
) -> None:
    async def scenario() -> None:
        launch = Launch(
            "codex", tmp_path, runner="container", native_home=tmp_path / "native"
        )
        manager = Conversations(tmp_path / "state.db", {"code": launch})
        cid = manager.get_or_create_shared("code")
        await manager.close()
        changed = (
            replace(launch, runner="local")
            if change == "runner"
            else replace(launch, native_home=tmp_path / "other-native")
        )
        fake = FakeAdapter(changed)
        manager = Conversations(
            tmp_path / "state.db", {"code": changed}, lambda _: fake
        )
        try:
            with pytest.raises(ValueError, match="execution binding changed"):
                manager.get_or_create_shared("code")
            manager.submit(cid, "new", "must not run")
            with pytest.raises(ValueError, match="execution binding changed"):
                manager.resume(cid)
            await asyncio.sleep(0)
            assert not fake.opened and manager.snapshot(cid)["paused"]
            assert "native_home" not in manager.snapshot(cid)
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_legacy_execution_binding_migrates_without_adopting_current_profile(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE conversations (id TEXT PRIMARY KEY, name TEXT NOT NULL, profile TEXT NOT NULL, harness TEXT NOT NULL, cwd TEXT NOT NULL, native_id TEXT, paused INTEGER NOT NULL, state TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', created REAL NOT NULL)"
        )
        db.execute(
            "INSERT INTO conversations VALUES ('legacy','Legacy','code','codex',?,'saved-session',0,'idle','',0)",
            (str(tmp_path.resolve()),),
        )

    async def scenario() -> None:
        launch = Launch(
            "codex", tmp_path, runner="container", native_home=tmp_path / "native"
        )
        manager = Conversations(path, {"code": launch})
        try:
            row = manager._row("legacy")
            assert row["runner"] == "local" and row["native_home"] is None
            with pytest.raises(ValueError, match="execution binding changed"):
                manager.resume("legacy")
            manager.profiles["code"] = Launch("codex", tmp_path)
            manager.resume("legacy")
            assert not manager.snapshot("legacy")["paused"]
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_known_cleanup_failure_rejects_before_saving_or_holding_input(
    tmp_path: Path,
) -> None:
    class FailedClose(FakeAdapter):
        async def close(self) -> None:
            raise RuntimeError("fixture cleanup failure")

    async def scenario() -> None:
        fake = FailedClose(Launch("codex", tmp_path))
        manager = Conversations(
            tmp_path / "state.db", {"code": fake.launch}, lambda _: fake
        )
        cid = manager.create("code")
        manager.submit(cid, "first", "first")
        manager.submit(cid, "older", "older")
        await until(lambda: bool(fake.sent))
        fake.events.put_nowait(ConnectionError("fixture native error"))
        rt = manager._rt(cid)
        await until(lambda: rt.task is not None and rt.task.done())
        try:
            before = manager.snapshot(cid)["queue"]
            with pytest.raises(Conflict, match="cleanup failed"):
                manager.submit(cid, "rejected", "must not be saved")
            with pytest.raises(Conflict, match="cleanup failed"):
                manager.submit(
                    cid,
                    "rejected-redirect",
                    "must not hold older",
                    redirect=True,
                    target="first",
                )
            assert manager.snapshot(cid)["queue"] == before
            assert not manager.db.execute(
                "SELECT 1 FROM requests WHERE id LIKE 'rejected%'"
            ).fetchone()
            # An earlier accepted request remains idempotently queryable even
            # though this execution cannot accept additional input.
            assert manager.submit(cid, "older", "older")["status"] == "queued"
            with pytest.raises(RuntimeError, match="fixture cleanup failure"):
                await manager.close()
        finally:
            manager.db.close()
            manager._lock.close()

    asyncio.run(scenario())


def test_failure_after_commit_preserves_accepted_request_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        fake = FakeAdapter(Launch("codex", tmp_path))
        manager = Conversations(
            tmp_path / "state.db", {"code": fake.launch}, lambda _: fake
        )
        cid = manager.create("code")
        schedule = manager._ensure_worker

        def unavailable(cid: str) -> None:
            raise RuntimeError("fixture scheduler unavailable")

        try:
            monkeypatch.setattr(manager, "_ensure_worker", unavailable)
            assert manager.submit(cid, "accepted", "hello") == {
                "id": "accepted",
                "status": "queued",
            }
            assert manager.snapshot(cid)["paused"]
            assert "Message accepted" in manager.snapshot(cid)["error"]
            assert manager.submit(cid, "accepted", "hello")["status"] == "queued"
            assert (
                manager.db.execute("SELECT count(*) FROM requests").fetchone()[0] == 1
            )
            monkeypatch.setattr(manager, "_ensure_worker", schedule)
            manager.resume(cid)
            await until(lambda: fake.sent == ["hello"])
            fake.finish()
            await until(lambda: manager.snapshot(cid)["active"] is None)
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_worker_retirement_owns_readiness_and_ignores_stale_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        first = FakeAdapter(Launch("codex", tmp_path))
        second = FakeAdapter(first.launch)
        adapters = iter([first, second])
        manager = Conversations(
            tmp_path / "state.db", {"code": first.launch}, lambda _: next(adapters)
        )
        cid = manager.create("code")
        finished: list[asyncio.Task[None]] = []
        retire = manager._worker_finished
        monkeypatch.setattr(
            manager, "_worker_finished", lambda cid, task: finished.append(task)
        )
        try:
            manager.submit(cid, "first", "first")
            await until(lambda: first.sent == ["first"])
            first.events.put_nowait(ConnectionError("fixture native error"))
            await until(lambda: bool(finished))
            old_task = finished[0]
            assert old_task.done() and first.closed
            assert manager.snapshot(cid)["stopping"]
            manager.resolve(cid, "first")
            with pytest.raises(Conflict, match="still stopping"):
                manager.resume(cid)
            assert manager.snapshot(cid)["paused"]
            # Ordinary input is durably accepted during cleanup, but cannot
            # accidentally release the recovery pause or acquire a second actor.
            assert manager.submit(cid, "second", "second")["status"] == "queued"
            assert manager._rt(cid).task is old_task and not second.sent
            monkeypatch.setattr(manager, "_worker_finished", retire)
            retire(cid, old_task)
            assert not manager.snapshot(cid)["stopping"]
            assert manager.snapshot(cid)["active"] is None
            manager.resume(cid)
            replacement = manager._rt(cid).task
            assert replacement is not None and replacement is not old_task
            retire(cid, old_task)
            assert manager._rt(cid).task is replacement
            await until(lambda: second.sent == ["second"])
            second.finish()
            await until(lambda: manager.snapshot(cid)["active"] is None)
        finally:
            monkeypatch.setattr(manager, "_worker_finished", retire)
            await manager.close()

    asyncio.run(scenario())


def test_effective_local_native_roots_are_recorded_and_cannot_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    async def scenario() -> None:
        original = tmp_path / "native-a"
        monkeypatch.setenv("CODEX_HOME", str(original))
        launch = Launch("codex", tmp_path)
        manager = Conversations(tmp_path / "state.db", {"code": launch})
        cid = manager.create("code")
        assert json.loads(manager._row(cid)["native_roots"]) == [str(original)]
        assert "native_roots" not in manager.snapshot(cid)
        await manager.close()
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "native-b"))
        manager = Conversations(tmp_path / "state.db", {"code": launch})
        try:
            with pytest.raises(ValueError, match="binding changed"):
                manager.resume(cid)
            assert manager.snapshot(cid)["paused"]
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_async_launch_preparation_finishes_before_native_open(tmp_path: Path) -> None:
    async def scenario() -> None:
        fake = FakeAdapter(Launch("codex", tmp_path))
        manager = Conversations(
            tmp_path / "state.db", {"code": fake.launch}, lambda _: fake
        )
        cid = manager.create("code")
        preparing, release = asyncio.Event(), asyncio.Event()

        async def prepare(cid: str, launch: Launch) -> Launch:
            preparing.set()
            await release.wait()
            return launch

        manager.prepare_launch = prepare
        try:
            manager.submit(cid, "first", "first")
            await preparing.wait()
            assert not fake.opened and not fake.sent
            release.set()
            await until(lambda: fake.sent == ["first"])
            fake.finish()
            await until(lambda: manager.snapshot(cid)["active"] is None)
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_standalone_manager_uses_container_runner_and_protects_its_database(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        workspace, home = tmp_path / "workspace", tmp_path / "native"
        workspace.mkdir()
        home.mkdir()
        manager = Conversations(
            workspace / "state.sqlite3",
            {"home": Launch("codex", workspace, runner="container", native_home=home)},
        )
        cid = manager.create("home")
        try:
            manager.submit(cid, "one", "must never launch on the host")
            await until(lambda: bool(manager.snapshot(cid)["error"]))
            assert "overlaps private service state" in manager.snapshot(cid)["error"]
            assert manager.snapshot(cid)["queue"][0]["status"] == "queued"
            assert not manager.history(cid)
        finally:
            await manager.close()

    asyncio.run(scenario())

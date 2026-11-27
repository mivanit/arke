"""Lifecycle controls at native submission, cleanup, and browser boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from arke.harnesses import Event, Launch
from arke.sessions import Conflict, Conversations
from arke.webapp import create_app
from tests.test_conversation_web import CHAT, OWNER, config
from tests.test_sessions import FakeAdapter, until


def test_graceful_stop_keeps_approval_and_queue_until_explicit_resume(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        first = FakeAdapter(Launch("codex", tmp_path))
        second = FakeAdapter(first.launch)
        adapters = iter([first, second])
        manager = Conversations(
            tmp_path / "state.db", {"code": first.launch}, lambda _: next(adapters)
        )
        cid = manager.create("code")
        try:
            manager.submit(cid, "one", "first")
            manager.submit(cid, "two", "second")
            await until(lambda: bool(first.sent))
            first.events.put_nowait(
                Event(
                    "approval",
                    {"request_id": "native", "kind": "permission", "details": {}},
                )
            )
            await until(lambda: bool(manager.snapshot(cid)["approvals"]))
            aid = manager.snapshot(cid)["approvals"][0]["id"]
            manager.stop(cid, expected=manager.control_token(cid))
            assert (
                manager.snapshot(cid)["paused"]
                and not manager.snapshot(cid)["stopping"]
            )
            await manager.answer(cid, aid, {"allow": True})
            assert first.answers == [("native", {"allow": True})]
            with pytest.raises(Conflict):
                manager.submit(cid, "redirect", "bypass", redirect=True, target="one")
            first.finish()
            await until(lambda: manager._rt(cid).task is None)
            assert first.closed and first.interrupts == 0 and first.sent == ["first"]
            manager.submit(cid, "three", "third")
            assert manager._rt(cid).task is None
            manager.resume(cid, expected=manager.control_token(cid))
            await until(lambda: second.sent == ["second"])
            second.finish()
            await until(lambda: second.sent == ["second", "third"])
            second.finish()
            await until(lambda: manager.snapshot(cid)["active"] is None)
        finally:
            await manager.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["prepare", "open"])
def test_stop_before_submission_preserves_safe_queue(
    tmp_path: Path, phase: str
) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()

        class Preparing(FakeAdapter):
            async def open(self, native_id: str | None) -> str:
                if phase == "open":
                    entered.set()
                    await asyncio.Event().wait()
                return await super().open(native_id)

        fake = Preparing(Launch("codex", tmp_path))
        manager = Conversations(
            tmp_path / "state.db", {"code": fake.launch}, lambda _: fake
        )

        async def prepare(cid: str, launch: Launch) -> Launch:
            if phase == "prepare":
                entered.set()
                await asyncio.Event().wait()
            return launch

        manager.prepare_launch = prepare
        cid = manager.create("code")
        try:
            manager.submit(cid, "one", "never submitted")
            await entered.wait()
            manager.stop(cid)
            await until(lambda: manager._rt(cid).task is None)
            assert manager.snapshot(cid)["queue"][0]["status"] == "queued"
            assert not fake.sent and not manager.snapshot(cid)["error"]
            assert fake.closed == (phase == "open")
        finally:
            await manager.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["started", "completed", "missing"])
def test_stop_now_before_native_ack_never_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    from arke import sessions

    monkeypatch.setattr(sessions, "INTERRUPT_TIMEOUT", 0.08)

    async def scenario() -> None:
        class LateAck(FakeAdapter):
            async def run(self, text: str) -> AsyncIterator[Event]:
                self.sent.append(text)
                while True:
                    event = await self.events.get()
                    assert isinstance(event, Event)
                    yield event
                    if event.kind == "completed":
                        return

        fake = LateAck(Launch("codex", tmp_path))
        manager = Conversations(
            tmp_path / "state.db", {"code": fake.launch}, lambda _: fake
        )
        cid = manager.create("code")
        try:
            manager.submit(cid, "one", "possibly submitted")
            await until(lambda: bool(fake.sent))
            assert not manager._rt(cid).started
            manager.stop(
                cid, mode="now", active="one", expected=manager.control_token(cid)
            )
            if outcome != "missing":
                fake.events.put_nowait(Event(outcome, {"status": "completed"}))
            await until(lambda: manager._rt(cid).task is None)
            receipt = manager.submit(cid, "one", "possibly submitted")
            assert (
                receipt["status"]
                == {
                    "started": "interrupted",
                    "completed": "completed",
                    "missing": "uncertain",
                }[outcome]
            )
            assert fake.interrupts == (outcome == "started")
            assert fake.closed and fake.sent == ["possibly submitted"]
            if outcome == "missing":
                with pytest.raises(Conflict, match="uncertain"):
                    manager.resume(cid)
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_stop_preserves_redirect_and_held_work(tmp_path: Path) -> None:
    async def scenario() -> None:
        fake = FakeAdapter(Launch("codex", tmp_path))
        manager = Conversations(
            tmp_path / "state.db", {"code": fake.launch}, lambda _: fake
        )
        cid = manager.create("code")
        try:
            manager.submit(cid, "one", "first")
            manager.submit(cid, "two", "older")
            await until(lambda: bool(fake.sent))
            manager.submit(cid, "three", "redirect", redirect=True, target="one")
            manager.stop(cid)
            await until(lambda: manager._rt(cid).task is None)
            assert fake.sent == ["first"]
            assert [(r["id"], r["status"]) for r in manager.snapshot(cid)["queue"]] == [
                ("two", "held"),
                ("three", "queued"),
            ]
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_controls_reject_stale_clicks_and_wait_for_cleanup(tmp_path: Path) -> None:
    async def scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        class SlowClose(FakeAdapter):
            async def close(self) -> None:
                entered.set()
                await release.wait()
                await super().close()

        fake = SlowClose(Launch("codex", tmp_path))
        manager = Conversations(
            tmp_path / "state.db", {"code": fake.launch}, lambda _: fake
        )
        cid = manager.create("code")
        try:
            manager.submit(cid, "one", "first")
            manager.submit(cid, "two", "second")
            await until(lambda: bool(fake.sent))
            stale = manager.control_token(cid)
            fake.finish()
            await until(lambda: len(fake.sent) == 2)
            # Same control version, different turn: target checking is necessary.
            assert stale == manager.control_token(cid)
            with pytest.raises(Conflict, match="targeted turn"):
                manager.stop(cid, mode="now", expected=stale, active="one")
            manager.stop(cid, mode="now", expected=stale, active="two")
            await entered.wait()
            with pytest.raises(Conflict):
                manager.resume(cid)
            with pytest.raises(Conflict):
                manager.archive(cid)
            release.set()
            await until(lambda: manager._rt(cid).task is None)
            stale = manager.control_token(cid)
            manager.stop(cid)
            with pytest.raises(Conflict, match="controls changed"):
                manager.resume(cid, expected=stale)
            manager.archive(cid)
            with pytest.raises(Conflict):
                manager.submit(cid, "new", "not accepted")
            assert manager.submit(cid, "one", "first")["status"] == "completed"
            assert not manager.list_conversations()
            manager.profiles.clear()
            manager.restore(cid)
            assert manager.snapshot(cid)["paused"]
            manager.archive(cid)
            token = manager.control_token(cid)
        finally:
            release.set()
            await manager.close()
        manager = Conversations(tmp_path / "state.db", {})
        try:
            assert manager.snapshot(cid)["archived"]
            with pytest.raises(Conflict):
                manager.restore(cid, expected=token)
            manager.restore(cid, expected=manager.control_token(cid))
            with pytest.raises(Conflict, match="removed"):
                manager.resume(cid)
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_archive_requires_empty_resolved_queue_and_not_shared(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = Conversations(
            tmp_path / "state.db", {"code": Launch("codex", tmp_path)}, FakeAdapter
        )
        cid = manager.create("code")
        shared = manager.get_or_create_shared("code")
        try:
            manager.stop(cid)
            manager.submit(cid, "one", "queued")
            with pytest.raises(Conflict, match="pending"):
                manager.archive(cid)
            manager.discard(cid, "one")
            manager.archive(cid)
            assert manager.submit(cid, "one", "queued")["status"] == "discarded"
            manager.stop(shared)
            with pytest.raises(Conflict, match="shared"):
                manager.archive(shared)
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_http_lifecycle_validates_owner_version_and_archive_listing(
    tmp_path: Path,
) -> None:
    app = create_app(config(tmp_path), FakeAdapter)
    with TestClient(app) as client:
        row = client.post(
            "/api/conversations", headers=OWNER, json={"profile": "private"}
        ).json()
        path = f"/api/conversations/{row['id']}"
        for action in ("stop", "resume", "archive", "restore"):
            assert (
                client.post(path + "/" + action, headers=CHAT, json={}).status_code
                == 401
            )
            assert (
                client.post(path + "/" + action, headers=OWNER, json={}).status_code
                == 400
            )
        stale = row["control_token"]
        result = client.post(
            path + "/stop",
            headers=OWNER,
            json={"expected_control": stale, "mode": "now", "expected_active": None},
        )
        assert result.status_code == 200
        row = result.json()["conversation"]
        assert (
            client.post(
                path + "/resume", headers=OWNER, json={"expected_control": stale}
            ).status_code
            == 409
        )
        result = client.post(
            path + "/archive",
            headers=OWNER,
            json={"expected_control": row["control_token"]},
        )
        assert result.status_code == 200
        row = result.json()["conversation"]
        assert row["archived"]
        assert row["id"] not in [
            r["id"]
            for r in client.get("/api/conversations", headers=OWNER).json()[
                "conversations"
            ]
        ]
        assert row["id"] in [
            r["id"]
            for r in client.get(
                "/api/conversations?include_archived=true", headers=OWNER
            ).json()["conversations"]
        ]
        assert client.get(path, headers=OWNER).json()["archived"]
        result = client.post(
            path + "/restore",
            headers=OWNER,
            json={"expected_control": row["control_token"]},
        )
        assert result.json()["conversation"]["paused"]
        shared = client.get("/api/conversations", headers=CHAT).json()["conversations"][
            0
        ]
        assert not any(c["allowed"] for c in shared["controls"].values())


def test_stop_now_rejects_approval_while_interrupt_is_pending(tmp_path: Path) -> None:
    async def scenario() -> None:
        class DelayedInterrupt(FakeAdapter):
            async def interrupt(self) -> None:
                self.interrupts += 1
                await asyncio.Event().wait()

        fake = DelayedInterrupt(Launch("codex", tmp_path))
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
                    {"request_id": "native", "kind": "permission", "details": {}},
                )
            )
            await until(lambda: bool(manager.snapshot(cid)["approvals"]))
            aid = manager.snapshot(cid)["approvals"][0]["id"]
            manager.stop(cid, mode="now", active="one")
            with pytest.raises(Conflict, match="no longer pending"):
                await manager.answer(cid, aid, {"allow": True})
            fake.finish()
            await until(lambda: manager._rt(cid).task is None)
            assert not fake.answers
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_archived_native_history_stays_protected(tmp_path: Path) -> None:
    from arke.webapp import shared_protected_paths

    async def scenario() -> None:
        conf = config(tmp_path)
        native_home = tmp_path / "old-private-native"
        native_home.mkdir()
        launch = Launch(
            "codex",
            conf.profiles["private"].cwd,
            runner="container",
            native_home=native_home,
        )
        manager = Conversations(tmp_path / "state.db", {"old": launch}, FakeAdapter)
        try:
            cid = manager.create("old")
            manager._change(cid, native_id="saved")
            manager.stop(cid)
            manager.archive(cid)
            manager.profiles.clear()
            assert native_home in shared_protected_paths(conf, manager, None, ())
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_rejected_stop_preserves_runtime_and_control_token(tmp_path: Path) -> None:
    import sqlite3

    async def scenario() -> None:
        fake = FakeAdapter(Launch("codex", tmp_path))
        manager = Conversations(
            tmp_path / "state.db", {"code": fake.launch}, lambda _: fake
        )
        cid = manager.create("code")
        try:
            manager.submit(cid, "one", "first")
            await until(lambda: bool(fake.sent))
            manager.submit(cid, "redirect", "direction", redirect=True, target="one")
            token = manager.control_token(cid)
            manager.db.execute(
                "CREATE TEMP TRIGGER reject_pause BEFORE UPDATE OF paused ON conversations BEGIN SELECT RAISE(FAIL, 'fixture write failure'); END"
            )
            with pytest.raises(sqlite3.DatabaseError, match="fixture write failure"):
                manager.stop(cid, expected=token)
            assert not manager._rt(cid).retire
            assert manager._rt(cid).redirect == "redirect"
            assert not manager.snapshot(cid)["paused"]
            assert manager.control_token(cid) == token
        finally:
            manager.db.execute("DROP TRIGGER IF EXISTS reject_pause")
            await manager.close()

    asyncio.run(scenario())


def test_resume_failure_cannot_release_held_requests(tmp_path: Path) -> None:
    import sqlite3

    async def scenario() -> None:
        manager = Conversations(
            tmp_path / "state.db", {"code": Launch("codex", tmp_path)}, FakeAdapter
        )
        cid = manager.create("code")
        manager.stop(cid)
        manager.submit(cid, "held", "keep held")
        with manager.db:
            manager.db.execute("UPDATE requests SET status='held'")
        token = manager.control_token(cid)
        try:
            manager.db.execute(
                "CREATE TEMP TRIGGER reject_resume BEFORE UPDATE OF paused ON conversations BEGIN SELECT RAISE(FAIL, 'fixture write failure'); END"
            )
            with pytest.raises(sqlite3.DatabaseError, match="fixture write failure"):
                manager.resume(cid, expected=token)
            assert manager.snapshot(cid)["queue"][0]["status"] == "held"
            assert manager.snapshot(cid)["paused"]
            assert manager.control_token(cid) == token
            assert manager._rt(cid).resume_revision == 0
        finally:
            manager.db.execute("DROP TRIGGER IF EXISTS reject_resume")
            await manager.close()

    asyncio.run(scenario())


def test_retirement_hook_failure_still_closes_native_execution(tmp_path: Path) -> None:
    async def scenario() -> None:
        fake = FakeAdapter(Launch("codex", tmp_path))
        manager = Conversations(
            tmp_path / "state.db", {"code": fake.launch}, lambda _: fake
        )
        cid = manager.create("code")

        async def failed_retirement(cid: str) -> None:
            raise RuntimeError("fixture retirement failed")

        manager.retire_launch = failed_retirement
        try:
            manager.submit(cid, "one", "first")
            await until(lambda: bool(fake.sent))
            manager.stop(cid)
            fake.finish()
            task = manager._rt(cid).task
            assert task is not None
            await until(task.done)
            assert fake.closed
            await asyncio.sleep(
                0
            )  # allow the actor's readiness callback to report failure
            assert manager.snapshot(cid)["stopping"]
            assert "cleanup failed" in manager.snapshot(cid)["error"]
            with pytest.raises(Conflict, match="cleanup failed"):
                manager.resume(cid)
        finally:
            rt = manager._rt(cid)
            if rt.cleanup:
                with pytest.raises(RuntimeError, match="retirement failed"):
                    await rt.cleanup
            manager.retire_launch = None
            rt.cleanup = asyncio.create_task(manager._cleanup(cid))
            await rt.cleanup
            await manager.close()

    asyncio.run(scenario())

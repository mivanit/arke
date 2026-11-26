"""Archive projection, durable identity, and callbacks that outlive their worker."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from arke.dashboard import Dashboard
from arke.harnesses import Launch
from arke.sessions import Conversations
from tests.test_conversation_web import config
from tests.test_sessions import FakeAdapter, until


def setup(tmp_path: Path) -> tuple[Conversations, Dashboard]:
    bus_config = tmp_path / "bus.toml"
    bus_config.write_text(f'[server]\nstate_dir = "{tmp_path / "bus"}"\n')
    manager = Conversations(
        tmp_path / "conversations.db", {"code": Launch("codex", tmp_path)}, FakeAdapter
    )
    return manager, Dashboard(config(tmp_path, bus_config=bus_config), manager)


def test_archive_reserves_identity_filters_routing_and_replays_receipts(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        manager, dashboard = setup(tmp_path)
        cid = manager.create("code")
        bus = dashboard.bus
        try:
            await dashboard.project(cid)
            sid = "managed-" + cid
            agent = bus.agents[sid]
            original = agent["name"]
            bus.rename("", "human", {"agent": original, "name": "house"})
            bus.projects(sid, "", {"project": "/home", "action": "join"})
            receipt = bus.send(
                "human", {"to": [original], "text": "saved mail", "request_id": "old"}
            )
            stale = dashboard.projection(cid)
            manager.stop(cid)
            manager.archive(cid)
            await dashboard.project(cid)
            assert agent["managed_archived"] and agent["state"] == "offline"
            assert bus.by_name("house") is agent and original in agent["aka"]
            assert (
                not bus.list_agents("")
                and not bus.roster("", "human", global_scope=True, show_all=True)[
                    "agents"
                ]
            )
            assert not bus.project_tree()
            with bus.change():
                terminal = bus.register(
                    "terminal", {"names": ["house"], "kind": "codex"}
                )
                alias = bus.register("alias", {"names": [original], "kind": "codex"})
                bus.save()
            assert terminal["name"] != "house" and alias["name"] != original
            with pytest.raises(dashboard.server.HttpError):
                bus.rename("terminal", "", {"name": "house"})
            assert bus.resolve(["*"], "human")["reach"] == [
                terminal["name"],
                alias["name"],
            ]
            assert not bus.resolve(["/home"], "human")["reach"]
            before = len(bus.messages)
            for recipients in (["house"], [original], [terminal["name"], "house"]):
                with pytest.raises(dashboard.server.HttpError, match="archived"):
                    bus.send("human", {"to": recipients, "text": "reject all"})
            with pytest.raises(dashboard.server.HttpError, match="archived"):
                bus.send(
                    "", {"to": ["human"], "text": "helper"}, sid=sid, helper="helper1"
                )
            assert len(bus.messages) == before
            assert (
                bus.send(
                    "human",
                    {"to": [original], "text": "saved mail", "request_id": "old"},
                )["id"]
                == receipt["id"]
            )
            assert [mail[0] for mail in agent["inbox"]] == [receipt["id"]]
            dashboard.ensure_agent(stale)
            assert agent["managed_archived"]
            wrong_epoch = dict(
                stale, control_token="old-process:99999", revision="old-process:99999"
            )
            dashboard.ensure_agent(wrong_epoch)
            assert agent["managed_archived"]
        finally:
            await manager.close()
            await dashboard.stop()
        manager, dashboard = setup(tmp_path)
        try:
            await dashboard.start()
            agent = dashboard.bus.agents["managed-" + cid]
            assert (
                agent["managed_archived"]
                and agent["name"] == "house"
                and agent["inbox"]
            )
            manager.restore(cid)
            await dashboard.project(cid)
            assert manager.snapshot(cid)["paused"] and not agent["managed_archived"]
            assert agent["projects"] == {"/home": True}
            assert not manager.snapshot(cid)["queue"]
        finally:
            await manager.close()
            await dashboard.stop()

    asyncio.run(scenario())


def test_canceled_callback_cannot_commit_after_archive_and_restore(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        manager, dashboard = setup(tmp_path)
        cid = manager.create("code")
        entered, release = threading.Event(), threading.Event()
        try:
            await dashboard.prepare_launch(cid, manager.profiles["code"])
            token = dashboard.capabilities[cid]

            def delayed() -> object:
                entered.set()
                assert release.wait(3)
                return dashboard.tool(
                    cid,
                    "send",
                    {"to": ["human"], "text": "late", "request_id": "late"},
                    token,
                )

            callback = asyncio.create_task(dashboard.execution.call(delayed))
            await until(entered.is_set)
            callback.cancel()
            with pytest.raises(asyncio.CancelledError):
                await callback
            manager.stop(cid)
            manager.archive(cid)
            await dashboard.project(cid)
            manager.restore(cid)
            await dashboard.project(cid)
            # Even a new worker token must not revive a callback from the old one.
            await dashboard.prepare_launch(cid, manager.profiles["code"])
            assert dashboard.capabilities[cid] != token
            release.set()
            await dashboard.execution.drain()
            assert not dashboard.bus.messages
            with pytest.raises(dashboard.server.HttpError, match="retired"):
                dashboard.tool(cid, "inbox", {}, token)
        finally:
            release.set()
            await manager.close()
            await dashboard.stop()

    asyncio.run(scenario())


def test_failed_archive_publication_is_reconciled_before_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        manager, dashboard = setup(tmp_path)
        cid = manager.create("code")
        await dashboard.project(cid)
        manager.stop(cid)
        manager.archive(cid)
        commit = dashboard.bus.store.commit

        def fail(state: object) -> None:
            raise OSError("fixture disk failure")

        monkeypatch.setattr(dashboard.bus.store, "commit", fail)
        try:
            with pytest.raises(OSError):
                await dashboard.project(cid)
            assert manager.snapshot(cid)["archived"]
            assert not dashboard.bus.agents["managed-" + cid]["managed_archived"]
            with pytest.raises(OSError):
                await dashboard.start()
            assert not dashboard.threads and dashboard.poller is None
        finally:
            monkeypatch.setattr(dashboard.bus.store, "commit", commit)
            await manager.close()
            await dashboard.stop()
        manager, dashboard = setup(tmp_path)
        try:
            await dashboard.start()
            assert dashboard.bus.agents["managed-" + cid]["managed_archived"]
        finally:
            await manager.close()
            await dashboard.stop()

    asyncio.run(scenario())


def test_unread_mail_waits_for_resume_and_discarded_notification_can_retry(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        manager, dashboard = setup(tmp_path)
        cid = manager.create("code")
        try:
            manager.stop(cid)
            await dashboard.project(cid)
            agent = dashboard.bus.agents["managed-" + cid]
            receipt = dashboard.bus.send(
                "human", {"to": [agent["name"]], "text": "unread"}
            )
            await dashboard.sync_agents()
            assert not manager.snapshot(cid)["queue"]
            manager.resume(cid)
            dashboard.notify(cid, [receipt["id"]])
            first = manager.snapshot(cid)["queue"][0]["id"]
            manager.stop(cid)
            await until(lambda: manager._rt(cid).task is None)
            manager.discard(cid, first)
            manager.archive(cid)
            await dashboard.project(cid)
            manager.restore(cid)
            await dashboard.project(cid)
            assert not manager.snapshot(cid)["queue"]
            manager.resume(cid)
            dashboard.notify(cid, [receipt["id"]])
            second = manager.snapshot(cid)["queue"][0]["id"]
            assert second != first
            dashboard.notify(cid, [receipt["id"]])
            assert len(manager.snapshot(cid)["queue"]) == 1
            manager.stop(cid)
            await until(lambda: manager._rt(cid).task is None)
            manager.resume(cid)
            dashboard.notify(cid, [receipt["id"]])
            assert [r["id"] for r in manager.snapshot(cid)["queue"]] == [second]
            manager.stop(cid)
            await until(lambda: manager._rt(cid).task is None)
        finally:
            await manager.close()
            await dashboard.stop()

    asyncio.run(scenario())


def test_pending_publication_clears_after_newer_pause(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager, dashboard = setup(tmp_path)
        cid = manager.create("code")
        try:
            manager.stop(cid)
            dashboard.pending[cid] = manager.control_token(cid)
            manager._change(cid, paused=1, error="later native cleanup result")
            await dashboard.project(cid)
            assert cid not in dashboard.pending
        finally:
            await manager.close()
            await dashboard.stop()

    asyncio.run(scenario())


def test_http_archive_reports_pending_publication_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from arke.webapp import create_app
    from tests.test_conversation_web import OWNER

    bus_config = tmp_path / "bus.toml"
    bus_config.write_text(f'[server]\nstate_dir = "{tmp_path / "bus"}"\n')
    app = create_app(
        config(tmp_path, dashboard=True, bus_config=bus_config), FakeAdapter
    )
    with TestClient(app) as client:
        cid = client.post(
            "/api/conversations", headers=OWNER, json={"profile": "private"}
        ).json()["id"]
        path = f"/api/conversations/{cid}"
        token = client.get(path, headers=OWNER).json()["control_token"]
        row = client.post(
            path + "/stop",
            headers=OWNER,
            json={"mode": "now", "expected_active": None, "expected_control": token},
        ).json()["conversation"]
        dashboard = app.state.dashboard
        commit = dashboard.bus.store.commit

        def fail(state: object) -> None:
            raise OSError("fixture disk failure")

        monkeypatch.setattr(dashboard.bus.store, "commit", fail)
        result = client.post(
            path + "/archive",
            headers=OWNER,
            json={"expected_control": row["control_token"]},
        )
        assert result.status_code == 202
        assert (
            result.json()["conversation"]["archived"] and result.json()["synchronizing"]
        )
        assert client.get(path, headers=OWNER).json()["synchronizing"]
        monkeypatch.setattr(dashboard.bus.store, "commit", commit)
        assert client.portal is not None
        client.portal.call(dashboard.sync_agents)
        assert not client.get(path, headers=OWNER).json()["synchronizing"]
        assert dashboard.bus.agents["managed-" + cid]["managed_archived"]

"""The combined app reuses the bus while keeping managed tools scoped to one agent."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from arke.webapp import create_app
from tests.test_conversation_web import CHAT, OWNER, config
from tests.test_sessions import FakeAdapter


def test_dashboard_bus_scopes_and_paused_mail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arke import server

    monkeypatch.setattr(server, "STATE_DIR", tmp_path / "bus")
    monkeypatch.setattr(server, "LOG_FILE", tmp_path / "bus/log.jsonl")
    monkeypatch.setattr(server, "SETTINGS", {"state_dir": str(tmp_path / "bus")})
    app = create_app(config(tmp_path / "conversations", dashboard=True), FakeAdapter)
    # The configured workspace must exist before creating the shared conversation.
    (tmp_path / "conversations").mkdir(exist_ok=True)
    with TestClient(app) as client:
        assert 'id="managed-link" href=' in client.get("/").text
        assert client.get("/agents", headers=OWNER).status_code == 200
        assert client.get("/agents", headers=CHAT).status_code == 401
        listing = client.get("/api/conversations", headers=OWNER).json()
        cid = listing["shared"]
        dashboard = app.state.dashboard
        assert client.portal is not None
        client.portal.call(
            dashboard.prepare_launch, cid, app.state.conversations.profiles["code"]
        )
        cap = {"Authorization": "Bearer " + dashboard.capabilities[cid]}
        assert (
            client.post(f"/api/bus/{cid}/list", headers=cap, json={}).status_code == 200
        )
        assert (
            client.post("/api/bus/other/list", headers=cap, json={}).status_code == 401
        )
        assert (
            client.post(
                "/api/conversations", headers=cap, json={"profile": "code"}
            ).status_code
            == 401
        )
        assert client.get("/agents", headers=cap).status_code == 401
        agent = dashboard.bus.agents["managed-" + cid]
        assert agent["managed"] == cid and not agent.get("tmux")
        response = client.post(
            "/send",
            headers=OWNER | {"X-Agent-As": "human"},
            json={"to": [agent["name"]], "text": "untrusted mail"},
        )
        assert response.status_code == 200
        inbox = client.post(f"/api/bus/{cid}/inbox", headers=cap, json={})
        assert inbox.status_code == 200
        assert "untrusted mail" in inbox.text
        assert dashboard.bus.agents["managed-" + cid]["inbox"], (
            "read returns a receipt; it must not acknowledge early"
        )
        ids = inbox.json()["delivery"]["ids"]
        assert (
            client.post(
                f"/api/bus/{cid}/ack", headers=cap, json={"ids": ids}
            ).status_code
            == 200
        )
        assert not dashboard.bus.agents["managed-" + cid]["inbox"]


def test_explicit_bus_config_owns_state_log_and_watch_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import json

    from arke import server
    from arke.dashboard import Dashboard
    from arke.harnesses import Launch
    from arke.sessions import Conversations

    stale_state = tmp_path / "imported-state"
    monkeypatch.setattr(server, "STATE_DIR", stale_state)
    monkeypatch.setattr(server, "LOG_FILE", stale_state / "wrong.log")
    monkeypatch.setenv("ARKE_DIR", str(stale_state))
    monkeypatch.setenv("ARKE_LOG", str(stale_state / "wrong.log"))
    config_file = tmp_path / "bus.toml"
    target = tmp_path / "explicit-state"
    config_file.write_text(
        f'[server]\nstate_dir = "{target}"\n'
        "stale_seconds = 42\nprocess_interval = 0.25\ntranscript_roots = []\n"
    )

    async def scenario() -> None:
        manager = Conversations(
            tmp_path / "conversations.db",
            {"code": Launch("codex", tmp_path)},
            FakeAdapter,
        )
        dashboard = Dashboard(config(tmp_path, bus_config=config_file), manager)
        try:
            assert dashboard.bus.state_file == target / "state.sqlite3"
            assert dashboard.bus.config.stale_seconds == 42
            assert dashboard.bus.config.process_interval == 0.25
            assert dashboard.bus.config.transcript_roots == ()
            dashboard.bus.log("explicit-test")
            assert (
                json.loads((target / "log.jsonl").read_text().splitlines()[-1])["event"]
                == "explicit-test"
            )
            assert not stale_state.exists()
            events = server.dispatch(dashboard.bus, "GET", "/events", {}, {}, {})[1]
            assert events["events"][-1]["event"] == "explicit-test"
        finally:
            await manager.close()
            await dashboard.stop()
            await dashboard.stop()

    asyncio.run(scenario())


def test_full_queue_does_not_block_startup_or_other_agent_mail(tmp_path: Path) -> None:
    import asyncio

    from arke.dashboard import Dashboard
    from arke.harnesses import Launch
    from arke.sessions import Conversations

    config_file = tmp_path / "bus.toml"
    config_file.write_text(f'[server]\nstate_dir = "{tmp_path / "bus"}"\n')

    async def scenario() -> None:
        manager = Conversations(
            tmp_path / "conversations.db",
            {"code": Launch("codex", tmp_path)},
            FakeAdapter,
        )
        first, second = manager.create("code"), manager.create("code")
        manager._change(first, paused=1)
        manager._change(second, paused=1)
        dashboard = Dashboard(config(tmp_path, bus_config=config_file), manager)
        try:
            await dashboard.sync_agents()
            for n in range(100):
                manager.submit(first, str(n), "waiting")
            for cid in (first, second):
                agent = dashboard.bus.agents["managed-" + cid]
                dashboard.bus.send("human", {"to": [agent["name"]], "text": "mail"})
            await dashboard.start()
            assert len(manager.snapshot(first)["queue"]) == 100
            assert len(manager.snapshot(second)["queue"]) == 1
            assert dashboard.bus.agents["managed-" + first]["inbox"]
            assert dashboard.bus.agents["managed-" + second]["inbox"]
            # An agent created after startup has working tools before the next poll.
            third = manager.create("code")
            launch = await dashboard.prepare_launch(third, manager.profiles["code"])
            assert launch.bus_call is not None
            await launch.bus_call("list", {})
            inbox = await launch.bus_call("inbox", {})
            assert inbox["delivery"]["ids"] == []
        finally:
            dashboard.stop_event.set()
            await manager.close()
            await dashboard.stop()

    asyncio.run(scenario())


def test_dashboard_constructor_failure_releases_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import fcntl

    from arke import server
    from arke.dashboard import Dashboard
    from arke.harnesses import Launch
    from arke.sessions import Conversations

    config_file = tmp_path / "bus.toml"
    state = tmp_path / "bus"
    config_file.write_text(f'[server]\nstate_dir = "{state}"\n')

    def fail_key(values: dict[str, object]) -> str:
        raise ValueError("invalid key")

    async def scenario() -> None:
        manager = Conversations(
            tmp_path / "conversations.db",
            {"code": Launch("codex", tmp_path)},
            FakeAdapter,
        )
        try:
            monkeypatch.setattr(server.settings, "server_key", fail_key)
            with pytest.raises(ValueError, match="invalid key"):
                Dashboard(config(tmp_path, bus_config=config_file), manager)
            with (state / "server.lock").open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_cancelled_bus_request_is_drained_before_database_close(tmp_path: Path) -> None:
    import asyncio
    import threading

    from arke.bus_execution import BusBusy
    from arke.dashboard import Dashboard
    from arke.harnesses import Launch
    from arke.sessions import Conversations

    config_file = tmp_path / "bus.toml"
    config_file.write_text(f'[server]\nstate_dir = "{tmp_path / "bus"}"\n')

    async def scenario() -> None:
        manager = Conversations(
            tmp_path / "conversations.db",
            {"code": Launch("codex", tmp_path)},
            FakeAdapter,
        )
        dashboard = Dashboard(config(tmp_path, bus_config=config_file), manager)
        entered, release = threading.Event(), threading.Event()
        committed = []

        def delayed_write() -> None:
            entered.set()
            assert release.wait(3)
            with dashboard.bus.change():
                dashboard.bus.colors["fixture"] = "#ffffff"
                dashboard.bus.save()
                committed.append(
                    dashboard.bus.store.db.execute("SELECT 1").fetchone()[0]
                )

        request = asyncio.create_task(dashboard.execution.call(delayed_write))
        try:
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(0.001)
            # The blocked bus operation must not prevent event-loop control work.
            await asyncio.wait_for(asyncio.sleep(0), 0.1)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            dashboard.begin_shutdown()
            shutdown = asyncio.create_task(dashboard.stop())
            await asyncio.sleep(0.02)
            assert not shutdown.done() and not dashboard.lock.closed
            with pytest.raises(BusBusy, match="stopping"):
                await dashboard.execution.call(lambda: None)
            release.set()
            await asyncio.wait_for(shutdown, 2)
            assert committed == [1] and dashboard.lock.closed
        finally:
            release.set()
            await manager.close()
            await dashboard.stop()

    asyncio.run(scenario())


def test_long_polls_have_separate_capacity_and_shutdown_wakes_them(
    tmp_path: Path,
) -> None:
    import asyncio

    from arke.bus_execution import BusBusy
    from arke.dashboard import Dashboard
    from arke.harnesses import Launch
    from arke.sessions import Conversations

    config_file = tmp_path / "bus.toml"
    config_file.write_text(f'[server]\nstate_dir = "{tmp_path / "bus"}"\n')

    async def scenario() -> None:
        manager = Conversations(
            tmp_path / "conversations.db",
            {"code": Launch("codex", tmp_path)},
            FakeAdapter,
        )
        dashboard = Dashboard(config(tmp_path, bus_config=config_file), manager)
        cid = manager.create("code")
        await dashboard.prepare_launch(cid, manager.profiles["code"])
        agent = dashboard.bus.agents["managed-" + cid]
        pending = [
            asyncio.create_task(
                dashboard.execution.call(
                    dashboard.bus.inbox, agent, 600, True, long_poll=True
                )
            )
            for _ in range(8)
        ]
        try:
            async with asyncio.timeout(2):
                while len(dashboard.execution.pending[1]) != 8:
                    await asyncio.sleep(0.001)
            with pytest.raises(BusBusy, match="busy"):
                await dashboard.execution.call(lambda: None, long_poll=True)
            # Tools still have execution capacity while every long-poll slot waits.
            assert (await dashboard.call_tool(cid, "inbox", {}))["messages"] == []
            dashboard.begin_shutdown()
            results = await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True), 2
            )
            assert all(
                isinstance(result, dashboard.server.HttpError) and result.status == 503
                for result in results
            )
            await dashboard.stop()
        finally:
            dashboard.begin_shutdown()
            await asyncio.gather(*pending, return_exceptions=True)
            await manager.close()
            await dashboard.stop()

    asyncio.run(scenario())

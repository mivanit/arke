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
    (tmp_path / "conversations").mkdir()
    with TestClient(app) as client:
        assert 'id="managed-link" href=' in client.get("/").text
        assert client.get("/agents", headers=OWNER).status_code == 200
        assert client.get("/agents", headers=CHAT).status_code == 401
        listing = client.get("/api/conversations", headers=OWNER).json()
        cid = listing["shared"]
        dashboard = app.state.dashboard
        dashboard.prepare_launch(cid, app.state.conversations.profiles["code"])
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

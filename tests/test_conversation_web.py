"""Owner/chat isolation and shared backend state, using an in-memory ASGI client."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from arke.harnesses import Launch
from arke.webapp import create_app
from arke.webconfig import WebConfig
from tests.test_sessions import FakeAdapter

OWNER = {"Authorization": "Bearer " + "a" * 43}
CHAT = {"Authorization": "Bearer " + "b" * 43}


def config(tmp_path: Path, **overrides: Any) -> WebConfig:
    workspace, native_home = tmp_path / "workspace", tmp_path / "native"
    workspace.mkdir(parents=True, exist_ok=True)
    native_home.mkdir(parents=True, exist_ok=True)
    values: dict[str, Any] = {
        "state": tmp_path / "state",
        "profiles": {
            "code": Launch(
                "codex", workspace, runner="container", native_home=native_home
            ),
            "private": Launch("codex", workspace),
        },
        "owner_key": "a" * 43,
        "chat_key": "b" * 43,
        "shared_profile": "code",
        "allowed_hosts": ("testserver", "localhost", "127.0.0.1"),
    }
    return WebConfig(**(values | overrides))


def test_owner_chat_and_request_validation(tmp_path: Path) -> None:
    app = create_app(config(tmp_path), FakeAdapter)
    with TestClient(app) as client:
        assert client.get("/api/conversations").status_code == 401
        listing = client.get("/api/conversations", headers=CHAT).json()
        cid = listing["shared"]
        assert listing["role"] == "chat" and not listing["profiles"]
        assert (
            client.post(
                "/api/conversations", headers=CHAT, json={"profile": "code"}
            ).status_code
            == 401
        )
        assert (
            client.post(
                f"/api/conversations/{cid}/resume", headers=CHAT, json={}
            ).status_code
            == 401
        )
        assert (
            client.post(
                f"/api/conversations/{cid}/approvals/fake",
                headers=CHAT,
                json={"allow": True},
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/api/conversations",
                headers=OWNER,
                json={"profile": "code", "cwd": "/"},
            ).status_code
            == 400
        )
        other = client.post(
            "/api/conversations", headers=OWNER, json={"profile": "private"}
        ).json()["id"]
        assert (
            client.get(f"/api/conversations/{other}", headers=CHAT).status_code == 403
        )
        assert (
            client.get(
                "/api/conversations", headers=OWNER | {"Host": "evil.example"}
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/api/conversations", headers=OWNER | {"Origin": "null"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/conversations/{cid}/messages", headers=CHAT, content="x"
            ).status_code
            == 415
        )
        assert (
            client.post(
                f"/api/conversations/{cid}/messages",
                headers=CHAT,
                json={"text": "x", "request_id": "x", "interrupt": "false"},
            ).status_code
            == 400
        )
        assert (
            client.post(
                f"/api/conversations/{cid}/messages",
                headers=CHAT,
                json={"text": "x" * 1_000_001, "request_id": "x"},
            ).status_code
            == 413
        )
        result = client.post(
            f"/api/conversations/{cid}/messages",
            headers=CHAT,
            json={"text": "hello", "request_id": "one"},
        )
        assert result.status_code == 200
        duplicate = client.post(
            f"/api/conversations/{cid}/messages",
            headers=CHAT,
            json={"text": "hello", "request_id": "one"},
        )
        assert duplicate.status_code == 200
        snapshot = client.get(f"/api/conversations/{cid}", headers=CHAT).json()
        assert "native_id" not in snapshot
        assert len(snapshot["queue"]) == 1


def test_restart_and_two_browsers_share_one_conversation(tmp_path: Path) -> None:
    settings = config(tmp_path)
    with TestClient(create_app(settings, FakeAdapter)) as client:
        cid = client.get("/api/conversations", headers=OWNER).json()["shared"]
        assert client.get("/api/conversations", headers=CHAT).json()["shared"] == cid
    app = create_app(settings, FakeAdapter)
    with TestClient(app) as client:
        data = client.get("/api/conversations", headers=CHAT).json()
        assert data["shared"] == cid and len(data["conversations"]) == 1
        assert data["conversations"][0]["paused"]
        client.post(
            f"/api/conversations/{cid}/messages",
            headers=CHAT,
            json={"text": "wait", "request_id": "one"},
        )
        assert (
            client.get(f"/api/conversations/{cid}", headers=CHAT).json()["active"]
            is None
        )


def test_sse_snapshot_does_not_start_process(tmp_path: Path) -> None:
    async def scenario() -> None:
        from starlette.requests import Request

        app = create_app(config(tmp_path), FakeAdapter)
        async with app.router.lifespan_context(app):
            manager = app.state.conversations
            cid = app.state.shared
            route: Any = next(
                r
                for r in app.routes
                if getattr(r, "path", "") == "/api/conversations/{cid}/events"
            )
            request = Request(
                {
                    "type": "http",
                    "headers": [(b"authorization", OWNER["Authorization"].encode())],
                }
            )
            response = await route.endpoint(cid, request)
            stream = response.body_iterator
            frame = await anext(stream)
            assert "event: snapshot" in frame and cid in frame
            assert manager._rt(cid).adapter is None
            await stream.aclose()

    asyncio.run(scenario())


def test_shared_access_requires_execution_isolation(tmp_path: Path) -> None:
    import pytest

    settings = config(tmp_path, profiles={"code": Launch("codex", tmp_path)})
    with (
        pytest.raises(ValueError, match="requires runner"),
        TestClient(create_app(settings, FakeAdapter)),
    ):
        pass


def test_private_to_shared_never_adopts_private_history(tmp_path: Path) -> None:
    with TestClient(
        create_app(config(tmp_path, shared_profile=None, chat_key=""), FakeAdapter)
    ) as client:
        private = client.post(
            "/api/conversations",
            headers=OWNER,
            json={"profile": "code", "name": "Private"},
        ).json()["id"]
    with TestClient(create_app(config(tmp_path), FakeAdapter)) as client:
        listing = client.get("/api/conversations", headers=CHAT).json()
        assert listing["shared"] != private
        assert (
            client.get(f"/api/conversations/{private}", headers=CHAT).status_code == 403
        )


def test_mount_with_explicit_lifetime(tmp_path: Path) -> None:
    from collections.abc import AsyncGenerator
    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    from arke.webapp import service_lifespan

    child = create_app(config(tmp_path), FakeAdapter)

    @asynccontextmanager
    async def lifetime(parent: FastAPI) -> AsyncGenerator[None]:
        async with service_lifespan(child):
            yield

    parent = FastAPI(lifespan=lifetime)
    parent.mount("/agent", child)
    with TestClient(parent) as client:
        listing = client.get("/agent/api/conversations", headers=OWNER)
        assert listing.status_code == 200
        cid = listing.json()["shared"]
        assert (
            client.get(f"/agent/api/conversations/{cid}", headers=CHAT).status_code
            == 200
        )
        manager = child.state.conversations
    assert manager.closed
    # Shutdown released the database lock; a new lifetime can own it.
    with TestClient(parent) as client:
        assert (
            client.get("/agent/api/conversations", headers=CHAT).json()["shared"] == cid
        )

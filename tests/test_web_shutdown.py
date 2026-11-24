"""Exercise actual HTTP drain before lifespan cleanup, with an open browser stream."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import httpx
import uvicorn

from arke.sessions import Conversations
from arke.webapp import HTTP_DRAIN_TIMEOUT, create_app, server_config
from tests.test_conversation_web import OWNER, config
from tests.test_sessions import FakeAdapter, until


def test_shutdown_with_open_sse_and_running_agent(tmp_path: Path) -> None:
    async def scenario() -> None:
        settings = config(tmp_path)
        adapters: list[FakeAdapter] = []

        def factory(launch: object) -> FakeAdapter:
            fake = FakeAdapter(settings.profiles["code"])
            adapters.append(fake)
            return fake

        app = create_app(settings, factory)
        policy = server_config(settings, app)
        assert policy.timeout_graceful_shutdown == HTTP_DRAIN_TIMEOUT
        policy.timeout_graceful_shutdown = 1
        policy.log_level = "critical"
        server = uvicorn.Server(policy)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            task = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                await until(lambda: server.started)
                port = listener.getsockname()[1]
                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{port}"
                ) as client:
                    cid = (
                        await client.get("/api/conversations", headers=OWNER)
                    ).json()["shared"]
                    await client.post(
                        f"/api/conversations/{cid}/messages",
                        headers=OWNER,
                        json={"request_id": "one", "text": "wait"},
                    )
                    await until(lambda: bool(adapters and adapters[0].sent))
                    async with client.stream(
                        "GET", f"/api/conversations/{cid}/events", headers=OWNER
                    ) as stream:
                        assert stream.status_code == 200
                        assert "event: snapshot" in await anext(stream.aiter_text())
                        server.should_exit = True
                        await asyncio.wait_for(task, 5)
                assert adapters[0].closed
                assert app.state.conversations.closed
                # Another manager can acquire the same lock after the serving
                # lifecycle has completed, even though the browser was left open.
                next_manager = Conversations(
                    settings.state / "conversations.sqlite3", settings.profiles, factory
                )
                await next_manager.close()
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, 5)

    asyncio.run(scenario())

"""History byte bounds, lossless pagination and incremental browser delivery."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.routing import Route

from arke.harnesses import Event, Launch
from arke.sessions import HISTORY_PAGE_BYTES, Conversations
from arke.webapp import create_app, service_lifespan
from tests.test_conversation_web import CHAT, OWNER, config
from tests.test_sessions import FakeAdapter


def test_bounded_history_is_losslessly_pageable(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = Conversations(
            tmp_path / "state.db", {"code": Launch("codex", tmp_path)}
        )
        try:
            cid = manager.create("code")
            other = manager.create("code")
            expected = []
            for i in range(120):
                # Mix large records with small ones that exhaust the byte budget
                # well before the count limit. Other conversations make ID gaps.
                data = {"details": {"output": "\U0001f600" * (25000 if i % 2 else 600)}}
                manager._record(cid, str(i), Event("tool", data))
                expected.append((manager.history_tail(cid), data))
                manager._record(other, str(i), Event("message", {"text": "private"}))
            seen = []
            before = None
            while True:
                page = manager.history_page(cid, before)
                assert len(json.dumps(page["history"]).encode()) <= HISTORY_PAGE_BYTES
                assert len(page["history"]) < 100
                seen[0:0] = [item["seq"] for item in page["history"]]
                for item in page["history"]:
                    full = manager.history_item(cid, item["seq"])
                    assert full["data"] == dict(expected)[item["seq"]]
                    if item["deferred"]:
                        assert item["data"] == {}
                    else:
                        assert item["data"] == full["data"]
                if not page["has_earlier"]:
                    break
                before = page["history"][0]["seq"]
            assert seen == [seq for seq, _ in expected]
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_detail_endpoint_preserves_conversation_authorization(tmp_path: Path) -> None:
    app = create_app(config(tmp_path), FakeAdapter)
    with TestClient(app) as client:
        assert client.portal is not None
        manager = app.state.conversations
        cid = app.state.shared
        other = client.post(
            "/api/conversations", headers=OWNER, json={"profile": "private"}
        ).json()["id"]
        text = "full tool output" * 20000
        for target in (cid, other):
            client.portal.call(
                manager._record, target, "one", Event("tool", {"details": text})
            )
        shared_seq = client.portal.call(manager.history_tail, cid)
        private_seq = client.portal.call(manager.history_tail, other)
        prefix = f"/api/conversations/{cid}/history"
        page = client.get(prefix, headers=CHAT).json()
        assert page["history"][0]["deferred"]
        assert (
            client.get(f"{prefix}/{shared_seq}", headers=CHAT).json()["data"]["details"]
            == text
        )
        assert client.get(f"{prefix}/{shared_seq}").status_code == 401
        assert client.get(f"{prefix}/{private_seq}", headers=CHAT).status_code == 404
        assert (
            client.get(
                f"/api/conversations/{other}/history/{private_seq}", headers=CHAT
            ).status_code
            == 403
        )


def test_stream_sends_history_only_when_changed_and_recovers_gaps(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app = create_app(config(tmp_path), FakeAdapter)
        async with service_lifespan(app):
            manager = app.state.conversations
            cid = app.state.shared
            request = Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": f"/api/conversations/{cid}/events",
                    "headers": [(b"authorization", OWNER["Authorization"].encode())],
                }
            )
            route = next(
                r
                for r in app.routes
                if isinstance(r, Route) and r.path.endswith("/{cid}/events")
            )
            response = await route.endpoint(cid, request)
            stream = response.body_iterator

            async def next_frame() -> dict:
                frame = await asyncio.wait_for(anext(stream), 1)
                return json.loads(frame.split("data: ", 1)[1])

            manager._record(cid, "one", Event("tool", {"details": "x" * 262144}))
            initial = await next_frame()
            assert initial["history"][0]["deferred"]
            assert initial["history_mode"] == "replace"
            manager._rt(cid).preview = "next token"
            manager._change(cid)
            preview = await next_frame()
            assert preview["preview"] == "next token" and "history" not in preview
            manager._record(cid, "one", Event("message", {"text": "new message"}))
            manager._change(cid)
            appended = await next_frame()
            assert appended["history_mode"] == "append"
            assert [i["kind"] for i in appended["history"]] == ["message"]
            for i in range(120):
                manager._record(cid, str(i), Event("message", {"text": str(i)}))
            manager._change(cid)
            gap = await next_frame()
            assert gap["history_mode"] == "replace" and gap["has_earlier"]
            assert len(gap["history"]) == 100
            await stream.aclose()
            fresh = await route.endpoint(cid, request)
            assert '"history_mode": "replace"' in await anext(fresh.body_iterator)
            await fresh.body_iterator.aclose()

    asyncio.run(scenario())

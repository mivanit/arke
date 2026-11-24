"""Standalone conversation app and optional full dashboard, built from the same UI/API."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from .harnesses import Adapter, Launch, adapter
from .sessions import Conflict, Conversations
from .webconfig import WebConfig, load


def assets() -> Path:
    installed = Path(__file__).parent / "web"
    return installed if installed.is_dir() else Path(__file__).parent.parent / "web"


def create_app(
    config: WebConfig, factory: Callable[[Launch], Adapter] = adapter
) -> FastAPI:
    """Create an app without starting processes. Mount it in another ASGI application if desired."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        manager = Conversations(
            config.state / "conversations.sqlite3", config.profiles, factory
        )
        app.state.conversations = manager
        app.state.shared = None
        app.state.dashboard = None
        try:
            if config.shared_profile:
                existing = [
                    c
                    for c in manager.list_conversations()
                    if c["profile"] == config.shared_profile
                ]
                app.state.shared = (
                    existing[0]["id"]
                    if existing
                    else manager.create(config.shared_profile, config.title)
                )
            if config.dashboard:
                from .dashboard import Dashboard

                app.state.dashboard = Dashboard(config, manager)
                await app.state.dashboard.start()
            yield
        finally:
            if app.state.dashboard:
                app.state.dashboard.stop_event.set()
            await manager.close()
            if app.state.dashboard:
                await app.state.dashboard.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def boundary(request: Request, call_next: Callable[..., Any]) -> Response:
        host = request.headers.get("host", "")
        hostname = request.url.hostname or ""
        origin = request.headers.get("origin")
        site = request.headers.get("sec-fetch-site", "")
        allowed = {h.lower().strip("[]") for h in config.allowed_hosts}
        if (
            hostname.lower() not in allowed
            or site not in ("", "same-origin", "none")
            or (origin is not None and origin != f"{request.url.scheme}://{host}")
        ):
            return JSONResponse(
                {"error": "open this interface at an allowed, same-origin address"},
                status_code=403,
            )
        # Bound bodies before parsing JSON. Streaming chunked uploads are bounded too.
        if request.method not in ("GET", "HEAD"):
            if (
                request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                != "application/json"
            ):
                return JSONResponse({"error": "use application/json"}, status_code=415)
            chunks = bytearray()
            async for chunk in request.stream():
                chunks.extend(chunk)
                if len(chunks) > 1_000_000:
                    return JSONResponse(
                        {"error": "request body too large"}, status_code=413
                    )
            request._body = bytes(chunks)
        response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Cross-Origin-Resource-Policy": "same-origin",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": "frame-ancestors 'none'",
            }
        )
        return response

    @app.exception_handler(Conflict)
    async def conflict(request: Request, exc: Conflict) -> Response:
        return JSONResponse({"error": str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid(request: Request, exc: ValueError) -> Response:
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(KeyError)
    async def missing(request: Request, exc: KeyError) -> Response:
        return JSONResponse({"error": "not found"}, status_code=404)

    def role(request: Request, *, owner: bool = False, cid: str | None = None) -> str:
        headers = request.headers.getlist("authorization")
        scheme, _, token = (
            headers[0].partition(" ") if len(headers) == 1 else ("", "", "")
        )
        if (
            scheme.lower() == "bearer"
            and token
            and hmac.compare_digest(token.encode(), config.owner_key.encode())
        ):
            return "owner"
        if (
            not owner
            and config.chat_key
            and scheme.lower() == "bearer"
            and hmac.compare_digest(token.encode(), config.chat_key.encode())
        ):
            if cid is None or cid == app.state.shared:
                return "chat"
            raise HTTPException(
                403, "this credential only opens the shared conversation"
            )
        raise HTTPException(
            401, "owner key required" if owner else "access key required"
        )

    async def body(request: Request) -> dict[str, Any]:
        value = await request.json()
        if not isinstance(value, dict):
            raise ValueError("body must be an object")  # noqa: TRY004 — HTTP input validation
        json.dumps(value, ensure_ascii=False).encode()
        return value

    @app.get("/")
    async def index() -> Response:
        if config.dashboard:
            html = (
                (assets() / "console.html")
                .read_text()
                .replace('id="managed-link" hidden', 'id="managed-link"')
            )
            return Response(html, media_type="text/html")
        return FileResponse(assets() / "chat.html", media_type="text/html")

    @app.get("/chat/{asset}")
    async def chat_asset(asset: str) -> Response:
        if asset not in ("chat.js", "chat.css", "app.js"):
            raise HTTPException(404)
        return FileResponse(assets() / asset)

    @app.get("/conversations")
    @app.get("/chat")
    async def conversations_page() -> Response:
        return FileResponse(assets() / "chat.html", media_type="text/html")

    @app.get("/api/conversations")
    async def listing(request: Request) -> dict[str, Any]:
        access = role(request)
        manager: Conversations = app.state.conversations
        rows = manager.list_conversations()
        if access == "chat":
            rows = [row for row in rows if row["id"] == app.state.shared]
        return {
            "conversations": rows,
            "role": access,
            "shared": app.state.shared,
            "title": config.title,
            "dashboard": config.dashboard,
            "profiles": [
                {"id": name, "harness": p.harness, "cwd": str(p.cwd)}
                for name, p in config.profiles.items()
            ]
            if access == "owner"
            else [],
        }

    @app.post("/api/conversations")
    async def create(request: Request) -> dict[str, Any]:
        role(request, owner=True)
        data = await body(request)
        if (
            set(data) - {"profile", "name"}
            or not isinstance(data.get("profile"), str)
            or not isinstance(data.get("name", ""), str)
        ):
            raise ValueError("supply a profile and optional name")
        manager: Conversations = app.state.conversations
        cid = manager.create(data["profile"], data.get("name", ""))
        return manager.snapshot(cid)

    @app.get("/api/conversations/{cid}")
    async def snapshot(cid: str, request: Request) -> dict[str, Any]:
        role(request, cid=cid)
        return app.state.conversations.snapshot(cid)

    @app.get("/api/conversations/{cid}/history")
    async def history(
        cid: str, request: Request, before: int | None = None
    ) -> dict[str, Any]:
        role(request, cid=cid)
        return {"history": app.state.conversations.history(cid, before)}

    @app.get("/api/conversations/{cid}/events")
    async def events(cid: str, request: Request) -> Response:
        role(request, cid=cid)
        manager: Conversations = app.state.conversations
        rt = manager._rt(cid)

        async def stream() -> AsyncIterator[str]:
            # Always begin with a full bounded snapshot. Reconnect cursors cannot leave a history gap.
            while not manager.closed:
                changed = rt.changed
                snapshot = manager.snapshot(cid)
                yield f"id: {snapshot['revision']}\nevent: snapshot\ndata: {json.dumps(snapshot)}\n\n"
                try:
                    await asyncio.wait_for(changed.wait(), 15)
                    await asyncio.sleep(
                        0.05
                    )  # coalesce token bursts; no per-viewer queue
                except TimeoutError:
                    yield ": keepalive\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    @app.post("/api/conversations/{cid}/messages")
    async def submit(cid: str, request: Request) -> dict[str, Any]:
        role(request, cid=cid)
        data = await body(request)
        if (
            set(data) - {"request_id", "text", "interrupt", "target"}
            or type(data.get("interrupt", False)) is not bool
        ):
            raise ValueError("invalid message fields")
        return app.state.conversations.submit(
            cid,
            data.get("request_id"),
            data.get("text"),
            redirect=data.get("interrupt", False),
            target=data.get("target"),
        )

    @app.post("/api/conversations/{cid}/resume")
    async def resume(cid: str, request: Request) -> dict[str, bool]:
        role(request, owner=True, cid=cid)
        app.state.conversations.resume(cid)
        return {"ok": True}

    @app.post("/api/conversations/{cid}/requests/{rid}/{action}")
    async def queued(
        cid: str, rid: str, action: str, request: Request
    ) -> dict[str, bool]:
        role(request, owner=True, cid=cid)
        manager: Conversations = app.state.conversations
        if action == "discard":
            manager.discard(cid, rid)
        elif action == "resolve":
            manager.resolve(cid, rid)
        else:
            raise HTTPException(404)
        return {"ok": True}

    @app.post("/api/conversations/{cid}/approvals/{aid}")
    async def approve(cid: str, aid: str, request: Request) -> dict[str, bool]:
        role(request, owner=True, cid=cid)
        data = await body(request)
        if set(data) - {"allow", "answers"} or (
            "allow" in data and type(data["allow"]) is not bool
        ):
            raise ValueError("invalid approval response")
        await app.state.conversations.answer(cid, aid, data)
        return {"ok": True}

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def legacy(path: str, request: Request) -> Response:
        dashboard = app.state.dashboard
        if dashboard is None:
            raise HTTPException(404)
        return await dashboard.request(request, role, body)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a standalone agent interface or the Arke dashboard"
    )
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = load(args.config)
    import uvicorn

    # Multiple server workers must never race to own the same native conversation.
    uvicorn.run(
        create_app(config),
        host=config.host,
        port=config.port,
        workers=1,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()

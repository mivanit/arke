"""Standalone conversation app and optional full dashboard, built from the same UI/API."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from .harnesses import Adapter, Launch
from .harnesses.base import native_data_paths
from .runner import Runner, validate_isolation
from .sessions import Conflict, Conversations
from .webconfig import WebConfig, load

if TYPE_CHECKING:
    from uvicorn import Config

HTTP_DRAIN_TIMEOUT = 5


def assets() -> Path:
    installed = Path(__file__).parent / "web"
    return installed if installed.is_dir() else Path(__file__).parent.parent / "web"


def shared_protected_paths(
    config: WebConfig,
    manager: Conversations,
    shared: str | None,
    protected: tuple[Path, ...],
) -> tuple[Path, ...]:
    """Inventory known private native storage, including retired launch profiles."""
    paths = list(protected)
    # Also protect the current owner's native stores used by terminal agents.
    for harness in ("claude", "codex"):
        paths.extend(native_data_paths(Launch(harness, Path.cwd())))
    for name, profile in config.profiles.items():
        if name != config.shared_profile:
            paths.extend(native_data_paths(profile))
    for old in manager.db.execute(
        "SELECT id,profile,runner,native_home,native_roots FROM conversations c "
        "WHERE (native_id IS NOT NULL OR EXISTS (SELECT 1 FROM requests r WHERE r.conversation=c.id))"
    ):
        if old["id"] == shared:
            continue
        if old["profile"] == config.shared_profile:
            raise ValueError(
                "this profile has private native history; use a fresh shared profile and dedicated native_home"
            )
        if old["native_roots"] is not None:
            paths.extend(Path(value) for value in json.loads(old["native_roots"]))
        elif old["native_home"]:
            paths.append(Path(old["native_home"]))
        # Legacy local records never captured historical environment overrides.
        # Current native roots are protected above; do not invent past locations.
    return tuple(dict.fromkeys(path.resolve() for path in paths))


@asynccontextmanager
async def service_lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Own the service once. Mounting hosts explicitly enter this context.

    The HTTP server must bound its request drain before entering this shutdown;
    a child ASGI lifespan cannot stop a parent waiting forever on open streams.
    """
    if getattr(app.state, "service_running", False):
        raise RuntimeError("conversation service already has a lifetime owner")
    config: WebConfig = app.state.config
    if config.chat_key and (
        not config.shared_profile or config.chat_key == config.owner_key
    ):
        raise ValueError("chat access needs a shared profile and distinct credential")
    shared_launch = (
        config.profiles.get(config.shared_profile) if config.shared_profile else None
    )
    if config.chat_key and shared_launch is None:
        raise ValueError("shared_profile must name a configured profile")
    for launch in config.profiles.values():
        if launch.runner not in ("local", "container"):
            raise ValueError("runner must be local or container")
        native_data_paths(launch)  # reject unused/misleading execution configuration
    if config.chat_key:
        assert shared_launch is not None
        validate_isolation(
            shared_launch,
            (config.state, *config.protected_paths),
        )
    manager = Conversations(
        config.state / "conversations.sqlite3", config.profiles, app.state.factory
    )
    app.state.service_running = True
    app.state.conversations = manager
    app.state.shared = None
    app.state.dashboard = None
    try:
        protected = (config.state, *config.protected_paths)
        if config.dashboard:
            from .dashboard import Dashboard

            # Acquire resources and resolve private bus paths, but start no
            # background work until sharing and mount validation have succeeded.
            app.state.dashboard = Dashboard(config, manager)
            protected += app.state.dashboard.protected_paths
        old_shared = manager.db.execute(
            "SELECT value FROM metadata WHERE key='shared_conversation'"
        ).fetchone()
        proposed_shared = old_shared[0] if old_shared else None

        def validate_launch(launch: Launch) -> Launch:
            paths = protected
            if config.chat_key and launch is shared_launch:
                paths = shared_protected_paths(
                    config, manager, app.state.shared or proposed_shared, paths
                )
            if launch.runner == "container":
                try:
                    validate_isolation(launch, paths)
                except ValueError as exc:
                    if config.chat_key and launch is shared_launch:
                        raise ValueError(
                            f"shared mounts must be exclusive of private native history and service state: {exc}"
                        ) from exc
                    raise
            return replace(launch, protected_paths=paths)

        # Validate before get_or_create_shared commits its new identity. Invalid
        # configuration can then be corrected without abandoning the database.
        for launch in config.profiles.values():
            validate_launch(launch)
        if config.shared_profile:
            app.state.shared = manager.get_or_create_shared(
                config.shared_profile, config.title
            )

        async def prepare_launch(cid: str, launch: Launch) -> Launch:
            checked = validate_launch(launch)
            if app.state.dashboard:
                checked = await app.state.dashboard.prepare_launch(cid, checked)
            return checked

        manager.prepare_launch = prepare_launch
        if app.state.dashboard:
            await app.state.dashboard.start()
        yield
    finally:
        if app.state.dashboard:
            app.state.dashboard.begin_shutdown()
        try:
            await manager.close()
        finally:
            try:
                if app.state.dashboard:
                    await app.state.dashboard.stop()
            finally:
                app.state.service_running = False


def create_app(
    config: WebConfig, factory: Callable[[Launch], Adapter] = Runner
) -> FastAPI:
    """Build the API; mounting hosts must explicitly enter service_lifespan(app)."""
    app = FastAPI(
        lifespan=service_lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.config = config
    app.state.factory = factory

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
        if data["profile"] == config.shared_profile:
            raise ValueError(
                "the shared profile is reserved for its shared conversation"
            )
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
        return app.state.conversations.history_page(cid, before)

    @app.get("/api/conversations/{cid}/history/{seq}")
    async def history_item(cid: str, seq: int, request: Request) -> dict[str, Any]:
        role(request, cid=cid)
        return app.state.conversations.history_item(cid, seq)

    @app.get("/api/conversations/{cid}/events")
    async def events(cid: str, request: Request) -> Response:
        role(request, cid=cid)
        manager: Conversations = app.state.conversations
        rt = manager._rt(cid)

        async def stream() -> AsyncIterator[str]:
            cursor: int | None = None
            while not manager.closed:
                changed = rt.changed
                snapshot = manager.snapshot(cid, history_limit=0)
                for field in ("history", "has_earlier", "history_mode"):
                    snapshot.pop(field)
                tail = manager.history_tail(cid)
                if cursor is None or tail != cursor:
                    snapshot.update(manager.history_page(cid, after=cursor or 0))
                    cursor = tail
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


def server_config(config: WebConfig, app: FastAPI | None = None) -> Config:
    """Standalone serving policy; embedding hosts must also bound HTTP drain."""
    from uvicorn import Config

    return Config(
        app if app is not None else create_app(config),
        host=config.host,
        port=config.port,
        workers=1,
        proxy_headers=False,
        timeout_graceful_shutdown=HTTP_DRAIN_TIMEOUT,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a standalone agent interface or the Arke dashboard"
    )
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    from uvicorn import Server

    # Multiple workers must never race to own the same native conversation.
    Server(server_config(load(args.config))).run()


if __name__ == "__main__":
    main()

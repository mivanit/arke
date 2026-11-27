"""Codex app-server over stdio, using the user's native ChatGPT login."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

from .base import (
    Event,
    Launch,
    SubscriptionRequired,
    native_reference,
    subscription_environment,
)
from .events import READ_BATCH, EventBuffer, native_buffer

MCP_START_TIMEOUT = 30.0


def mcp_tool_approval(method: str, params: dict[str, Any]) -> bool:
    """Recognize Codex's allow-once prompt, not arbitrary MCP forms or URLs."""
    meta = params.get("_meta")
    return (
        method == "mcpServer/elicitation/request"
        and params.get("mode") == "form"
        and isinstance(meta, dict)
        and meta.get("codex_approval_kind") == "mcp_tool_call"
        and params.get("requestedSchema") == {"type": "object", "properties": {}}
    )


def subscription_config(config: dict[str, Any]) -> None:
    """Validate effective native configuration, not just the account label.

    Do not try to reinterpret custom provider configurations as subscription
    access. Reject them before creating or submitting a native conversation.
    """
    if (
        config.get("model_provider") != "openai"
        or config.get("forced_login_method") != "chatgpt"
    ):
        raise SubscriptionRequired(
            "Codex effective configuration must use ChatGPT login and the OpenAI provider"
        )
    providers = config.get("model_providers") or {}
    if (
        not isinstance(providers, dict)
        or providers.get("openai")
        or config.get("openai_base_url")
        or config.get("chatgpt_base_url")
        not in (
            None,
            "https://chatgpt.com/backend-api",
            "https://chatgpt.com/backend-api/",
        )
    ):
        raise SubscriptionRequired(
            "Codex subscription mode does not accept custom OpenAI provider or endpoint overrides"
        )


class Codex:
    def __init__(self, launch: Launch, *, external_sandbox: bool = False) -> None:
        self.launch = launch
        self.external_sandbox = external_sandbox
        self.process: asyncio.subprocess.Process | None = None
        self.reader: asyncio.Task[None] | None = None
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.requests: dict[str, tuple[Any, str, dict[str, Any]]] = {}
        self.events: EventBuffer[dict[str, Any]] = native_buffer()
        self.failure: Exception | None = None
        self.sequence = 0
        self.thread = ""
        self.turn = ""

    async def _write(self, message: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise ConnectionError("Codex is not connected")
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        await self.process.stdin.drain()

    async def _call(self, method: str, params: dict[str, Any]) -> Any:
        if self.failure is not None:
            raise self.failure
        self.sequence += 1
        number = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[number] = future
        try:
            await self._write({"id": number, "method": method, "params": params})
            return await asyncio.wait_for(future, 60)
        finally:
            self.pending.pop(number, None)

    async def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        failure: Exception = ConnectionError("Codex app-server disconnected")
        batch = 0
        try:
            while line := await self.process.stdout.readline():
                batch += 1
                if batch >= READ_BATCH:
                    batch = 0
                    await asyncio.sleep(0)
                message = json.loads(line)
                if "method" not in message:
                    future = self.pending.get(message.get("id"))
                    if future is not None and not future.done():
                        if "error" in message:
                            future.set_exception(
                                RuntimeError(
                                    str(
                                        message["error"].get(
                                            "message", "Codex request failed"
                                        )
                                    )
                                )
                            )
                        else:
                            future.set_result(message.get("result", {}))
                else:
                    # Never wait for the browser here: responses to interrupt/start use this reader too.
                    self.events.put_nowait(message)
        except Exception as exc:  # noqa: BLE001 — forward reader failures to the turn owner
            failure = exc
        finally:
            self.failure = failure
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(failure)
            self.events.fail(failure)

    async def open(self, native_id: str | None) -> str:
        native_reference(native_id)
        env = subscription_environment("codex")
        command = [
            self.launch.executable or "codex",
            "app-server",
            "--listen",
            "stdio://",
            "-c",
            'forced_login_method="chatgpt"',
            "-c",
            'model_provider="openai"',
        ]
        # Owner-provided MCP tools, identically usable in standalone and dashboard apps.
        for name, config in self.launch.mcp_servers.items():
            if not name.replace("_", "").replace("-", "").isalnum():
                raise ValueError("invalid MCP server name")
            for key, value in config.items():
                if key not in ("command", "args", "env", "url", "enabled"):
                    raise ValueError(f"unsupported Codex MCP setting: {key}")
                # JSON scalars/arrays also have TOML syntax; env tables need inline TOML.
                encoded = (
                    "{"
                    + ", ".join(
                        f"{json.dumps(k)} = {json.dumps(v)}" for k, v in value.items()
                    )
                    + "}"
                    if isinstance(value, dict)
                    else json.dumps(value)
                )
                command += ["-c", f"mcp_servers.{name}.{key}={encoded}"]
        self.process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self.launch.cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=8 * 1024 * 1024,
        )
        self.reader = asyncio.create_task(self._read())
        await self._call(
            "initialize", {"clientInfo": {"name": "arke", "version": "0.2.0"}}
        )
        await self._write({"method": "initialized"})
        effective = await self._call(
            "config/read", {"cwd": str(self.launch.cwd), "includeLayers": False}
        )
        subscription_config(effective.get("config", {}))
        # Inspect the managed login without forcing OAuth rotation on every
        # worker start. Codex owns refresh when needed for native requests.
        account = await self._call("account/read", {"refreshToken": False})
        if (account.get("account") or {}).get("type") != "chatgpt":
            raise SubscriptionRequired(
                "Codex needs a ChatGPT subscription login; run codex login on the host"
            )
        params: dict[str, Any] = {
            "cwd": str(self.launch.cwd),
            "approvalPolicy": "untrusted",
            "approvalsReviewer": "user",
            "sandbox": "workspace-write",
            "modelProvider": "openai",
        }
        if self.launch.model:
            params["model"] = self.launch.model
        if self.launch.instructions:
            params["developerInstructions"] = self.launch.instructions
        if native_id:
            params["threadId"] = native_id
        result = await self._call(
            "thread/resume" if native_id else "thread/start", params
        )
        self.thread = native_reference(result["thread"]["id"]) or ""
        # An explicit resume must not attach and submit into someone else's running turn.
        if any(
            t.get("status") == "inProgress" for t in result["thread"].get("turns", [])
        ):
            raise RuntimeError(
                "native conversation still has an active turn; inspect it before resuming"
            )
        await self._wait_for_mcp()
        return self.thread

    async def _wait_for_mcp(self) -> None:
        """Thread creation can return before its MCP catalog is available."""
        pending = {
            name
            for name, config in self.launch.mcp_servers.items()
            if config.get("enabled", True)
        }
        if not pending:
            return
        try:
            async with asyncio.timeout(MCP_START_TIMEOUT):
                while pending:
                    for name in sorted(pending):
                        result = await self._call(
                            "mcpServerStatus/list",
                            {"threadId": self.thread, "serverName": name},
                        )
                        for server in result.get("data", []):
                            if server.get("name") != name:
                                continue
                            status = server.get("runtimeStatus")
                            error = server.get("toolsError")
                            if error or status in (
                                "authenticationRequired",
                                "failed",
                                "cancelled",
                                "disabled",
                            ):
                                raise RuntimeError(
                                    f"Codex MCP server {name!r} is unavailable: {error or status}"
                                )
                            if status == "connected":
                                pending.remove(name)
                                break
                    if pending:
                        await asyncio.sleep(0.1)
        except TimeoutError:
            raise RuntimeError(
                "Timed out waiting for Codex MCP servers: " + ", ".join(sorted(pending))
            ) from None

    async def run(self, text: str) -> AsyncIterator[Event]:
        params: dict[str, Any] = {
            "threadId": self.thread,
            "input": [{"type": "text", "text": text}],
            "approvalPolicy": "untrusted",
            "approvalsReviewer": "user",
        }
        if self.external_sandbox:
            # The supervisor already started us inside its restricted container.
            # Docker owns filesystem/network isolation; nested bubblewrap cannot
            # create namespaces under those restrictions. Apply on every turn,
            # including the first turn after native resume. Approval is separate.
            params["sandboxPolicy"] = {
                "type": "externalSandbox",
                "networkAccess": "enabled",
            }
        result = await self._call(
            "turn/start",
            params,
        )
        self.turn = result["turn"]["id"]
        yield Event("started", {"turn_id": self.turn})
        while True:
            message = await self.events.get()
            if isinstance(message, Exception):
                raise message
            method, params = message["method"], message.get("params", {})
            if params.get("threadId", self.thread) != self.thread:
                continue
            if params.get("turnId", self.turn) != self.turn and not (
                method == "mcpServer/elicitation/request"
                and params.get("turnId") is None
            ):
                continue
            if "id" in message:
                request_id = str(message["id"])
                self.requests[request_id] = (message["id"], method, params)
                supported = mcp_tool_approval(method, params) or method in (
                    "item/commandExecution/requestApproval",
                    "item/fileChange/requestApproval",
                    "item/tool/requestUserInput",
                )
                if supported:
                    yield Event(
                        "approval",
                        {
                            "request_id": request_id,
                            "kind": "input"
                            if method.endswith("requestUserInput")
                            else "permission",
                            "details": params,
                        },
                    )
                else:
                    # Unknown protocol requests must fail closed, not hang the native reader.
                    await self._write(
                        {
                            "id": message["id"],
                            "error": {
                                "code": -32601,
                                "message": "This request is not supported by Arke",
                            },
                        }
                    )
                    self.requests.pop(request_id, None)
                    yield Event(
                        "notice", {"text": f"Unsupported native request: {method}"}
                    )
            elif method == "serverRequest/resolved":
                request_id = str(params.get("requestId"))
                self.requests.pop(request_id, None)
                yield Event("approval_resolved", {"request_id": request_id})
            elif method == "item/agentMessage/delta":
                yield Event("delta", {"text": params.get("delta", "")})
            elif method == "item/completed":
                item = params.get("item", {})
                if item.get("type") == "agentMessage":
                    yield Event("message", {"text": item.get("text", "")})
                elif item.get("type") in (
                    "commandExecution",
                    "fileChange",
                    "mcpToolCall",
                ):
                    yield Event("tool", {"details": item})
            elif (
                method == "turn/completed"
                and params.get("turn", {}).get("id") == self.turn
            ):
                turn = params["turn"]
                self.turn = ""
                self.requests.clear()
                yield Event(
                    "completed",
                    {
                        "status": turn.get("status", "failed"),
                        "error": turn.get("error"),
                    },
                )
                return

    async def interrupt(self) -> None:
        if not self.turn:
            raise RuntimeError("turn has not started yet")
        await self._call(
            "turn/interrupt", {"threadId": self.thread, "turnId": self.turn}
        )

    async def respond(self, request_id: str, answer: dict[str, Any]) -> None:
        if self.failure is not None:
            raise self.failure
        request = self.requests.get(request_id)
        if request is None:
            raise ValueError("approval is no longer pending")
        native_id, method, params = request
        if mcp_tool_approval(method, params):
            # No persistence metadata: the owner grants this invocation only.
            result = {
                "action": "accept" if answer.get("allow") is True else "decline",
                "content": {} if answer.get("allow") is True else None,
            }
        elif method.endswith("requestUserInput"):
            answers = answer.get("answers", {})
            allowed = {q["id"] for q in params.get("questions", [])}
            if (
                not isinstance(answers, dict)
                or set(answers) != allowed
                or any(not isinstance(v, str) for v in answers.values())
            ):
                raise ValueError("answer each requested question")
            result = {"answers": {k: {"answers": [v]} for k, v in answers.items()}}
        else:
            result = {
                "decision": "accept" if answer.get("allow") is True else "decline"
            }
        await self._write({"id": native_id, "result": result})
        self.requests.pop(request_id, None)

    async def close(self) -> None:
        if self.process is not None and self.process.returncode is None:
            if self.process.stdin:
                self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.reader is not None:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader

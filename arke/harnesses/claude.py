"""Claude's supported SDK transport, without a terminal or API billing fallback."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import uuid
from collections.abc import AsyncIterator
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
)
from claude_agent_sdk.types import StreamEvent, ToolUseBlock

from .base import (
    Event,
    Launch,
    SubscriptionRequired,
    native_reference,
    subscription_environment,
)
from .events import READ_BATCH, EventBuffer, EventOverflow, event_buffer


class Claude:
    def __init__(self, launch: Launch) -> None:
        self.launch = launch
        self.client: ClaudeSDKClient | None = None
        self.requests: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.events: EventBuffer[Event] = event_buffer()
        self.receiver: asyncio.Task[None] | None = None

    async def _permission(
        self, name: str, inputs: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        if self.events.failure is not None:
            return PermissionResultDeny(message="Native event delivery failed")
        request_id = uuid.uuid4().hex
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self.requests[request_id] = future
        try:
            self.events.put_nowait(
                Event(
                    "approval",
                    {
                        "request_id": request_id,
                        "kind": "input" if name == "AskUserQuestion" else "permission",
                        "details": {
                            "tool": name,
                            "input": inputs,
                            "questions": inputs.get("questions", []),
                        },
                    },
                )
            )
            answer = await future
            if name == "AskUserQuestion":
                answers = answer.get("answers", {})
                expected = {q["question"] for q in inputs.get("questions", [])}
                if (
                    isinstance(answers, dict)
                    and set(answers) == expected
                    and all(isinstance(v, str) for v in answers.values())
                ):
                    return PermissionResultAllow(
                        updated_input=inputs | {"answers": answers}
                    )
            elif answer.get("allow") is True:
                return PermissionResultAllow(updated_input=inputs)
            return PermissionResultDeny(message="Denied by the owner")
        except EventOverflow:
            return PermissionResultDeny(
                message="Native event delivery exceeded its limit"
            )
        finally:
            self.requests.pop(request_id, None)
            self.events.put_nowait(
                Event("approval_resolved", {"request_id": request_id})
            )

    async def open(self, native_id: str | None) -> str | None:
        native_reference(native_id)
        env = subscription_environment("claude")
        executable = self.launch.executable or shutil.which("claude")
        if not executable:
            raise RuntimeError("install Claude Code and sign in with claude auth login")
        process = await asyncio.create_subprocess_exec(
            executable,
            "auth",
            "status",
            "--json",
            cwd=self.launch.cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), 20)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        status = json.loads(stdout or b"{}")
        if (
            process.returncode
            or status.get("authMethod") not in ("claude.ai", "oauth_token")
            or not status.get("loggedIn")
        ):
            raise SubscriptionRequired(
                "Claude needs subscription authentication; run claude auth login on the host"
            )
        options = ClaudeAgentOptions(
            cwd=str(self.launch.cwd),
            cli_path=executable,
            model=self.launch.model,
            resume=native_id,
            env=env,
            setting_sources=[],
            settings=json.dumps(
                {"forceLoginMethod": "claudeai", "disableAllHooks": True}
            ),
            permission_mode="default",
            can_use_tool=self._permission,
            include_partial_messages=True,
            mcp_servers=self.launch.mcp_servers,
            extra_args={"strict-mcp-config": None},
        )
        if self.launch.instructions:
            options.system_prompt = {
                "type": "preset",
                "preset": "claude_code",
                "append": self.launch.instructions,
            }
        self.client = ClaudeSDKClient(options=options)
        await self.client.connect()
        return native_id

    async def _receive(self) -> None:
        assert self.client is not None
        batch = 0
        try:
            async for message in self.client.receive_response():
                batch += 1
                if batch >= READ_BATCH:
                    batch = 0
                    await asyncio.sleep(0)
                if isinstance(message, SystemMessage) and message.subtype == "init":
                    self.events.put_nowait(
                        Event(
                            "session",
                            {
                                "native_id": native_reference(
                                    message.data.get("session_id")
                                )
                            },
                        )
                    )
                elif isinstance(message, StreamEvent):
                    event = message.event
                    if (
                        event.get("type") == "content_block_delta"
                        and event.get("delta", {}).get("type") == "text_delta"
                    ):
                        self.events.put_nowait(
                            Event("delta", {"text": event["delta"]["text"]})
                        )
                elif isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            self.events.put_nowait(
                                Event("message", {"text": block.text})
                            )
                        elif isinstance(block, ToolUseBlock):
                            self.events.put_nowait(
                                Event(
                                    "tool",
                                    {
                                        "details": {
                                            "name": block.name,
                                            "input": block.input,
                                        }
                                    },
                                )
                            )
                elif isinstance(message, ResultMessage):
                    self.events.put_nowait(
                        Event(
                            "session",
                            {"native_id": native_reference(message.session_id)},
                        )
                    )
                    self.events.put_nowait(
                        Event(
                            "completed",
                            {
                                "status": "failed" if message.is_error else "completed",
                                "error": message.result if message.is_error else None,
                            },
                        )
                    )
                    return
            self.events.put_nowait(
                ConnectionError("Claude disconnected before a result")
            )
        except Exception as exc:  # noqa: BLE001 — forward reader failures to the turn owner
            self.events.fail(exc)

    async def run(self, text: str) -> AsyncIterator[Event]:
        assert self.client is not None
        if self.events.failure is not None:
            raise self.events.failure
        await self.client.query(text)
        yield Event("started")
        self.receiver = asyncio.create_task(self._receive())
        try:
            while True:
                event = await self.events.get()
                if isinstance(event, Exception):
                    raise event
                yield event
                if event.kind == "completed":
                    return
        finally:
            if self.receiver is not None:
                self.receiver.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.receiver
                self.receiver = None

    async def interrupt(self) -> None:
        assert self.client is not None
        await self.client.interrupt()
        for future in list(self.requests.values()):
            if not future.done():
                future.set_result({"allow": False})

    async def respond(self, request_id: str, answer: dict[str, Any]) -> None:
        if self.events.failure is not None:
            raise self.events.failure
        future = self.requests.get(request_id)
        if future is None or future.done():
            raise ValueError("approval is no longer pending")
        future.set_result(answer)

    async def close(self) -> None:
        for future in list(self.requests.values()):
            if not future.done():
                future.set_result({"allow": False})
        if self.client is not None:
            await self.client.disconnect()
            self.client = None

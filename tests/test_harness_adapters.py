"""Native protocol fixtures: subscription guard, independent control flow, and tool approvals."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from arke.harnesses import Launch, SubscriptionRequired
from arke.harnesses.base import native_reference, subscription_environment
from arke.harnesses.codex import Codex

CODEX_FIXTURE = r"""
import json, sys
def emit(message):
    print(json.dumps(message), flush=True)
turn = 0
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    rid = message.get("id")
    result = {}
    if method == "initialize":
        result = {"userAgent": "fixture"}
    elif method == "account/read":
        result = {"account": {"type": "chatgpt"}, "requiresOpenaiAuth": True}
    elif method == "config/read":
        result = {"config": {"model_provider": "openai", "forced_login_method": "chatgpt"}}
    elif method in ("thread/start", "thread/resume"):
        result = {"thread": {"id": "thread-fixture", "turns": []}}
    elif method == "turn/start":
        turn += 1
        result = {"turn": {"id": str(turn), "status": "inProgress"}}
        emit({"id": rid, "result": result})
        emit({"method": "item/agentMessage/delta", "params": {"threadId": "thread-fixture", "turnId": str(turn), "delta": "Hello"}})
        emit({"id": "approval-" + str(turn), "method": "item/commandExecution/requestApproval", "params": {"threadId": "thread-fixture", "turnId": str(turn), "command": "echo hello"}})
        continue
    elif method == "turn/interrupt":
        # Completion may arrive before the interrupt response. The reader must accept both.
        emit({"method": "turn/completed", "params": {"threadId": "thread-fixture", "turn": {"id": str(turn), "status": "interrupted"}}})
    elif method is None and str(rid).startswith("approval-"):
        assert message["result"]["decision"] in ("accept", "decline")
        emit({"method": "item/completed", "params": {"threadId": "thread-fixture", "turnId": str(turn), "item": {"type": "agentMessage", "text": "Hello"}}})
        emit({"method": "turn/completed", "params": {"threadId": "thread-fixture", "turn": {"id": str(turn), "status": "completed"}}})
        continue
    if rid is not None:
        emit({"id": rid, "result": result})
"""


CLAUDE_FIXTURE = r"""
import json, sys
def emit(message):
    print(json.dumps(message), flush=True)
def result(error=False):
    emit({"type": "result", "subtype": "success", "duration_ms": 1, "duration_api_ms": 1,
          "is_error": error, "num_turns": 1, "session_id": "claude-fixture", "result": "done"})
if sys.argv[1:3] == ["auth", "status"]:
    emit({"loggedIn": True, "authMethod": "claude.ai"})
    sys.exit(0)
turn = 0
for line in sys.stdin:
    message = json.loads(line)
    if message["type"] == "control_request":
        subtype = message["request"]["subtype"]
        emit({"type": "control_response", "response": {"subtype": "success", "request_id": message["request_id"], "response": {}}})
        if subtype == "interrupt":
            emit({"type": "control_cancel_request", "request_id": "permission-" + str(turn)})
            result()
    elif message["type"] == "user":
        turn += 1
        emit({"type": "system", "subtype": "init", "session_id": "claude-fixture", "claude_code_version": "2.1.310"})
        emit({"type": "control_request", "request_id": "permission-" + str(turn),
              "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "echo hello"}, "tool_use_id": "tool-1"}})
    elif message["type"] == "control_response":
        emit({"type": "assistant", "message": {"model": "fixture", "content": [{"type": "text", "text": "hello"}]}})
        result()
"""


def clear_billing(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in os.environ:
        if name.startswith(("ANTHROPIC_", "OPENAI_", "CODEX_API", "CLAUDE_CODE_USE_")):
            monkeypatch.delenv(name)


def test_codex_protocol_approval_and_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_billing(monkeypatch)
    executable = tmp_path / "codex-fixture"
    executable.write_text(f"#!{sys.executable}\n" + CODEX_FIXTURE)
    executable.chmod(0o700)

    async def scenario() -> None:
        adapter = Codex(Launch("codex", tmp_path, executable=str(executable)))
        try:
            assert await adapter.open(None) == "thread-fixture"
            events = []
            async for event in adapter.run("hello"):
                events.append(event)
                if event.kind == "approval":
                    await adapter.respond(event.data["request_id"], {"allow": True})
            assert [e.kind for e in events] == [
                "started",
                "delta",
                "approval",
                "message",
                "completed",
            ]
            async for event in adapter.run("interrupt me"):
                if event.kind == "approval":
                    await adapter.interrupt()
                if event.kind == "completed":
                    assert event.data["status"] == "interrupted"
        finally:
            await adapter.close()
        assert adapter.process is not None and adapter.process.returncode is not None

    asyncio.run(scenario())


def test_billing_overrides_and_native_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_billing(monkeypatch)
    for harness, variable in (
        ("claude", "ANTHROPIC_API_KEY"),
        ("codex", "CODEX_API_KEY"),
    ):
        monkeypatch.setenv(variable, "not-a-real-key")
        with pytest.raises(SubscriptionRequired, match=variable):
            subscription_environment(harness)
        monkeypatch.delenv(variable)
    monkeypatch.setenv("ARKE_AS", "human")
    assert not subscription_environment("codex")["ARKE_AS"]
    with pytest.raises(ValueError):
        native_reference("--mcp-config=bad")


def test_claude_sdk_transport_and_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arke.harnesses.claude import Claude

    clear_billing(monkeypatch)
    executable = tmp_path / "claude-fixture"
    executable.write_text(f"#!{sys.executable}\n" + CLAUDE_FIXTURE)
    executable.chmod(0o700)

    async def scenario() -> None:
        native = Claude(Launch("claude", tmp_path, executable=str(executable)))
        try:
            await native.open(None)
            kinds = []
            async for event in native.run("hello"):
                kinds.append(event.kind)
                if event.kind == "approval":
                    await native.respond(event.data["request_id"], {"allow": True})
            assert (
                "approval" in kinds and "message" in kinds and kinds[-1] == "completed"
            )
            async for event in native.run("stop while waiting for approval"):
                if event.kind == "approval":
                    await native.interrupt()
            assert not native.requests
        finally:
            await native.close()

    asyncio.run(scenario())


def test_claude_auth_and_owner_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_agent_sdk import PermissionResultAllow

    from arke.harnesses.claude import Claude, ToolPermissionContext

    clear_billing(monkeypatch)

    class Auth:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return json.dumps({"loggedIn": True, "authMethod": "api_key"}).encode(), b""

    async def spawn(*args: Any, **kwargs: Any) -> Auth:
        return Auth()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    async def scenario() -> None:
        adapter = Claude(Launch("claude", tmp_path, executable="fixture"))
        with pytest.raises(SubscriptionRequired):
            await adapter.open(None)
        task = asyncio.create_task(
            adapter._permission(
                "Bash", {"command": "echo hello"}, ToolPermissionContext()
            )
        )
        approval = await adapter.events.get()
        assert not isinstance(approval, Exception)
        assert not task.done()
        await adapter.respond(approval.data["request_id"], {"allow": True})
        result = await task
        assert isinstance(result, PermissionResultAllow)
        assert result.updated_input == {"command": "echo hello"}
        with pytest.raises(ValueError):
            await adapter.respond(approval.data["request_id"], {"allow": True})

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "overrides",
    [
        {"model_provider": "custom"},
        {"forced_login_method": "api"},
        {"model_providers": {"openai": {"base_url": "https://example.invalid"}}},
        {"openai_base_url": "https://example.invalid"},
        {"chatgpt_base_url": "https://example.invalid"},
    ],
)
def test_codex_rejects_effective_provider_overrides(overrides: dict[str, Any]) -> None:
    from arke.harnesses.codex import subscription_config

    with pytest.raises(SubscriptionRequired):
        subscription_config(
            {"model_provider": "openai", "forced_login_method": "chatgpt"} | overrides
        )


def test_codex_accepts_native_default_chatgpt_backend() -> None:
    from arke.harnesses.codex import subscription_config

    subscription_config(
        {
            "model_provider": "openai",
            "forced_login_method": "chatgpt",
            "chatgpt_base_url": "https://chatgpt.com/backend-api/",
        }
    )

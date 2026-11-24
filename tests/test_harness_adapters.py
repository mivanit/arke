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
from tests.native_fixtures import CLAUDE_FIXTURE, CODEX_FIXTURE


def clear_billing(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in os.environ:
        if name.startswith(("ANTHROPIC_", "OPENAI_", "CODEX_API", "CLAUDE_CODE_USE_")):
            monkeypatch.delenv(name)


@pytest.mark.parametrize("external_sandbox", [False, True])
def test_codex_protocol_approval_and_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, external_sandbox: bool
) -> None:
    clear_billing(monkeypatch)
    executable = tmp_path / "codex-fixture"
    policy = (
        {"type": "externalSandbox", "networkAccess": "enabled"}
        if external_sandbox
        else None
    )
    source = CODEX_FIXTURE.replace(
        '    elif method == "turn/start":',
        '    elif method == "turn/start":\n'
        '        assert message["params"]["approvalPolicy"] == "untrusted"\n'
        '        assert message["params"]["approvalsReviewer"] == "user"\n'
        f'        assert message["params"].get("sandboxPolicy") == {policy!r}',
    )
    executable.write_text(f"#!{sys.executable}\n" + source)
    executable.chmod(0o700)

    async def scenario() -> None:
        launch = Launch("codex", tmp_path, executable=str(executable))
        adapter = Codex(launch, external_sandbox=external_sandbox)
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
        resumed = Codex(launch, external_sandbox=external_sandbox)
        try:
            assert await resumed.open("thread-fixture") == "thread-fixture"
            async for event in resumed.run("after restart"):
                if event.kind == "approval":
                    await resumed.respond(event.data["request_id"], {"allow": False})
        finally:
            await resumed.close()

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


def test_raw_adapter_refuses_container_execution_settings(tmp_path: Path) -> None:
    from arke.harnesses import adapter

    with pytest.raises(ValueError, match="Runner"):
        adapter(
            Launch(
                "codex", tmp_path, runner="container", native_home=tmp_path / "native"
            )
        )

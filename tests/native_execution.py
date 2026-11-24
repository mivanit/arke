"""Real CLI tool execution against a loopback scripted model, inside an offline image.

This deliberately uses test provider settings through the native protocols, not
through Arke's subscription adapters. Production authentication is never bypassed.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    ResultMessage,
    ToolPermissionContext,
)

from arke.harnesses import Launch
from arke.harnesses.codex import Codex
from tests.scripted_model import ScriptedModel


def require_offline_container() -> None:
    # Fail closed if somebody invokes the script directly on their workstation.
    if not Path("/.dockerenv").exists():
        raise RuntimeError("run this probe through make test-deployment")
    if set(os.listdir("/sys/class/net")) != {"lo"}:
        raise RuntimeError("native execution requires Docker --network=none")


class CommandModel(ScriptedModel):
    def __init__(self, command: str) -> None:
        super().__init__()
        self.command = command

    def next_action(self, body: dict[str, Any]) -> dict[str, Any] | None:
        if len(self.requests) > 6:
            raise ValueError("unexpected extra model request")
        history = body.get("messages", body.get("input", []))
        for message in history:
            if message.get("type") in (
                "function_call_output",
                "custom_tool_call_output",
            ):
                return None
            content = message.get("content", [])
            if isinstance(content, list) and any(
                part.get("type") == "tool_result" for part in content
            ):
                return None
        self.actions.append("execute marker command")
        if "messages" in body:
            return {
                "id": "call_marker",
                "name": "Bash",
                "arguments": {"command": self.command},
            }
        return {
            "id": "call_marker",
            "name": "exec",
            "namespace": "functions",
            "arguments": "text(await tools.exec_command("
            + json.dumps({"cmd": self.command})
            + "));",
        }


async def codex_command(root: Path, model: CommandModel) -> int:
    home = Path(os.environ["CODEX_HOME"])
    home.mkdir(parents=True)
    (home / "config.toml").write_text(
        'model_provider="arke_fixture"\nweb_search="disabled"\n'
        '[model_providers.arke_fixture]\nname="Offline test"\n'
        f'base_url="{model.url}/v1"\nwire_api="responses"\n'
        "requires_openai_auth=false\nrequest_max_retries=0\nstream_max_retries=0\n"
    )
    # Reuse event/approval translation, but explicitly initialize the test provider.
    # Codex.open() remains subscription-only and is tested separately with fixtures.
    native = Codex(Launch("codex", root))
    approvals = 0
    try:
        native.process = await asyncio.create_subprocess_exec(
            "codex",
            "app-server",
            "--listen",
            "stdio://",
            cwd=root,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=8 * 1024 * 1024,
        )
        native.reader = asyncio.create_task(native._read())
        await native._call(
            "initialize", {"clientInfo": {"name": "arke-ci", "version": "1"}}
        )
        await native._write({"method": "initialized"})
        result = await native._call(
            "thread/start",
            {
                "cwd": str(root),
                "model": "gpt-6.1-sol",
                "modelProvider": "arke_fixture",
                "approvalPolicy": "untrusted",
                "sandbox": "workspace-write",
            },
        )
        native.thread = result["thread"]["id"]
        async for event in native.run("Execute the fixture command, then finish."):
            if event.kind == "approval":
                assert event.data["details"]["command"], event
                approvals += 1
                await native.respond(event.data["request_id"], {"allow": True})
            if event.kind == "completed":
                assert event.data["status"] == "completed", event
    finally:
        await native.close()
    return approvals


async def claude_command(root: Path, model: CommandModel) -> int:
    approvals = 0

    async def allow(
        name: str, inputs: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow:
        nonlocal approvals
        assert name == "Bash" and inputs["command"] == model.command
        approvals += 1
        return PermissionResultAllow(updated_input=inputs)

    client = ClaudeSDKClient(
        options=ClaudeAgentOptions(
            cli_path=shutil.which("claude"),
            cwd=str(root),
            model="claude-sonnet-4-5-20250929",
            setting_sources=[],
            settings=json.dumps({"disableAllHooks": True}),
            extra_args={"strict-mcp-config": None},
            permission_mode="default",
            can_use_tool=allow,
            env={
                "ANTHROPIC_BASE_URL": model.url,
                "ANTHROPIC_API_KEY": "test-unused",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            },
        )
    )
    completed = False
    try:
        await client.connect()
        await client.query("Execute the fixture command, then finish.")
        async for message in client.receive_response():
            if isinstance(message, ResultMessage):
                assert not message.is_error, message
                completed = True
    finally:
        await client.disconnect()
    assert completed
    return approvals


async def image_service(root: Path) -> None:
    """Start the installed web entry point and serve its packaged assets."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    config = root / "web.toml"
    state = root / "web-state"
    config.write_text(
        f"[web]\nport={port}\nstate_dir={json.dumps(str(state))}\n"
        f'[profiles.test]\nharness="codex"\ncwd={json.dumps(str(root))}\n'
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "arke.webapp",
        "--config",
        str(config),
        stdout=asyncio.subprocess.DEVNULL,
    )

    def get(path: str, key: str = "") -> tuple[int, str]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            headers={"Authorization": "Bearer " + key} if key else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode()

    try:
        async with asyncio.timeout(20):
            while True:
                assert process.returncode is None, "installed web entry point exited"
                try:
                    status, html = await asyncio.to_thread(get, "/")
                    break
                except urllib.error.URLError:
                    await asyncio.sleep(0.1)
        assert status == 200 and "<arke-chat>" in html
        assert (await asyncio.to_thread(get, "/chat/chat.js"))[0] == 200
        assert (await asyncio.to_thread(get, "/api/conversations"))[0] == 401
        status, body = await asyncio.to_thread(
            get, "/api/conversations", (state / "owner.key").read_text().strip()
        )
        assert status == 200 and json.loads(body)["profiles"][0]["id"] == "test"
    finally:
        if process.returncode is None:
            process.terminate()
        try:
            async with asyncio.timeout(10):
                await process.wait()
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
    # Uvicorn restores/re-raises the initiating signal after lifespan cleanup.
    assert process.returncode in (0, -signal.SIGTERM)
    print(
        "Passed installed web entry point, authentication, assets and graceful shutdown.",
        flush=True,
    )


async def check() -> None:
    require_offline_container()
    import arke.webapp

    # Never mask an image packaging defect by importing the checkout's app.
    assert Path(arke.webapp.__file__).is_relative_to("/app")
    with tempfile.TemporaryDirectory(
        dir=Path.cwd(), prefix="native-execution-"
    ) as temp:
        directory = Path(temp)
        # SDK env overlays the process environment; scrub the whole test process.
        path = os.environ["PATH"]
        os.environ.clear()
        os.environ.update(
            {
                "PATH": path,
                "HOME": str(directory / "home"),
                "CODEX_HOME": str(directory / "home/.codex"),
                "CLAUDE_CONFIG_DIR": str(directory / "home/.claude"),
                "TMPDIR": str(directory),
                "NO_COLOR": "1",
            }
        )
        Path(os.environ["HOME"]).mkdir()
        await image_service(directory)
        for harness, execute in (("claude", claude_command), ("codex", codex_command)):
            root = directory / harness
            root.mkdir()
            git = await asyncio.create_subprocess_exec(
                "git", "init", "--quiet", str(root)
            )
            assert await git.wait() == 0
            marker = root / "executed.txt"
            command = 'python3 -c \'from pathlib import Path; Path("executed.txt").write_text("native command ran")\''
            with CommandModel(command) as model:
                try:
                    async with asyncio.timeout(120):
                        approvals = await execute(root, model)
                    assert approvals > 0, f"{harness}: owner approval not exercised"
                    assert marker.read_text() == "native command ran"
                    assert model.actions and not model.errors
                except BaseException:
                    # Only synthetic model traffic; useful when a host's native
                    # sandbox fails even though the CLI itself initialized.
                    print(model.diagnostics(), file=sys.stderr)
                    raise
            print(
                f"Passed {harness} native tool execution and owner approval under production Docker restrictions.",
                flush=True,
            )


if __name__ == "__main__":
    asyncio.run(check())

"""Real CLIs -> stdio MCP -> production worker -> scoped dashboard bus, offline.

Only native initialization is replaced to select the loopback scripted provider.
The adapters' event/permission paths, worker and bus server are production code.
This module is copied into the offline deployment fixture, never the app image.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from arke.dashboard import Dashboard
from arke.harnesses import Adapter, Launch
from arke.harnesses.claude import Claude
from arke.harnesses.codex import Codex
from arke.runner import Runner
from arke.sessions import Conversations
from arke.webconfig import WebConfig
from tests.native_execution import require_offline_container
from tests.scripted_model import ScriptedModel


class BusModel(ScriptedModel):
    def next_action(self, body: dict[str, Any]) -> dict[str, Any] | None:
        if len(self.requests) > 8:
            raise ValueError("unexpected extra MCP model request")
        history = json.dumps(body.get("messages", body.get("input", [])))
        if "call_mcp_send" in history:
            return None
        tool, arguments = "arke_inbox", {}
        if "call_mcp_inbox" in history:
            nonce = re.search(r"MCP-NONCE: ([a-f0-9]{32})", history)
            assert nonce, "native model did not receive the MCP inbox result"
            tool, arguments = (
                "arke_send",
                {
                    "to": ["human"],
                    "text": "MCP echo: " + nonce[1],
                    "request_id": "native-mcp-reply",
                },
            )
        call = {"id": "call_mcp_" + tool.removeprefix("arke_")}
        if "messages" in body:
            name = next(
                (
                    t["name"]
                    for t in body["tools"]
                    if t.get("name", "").endswith("__" + tool)
                ),
                None,
            )
            if name is None:
                assert any(t.get("name") == "ToolSearch" for t in body["tools"]), (
                    f"Claude did not discover the {tool} MCP tool"
                )
                return {
                    "id": "call_mcp_discover",
                    "name": "ToolSearch",
                    "arguments": {
                        "query": "select:mcp__arke__arke_inbox,mcp__arke__arke_send"
                    },
                }
            self.actions.append(tool)
            return call | {"name": name, "arguments": arguments}
        self.actions.append(tool)
        return call | {
            "name": "exec",
            "namespace": "functions",
            "arguments": "const t = ALL_TOOLS.find(t => t.name.endsWith("
            + json.dumps("__" + tool)
            + ")); if (!t) throw new Error('Arke MCP tool missing'); text(await tools[t.name]("
            + json.dumps(arguments)
            + "));",
        }


async def worker(model_url: str) -> None:
    """Test-only initialization; no production auth escape hatch is introduced."""
    require_offline_container()
    from arke import worker as production

    class FixtureCodex(Codex):
        async def open(self, native_id: str | None) -> str:
            assert native_id is None
            home = Path(os.environ["CODEX_HOME"])
            home.mkdir(parents=True, exist_ok=True)
            text = (
                'model_provider="arke_fixture"\nweb_search="disabled"\n'
                '[model_providers.arke_fixture]\nname="Offline MCP test"\n'
                f'base_url="{model_url}/v1"\nwire_api="responses"\n'
                "requires_openai_auth=false\nrequest_max_retries=0\nstream_max_retries=0\n"
            )
            for name, settings in self.launch.mcp_servers.items():
                text += f"\n[mcp_servers.{name}]\n"
                for key, value in settings.items():
                    encoded = (
                        "{"
                        + ", ".join(
                            f"{json.dumps(k)}={json.dumps(v)}" for k, v in value.items()
                        )
                        + "}"
                        if isinstance(value, dict)
                        else json.dumps(value)
                    )
                    text += f"{key}={encoded}\n"
            (home / "config.toml").write_text(text)
            # Keep native startup errors in this synthetic workspace. A missing
            # MCP tool otherwise reports only a later scripted-model failure.
            with (self.launch.cwd / "codex-stderr.log").open("wb") as stderr:
                self.process = await asyncio.create_subprocess_exec(
                    "codex",
                    "app-server",
                    "--listen",
                    "stdio://",
                    cwd=self.launch.cwd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=stderr,
                    limit=8 * 1024 * 1024,
                )
            self.reader = asyncio.create_task(self._read())
            await self._call(
                "initialize", {"clientInfo": {"name": "arke-ci", "version": "1"}}
            )
            await self._write({"method": "initialized"})
            result = await self._call(
                "thread/start",
                {
                    "cwd": str(self.launch.cwd),
                    "model": "gpt-6.1-sol",
                    "modelProvider": "arke_fixture",
                    "approvalPolicy": "untrusted",
                    "approvalsReviewer": "user",
                    "sandbox": "workspace-write",
                },
            )
            self.thread = result["thread"]["id"]
            return self.thread

    class FixtureClaude(Claude):
        async def open(self, native_id: str | None) -> None:
            assert native_id is None
            self.client = ClaudeSDKClient(
                options=ClaudeAgentOptions(
                    cli_path=shutil.which("claude"),
                    cwd=str(self.launch.cwd),
                    model="claude-sonnet-4-5-20250929",
                    setting_sources=[],
                    settings=json.dumps({"disableAllHooks": True}),
                    extra_args={"strict-mcp-config": None},
                    mcp_servers=self.launch.mcp_servers,
                    permission_mode="default",
                    can_use_tool=self._permission,
                    env={
                        "ANTHROPIC_BASE_URL": model_url,
                        "ANTHROPIC_API_KEY": "test-unused",
                        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                    },
                )
            )
            await self.client.connect()

    def adapter(launch: Launch, *, external_sandbox: bool = False) -> Adapter:
        return (
            FixtureCodex(launch, external_sandbox=True)
            if launch.harness == "codex"
            else FixtureClaude(launch)
        )

    with patch.object(production, "adapter", adapter):
        await production.serve()


async def check(root: Path, harness: str) -> None:
    require_offline_container()
    root.mkdir()
    workspace = root / "workspace"
    workspace.mkdir()
    bus_config = root / "bus.toml"
    bus_config.write_text(
        f"[server]\nstate_dir={json.dumps(str(root / 'bus'))}\ntranscript_roots=[]\n"
    )
    config = WebConfig(
        state=root / "state",
        profiles={"test": Launch(harness, workspace)},
        owner_key="a" * 43,
        bus_config=bus_config,
    )
    manager = Conversations(root / "state/conversations.sqlite3", config.profiles)
    dashboard = Dashboard(config, manager)
    runner: Runner | None = None
    model = BusModel()
    try:
        cid = manager.create("test")
        launch = await dashboard.prepare_launch(cid, config.profiles["test"])
        nonce = uuid.uuid4().hex
        agent = dashboard.bus.agents["managed-" + cid]
        dashboard.bus.send(
            "human", {"to": [agent["name"]], "text": "MCP-NONCE: " + nonce}
        )
        with model:

            class ProbeRunner(Runner):
                def command(self) -> list[str]:
                    # The whole acceptance probe is already inside the offline
                    # worker container. Only test initialization is substituted.
                    return [
                        sys.executable,
                        "-c",
                        "import runpy,sys; sys.path.insert(0, "
                        + repr(str(Path(__file__).resolve().parents[1]))
                        + "); sys.argv=['native-mcp', "
                        + repr(model.url)
                        + "]; runpy.run_module('tests.native_mcp', run_name='__main__')",
                    ]

            runner = ProbeRunner(launch)
            completed = False
            approvals = 0
            async with asyncio.timeout(120):
                await runner.open(None)
                async for event in runner.run(
                    "Read your Arke inbox and reply to the human."
                ):
                    if event.kind == "approval":
                        approvals += 1
                        await runner.respond(event.data["request_id"], {"allow": True})
                    if event.kind == "completed":
                        assert event.data["status"] == "completed", event
                        completed = True
            assert completed and model.actions == ["arke_inbox", "arke_send"], (
                model.diagnostics()
            )
            assert not model.errors, model.diagnostics()
            assert approvals >= 2, "MCP calls did not request owner approval"
            replies = [
                message
                for message in dashboard.bus.messages
                if message.get("text") == "MCP echo: " + nonce
            ]
            assert len(replies) == 1, "native MCP reply did not reach the bus"
            assert replies[0]["from"] == agent["name"], replies
    except BaseException:
        stderr = workspace / "codex-stderr.log"
        if stderr.exists():
            with stderr.open("rb") as log:
                log.seek(max(0, stderr.stat().st_size - 16384))
                print(log.read().decode(errors="replace"), file=sys.stderr)
        print(model.diagnostics(), file=sys.stderr)
        raise
    finally:
        if runner is not None:
            await runner.close()
        dashboard.begin_shutdown()
        await manager.close()
        await dashboard.stop()
    print(
        f"Passed {harness} native MCP discovery, inbox result and scoped bus reply.",
        flush=True,
    )


if __name__ == "__main__":
    # Ensure the mounted test package and installed app are importable in the
    # child without modifying the worker's native/MCP environment.
    asyncio.run(worker(sys.argv[1]))

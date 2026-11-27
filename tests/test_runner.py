"""Real worker processes with fake native CLIs; never use a subscription or Docker."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from arke.harnesses import Event, Launch
from arke.runner import Runner
from tests.native_fixtures import CLAUDE_FIXTURE, CODEX_FIXTURE
from tests.test_harness_adapters import clear_billing


def executable(tmp_path: Path, harness: str, source: str) -> Path:
    path = tmp_path / (harness + "-fixture")
    path.write_text(f"#!{sys.executable}\n" + source)
    path.chmod(0o700)
    return path


async def complete(runner: Runner, *, interrupt: bool = False) -> list[Event]:
    events = []
    async for event in runner.run("fixture turn"):
        events.append(event)
        if event.kind == "approval":
            if interrupt:
                await runner.interrupt()
            else:
                await runner.respond(event.data["request_id"], {"allow": True})
    assert events[-1].kind == "completed"
    return events


@pytest.mark.parametrize("container", [False, True])
def test_runner_derives_sandbox_context_from_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, container: bool
) -> None:
    # An inherited flag must not silently disable the local native sandbox.
    monkeypatch.setenv("ARKE_EXTERNAL_SANDBOX", "1")
    workspace, home = tmp_path / "workspace", tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    capture = tmp_path / "open.json"

    class CaptureRunner(Runner):
        def command(self) -> list[str]:
            super().command()  # Real validation and container/local selection.
            return [
                sys.executable,
                "-c",
                """
import json, sys
from pathlib import Path
for line in sys.stdin:
    message = json.loads(line)
    if message['method'] == 'open':
        Path(sys.argv[1]).write_text(json.dumps(message['params']))
    print(json.dumps({'id': message['id'], 'result': None}), flush=True)
    if message['method'] == 'close':
        break
""",
                str(capture),
            ]

    async def scenario() -> None:
        runner = CaptureRunner(
            Launch(
                "codex",
                workspace,
                runner="container" if container else "local",
                native_home=home if container else None,
            )
        )
        try:
            await runner.open(None)
            request = json.loads(capture.read_text())
            assert request["external_sandbox"] is container
            assert "external_sandbox" not in request["launch"]
        finally:
            # This test substitutes a local protocol recorder for Docker.
            runner.container = None
            await runner.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("external_sandbox", [False, True])
def test_worker_applies_supervisor_sandbox_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, external_sandbox: bool
) -> None:
    clear_billing(monkeypatch)
    policy = (
        {"type": "externalSandbox", "networkAccess": "enabled"}
        if external_sandbox
        else None
    )
    source = CODEX_FIXTURE.replace(
        '    elif method == "turn/start":',
        '    elif method == "turn/start":\n'
        f'        assert message["params"].get("sandboxPolicy") == {policy!r}\n'
        '        assert message["params"]["approvalPolicy"] == "untrusted"\n'
        '        assert message["params"]["approvalsReviewer"] == "user"',
    )
    path = executable(tmp_path, "codex", source)

    class WorkerContext(Runner):
        async def _call(self, method: str, params: dict[str, Any]) -> Any:
            if method == "open":
                params["external_sandbox"] = external_sandbox
            return await super()._call(method, params)

    async def scenario() -> None:
        runner = WorkerContext(Launch("codex", tmp_path, executable=str(path)))
        try:
            await runner.open(None)
            await complete(runner)
        finally:
            await runner.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("harness", ["codex", "claude"])
def test_worker_subscription_protocol_control_and_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    clear_billing(monkeypatch)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    source = CODEX_FIXTURE if harness == "codex" else CLAUDE_FIXTURE
    path = executable(tmp_path, harness, source)

    async def scenario() -> None:
        launch = Launch(harness, tmp_path, executable=str(path))
        runner = Runner(launch)
        reference = None
        try:
            reference = await runner.open(None)
            # Consecutive turns expose early terminal forwarding before the
            # worker's previous async iterator has actually completed cleanup.
            for _ in range(3):
                events = await complete(runner)
                assert any(e.kind == "message" for e in events)
                for event in events:
                    if event.kind == "session":
                        reference = event.data["native_id"]
            assert reference
            await complete(runner, interrupt=True)
            await complete(runner)
        finally:
            await runner.close()
        assert runner.process is not None and runner.process.returncode is not None
        await runner.close()
        resumed = Runner(launch)
        try:
            assert await resumed.open(reference) == reference
            await complete(resumed)
        finally:
            await resumed.close()
        assert resumed.process is not None and resumed.process.returncode is not None

    asyncio.run(scenario())


@pytest.mark.parametrize("same_user", [True, False])
def test_worker_forwards_only_injected_bus_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same_user: bool
) -> None:
    clear_billing(monkeypatch)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    # This fake native CLI exercises the same worker socket that the stdio MCP
    # server uses, without loading a model or opening a network listener.
    source = CODEX_FIXTURE.replace(
        "import json, sys", "import json, sys, socket, tomllib"
    )
    source = source.replace(
        "        turn += 1",
        """        turn += 1
        setting = next(arg for arg in sys.argv if arg.startswith("mcp_servers.arke.env="))
        socket_path = tomllib.loads("env=" + setting.split("=", 1)[1])["env"]["ARKE_MANAGED_SOCKET"]
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect("\\0" + socket_path[1:])
            connection.sendall(json.dumps({"tool": "inbox", "data": {}}).encode() + b"\\n")
            response = json.loads(connection.makefile("rb").readline())
        assert response == {"result": {"message": "scoped mailbox fixture"}}, response
""",
    )
    if not same_user:
        source = source.replace(
            '{"result": {"message": "scoped mailbox fixture"}}',
            '{"error": "bus socket belongs to another execution user"}',
        )

    class DifferentUser(Runner):
        def command(self) -> list[str]:
            # Real SO_PEERCRED remains untouched. Simulate a different worker
            # owner without requiring root or changing the test process's UID.
            return [
                sys.executable,
                "-c",
                "from arke import worker; worker.os.getuid = lambda: -1; worker.main()",
            ]

    path = executable(tmp_path, "codex", source)
    calls: list[tuple[str, dict[str, Any]]] = []

    def bus_call(tool: str, data: dict[str, Any]) -> dict[str, str]:
        calls.append((tool, data))
        return {"message": "scoped mailbox fixture"}

    async def scenario() -> None:
        factory = Runner if same_user else DifferentUser
        runner = factory(
            Launch("codex", tmp_path, executable=str(path), bus_call=bus_call)
        )
        try:
            await runner.open(None)
            await complete(runner)
            assert calls == ([("inbox", {})] if same_user else [])
        finally:
            await runner.close()
        assert not runner.callbacks

    asyncio.run(scenario())


def test_failed_open_still_closes_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_billing(monkeypatch)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    path = executable(
        tmp_path,
        "codex",
        CODEX_FIXTURE.replace('"type": "chatgpt"', '"type": "apiKey"'),
    )

    async def scenario() -> None:
        runner = Runner(Launch("codex", tmp_path, executable=str(path)))
        try:
            with pytest.raises(RuntimeError, match="subscription"):
                await runner.open(None)
        finally:
            await runner.close()
        assert runner.process is not None and runner.process.returncode is not None

    asyncio.run(scenario())


def test_cancelled_close_caller_does_not_leave_worker_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_billing(monkeypatch)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    path = executable(tmp_path, "codex", CODEX_FIXTURE)

    async def scenario() -> None:
        runner = Runner(Launch("codex", tmp_path, executable=str(path)))
        await runner.open(None)
        closing = asyncio.create_task(runner.close())
        await asyncio.sleep(0)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        await runner.close()
        assert runner.process is not None and runner.process.returncode is not None

    asyncio.run(scenario())


def test_forced_close_stops_native_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_billing(monkeypatch)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    pidfile = tmp_path / "native.pid"
    source = CODEX_FIXTURE.replace(
        "import json, sys",
        "import json, sys, os\nfrom pathlib import Path\nPath("
        + repr(str(pidfile))
        + ").write_text(str(os.getpid()))",
    )
    path = executable(tmp_path, "codex", source)

    class NoCloseReply(Runner):
        async def _call(self, method: str, params: dict[str, Any]) -> Any:
            if method == "close":
                raise TimeoutError("fixture: worker did not acknowledge close")
            return await super()._call(method, params)

    async def scenario() -> None:
        runner = NoCloseReply(Launch("codex", tmp_path, executable=str(path)))
        await runner.open(None)
        native_pid = int(pidfile.read_text())
        await asyncio.wait_for(runner.close(), 3)
        assert runner.process is not None and runner.process.returncode is not None
        # A killed grandchild may briefly remain a zombie pending its OS reaper;
        # it must never remain an executing process after the worker is closed.
        stat = Path(f"/proc/{native_pid}/stat")
        if stat.exists():
            assert stat.read_text().split(")", 1)[1].split()[0] == "Z"
        else:
            with pytest.raises(ProcessLookupError):
                os.kill(native_pid, 0)

    asyncio.run(scenario())

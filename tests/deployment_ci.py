"""Credential-free deployment acceptance. Docker and an installed browser are required.

Run `make test-deployment`. Only synthetic state is mounted into workers; native
CLI probes and managed workers all run with Docker networking disabled. The
supervisor and browser communicate over loopback. No vendor credentials are used.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import socket
import tempfile
from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
import uvicorn

from arke.harnesses import Launch
from arke.runner import Runner
from arke.webapp import create_app, server_config
from arke.webconfig import WebConfig
from tests.container_acceptance import check as boundary_check
from tests.native_fixtures import CLAUDE_FIXTURE, CODEX_FIXTURE

ROOT = Path(__file__).resolve().parents[1]


class OfflineRunner(Runner):
    """Add network isolation to the unmodified production worker command."""

    def command(self) -> list[str]:
        if self.launch.runner != "container":
            raise ValueError("deployment tests must execute workers in containers")
        command = super().command()
        command.insert(2, "--network=none")
        return command


def fixtures(workspace: Path, harness: str) -> None:
    source = CODEX_FIXTURE if harness == "codex" else CLAUDE_FIXTURE
    # Record protocol traffic to verify native resume rather than a fresh session.
    source = source.replace(
        "import json, sys",
        "import json, sys\n"
        + r"""
import socket, tomllib, uuid
from pathlib import Path
def record(value):
    with Path("protocol.jsonl").open("a") as out:
        out.write(json.dumps(value) + "\n")
record({"argv": sys.argv})
def bus():
    address = None
    for index, arg in enumerate(sys.argv):
        if arg.startswith("mcp_servers.arke.env="):
            address = tomllib.loads("env=" + arg.split("=", 1)[1])["env"]["ARKE_MANAGED_SOCKET"]
        if arg == "--mcp-config":
            address = json.loads(sys.argv[index + 1])["mcpServers"]["arke"]["env"]["ARKE_MANAGED_SOCKET"]
    if address:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(10)
            connection.connect("\0" + address[1:])
            connection.sendall((json.dumps({"tool": "send", "data": {
                "to": ["human"], "text": "container bus fixture", "request_id": uuid.uuid4().hex
            }}) + "\n").encode())
            result = json.loads(connection.makefile("rb").readline())
            assert "result" in result, result
            record({"bus": "passed"})
""",
    )
    source = source.replace(
        "    message = json.loads(line)",
        "    message = json.loads(line)\n    record(message)",
    )
    source = source.replace("        turn += 1", "        turn += 1\n        bus()")
    executable = workspace / "native-fixture"
    executable.write_text("#!/app/.venv/bin/python\n" + source)
    executable.chmod(0o700)


async def removed(runner: Runner) -> None:
    assert runner.process is not None and runner.process.returncode is not None
    assert runner.container
    process = await asyncio.create_subprocess_exec(
        "docker",
        "container",
        "inspect",
        runner.container,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    async with asyncio.timeout(10):
        _, stderr = await process.communicate()
    assert process.returncode and b"No such" in stderr, stderr.decode()


async def image_probes(image: str, directory: Path) -> None:
    workspace, home = directory / "workspace", directory / "home"
    workspace.mkdir(parents=True)
    home.mkdir()
    tests = workspace / "fixtures/tests"
    tests.mkdir(parents=True)
    for name in (
        "__init__.py",
        "native_contract.py",
        "native_execution.py",
        "scripted_model.py",
    ):
        shutil.copyfile(ROOT / "tests" / name, tests / name)
    runner = OfflineRunner(
        Launch("codex", workspace, runner="container", native_home=home, image=image)
    )
    command = runner.command()
    command[-2:] = [
        "-c",
        """
import asyncio, runpy, sys
from pathlib import Path
import arke.webapp
assert Path(arke.webapp.__file__).is_relative_to('/app'), arke.webapp.__file__
sys.path.insert(0, '/workspace/fixtures')
from tests.native_contract import probe
asyncio.run(probe(thread=True))
runpy.run_module('tests.native_execution', run_name='__main__')
""",
    ]
    try:
        process = await asyncio.create_subprocess_exec(*command)
        runner.process = process
        async with asyncio.timeout(360):
            code = await process.wait()
        assert code == 0, "installed native contract/execution probe failed"
    finally:
        if runner.container:
            cleanup = await asyncio.create_subprocess_exec(
                "docker",
                "rm",
                "-f",
                runner.container,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            async with asyncio.timeout(15):
                await cleanup.wait()
        if runner.process and runner.process.returncode is None:
            runner.process.kill()
            await runner.process.wait()
    await removed(runner)


@contextlib.asynccontextmanager
async def serving(config: WebConfig, runners: list[Runner]) -> AsyncGenerator[str]:
    def factory(launch: Launch) -> Runner:
        runner = OfflineRunner(launch)
        runners.append(runner)
        return runner

    app = create_app(config, factory)
    policy = server_config(config, app)
    policy.log_level = "warning"
    policy.access_log = False
    policy.configure_logging()
    server = uvicorn.Server(policy)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(20):
                while not server.started:
                    if task.done():
                        await task
                        raise RuntimeError("web service exited before startup")
                    await asyncio.sleep(0.05)
            yield f"http://127.0.0.1:{listener.getsockname()[1]}"
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 60)
    assert app.state.conversations.closed
    for runner in runners:
        await removed(runner)


async def browser(url: str, mode: str, phase: str, cid: str = "") -> str:
    process = await asyncio.create_subprocess_exec(
        "node",
        str(ROOT / "tests/deployment_browser.cjs"),
        url,
        mode,
        phase,
        cid,
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(180):
            stdout, _ = await process.communicate()
        assert process.returncode == 0, f"browser scenario failed: {mode}/{phase}"
        return json.loads(stdout)["cid"]
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def managed(image: str, root: Path, harness: str, dashboard: bool) -> None:
    mode = "dashboard" if dashboard else "standalone"
    root.mkdir(parents=True)
    profiles = {}
    for name in ("shared", "private"):
        workspace, home = root / name / "workspace", root / name / "home"
        workspace.mkdir(parents=True)
        home.mkdir()
        fixtures(workspace, harness)
        profiles[name] = Launch(
            harness,
            workspace,
            runner="container",
            native_home=home,
            image=image,
            executable="/workspace/native-fixture",
        )
    bus = root / "bus.toml"
    bus.write_text(
        f"[server]\nstate_dir={json.dumps(str(root / 'bus'))}\ntranscript_roots=[]\n"
    )
    config = WebConfig(
        state=root / "state",
        profiles=profiles,
        owner_key="a" * 43,
        chat_key="b" * 43,
        shared_profile="shared",
        dashboard=dashboard,
        bus_config=bus,
    )
    runners: list[Runner] = []
    async with serving(config, runners) as url:
        cid = await browser(url, mode, "initial")
    assert runners, "browser did not start a worker"
    workspace = profiles["private" if dashboard else "shared"].cwd
    before = (workspace / "protocol.jsonl").read_text()
    resumed: list[Runner] = []
    async with serving(config, resumed) as url:
        # Startup and a browser reading state must not launch/replay native work.
        async with httpx.AsyncClient(base_url=url, trust_env=False) as client:
            result = await client.get(
                f"/api/conversations/{cid}",
                headers={"Authorization": "Bearer " + config.owner_key},
            )
            assert result.status_code == 200 and result.json()["paused"]
        assert not resumed
        assert (workspace / "protocol.jsonl").read_text() == before
        assert await browser(url, mode, "recovery", cid) == cid
        if dashboard:
            async with httpx.AsyncClient(base_url=url, trust_env=False) as client:
                inbox = await client.get(
                    "/log?agent=human",
                    headers={
                        "Authorization": "Bearer " + config.owner_key,
                        "X-Agent-As": "human",
                    },
                )
                assert inbox.status_code == 200, inbox.text
                assert "container bus fixture" in inbox.text, inbox.text
    records = [
        json.loads(line)
        for line in (workspace / "protocol.jsonl").read_text().splitlines()
    ]
    if harness == "codex":
        turns = [r["params"] for r in records if r.get("method") == "turn/start"]
        assert turns
        assert all(
            turn["sandboxPolicy"]
            == {"type": "externalSandbox", "networkAccess": "enabled"}
            and turn["approvalPolicy"] == "untrusted"
            and turn["approvalsReviewer"] == "user"
            for turn in turns
        )
        assert any(
            r.get("method") == "thread/resume"
            and r["params"]["threadId"] == "thread-fixture"
            for r in records
        )
    else:
        assert any("--resume=claude-fixture" in r.get("argv", []) for r in records)
    assert any(r.get("bus") == "passed" for r in records) == dashboard
    print(
        f"Passed {harness} {mode}: browser creation/chat, approvals, redirect/held queue, recovery/resume, bus isolation, container cleanup.",
        flush=True,
    )


async def check(image: str, root: Path) -> None:
    # Do not inherit vendor overrides, owner homes, proxy credentials or bus IDs.
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        in (
            "PATH",
            "ARKE_CHROME",
            "ARKE_PLAYWRIGHT_MODULE",
            "PLAYWRIGHT_BROWSERS_PATH",
        )
    }
    home, tmp = root / "supervisor-home", root / "tmp"
    home.mkdir()
    tmp.mkdir()
    os.environ.clear()
    os.environ.update(
        env | {"HOME": str(home), "TMPDIR": str(tmp), "PYTHONDONTWRITEBYTECODE": "1"}
    )
    boundary_check(image, root)
    await image_probes(image, root / "image")
    for harness in ("claude", "codex"):
        for dashboard in (False, True):
            await managed(image, root / f"{harness}-{dashboard}", harness, dashboard)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="arke-agent:local")
    args = parser.parse_args()
    root = ROOT / ".temp/deployment"
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as temp:
        asyncio.run(check(args.image, Path(temp)))


if __name__ == "__main__":
    main()

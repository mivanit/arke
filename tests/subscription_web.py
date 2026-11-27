"""Opt-in Codex subscription lifecycle acceptance through the real browser.

Four native turns; never invoked by pytest, make check, or CI. Only the existing
login is copied into private temporary storage; the source native home is untouched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import socket
import sys
import tempfile
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn

from arke.harnesses import Launch
from arke.runner import Runner
from arke.webapp import create_app, server_config
from arke.webconfig import WebConfig

ROOT = Path(__file__).resolve().parents[1]
TOOL = '''from pathlib import Path
from mcp.server.mcpserver import MCPServer
import sys
server = MCPServer("acceptance")
@server.tool()
def record_marker(value: str) -> str:
    """Write the acceptance marker. Requires the owner's approval."""
    Path(sys.argv[1]).write_text(value)
    return value
server.run()
'''


@asynccontextmanager
async def serving(config: WebConfig) -> AsyncGenerator[tuple[str, list[Runner]]]:
    runners: list[Runner] = []

    def factory(launch: Launch) -> Runner:
        runner = Runner(launch)
        runners.append(runner)
        return runner

    app = create_app(config, factory)
    policy = server_config(config, app)
    policy.log_level, policy.access_log = "warning", False
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
                        raise RuntimeError("service stopped before startup")
                    await asyncio.sleep(0.05)
            yield f"http://127.0.0.1:{listener.getsockname()[1]}", runners
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 60)
    assert all(
        r.process is not None and r.process.returncode is not None for r in runners
    )


async def browser(
    url: str, phase: str, marker: str, key: str, record: Path, cid: str = ""
) -> str:
    env = os.environ | {
        "ARKE_TEST_OWNER_KEY": key,
        "ARKE_TEST_MARKER_FILE": str(record),
    }
    process = await asyncio.create_subprocess_exec(
        "node",
        str(ROOT / "tests/subscription_web.cjs"),
        url,
        phase,
        marker,
        cid,
        env=env,
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(600):
            output, _ = await process.communicate()
        assert process.returncode == 0, f"subscription browser check failed: {phase}"
        lines = output.decode().splitlines()
        for line in lines[:-1]:
            print(line, flush=True)
        return json.loads(lines[-1])["cid"]
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def check(root: Path, model: str | None) -> None:
    workspace = root / "workspace"
    workspace.mkdir()
    git = await asyncio.create_subprocess_exec("git", "init", "--quiet", str(workspace))
    assert await git.wait() == 0
    tool, record = root / "acceptance.py", root / "marker.txt"
    tool.write_text(TOOL)
    config = WebConfig(
        state=root / "service",
        owner_key=secrets.token_urlsafe(32),
        profiles={
            "codex": Launch(
                "codex",
                workspace,
                model=model,
                mcp_servers={
                    "acceptance": {
                        "command": sys.executable,
                        "args": [str(tool), str(record)],
                    }
                },
            )
        },
    )
    marker = "ARKE_" + secrets.token_hex(16)
    async with serving(config) as (url, runners):
        cid = await browser(url, "initial", marker, config.owner_key, record)
        assert len(runners) == 2, "Stop/Resume did not replace the worker exactly once"
        assert all(
            r.process is not None and r.process.returncode is not None for r in runners
        )
    async with serving(config) as (url, runners):
        assert not runners, "restart must not start native execution"
        assert (
            await browser(url, "recovery", marker, config.owner_key, record, cid) == cid
        )
        assert len(runners) == 1
    assert record.read_text() == "approved", "an unapproved tool was executed"
    print(
        "Passed real Codex subscription browser lifecycle and native recall.",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--codex-home",
        type=Path,
        help="Existing subscription login to copy (read-only)",
    )
    parser.add_argument("--model")
    parser.add_argument("--use-subscription", action="store_true", required=True)
    args = parser.parse_args()
    source = (
        (
            args.codex_home
            or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        )
        .expanduser()
        .resolve()
    )
    auth = source / "auth.json"
    if not auth.is_file():
        parser.error(
            "Codex subscription auth.json is missing; sign in before this check"
        )
    # Reject billing overrides before copying a login or starting any turn.
    from arke.harnesses.base import subscription_environment

    subscription_environment("codex")
    base = ROOT / ".temp/subscription-web"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(dir=base) as temporary:
        root = Path(temporary)
        home = root / "home"
        native = home / ".codex"
        native.mkdir(parents=True, mode=0o700)
        with (
            open(
                native / "auth.json", "xb", opener=lambda p, f: os.open(p, f, 0o600)
            ) as target,
            auth.open("rb") as original,
        ):
            shutil.copyfileobj(original, target)
        for name in ("tmp", "cache", "config", "state"):
            (root / name).mkdir()
        os.environ.update(
            {
                "HOME": str(home),
                "CODEX_HOME": str(native),
                "TMPDIR": str(root / "tmp"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_STATE_HOME": str(root / "state"),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        print(
            "Starting four-turn Codex subscription browser check; all native state is temporary and repo-local.",
            flush=True,
        )
        asyncio.run(check(root, args.model))
    print("Temporary login copy and native test history removed.", flush=True)


if __name__ == "__main__":
    main()

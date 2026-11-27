"""Opt-in installed-CLI contract probe: no login, account refresh, or model turns.

Run with `python -m tests.native_contract [--thread]`. Every writable native
location is a fresh directory under this checkout's .temp; provider credentials
and the caller's environment are not inherited. Network proxies point at an
unused loopback port. This checks protocol/config compatibility, not billing,
native sandbox execution, or actual model behavior.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from arke.harnesses.codex import subscription_config

CLAUDE_INITIALIZE = """
import asyncio, json, os, shutil
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

async def main():
    client = ClaudeSDKClient(options=ClaudeAgentOptions(
        cli_path=shutil.which("claude"), cwd=os.getcwd(), setting_sources=[],
        settings=json.dumps({"forceLoginMethod": "claudeai", "disableAllHooks": True}),
        extra_args={"strict-mcp-config": None},
    ))
    async with asyncio.timeout(45):
        try:
            await client.connect()  # No prompt, query, login, or account refresh.
            assert isinstance(await client.get_server_info(), dict)
        finally:
            await client.disconnect()
    print("passed (no prompt submitted)")

asyncio.run(main())
"""


async def probe(thread: bool = False) -> None:
    root = Path(__file__).resolve().parents[1] / ".temp" / "native-contract"
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as temporary:
        directory = Path(temporary)
        for name in (
            "home",
            "codex",
            "claude",
            "tmp",
            "cache",
            "config",
            "state",
            "workspace",
        ):
            (directory / name).mkdir()
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(directory / "home"),
            "CODEX_HOME": str(directory / "codex"),
            "CLAUDE_CONFIG_DIR": str(directory / "claude"),
            "TMPDIR": str(directory / "tmp"),
            "XDG_CACHE_HOME": str(directory / "cache"),
            "XDG_CONFIG_HOME": str(directory / "config"),
            "XDG_STATE_HOME": str(directory / "state"),
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "",
            "NO_COLOR": "1",
        }
        cwd = directory / "workspace"

        async def command(*args: str) -> str:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                async with asyncio.timeout(60):
                    stdout, stderr = await process.communicate()
                if process.returncode:
                    raise RuntimeError(
                        stderr.decode().strip() or "native command failed"
                    )
                return stdout.decode().strip()
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()

        # A separate Git root prevents loading this checkout's project settings.
        await command("git", "init", "--quiet")
        codex, claude = shutil.which("codex"), shutil.which("claude")
        if not codex or not claude:
            raise RuntimeError(
                "install both native CLIs before running the contract probe"
            )
        versions = {
            "codex": await command(codex, "--version"),
            "claude": await command(claude, "--version"),
        }
        # SDK options overlay the process environment. A separate Python process
        # ensures even inherited SDK variables and credentials are absent.
        claude_initialize = await command(sys.executable, "-c", CLAUDE_INITIALIZE)
        process = await asyncio.create_subprocess_exec(
            codex,
            "app-server",
            "--listen",
            "stdio://",
            "-c",
            'forced_login_method="chatgpt"',
            "-c",
            'model_provider="openai"',
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=8 * 1024 * 1024,
        )
        writer, reader = process.stdin, process.stdout
        assert writer is not None and reader is not None
        sequence = 0

        async def call(method: str, params: dict[str, Any]) -> Any:
            nonlocal sequence
            sequence += 1
            writer.write(
                (
                    json.dumps({"id": sequence, "method": method, "params": params})
                    + "\n"
                ).encode()
            )
            await writer.drain()
            async with asyncio.timeout(20):
                while line := await reader.readline():
                    value = json.loads(line)
                    if value.get("id") == sequence and "method" not in value:
                        if "error" in value:
                            raise RuntimeError(f"{method}: {value['error']}")
                        return value.get("result")
                raise RuntimeError(f"native server disconnected during {method}")

        try:
            await call(
                "initialize",
                {"clientInfo": {"name": "arke-contract", "version": "0.2.0"}},
            )
            writer.write(b'{"method":"initialized"}\n')
            await writer.drain()
            effective = await call(
                "config/read", {"cwd": str(cwd), "includeLayers": False}
            )
            config = effective["config"]
            try:
                subscription_config(config)
            except Exception:
                # Only structural/default endpoint details from the fresh test
                # home; never dump config layers, headers or credential fields.
                diagnostics = {
                    "versions": versions,
                    "provider": config.get("model_provider"),
                    "login": config.get("forced_login_method"),
                    "custom_openai_provider": bool(
                        (config.get("model_providers") or {}).get("openai")
                    ),
                }
                for name in ("openai_base_url", "chatgpt_base_url"):
                    value = config.get(name)
                    parsed = urlsplit(value or "")
                    diagnostics[name] = (
                        f"{parsed.scheme}://{parsed.hostname}{parsed.path}"
                        if value
                        else None
                    )
                print(json.dumps(diagnostics, indent=2))
                raise
            report: dict[str, Any] = versions | {
                "claude_sdk_initialize": claude_initialize,
                "initialize": "passed",
                "subscription_config": "passed",
            }
            if thread:
                result = await call(
                    "thread/start",
                    {
                        "cwd": str(cwd),
                        "approvalPolicy": "untrusted",
                        "sandbox": "workspace-write",
                        "modelProvider": "openai",
                    },
                )
                assert result["thread"]["id"]
                report["thread_start"] = "passed (no turn submitted)"
            print(json.dumps(report, indent=2))
        finally:
            writer.close()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(5):
                    await process.wait()
            if process.returncode is None:
                process.kill()
                await process.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--thread",
        action="store_true",
        help="also create an unauthenticated local thread without submitting a turn",
    )
    asyncio.run(probe(parser.parse_args().thread))


if __name__ == "__main__":
    main()

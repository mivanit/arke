"""Real CLI smoke test with scripted replies or opt-in paid API access.

No model calls in the ordinary test suite. A disposable container supplies blank harness
configuration; this script starts its own bus and verifies actual hooks, helper routing,
receipt acknowledgment and resume after a bus restart. It does not exercise a TUI wake-up.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from contextlib import ExitStack, closing
from pathlib import Path
from typing import Any

ROOT: Path = Path(__file__).resolve().parents[1]


def stop(process: subprocess.Popen[Any] | None) -> None:
    if process is not None and process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("claude", "codex"))
    parser.add_argument("--model", default=os.environ.get("ARKE_LIVE_MODEL"))
    parser.add_argument(
        "--mock-model",
        action="store_true",
        help="use scripted local model replies; no credentials or internet",
    )
    args = parser.parse_args()
    if os.environ.get("ARKE_LIVE_CONTAINER") != "1":
        parser.error(
            "run with make test-mocked or test-live, inside the disposable test container"
        )
    credential = "ANTHROPIC_API_KEY" if args.kind == "claude" else "CODEX_API_KEY"
    if args.mock_model:
        args.model = (
            "claude-sonnet-4-5-20250929" if args.kind == "claude" else "gpt-6.1-sol"
        )
    if not args.mock_model and (not args.model or not os.environ.get(credential)):
        parser.error(f"set ARKE_LIVE_MODEL and {credential} before make test-live")
    print(
        subprocess.check_output([args.kind, "--version"], text=True).strip(), flush=True
    )
    # Only the harness invocation gets its credential. All state and messages belong to this test.
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(
            ("ARKE_", "AGENTBUS_", "CLAUDE_", "CODEX_", "ANTHROPIC_", "OPENAI_")
        )
    }
    bus: subprocess.Popen[Any] | None = None
    agent: subprocess.Popen[Any] | None = None
    with (
        tempfile.TemporaryDirectory(prefix="arke-live-") as directory,
        ExitStack() as stack,
    ):
        root = Path(directory)
        work = root / "work"
        work.mkdir()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        config = root / "config.toml"
        config.write_text(
            f"[server]\nport={port}\nstate_dir={json.dumps(str(root / 'server'))}\n"
            f"[client]\nstate_dir={json.dumps(str(root / 'client'))}\n"
        )
        env["ARKE_CONFIG"] = str(config)
        command = [sys.executable, str(ROOT / "arke/client.py")]
        hooks = subprocess.check_output([*command, "hooks", args.kind], env=env)
        settings = root / "hooks.json"
        settings.write_bytes(hooks)
        if args.kind == "codex":
            codex_config = Path.home() / ".codex"
            codex_config.mkdir(exist_ok=True)
            (codex_config / "hooks.json").write_bytes(hooks)
        harness_env = env | {
            credential: "test-unused" if args.mock_model else os.environ[credential]
        }
        model = None
        if args.mock_model:
            from tests.scripted_model import ScriptedModel

            model = stack.enter_context(ScriptedModel())
            if args.kind == "claude":
                harness_env |= {
                    "ANTHROPIC_BASE_URL": model.url,
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                }
            else:
                (Path.home() / ".codex/config.toml").write_text(
                    'model_provider="arke_mock"\nweb_search="disabled"\n'
                    '[model_providers.arke_mock]\nname="Arke test fixture"\n'
                    f'base_url="{model.url}/v1"\nwire_api="responses"\nrequires_openai_auth=false\n'
                    "request_max_retries=0\nstream_max_retries=0\n"
                )

        def api(path: str, body: dict[str, Any] | None = None, sid: str = "") -> Any:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}",
                data=json.dumps(body).encode() if body is not None else None,
                headers={
                    "Content-Type": "application/json",
                    **({"X-Agent-Session": sid} if sid else {"X-Agent-As": "human"}),
                },
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                return json.load(response)

        def start_bus(output: Any) -> subprocess.Popen[Any]:
            process = subprocess.Popen(
                [sys.executable, str(ROOT / "arke/server.py")],
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("test bus exited during startup")
                try:
                    api("/agents")
                    return process
                except urllib.error.URLError:
                    time.sleep(0.1)
            stop(process)
            raise TimeoutError("test bus did not start")

        def launch(prompt: str, output: Any, sid: str = "") -> subprocess.Popen[Any]:
            if args.kind == "claude":
                cmd = [
                    "claude",
                    "--print",
                    "--model",
                    args.model,
                    "--settings",
                    str(settings),
                    "--allowedTools",
                    "Bash,Agent",
                    "--max-turns",
                    "16",
                    "--max-budget-usd",
                    "3",
                ]
                if sid:
                    cmd += ["--resume", sid]
            else:
                # The container contains only this reviewed hook configuration, never a user's hooks.
                cmd = [
                    "codex",
                    "exec",
                    "--json",
                    "--model",
                    args.model,
                    "--skip-git-repo-check",
                    "--dangerously-bypass-hook-trust",
                    "--enable",
                    "multi_agent",
                    "--sandbox",
                    "danger-full-access",
                ]
                if sid:
                    cmd += ["resume", sid]
            return subprocess.Popen(
                [*cmd, prompt],
                cwd=work,
                env=harness_env,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

        parent_token, helper_token, receipt_token, resume_token = (
            uuid.uuid4().hex for _ in range(4)
        )
        send = shlex.join([*command, "send", "human"])
        prompt = (
            "This is a messaging integration test in a disposable container. "
            f"Use your shell tool to run `{send} {parent_token}`. "
            "Then start exactly one subagent with your Agent/spawn_agent tool and tell it to run "
            f"`{send} {helper_token}` with its shell tool. Wait for it to finish. "
            "Do not forward the subagent's forwarding request. "
            "If Arke delivers a message beginning RECEIPT-TOKEN:, send the complete token back to human "
            f"using `{send} TOKEN`. Do not invent or look up the token. Then finish."
        )
        if args.mock_model:
            prompt += "\nARKE_FIXTURE " + json.dumps(
                {
                    "mode": "parent",
                    "command": f"{send} {parent_token}",
                    "helper_command": f"{send} {helper_token}",
                }
            )
        with (
            (root / "bus.log").open("w") as bus_output,
            (root / "agent.log").open("w") as agent_output,
        ):
            try:
                bus = start_bus(bus_output)
                agent = launch(prompt, agent_output)
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    agents = api("/agents")
                    if agents:
                        break
                    if agent.poll() is not None:
                        raise RuntimeError(
                            "harness exited without registering; check hook trust/configuration"
                        )
                    time.sleep(0.1)
                else:
                    raise TimeoutError("harness did not register")
                assert len(agents) == 1, (
                    "helpers must not register as independent agents"
                )
                name = agents[0]["name"]
                # The public roster intentionally omits session IDs. Read only this test's database.
                with closing(
                    sqlite3.connect(
                        (root / "server/state.sqlite3").as_uri() + "?mode=ro", uri=True
                    )
                ) as database:
                    sid = database.execute("SELECT sid FROM agents").fetchone()[0]
                api("/send", {"to": [name], "text": f"RECEIPT-TOKEN: {receipt_token}"})
                if model is not None:
                    model.mail_queued.set()
                assert agent.wait(timeout=180) == 0, "harness failed"
                messages = api("/log")
                assert any(
                    m["from"] == name
                    and m["text"] == parent_token
                    and m["reach"] == ["human"]
                    for m in messages
                ), "parent send missing"
                forwarded = [
                    m for m in messages if m.get("helper") and helper_token in m["text"]
                ]
                assert len(forwarded) == 1 and forwarded[0]["reach"] == [name], (
                    "helper did not send only to parent"
                )
                assert forwarded[0]["requested_to"] == ["human"]
                assert not any(
                    helper_token in m["text"] and "human" in m["reach"]
                    for m in messages
                ), "helper message escaped to human"
                assert any(
                    m["from"] == name
                    and receipt_token in m["text"]
                    and m["reach"] == ["human"]
                    for m in messages
                ), "incoming hook mail was not read"
                assert api("/inbox?receipt=1", sid=sid)["messages"] == [], (
                    "mail was not acknowledged"
                )
                assert len(api("/agents")) == 1, "helper registered independently"
                stop(bus)
                bus = start_bus(bus_output)
                resume_prompt = (
                    f"Use your shell tool to run `{send} {resume_token}`, then finish."
                )
                if args.mock_model:
                    resume_prompt += "\nARKE_FIXTURE " + json.dumps(
                        {"mode": "resume", "command": f"{send} {resume_token}"}
                    )
                agent = launch(
                    resume_prompt,
                    agent_output,
                    sid,
                )
                assert agent.wait(timeout=180) == 0, "resume failed"
                assert any(
                    m["from"] == name
                    and m["text"] == resume_token
                    and m["reach"] == ["human"]
                    for m in api("/log")
                ), "resume lost the agent identity"
                assert len(api("/agents")) == 1
                if model is not None:
                    assert not model.errors, f"scripted model failed: {model.errors}"
                print(
                    "PASS: hooks, helper forwarding, incoming mail, acknowledgment, and resume after restart",
                    flush=True,
                )
            except BaseException:
                if model is not None:
                    print(model.diagnostics(), file=sys.stderr)
                agent_output.flush()
                bus_output.flush()
                print((root / "agent.log").read_text()[-12000:], file=sys.stderr)
                print((root / "bus.log").read_text()[-4000:], file=sys.stderr)
                raise
            finally:
                stop(agent)
                stop(bus)


if __name__ == "__main__":
    main()

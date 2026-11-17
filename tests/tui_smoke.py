"""Offline real-CLI tmux checks; run only in the disposable harness container."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

from tests.live_smoke import ROOT, stop
from tests.scripted_model import ScriptedModel


class TerminalModel(ScriptedModel):
    def __init__(self) -> None:
        super().__init__()
        self.pause_token = ""
        self.pause_started = threading.Event()
        self.pause_release = threading.Event()

    def next_action(self, body: dict[str, Any]) -> dict[str, Any] | None:
        claude = "messages" in body
        if claude and not any(
            tool.get("name") == "Bash" for tool in body.get("tools", [])
        ):
            return (
                None  # Claude also asks for background session titles, without tools.
            )
        history = body.get("messages", body.get("input", []))
        serialized = json.dumps(history)
        if (
            self.pause_token
            and self.pause_token in serialized
            and not self.pause_started.is_set()
        ):
            self.pause_started.set()
            if not self.pause_release.wait(45):
                raise ValueError("slow-response test did not release the model")
        # Unique tokens distinguish turns and prove mail arrived through hooks.
        matches = re.findall(r"TUI_(CHECK|MAIL|PERMISSION) ([0-9a-f]{32})", serialized)
        for kind, token in reversed(matches):
            call_id = f"call_{token}"
            if call_id in serialized:
                continue
            if kind == "PERMISSION":
                command = f"printf approved > /home/arke-live/permission-{token}"
            else:
                command = shlex.join(
                    [
                        sys.executable,
                        str(ROOT / "arke/client.py"),
                        "send",
                        "human",
                        token,
                    ]
                )
            self.actions.append(f"{kind}: {token}")
            if claude:
                return {
                    "id": call_id,
                    "name": "Bash",
                    "arguments": {"command": command},
                }
            arguments: dict[str, Any] = {"cmd": command}
            if kind == "PERMISSION":
                arguments |= {
                    "sandbox_permissions": "require_escalated",
                    "justification": "Allow the test to write its permission marker?",
                }
            return {
                "id": call_id,
                "namespace": "functions",
                "name": "exec",
                "arguments": "text(await tools.exec_command("
                + json.dumps(arguments)
                + "));",
            }
        return None


class TerminalTest:
    def __init__(
        self, root: Path, kind: str, model: TerminalModel, *, native: bool = False
    ) -> None:
        self.root, self.kind, self.model = root, kind, model
        self.work = root / "work"
        self.work.mkdir()
        self.sock = str(root / "tmux.sock")
        self.pane = ""
        self.name = ""
        self.bus: subprocess.Popen[Any] | None = None
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(
                (
                    "ARKE_",
                    "AGENTBUS_",
                    "CLAUDE_",
                    "CODEX_",
                    "ANTHROPIC_",
                    "OPENAI_",
                    "TMUX",
                )
            )
        }
        config = root / "config.toml"
        config.write_text(
            f"[server]\nport={self.port}\nstate_dir={json.dumps(str(root / 'server'))}\n[client]\nstate_dir={json.dumps(str(root / 'client'))}\n"
        )
        self.env |= {"ARKE_CONFIG": str(config), "TERM": "xterm-256color"}
        hooks = subprocess.check_output(
            [sys.executable, str(ROOT / "arke/client.py"), "hooks", kind], env=self.env
        )
        if kind == "claude" and not native:
            # This suite must keep exercising tmux even when native delivery is installed by default.
            config = json.loads(hooks)
            for rules in config["hooks"].values():
                for rule in rules:
                    rule["hooks"] = [
                        hook for hook in rule["hooks"] if not hook.get("asyncRewake")
                    ]
            hooks = json.dumps(config).encode()
        self.settings = root / "hooks.json"
        self.settings.write_bytes(hooks)
        if kind == "claude":
            self.env |= {
                "ANTHROPIC_API_KEY": "test-unused",
                "ANTHROPIC_BASE_URL": model.url,
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            }
            (Path.home() / ".claude.json").write_text(
                json.dumps(
                    {
                        "hasCompletedOnboarding": True,
                        "theme": "dark",
                        "customApiKeyResponses": {
                            "approved": ["test-unused"],
                            "rejected": [],
                        },
                        "projects": {str(self.work): {"hasTrustDialogAccepted": True}},
                    }
                )
            )
            self.command = [
                "claude",
                "--model",
                "claude-sonnet-4-5-20250929",
                "--settings",
                str(self.settings),
                "--allowedTools",
                f"Bash({sys.executable} {ROOT}/arke/client.py:*)",
            ]
        else:
            directory = Path.home() / ".codex"
            directory.mkdir(exist_ok=True)
            (directory / "hooks.json").write_bytes(hooks)
            (directory / "config.toml").write_text(
                'model_provider="arke_mock"\nweb_search="disabled"\n'
                "[sandbox_workspace_write]\nnetwork_access=true\n"
                + f'[projects.{json.dumps(str(self.work))}]\ntrust_level="trusted"\n'
                + '[model_providers.arke_mock]\nname="Arke fixture"\n'
                + f'base_url="{model.url}/v1"\nwire_api="responses"\nrequires_openai_auth=false\nrequest_max_retries=0\nstream_max_retries=0\n'
            )
            self.command = [
                "codex",
                "--model",
                "gpt-6.1-sol",
                "--dangerously-bypass-hook-trust",
                "--sandbox",
                "workspace-write",
                "--ask-for-approval",
                "on-request",
                "--no-alt-screen",
            ]

    def tmux(self, *args: str, stdin: str | None = None) -> str:
        return subprocess.run(
            ["tmux", "-S", self.sock, *args],
            input=stdin,
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()

    def screen(self) -> str:
        return self.tmux("capture-pane", "-p", "-t", self.pane)

    def wait(
        self, predicate: Callable[[], Any], label: str, seconds: float = 35
    ) -> Any:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.1)
        raise AssertionError(f"timed out: {label}")

    def api(self, path: str, body: dict[str, Any] | None = None, sid: str = "") -> Any:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Content-Type": "application/json",
                **({"X-Agent-Session": sid} if sid else {"X-Agent-As": "human"}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise AssertionError(
                f"{path}: {error.code}: {error.read().decode()}"
            ) from error

    def start(self) -> None:
        with (self.root / "bus.log").open("w") as output:
            self.bus = subprocess.Popen(
                [sys.executable, str(ROOT / "arke/server.py")],
                env=self.env,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

        def ready() -> bool:
            try:
                self.api("/agents")
                return True
            except OSError:
                return False

        self.wait(ready, "bus startup", 10)
        self.tmux(
            "new-session",
            "-d",
            "-s",
            "test",
            "-x",
            "140",
            "-y",
            "40",
            "-c",
            str(self.work),
            shlex.join(self.command),
        )
        self.pane = self.tmux("display-message", "-p", "-t", "test", "#{pane_id}")
        self.wait(
            lambda: (
                "❯" in self.screen() if self.kind == "claude" else "›" in self.screen()
            ),
            "interactive prompt",
        )

    def submit(self, text: str) -> None:
        self.tmux("load-buffer", "-b", "test-input", "-", stdin=text)
        self.tmux("paste-buffer", "-p", "-d", "-b", "test-input", "-t", self.pane)
        time.sleep(0.3)
        self.tmux("send-keys", "-t", self.pane, "Enter")

    def received(self, token: str) -> bool:
        agents = self.api("/agents")
        if not agents:
            return False
        expected = self.name or agents[0]["name"]
        return any(
            m["from"] == expected and m["text"] == token and m["reach"] == ["human"]
            for m in self.api("/log")
        )

    def record(self) -> dict[str, Any]:
        with closing(
            sqlite3.connect(
                (self.root / "server/state.sqlite3").as_uri() + "?mode=ro", uri=True
            )
        ) as database:
            rows = database.execute("SELECT sid, data FROM agents").fetchall()
        assert len(rows) == 1, rows
        return json.loads(rows[0][1])

    def idle(self) -> None:
        self.wait(lambda: self.api("/agents")[0]["state"] == "idle", "idle turn")
        time.sleep(0.5)

    def mail(self) -> str:
        token = uuid.uuid4().hex
        self.api("/send", {"to": [self.name], "text": f"TUI_MAIL {token}"})
        return token

    def acknowledged(self) -> None:
        self.wait(
            lambda: (
                not self.api("/inbox?receipt=1", sid=self.record()["sid"])["messages"]
            ),
            "mail acknowledgment",
        )

    def draft(self, *, space_under_cursor: bool = False) -> None:
        self.idle()
        draft = " " if space_under_cursor else "unfinished draft " + uuid.uuid4().hex
        if self.kind == "codex" and not space_under_cursor:
            # Numbered text and quoted menu/footer/prompt text all belong to the draft, not the terminal UI.
            draft = (
                "1. "
                + draft
                + "\nDo you want to review this?\nEsc to cancel\n80% context left\n❯"
                + "\nExplain Press enter to confirm or esc to cancel"
                + "\nExplain Press enter to confirm or esc to go back"
            )
            self.tmux("load-buffer", "-b", "draft", "-", stdin=draft)
            self.tmux("paste-buffer", "-p", "-d", "-b", "draft", "-t", self.pane)
        else:
            self.tmux("send-keys", "-l", "-t", self.pane, draft)
        if space_under_cursor:
            self.tmux("send-keys", "-t", self.pane, "Left")
            # Allow the real input widget to render the ambiguous cursor cell.
            time.sleep(0.3)
        rendered = draft.replace("\n", "\n  ")
        self.wait(lambda: rendered in self.screen(), "draft rendered")
        queued = time.monotonic()
        token = self.mail()
        self.wait(
            lambda: self.api("/agents")[0].get("wake", {}).get("status") == "held",
            "draft holds wake-up",
        )
        started = time.monotonic()
        while time.monotonic() - started < 3:
            assert rendered in self.screen(), "draft was damaged"
            assert not self.received(token), (
                "draft was submitted before the waiting period"
            )
            time.sleep(0.2)
        self.wait(
            lambda: self.received(token), "unchanged draft wake-up after a minute", 80
        )
        assert time.monotonic() - queued >= 60, "draft submitted before a minute"
        prompts = [
            part["text"]
            for body in self.model.requests
            for message in body.get("messages", body.get("input", []))
            if message.get("role") == "user"
            and isinstance(message.get("content"), list)
            for part in message["content"]
            if part.get("type") in ("text", "input_text")
        ]
        assert any(
            draft + "\n[user message cut off]\n[arke]" in prompt for prompt in prompts
        ), "draft/cutoff annotation missing from the actual submitted prompt"
        self.acknowledged()
        print(
            f"{self.kind}: draft held, preserved and submitted after the waiting period",
            flush=True,
        )

    def shell_draft(self) -> None:
        self.idle()
        marker = self.work / "shell-draft-ran"
        command = f"!printf ran > {marker}"
        self.tmux("send-keys", "-l", "-t", self.pane, command)
        self.wait(lambda: "Shell mode" in self.screen(), "shell draft rendered")
        queued = time.monotonic()
        token = self.mail()

        def refused() -> bool:
            assert not marker.exists(), "wake executed the shell draft"
            assert not self.received(token), "wake submitted mail through shell mode"
            return self.api("/agents")[0].get("wake", {}).get("status") == "failed"

        # A previous notice can still own the 30 s acknowledgment window when
        # this mail arrives. Then the shell draft gets its own 60 s hold.
        self.wait(
            refused, "shell draft wake refused after the ack and draft waits", 110
        )
        assert time.monotonic() - queued >= 60, "wake failed before the draft timeout"
        assert "command" in self.record()["wake"]["detail"]
        assert "! printf ran" in self.screen(), "shell draft was damaged"
        assert any(
            token in m["text"]
            for m in self.api("/inbox?receipt=1", sid=self.record()["sid"])["messages"]
        ), "pending mail was lost"
        self.tmux("send-keys", "-t", self.pane, "C-u", "BSpace")
        self.wait(lambda: "Shell mode" not in self.screen(), "shell draft cleared")
        self.submit("Resume after clearing the shell draft")
        self.wait(
            lambda: self.received(token), "mail after resuming from the shell draft", 45
        )
        assert not marker.exists(), "cleared shell draft executed"
        self.acknowledged()
        print(
            "codex: shell draft refused; queued mail delivered after clearing and resuming",
            flush=True,
        )

    def permission(self) -> None:
        self.idle()
        token = uuid.uuid4().hex
        marker = Path.home() / f"permission-{token}"
        self.submit(f"TUI_PERMISSION {token}")
        self.wait(
            lambda: any(
                text in self.screen()
                for text in ("Do you want", "Would you like", "Esc to cancel")
            ),
            "permission dialog",
        )
        mail = self.mail()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            assert not marker.exists(), "Arke approved a command"
            assert not self.received(mail), "mail ran through an open permission dialog"
            assert not any(
                "[arke] new message" in line
                for line in self.screen().splitlines()[-12:]
            ), "wake typed into dialog"
            time.sleep(0.2)
        self.tmux("send-keys", "-t", self.pane, "Escape")
        self.wait(
            lambda: self.received(mail), "mail after dismissing permission dialog", 45
        )
        assert not marker.exists(), "denied command executed"
        self.acknowledged()
        print(
            f"{self.kind}: permission dialog protected; queued mail delivered after dismissal",
            flush=True,
        )

    def replace(self) -> None:
        self.idle()
        old = self.record()
        self.tmux("respawn-pane", "-k", "-t", self.pane, "exec sleep 600")
        token = self.mail()
        assert any(
            token in m["text"]
            for m in self.api("/inbox?receipt=1", sid=old["sid"])["messages"]
        )
        self.tmux(
            "respawn-pane",
            "-k",
            "-t",
            self.pane,
            "-c",
            str(self.work),
            shlex.join(self.command),
        )
        self.wait(
            lambda: (
                "❯" in self.screen() if self.kind == "claude" else "›" in self.screen()
            ),
            "replacement prompt",
        )
        check = uuid.uuid4().hex
        self.submit(f"TUI_CHECK {check}")
        self.wait(
            lambda: self.received(check) and self.received(token),
            "replacement inherited pending mail",
            45,
        )
        new = self.record()
        assert new["sid"] != old["sid"] and new["name"] == old["name"]
        identity = {
            key: old[key] for key in ("kind", "host", "tmux", "pid", "process_token")
        }
        for event in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"):
            result = self.api(
                "/hook", identity | {"session_id": old["sid"], "event": event}
            )
            assert result.get("ignored"), result
            assert self.record()["sid"] == new["sid"], "late hook stole the mailbox"
        self.acknowledged()
        print(
            f"{self.kind}: pane replacement preserved identity/mail and rejected a late old-session hook",
            flush=True,
        )

    def check(self) -> None:
        token = uuid.uuid4().hex
        self.submit(f"TUI_CHECK {token}")
        self.wait(lambda: self.received(token), "initial tool send")
        print(f"{self.kind}: interactive tool and hooks work", flush=True)
        agents = self.api("/agents")
        assert len(agents) == 1
        self.name = agents[0]["name"]
        self.idle()
        token = uuid.uuid4().hex
        self.api("/send", {"to": [self.name], "text": f"TUI_MAIL {token}"})
        self.wait(lambda: self.received(token), "idle wake and incoming mail", 45)
        assert self.record()["wake"]["transport"] == "tmux"
        print(f"{self.kind}: idle wake works", flush=True)
        self.acknowledged()
        self.permission()
        self.draft()
        if self.kind == "codex":
            self.shell_draft()
        if self.kind == "claude":
            self.draft(space_under_cursor=True)
        self.replace()

    def check_native(self) -> None:
        """Real native delivery and a killed listener's fallback, with actual mailbox receipts."""
        token = uuid.uuid4().hex
        self.submit(f"TUI_CHECK {token}")
        self.wait(lambda: self.received(token), "initial tool send")
        self.name = self.api("/agents")[0]["name"]
        self.idle()
        draft = "UNSENT_NATIVE_DRAFT_" + uuid.uuid4().hex
        self.tmux("send-keys", "-l", "-t", self.pane, draft)
        for _ in range(3):
            token = self.mail()
            self.wait(
                lambda token=token: self.received(token), "native mail delivery", 45
            )
            self.acknowledged()
            self.idle()
            assert self.record()["wake"]["transport"] == "claude hook", self.record()[
                "wake"
            ]
            assert draft in self.screen(), "native wake lost the draft"
            assert all(
                draft not in json.dumps(request) for request in self.model.requests
            ), "native wake submitted the draft"
        print(
            "claude: repeated native delivery preserves the draft and acknowledges mail",
            flush=True,
        )
        self.tmux("send-keys", "-t", self.pane, "C-u")
        token = uuid.uuid4().hex
        self.model.pause_token = token
        self.api("/send", {"to": [self.name], "text": f"TUI_MAIL {token}"})
        try:
            self.wait(
                self.model.pause_started.is_set, "native model request started", 20
            )
            self.acknowledged()
            attempt = self.record()["wake"]["attempt"]
            time.sleep(32)  # longer than WAKE_ACK_S: thinking is not a failed wake-up
            record = self.record()
            assert record["state"] == "busy"
            assert record["wake"]["attempt"] == attempt
            assert record["wake"]["transport"] == "claude hook"
        finally:
            self.model.pause_release.set()
        self.wait(lambda: self.received(token), "slow native response", 20)
        self.idle()
        print(
            "claude: slow native response stays busy without terminal fallback",
            flush=True,
        )
        # Kill only this test's listener. A response sent to its abandoned long poll is uncertain;
        # the coordinator must wait for the receipt grace before attempting terminal fallback.
        import signal

        killed = 0
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                args = (proc / "cmdline").read_bytes().split(b"\0")
                environment = (proc / "environ").read_bytes().split(b"\0")
                if (
                    b"listen" in args
                    and b"claude" in args
                    and f"ARKE_CONFIG={self.env['ARKE_CONFIG']}".encode() in environment
                ):
                    os.kill(int(proc.name), signal.SIGKILL)
                    killed += 1
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
        assert killed, "no listener was active to kill"
        token = self.mail()
        self.wait(
            lambda: self.received(token), "tmux fallback after listener death", 55
        )
        self.acknowledged()
        assert self.record()["wake"]["transport"] == "tmux", self.record()["wake"]
        print(
            "claude: dead native listener falls back to tmux without losing mail",
            flush=True,
        )

    def check_print_exit(self) -> None:
        """Explicit and automatic print mode must exit even with a controlling terminal."""
        for mode in ("explicit", "redirect", "pipe"):
            exit_file = self.root / f"print-{mode}-exit"
            output = self.root / f"print-{mode}-output"
            arguments = [
                "--tools",
                "",
                "--system-prompt",
                "--" if mode == "explicit" else "Be brief.",
            ]
            if mode == "explicit":
                arguments += ["--print"]
            command = shlex.join(self.command + arguments + ["Say done."])
            status = f'printf "%s" "$?" > {shlex.quote(str(exit_file))}'
            if mode == "redirect":
                command = f"{command} > {shlex.quote(str(output))} 2>&1; {status}"
            elif mode == "pipe":
                # Record Claude's exit status, not cat's; stdout is a real pipe to another process.
                command = f"({command}; {status}) | cat > {shlex.quote(str(output))}"
            else:
                command = f"{command}; {status}"  # keep stdout on the terminal to test the argv guard
            pane = self.tmux(
                "new-window",
                "-d",
                "-P",
                "-F",
                "#{pane_id}",
                "-t",
                "test",
                "-n",
                f"print-{mode}",
                "-c",
                str(self.work),
                f"{command}; sleep 30",
            )
            try:
                self.wait(
                    exit_file.exists,
                    f"{mode} print command exits with native hooks installed",
                    15,
                )
                assert exit_file.read_text() == "0", (
                    self.tmux("capture-pane", "-p", "-t", pane)
                    if mode == "explicit"
                    else output.read_text()
                )
                self.wait(
                    lambda pane=pane, mode=mode, output=output: (
                        "DONE"
                        in (
                            self.tmux("capture-pane", "-p", "-t", pane)
                            if mode == "explicit"
                            else output.read_text()
                        )
                    ),
                    f"{mode} print command produces its answer",
                    5,
                )
            finally:
                self.tmux("kill-pane", "-t", pane)
        print(
            "claude: explicit, redirected and piped print commands exit normally",
            flush=True,
        )

    def close(self) -> None:
        try:
            subprocess.run(
                ["tmux", "-S", self.sock, "kill-server"],
                capture_output=True,
                timeout=5,
                check=False,
            )
        finally:
            stop(self.bus)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("claude", "codex"))
    parser.add_argument(
        "--native",
        action="store_true",
        help="exercise Claude native delivery and listener failure",
    )
    args = parser.parse_args()
    if os.environ.get("ARKE_LIVE_CONTAINER") != "1":
        parser.error("requires the disposable harness container")
    with (
        tempfile.TemporaryDirectory(prefix="arke-tui-") as directory,
        TerminalModel() as model,
    ):
        test = TerminalTest(Path(directory), args.kind, model, native=args.native)
        try:
            test.start()
            if args.native:
                assert args.kind == "claude"
                test.check_native()
                test.check_print_exit()
            else:
                test.check()
            assert not model.errors, model.errors
        except BaseException:
            if test.pane:
                print(test.screen(), file=sys.stderr)
            print((test.root / "bus.log").read_text()[-6000:], file=sys.stderr)
            print(model.diagnostics(), file=sys.stderr)
            raise
        finally:
            test.close()


if __name__ == "__main__":
    main()

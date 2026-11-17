"""Wake regressions using a private tmux server and simulated harnesses.

Run with: uv run pytest tests/test_wake_integration.py
No real harness, bus server, or existing tmux session is contacted.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import types
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest


def fake_harness(root: Path, kind: str) -> None:
    """Exercise real tty/process ownership with the two supported input layouts."""
    import tty

    Path("/proc/self/comm").write_text(kind)
    (root / "pid").write_text(str(os.getpid()))
    tty.setraw(sys.stdin.fileno())
    text: str = ""
    pending: bytes = b""
    pasting: bool = False

    def draw() -> None:
        mark: str = "› " if kind == "codex" else "❯ "
        lines: list[str] = text.split("\n")
        box: str = (
            mark + lines[0] + "\r\n" + "".join("  " + ln + "\r\n" for ln in lines[1:])
        )
        rule: str = "─" * 80 + "\r\n"
        layout: str = (
            box + "\r\n  gpt-6.1-sol medium · /tmp/work\r\n"
            if kind == "codex"
            else rule + box + rule + "  ⏵⏵ auto mode on\r\n"
        )
        sys.stdout.write("\x1b[?2004h\x1b[2J\x1b[H" + layout)
        sys.stdout.flush()

    draw()
    while True:
        pending += os.read(sys.stdin.fileno(), 4096)
        while pending:
            if pending.startswith(b"\x1b[200~"):
                pasting, pending = True, pending[6:]
            elif pending.startswith(b"\x1b[201~"):
                pasting, pending = False, pending[6:]
            elif pending.startswith(b"\x1b[4~"):
                pending = pending[
                    4:
                ]  # End: this fixture's cursor is already at the end of the draft
            elif pending in (
                b"\x1b",
                b"\x1b[",
                b"\x1b[4",
                b"\x1b[2",
                b"\x1b[20",
                b"\x1b[200",
                b"\x1b[201",
            ):
                break
            elif pending[:1] in (b"\r", b"\n"):
                pending = pending[1:]
                if pasting:
                    text += "\n"
                else:
                    with (root / "submitted.jsonl").open("a") as fh:
                        fh.write(json.dumps(text) + "\n")
                    text = ""
            elif pending[:1] == b"\x03":
                return
            else:
                text += pending[:1].decode("utf-8", "replace")
                pending = pending[1:]
        draw()


@pytest.mark.skipif(
    not (shutil.which("tmux") and sys.platform.startswith("linux")),
    reason="requires Linux and tmux",
)
class TestWakeTerminal:
    @pytest.fixture(autouse=True)
    def setup(self, bus: Any, tmp_path: Path) -> Generator[None]:
        import server

        self.server: types.ModuleType = server
        self.bus: Any = bus
        self.root: Path = tmp_path
        self.sock: str = str(self.root / "tmux.sock")
        yield
        subprocess.run(
            ["tmux", "-S", self.sock, "kill-server"],
            capture_output=True,
            check=False,
            timeout=5,
        )

    def tmux(self, *args: str) -> str:
        return subprocess.run(
            ["tmux", "-S", self.sock, *args],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()

    def wait_for(self, condition: Callable[[], bool]) -> None:
        deadline: float = time.monotonic() + 5
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.02)
        pytest.fail("private terminal did not reach the expected state")

    def agent(self, kind: str = "claude") -> str:
        command: str = shlex.join(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--tui",
                str(self.root),
                kind,
            ]
        )
        self.tmux("new-session", "-d", "-s", "probe", "-x", "100", "-y", "28", command)
        pane: str = self.tmux("display-message", "-p", "-t", "probe", "#{pane_id}")

        def harness_ready() -> bool:
            return (self.root / "pid").exists() and (
                "›" if kind == "codex" else "❯"
            ) in self.tmux("capture-pane", "-p", "-t", pane)

        self.wait_for(harness_ready)
        self.bus.hook(
            {
                "session_id": "probe",
                "event": "SessionStart",
                "kind": kind,
                "host": self.server.LOCAL_HOST,
                "cwd": str(self.root),
                "names": ["probe"],
                "pid": int((self.root / "pid").read_text()),
                "tmux": f"{self.sock}:{pane}",
            }
        )
        self.bus.send("human", {"to": ["probe"], "text": "test notification"})
        return pane

    def submitted(self) -> list[str]:
        path: Path = self.root / "submitted.jsonl"
        return (
            [json.loads(line) for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )

    def assert_wakes(self, kind: str) -> None:
        self.agent(kind)
        self.bus.wake_pass()
        assert self.bus.agents["probe"]["wake"]["status"] == "typed"
        assert len(self.submitted()) == 1
        assert "[arke] new message #1" in self.submitted()[0]

    def test_claude_foreground_harness_receives_notice(self) -> None:
        self.assert_wakes("claude")

    def test_codex_foreground_harness_receives_notice(self) -> None:
        self.assert_wakes("codex")

    def test_menu_like_drafts_are_cut_off_and_woken_after_a_minute(
        self, subtests: pytest.Subtests
    ) -> None:
        pane: str = self.agent("claude")
        for draft in ("1. Review the code", "Do you want to review the code?"):
            with subtests.test(draft=draft):
                self.tmux("send-keys", "-l", "-t", pane, draft)

                def draft_visible(draft: str = draft) -> bool:
                    return draft in self.tmux("capture-pane", "-p", "-t", pane)

                self.wait_for(draft_visible)
                a: dict[str, Any] = self.bus.agents["probe"]
                a.pop("wake", None)
                before: int = len(self.submitted())
                with (
                    patch.object(
                        self.server.time, "time", return_value=time.time()
                    ) as clock,
                    patch.object(self.server, "WAKE_RETRY_S", 0),
                ):
                    self.bus.wake_pass()
                    assert a["wake"]["status"] == "held"
                    clock.return_value += 59
                    self.bus.wake_pass()
                    assert len(self.submitted()) == before
                    clock.return_value += 1
                    self.bus.wake_pass()
                assert a["wake"]["status"] == "typed", a["wake"]
                assert len(self.submitted()) == before + 1
                assert self.submitted()[-1].startswith(
                    draft + "\n[user message cut off]\n[arke]"
                )

    def test_failed_hook_process_read_keeps_verified_harness_wakeable(
        self,
    ) -> None:
        self.agent("claude")
        a: dict[str, Any] = self.bus.agents["probe"]
        start: Any = a["pid_start"]
        body: dict[str, str | bool] = {
            "session_id": "probe",
            "event": "Stop",
            "failed": True,
            "process_token": f"{self.server.LOCAL_HOST}:{a['pid']}:{start}",
        }
        self.bus.hook(body)
        read: Callable[..., str] = Path.read_text

        def unavailable(path: Path, *args: Any, **kwargs: Any) -> str:
            if str(path) == f"/proc/{a['pid']}/stat":
                raise PermissionError("temporary process read failure")
            return read(path, *args, **kwargs)

        with patch.object(Path, "read_text", unavailable):
            self.bus.hook(body)
        assert a["pid_start"] == start
        self.bus.forget_old()
        self.bus.wake_pass()
        assert a["wake"]["status"] == "typed"
        assert len(self.submitted()) == 1
        assert "[arke] new message #1" in self.submitted()[0]
        inbox: Any = self.bus.inbox(a, 0, receipt=True)
        assert [m["text"] for m in inbox["messages"]] == ["test notification"]
        self.bus.acknowledge("probe", "", {"ids": inbox["delivery"]["ids"]})
        assert a["inbox"] == []
        self.bus.send("probe", {"to": ["human"], "text": "wake received"})
        assert self.bus.log_view({"agent": "human"})[-1]["text"] == "wake received"

    def test_shell_replacing_harness_gets_no_keystrokes(self) -> None:
        pane: str = self.agent()
        command: str = shlex.join(
            [sys.executable, str(Path(__file__).resolve()), "--shell"]
        )
        self.tmux("respawn-pane", "-k", "-t", pane, command)

        def shell_ready() -> bool:
            return (
                self.tmux(
                    "display-message", "-p", "-t", pane, "#{pane_current_command}"
                )
                == "bash"
                and self.tmux("capture-pane", "-p", "-t", pane).strip() == "❯"
            )

        self.wait_for(shell_ready)
        self.bus.wake_pass()
        screen: str = self.tmux("capture-pane", "-p", "-t", pane)
        assert "[arke]" not in screen
        assert self.submitted() == []
        assert self.bus.agents["probe"].get("wake", {}).get("status") != "typed"


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--tui":
        fake_harness(Path(sys.argv[2]), sys.argv[3])
    elif len(sys.argv) > 1 and sys.argv[1] == "--shell":
        sys.stdout.write("\x1b[2J\x1b[H")
        sys.stdout.flush()
        os.execvpe(
            "bash", ["bash", "--noprofile", "--norc", "-i"], os.environ | {"PS1": "❯ "}
        )
    else:
        raise SystemExit(pytest.main([__file__, *sys.argv[1:]]))

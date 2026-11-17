"""Arke client identity and usage regressions; all state is temporary, transport/processes are mocked.

uv run pytest tests/test_client.py  # from the clone root
"""

from __future__ import annotations

import email.message
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.error
from collections.abc import Callable, Generator
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import client
import pytest


class TestConfiguration:
    @pytest.mark.parametrize("under_home", [True, False])
    def test_advertised_commands_handle_shell_characters_and_different_homes(
        self, tmp_path: Path, under_home: bool
    ) -> None:
        home: Path = tmp_path / "home"
        home.mkdir()
        parent: Path = home if under_home else tmp_path / "home-neighbor"
        clone: Path = parent / "clone with 'quotes' $USER % ü"
        shutil.copytree(
            Path(client.__file__).parent,
            clone / "arke",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        env: dict[str, str] = {
            k: v for k, v in os.environ.items() if not k.startswith("ARKE_")
        } | {"HOME": str(home)}
        result: subprocess.CompletedProcess[str] = subprocess.run(
            [
                sys.executable,
                "-c",
                "import client, json, server; print(json.dumps([server.CLIENT, client.USAGE]))",
            ],
            cwd=clone / "arke",
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        command_path, usage = json.loads(result.stdout)
        assert f"`arke` = python3 {command_path}. Server:" in usage
        if under_home:
            assert command_path.startswith("~/")
            other_home: Path = tmp_path / "other-home"
            copied_clone: Path = other_home / clone.name
            shutil.copytree(clone, copied_clone)
            clone = copied_clone
            env["HOME"] = str(other_home)
        result = subprocess.run(
            ["bash", "-c", f"python3 {command_path} hooks claude"],
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        hooks: Any = json.loads(result.stdout)["hooks"]
        command: str = hooks["SessionStart"][0]["hooks"][0]["command"]
        assert shlex.split(command.rpartition("; exec ")[2])[1:] == [
            str(clone / "arke/client.py"),
            "hook",
            "claude",
        ]

    def test_default_server_is_local_on_any_machine(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(os.environ, {}, clear=True),
            patch.object(client, "CONFIG", Path(temporary) / "server-url"),
            patch.object(client, "HOST", "my-laptop"),
        ):
            assert client.server() == ("http://127.0.0.1:7777", "default")

    def test_generated_hooks_use_the_actual_clone_path(
        self, subtests: pytest.Subtests
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script: Path = (
                Path(temporary)
                / "clone with 'quotes' and spaces"
                / "arke"
                / "client.py"
            )
            shutil.copytree(
                Path(client.__file__).parent,
                script.parent,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            for kind in ("claude", "codex"):
                with subtests.test(kind=kind):
                    result: subprocess.CompletedProcess[str] = subprocess.run(
                        [sys.executable, str(script), "hooks", kind],
                        env={k: v for k, v in os.environ.items() if k != "ARKE_CONFIG"},
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    hooks: Any = json.loads(result.stdout)["hooks"]
                    events: set[str] = {
                        "SessionStart",
                        "UserPromptSubmit",
                        "PostToolUse",
                        "Stop",
                        "SessionEnd",
                    }
                    assert set(hooks) == events | (
                        {"StopFailure", "PreToolUse"}
                        if kind == "claude"
                        else {"SubagentStart"}
                    )
                    if kind == "claude":
                        assert hooks["PreToolUse"][0]["matcher"] == "Bash|Monitor"
                    for rules in hooks.values():
                        command: Any = rules[0]["hooks"][0]
                        assert command["type"] == "command"
                        assert shlex.split(
                            command["command"].rpartition("; exec ")[2]
                        ) == [
                            sys.executable,
                            str(script),
                            "hook",
                            kind,
                        ]
                    # a moved clone: exit 1 (a hook error), not python's 2 (Claude: block every tool call)
                    shutil.move(script, script.with_suffix(".moved"))
                    gone: subprocess.CompletedProcess[str] = subprocess.run(
                        ["bash", "-c", rules[0]["hooks"][0]["command"]],
                        check=False,
                        input="{}",
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    assert (gone.returncode, gone.stderr) == (
                        1,
                        f"arke: missing {script}\n",
                    )
                    shutil.move(script.with_suffix(".moved"), script)


class TestIdentity:
    @pytest.fixture(autouse=True)
    def setup(self) -> Generator[None]:
        with ExitStack() as self.stack:
            self.tmp: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory(
                prefix="arke-client-test-"
            )
            self.stack.callback(self.tmp.cleanup)
            self.root: Path = Path(self.tmp.name)
            self.stack.enter_context(patch.object(client, "STATE", self.root))
            self.stack.enter_context(
                patch.dict(
                    os.environ,
                    {
                        "CLAUDE_CODE_SESSION_ID": "outer-claude",
                        "CODEX_THREAD_ID": "inner-codex",
                        "TMUX": "/fake/socket,99,0",
                        "TMUX_PANE": "%1",
                        "ARKE_NAME": "outer-seat",
                    },
                    clear=True,
                )
            )
            self.parents: dict[int, int] = {
                300: 200,
                200: 150,
                150: 100,
                100: 90,
                90: 1,
            }
            self.kinds: dict[int, str] = {200: "codex", 100: "claude"}
            self.stack.enter_context(patch.object(os, "getppid", return_value=300))

            def parent_pid(pid: int) -> int:
                return self.parents[pid]

            self.stack.enter_context(
                patch.object(client, "parent", side_effect=parent_pid)
            )

            def harness_kind(pid: int) -> str:
                return self.kinds.get(pid, "")

            self.kind_mock: MagicMock | AsyncMock = self.stack.enter_context(
                patch.object(
                    client,
                    "harness_kind",
                    side_effect=harness_kind,
                )
            )
            self.git_mock: MagicMock | AsyncMock = self.stack.enter_context(
                patch.object(client, "git", return_value="/repo")
            )
            self.tmux_mock: MagicMock | AsyncMock = self.stack.enter_context(
                patch.object(
                    subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        [], 0, "outer-seat\t0\n", ""
                    ),
                )
            )
            self.call_mock: MagicMock | AsyncMock = self.stack.enter_context(
                patch.object(client, "call", return_value={})
            )
            self.init_mock: MagicMock | AsyncMock = self.stack.enter_context(
                patch.object(client, "sandbox_init", return_value="/sbin/init")
            )
            probe: Callable[..., bool] = client.is_daemon

            # These synthetic harness pids represent ordinary readable processes unless a test overrides the probe.
            def daemon_status(pid: int) -> bool:
                return False if pid in self.kinds else probe(pid)

            self.stack.enter_context(
                patch.object(
                    client,
                    "is_daemon",
                    side_effect=daemon_status,
                )
            )
            yield

    def record(self, pid: int, sid: str) -> None:
        directory: Path = self.root / "pid"
        directory.mkdir(exist_ok=True)
        (directory / str(pid)).write_text(sid)

    def test_nested_codex_does_not_claim_parent_seat_or_pid_mapping(self) -> None:
        self.record(100, "outer-claude")
        cache: Path = self.root / "seat" / "inner-codex.json"
        cache.parent.mkdir()
        cache.write_text(
            json.dumps(
                {
                    "names": ["outer-seat"],
                    "session": "outer-seat",
                    "repo": "repo",
                    "nested": False,
                }
            )
        )
        client.hook(
            "codex",
            {
                "hook_event_name": "SessionStart",
                "session_id": "inner-codex",
                "cwd": "/repo",
            },
        )
        body: Any = self.call_mock.call_args.args[2]
        assert body["nested"]
        assert body["names"] == []
        assert body["tmux_session"] == ""
        assert "tmux" not in body
        assert "pid" not in body
        assert (self.root / "pid" / "200").read_text() == "inner-codex"
        assert (self.root / "pid" / "100").read_text() == "outer-claude"
        self.tmux_mock.assert_not_called()
        assert os.environ["CLAUDE_CODE_SESSION_ID"] == "outer-claude"

    def test_resumed_session_recomputes_seat_for_new_harness_pid(self) -> None:
        cache: Path = self.root / "seat" / "inner-codex.json"
        cache.parent.mkdir()
        cache.write_text(
            json.dumps(
                {"version": 2, "pid": 999, "names": ["old-seat"], "nested": False}
            )
        )
        seat: dict[str, Any] = client.seat("inner-codex", "/repo")
        assert seat["nested"]
        assert seat["pid"] == 200

    def test_top_level_registration_preserves_explicit_name_and_tmux(
        self,
    ) -> None:
        self.kinds.pop(100)
        client.hook(
            "codex",
            {
                "hook_event_name": "SessionStart",
                "session_id": "inner-codex",
                "cwd": "/repo",
            },
        )
        body: Any = self.call_mock.call_args.args[2]
        assert not body["nested"]
        assert body["pid"] == 200
        assert body["tmux"] == "/fake/socket:%1"
        assert body["names"][0] == "outer-seat"

    def test_mixed_harness_cli_uses_nearest_harness_environment(self) -> None:
        self.record(100, "outer-claude")
        assert client.identity()[0] == "inner-codex"
        self.kinds[200], self.kinds[100] = "claude", "codex"
        os.environ["CLAUDE_CODE_SESSION_ID"] = "inner-claude"
        os.environ["CODEX_THREAD_ID"] = "outer-codex"
        assert client.identity()[0] == "inner-claude"

    def test_missing_inner_environment_uses_only_inner_pid_mapping(self) -> None:
        del os.environ["CODEX_THREAD_ID"]
        self.record(100, "outer-claude")
        assert client.identity()[0] == ""
        self.record(200, "inner-codex")
        assert client.identity()[0] == "inner-codex"

    def test_same_kind_nesting_prefers_child_hook_to_inherited_variable(
        self,
    ) -> None:
        self.kinds[200] = "claude"
        assert (
            client.identity()[0] == ""
        )  # ambiguous even when the parent has never registered
        self.record(100, "outer-claude")
        assert client.identity()[0] == ""
        self.record(200, "inner-claude")
        assert client.identity()[0] == "inner-claude"

    def test_unregistered_nested_agent_cannot_fall_back_to_inherited_ab_as(
        self,
    ) -> None:
        self.kinds[200] = "claude"
        os.environ["ARKE_AS"] = "outer-seat"
        with pytest.raises(client.BusError, match="nearest agent harness"):
            _CALL("GET", "/agents")

    def test_top_level_resumed_thread_environment_beats_old_pid_mapping(
        self,
    ) -> None:
        self.kinds.pop(100)
        self.record(200, "previous-codex-thread")
        assert client.identity()[0] == "inner-codex"

    def test_standalone_environment_and_legacy_mapping_still_work(self) -> None:
        self.kinds.clear()
        assert client.identity()[0] == "outer-claude"
        del os.environ["CLAUDE_CODE_SESSION_ID"], os.environ["CODEX_THREAD_ID"]
        assert client.identity()[0] == ""
        # a pid map at an ancestor that isn't a harness (left by one that had that pid before) is no identity:
        # it made a person's ARKE_AS=human command act as that old agent and read its mail
        self.record(200, "legacy-launcher")
        assert client.identity()[0] == ""

    def test_codex_sandbox_hiding_its_harness_uses_the_codex_thread(self) -> None:
        """Inside Codex's sandbox no harness is an ancestor (PID 1 is codex-linux-sandbox); the inherited
        CLAUDE_CODE_SESSION_ID belongs to the outer Claude that launched this Codex."""
        self.kinds.clear()
        self.init_mock.return_value = "codex-linux-sandbox"
        assert client.identity()[0] == "inner-codex"
        self.init_mock.return_value = "/sbin/init"
        assert client.identity()[0] == "outer-claude"

    def test_process_token_identifies_actual_harness_lifetime(self) -> None:
        fields: list[str] = ["0"] * 20
        fields[19] = "12345"
        first: str = "200 (codex worker) " + " ".join(fields)
        fields[19] = "12346"
        restarted: str = "200 (codex worker) " + " ".join(fields)
        with patch.object(Path, "read_text", side_effect=[first, first, restarted]):
            token: str = client.process_token(200)
            assert token == f"{client.HOST}:200:12345"
            assert client.process_token(200) == token
            assert client.process_token(200) != token

    def test_unverifiable_process_does_not_get_a_token(self) -> None:
        assert client.process_token(0) == ""
        with patch.object(Path, "read_text", side_effect=FileNotFoundError):
            assert client.process_token(200) == ""

    def test_all_nested_hooks_report_lifetime_without_sampling_parent(
        self,
    ) -> None:
        with patch.object(
            client, "process_token", return_value="host:200:12345"
        ) as token:
            for event in (*client.TURN_HOOKS, "PostToolUse"):
                client.hook(
                    "codex",
                    {
                        "hook_event_name": event,
                        "session_id": "inner-codex",
                        "cwd": "/repo",
                    },
                )
                token.assert_called_with(200)
                body: Any = self.call_mock.call_args.args[2]
                assert body["process_token"] == "host:200:12345"
                assert body["nested"]
                assert "pid" not in body
                assert "tmux" not in body

    def test_claude_app_server_prompt_keeps_its_claude_identity(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "CLAUDE_CODE_SESSION_ID": "my-claude",
                    "CODEX_THREAD_ID": "outer-codex",
                },
                clear=True,
            ),
            patch.object(client, "ancestors", return_value=iter([123])),
            patch.object(client, "harness_kind", return_value="claude"),
            patch.object(client, "is_daemon", return_value=True) as probe,
        ):
            assert client.identity()[0] == "my-claude"
            probe.assert_not_called()

    def test_codex_daemon_hook_reports_lifetime_without_claiming_a_pane_or_names(
        self,
    ) -> None:
        """The server needs the daemon lifetime to reject stale hooks, but it is not a thread's seat identity."""
        self.kinds = {200: "codex"}
        with (
            patch.object(client, "is_daemon", return_value=True),
            patch.object(client, "process_token", return_value="host:200:1"),
        ):
            client.hook(
                "codex",
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "thread",
                    "cwd": "/repo",
                    "prompt": "please summarize the results",
                },
            )
        body: Any = self.call_mock.call_args.args[2]
        assert body["daemon"]
        assert not body["nested"]
        assert (
            body["names"],
            body["tmux_session"],
            body["process_token"],
            body["pid"],
        ) == ([], "", "host:200:1:thread", 200)
        assert "tmux" not in body
        assert "prompt" not in body
        with (
            patch.object(client, "is_daemon", return_value=True),
            patch.object(client, "process_token", return_value="host:200:1"),
        ):
            client.hook(
                "codex",
                {
                    "hook_event_name": "SessionStart",
                    "session_id": "other-thread",
                    "cwd": "/repo",
                },
            )
        assert (
            body["process_token"] != self.call_mock.call_args.args[2]["process_token"]
        )

    def test_a_daemon_threads_ab_never_uses_the_shared_pid_map(self) -> None:
        """Every thread's hook runs in the one daemon, so pid/<daemon> names whichever hooked last."""
        self.kinds = {200: "codex"}
        self.record(200, "last-hooked-thread")
        os.environ.pop("CODEX_THREAD_ID")
        with patch.object(client, "is_daemon", return_value=True):
            assert client.identity()[0] == ""
            os.environ["CODEX_THREAD_ID"] = "my-thread"
            assert client.identity()[0] == "my-thread"
            with patch.object(client, "process_token", return_value="host:200:1"):
                client.hook(
                    "codex",
                    {
                        "hook_event_name": "PostToolUse",
                        "session_id": "another-thread",
                        "cwd": "/repo",
                    },
                )
        assert (self.root / "pid" / "200").read_text() == "last-hooked-thread"

    def test_codex_0164_daemon_under_another_codex_process_is_not_nested(
        self,
    ) -> None:
        """0.164's daemon is the child of `codex app-server daemon pid-update-loop`: that ancestor made every daemon
        thread look nested (no pid, never woken), and a v3 seat cache kept it so."""
        self.parents = {300: 200, 200: 150, 150: 1}
        self.kinds = {200: "codex", 150: "codex"}
        cache: Path = self.root / "seat" / "thread.json"
        cache.parent.mkdir()
        cache.write_text(
            json.dumps(
                {
                    "version": 3,
                    "pid": 200,
                    "names": [],
                    "session": "",
                    "repo": "repo",
                    "nested": True,
                }
            )
        )

        def is_inner_harness(pid: int) -> bool:
            return pid == 200

        with (
            patch.object(client, "is_daemon", side_effect=is_inner_harness),
            patch.object(client, "process_token", return_value="host:200:1"),
        ):
            st: dict[str, Any] = client.seat("thread", "/repo")
            assert (st["daemon"], st["nested"], st["names"]) == (True, False, [])
            client.hook(
                "codex",
                {
                    "hook_event_name": "SessionStart",
                    "session_id": "thread",
                    "cwd": "/repo",
                },
            )
        body: Any = self.call_mock.call_args.args[2]
        assert (body["daemon"], body["nested"], body["pid"]) == (True, False, 200)

    def test_only_a_wake_notice_is_no_prompt(self, subtests: pytest.Subtests) -> None:
        for prompt, wake in (
            ("[arke] 1 new message (#6 bob)", True),
            ("please check the [agentbus] logs", False),
            (
                (
                    "<task-notification> <summary>Stop hook feedback</summary> </task-notification>\n"
                    'Stop hook blocking error from command "listen": [arke] 1 new message (#6 bob)'
                ),
                True,
            ),
            ("why did [arke] wake you?", False),
        ):
            with subtests.test(prompt=prompt):
                client.hook(
                    "claude",
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "session_id": "outer-claude",
                        "cwd": "/repo",
                        "prompt": prompt,
                    },
                )
                assert self.call_mock.call_args.args[2]["wake"] == wake

    def test_only_codex_app_server_is_the_daemon(
        self, subtests: pytest.Subtests
    ) -> None:
        for argv, daemon in (
            (b"codex\0app-server\0--managed-daemon\0", True),
            (b"codex\0fix the app-server crash\0", False),
            (b"codex\0-C\0/src/app-server\0", False),
        ):
            with (
                subtests.test(argv=argv),
                patch.object(Path, "read_bytes", return_value=argv),
            ):
                assert client.is_daemon(1) == daemon

    def test_hook_inside_codex_sandbox_is_nested(self) -> None:
        """Audit 4a: inside Codex's sandbox the outer Codex is hidden; a Claude there must not take its pane or names."""
        self.kinds = {300: "claude"}  # only the inner Claude is visible
        self.init_mock.return_value = "codex-linux-sandbox"
        assert client.seat("inner-claude", "/repo")["nested"]

    def test_a_corrupt_seat_cache_is_rebuilt(self) -> None:
        """Audit 10: a write cut off by a full disk left every later hook failing until the file was deleted."""
        cache: Path = self.root / "seat" / "inner-codex.json"
        cache.parent.mkdir()
        cache.write_text('{"version": 2, "pid"')
        assert client.seat("inner-codex", "/repo")["pid"] == 200
        assert json.loads(cache.read_text())["pid"] == 200

    def test_a_hanging_git_doesnt_hold_the_hook(self) -> None:
        """Audit 11: git ran with no timeout before the hook's HTTP call."""

        def slow(cmd: list[str], **kw: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])

        with (
            patch.object(client, "git", _GIT),
            patch.object(subprocess, "run", side_effect=slow) as run,
        ):
            assert client.git("/repo", "branch", "--show-current") is None
        assert run.call_args.kwargs["timeout"] == client.PREP_TIMEOUT_S

    def test_a_git_timeout_isnt_cached(self) -> None:
        """one slow first git left the agent out of its repo's project for the whole session."""
        self.git_mock.return_value = None
        assert client.seat("inner-codex", "/repo")["repo"] == ""
        self.git_mock.return_value = "/home/u/repo"
        assert client.seat("inner-codex", "/repo")["repo"] == "repo"

    def test_a_seat_cached_before_the_sandbox_check_is_redone(self) -> None:
        """a running nested Codex upgraded in place kept its old nested:false cache and claimed the pane."""
        self.kinds = {300: "claude"}
        self.init_mock.return_value = "codex-linux-sandbox"
        cache: Path = self.root / "seat" / "inner-claude.json"
        cache.parent.mkdir()
        cache.write_text(
            json.dumps(
                {
                    "version": 2,
                    "pid": 300,
                    "names": ["outer-seat"],
                    "session": "outer-seat",
                    "repo": "repo",
                    "nested": False,
                }
            )
        )
        assert client.seat("inner-claude", "/repo")["nested"]

    def test_subagent_hooks_never_reach_the_bus_or_print(
        self, subtests: pytest.Subtests
    ) -> None:
        """Sub-agents are invisible to the bus: their hooks carry the parent's session_id plus agent_id."""
        for kind in ("claude", "codex"):
            for event in ("SubagentStart", "PostToolUse", "Stop", "UserPromptSubmit"):
                with (
                    subtests.test(kind=kind, event=event),
                    patch.object(
                        client.sys,
                        "stdin",
                        io.StringIO(
                            json.dumps(
                                {
                                    "hook_event_name": event,
                                    "session_id": "parent",
                                    "agent_id": "sub-thread",
                                    "agent_type": "default",
                                    "cwd": "/repo",
                                }
                            )
                        ),
                    ),
                    patch.object(client.sys, "stdout", io.StringIO()) as out,
                ):
                    client.run_hook(kind)
                    assert out.getvalue() == ""
                    self.call_mock.assert_not_called()
                    self.tmux_mock.assert_not_called()
        assert not (self.root / "pid").exists()

    @pytest.mark.parametrize("depth", [1, 2])
    def test_nested_codex_helper_cannot_send_directly_or_ack(self, depth: int) -> None:
        self.kinds = {200: "codex", 100: "codex"}
        client.hook(
            "codex", {"hook_event_name": "SessionStart", "session_id": "inner-codex"}
        )
        parent: str = "inner-codex"
        for n in range(depth):
            child: str = f"helper-{n}"
            with patch.object(
                client.sys,
                "stdin",
                io.StringIO(
                    json.dumps(
                        {
                            "hook_event_name": "SubagentStart",
                            "session_id": parent,
                            "agent_id": child,
                        }
                    )
                ),
            ):
                client.run_hook("codex")
            parent = child
        os.environ["CODEX_THREAD_ID"] = parent
        assert (self.root / "pid/200").read_text() == "inner-codex"
        assert client.identity() == ("inner-codex", parent)
        with (
            patch.object(client, "call", _CALL),
            patch.object(
                client,
                "open_bus",
                side_effect=[
                    io.BytesIO(b"{}"),
                    io.BytesIO(
                        b'{"messages":[],"delivery":{"sid":"inner-codex","ids":[1]}}'
                    ),
                ],
            ) as transport,
        ):
            client.call("POST", "/send", {"to": ["bob"], "text": "request"})
            client.print_inbox()
            with pytest.raises(client.BusError, match="read-only"):
                client.call("POST", "/about", {"about": "helper"})
        requests: list[Any] = [call.args[0] for call in transport.call_args_list]
        assert len(requests) == 2  # no acknowledgment, even if a receipt was returned
        assert requests[0].selector == "/helper/send"
        for req in requests:
            assert req.get_header("X-agent-helper") == parent
            assert req.get_header("X-agent-session") == "inner-codex"

    @pytest.mark.parametrize("kind", ["codex", "claude"])
    def test_inherited_outer_helper_mapping_does_not_identify_inner_harness(
        self, kind: str
    ) -> None:
        self.kinds = {200: kind, 100: "codex"}
        self.record(200, "inner-session")
        os.environ["CODEX_THREAD_ID"] = "outer-helper"
        os.environ["CLAUDE_CODE_SESSION_ID"] = "inner-session"
        client.save_cache(self.root / "thread/outer-helper", "outer-parent")
        assert client.identity() == ("inner-session", "")

    def test_cli_in_a_subagents_shell_identifies_its_parent_and_helper(self) -> None:
        """A Codex sub-agent's shell gets CODEX_THREAD_ID = its own agent_id; its SubagentStart maps that to the
        parent, while retaining the helper identity for restricted access."""
        self.kinds = {200: "codex"}
        os.environ["CODEX_THREAD_ID"] = "sub-thread"
        assert client.identity()[0] == "sub-thread"  # no map yet
        with patch.object(
            client.sys,
            "stdin",
            io.StringIO(
                json.dumps(
                    {
                        "hook_event_name": "SubagentStart",
                        "session_id": "parent-thread",
                        "agent_id": "sub-thread",
                        "agent_type": "default",
                    }
                )
            ),
        ):
            client.run_hook("codex")
        assert (self.root / "thread" / "sub-thread").read_text() == "parent-thread"
        assert client.identity()[0] == "parent-thread"
        assert client.identity() == ("parent-thread", "sub-thread")
        os.environ["CODEX_THREAD_ID"] = "parent-thread"  # the parent's own shell
        assert client.identity()[0] == "parent-thread"
        assert client.identity() == ("parent-thread", "")
        self.kinds = {100: "claude"}
        assert client.identity()[0] == "outer-claude"
        self.call_mock.assert_not_called()


_GIT: Callable[..., str | None] = client.git
_CALL: Callable[..., Any] = client.call


class TestDelivery:
    def test_hook_acknowledges_only_after_output_is_flushed(self) -> None:
        events: list[str] = []

        class Output(io.StringIO):
            def flush(self) -> None:
                events.append("flush")

        delivery: dict[str, str | list[int]] = {"sid": "a", "ids": [1]}

        def record_ack(*a: Any, **kw: Any) -> None:
            return events.append("ack")

        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(client, "STATE", Path(temp)),
            patch.object(
                client.sys, "stdin", io.StringIO('{"hook_event_name":"Stop"}')
            ),
            patch.object(
                client,
                "hook",
                return_value={
                    "decision": "block",
                    "reason": "mail",
                    "_delivery": delivery,
                },
            ),
            patch.object(client.sys, "stdout", Output()) as output,
            patch.object(client, "call", side_effect=record_ack) as call,
        ):
            client.run_hook("claude")
        assert events == ["flush", "ack"]
        assert json.loads(output.getvalue()) == {"decision": "block", "reason": "mail"}
        assert call.call_args.args == ("POST", "/ack", {"ids": [1]})
        assert call.call_args.kwargs["sid"] == "a"

    def test_broken_hook_output_is_not_acknowledged(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(client, "STATE", Path(temp)),
            patch.object(
                client.sys, "stdin", io.StringIO('{"hook_event_name":"Stop"}')
            ),
            patch.object(
                client,
                "hook",
                return_value={
                    "decision": "block",
                    "reason": "mail",
                    "_delivery": {"sid": "a", "ids": [1]},
                },
            ),
            patch.object(client, "call") as call,
            patch("builtins.print", side_effect=BrokenPipeError),
        ):
            with pytest.raises(BrokenPipeError):
                client.run_hook("claude")
            call.assert_not_called()

    def test_one_unreachable_server_doesnt_mute_tool_hooks_for_another(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(client, "STATE", Path(temp)),
            patch.object(client.sys, "stdout", io.StringIO()),
        ):
            with (
                patch.object(
                    client, "server", return_value=("http://dead:7777", "test")
                ),
                patch.object(
                    client.sys,
                    "stdin",
                    io.StringIO('{"hook_event_name":"PostToolUse"}'),
                ),
                patch.object(client, "hook", side_effect=client.Down("down")),
            ):
                client.run_hook("claude")
            with (
                patch.object(
                    client, "server", return_value=("http://your-bus-host:7777", "test")
                ),
                patch.object(
                    client.sys,
                    "stdin",
                    io.StringIO('{"hook_event_name":"PostToolUse"}'),
                ),
                patch.object(client, "hook", return_value=None) as live,
            ):
                client.run_hook("claude")
            live.assert_called_once()

    def test_a_failed_cache_write_still_reaches_the_bus(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            blocked: Path = Path(temp) / "state"
            blocked.write_text("a file where the state dir should be")
            with (
                patch.object(client, "STATE", blocked),
                patch.object(client, "call", return_value={}) as call,
                patch.object(client.sys, "stderr", io.StringIO()) as err,
            ):
                client.hook(
                    "claude",
                    {"hook_event_name": "Stop", "session_id": "s", "cwd": temp},
                )
            call.assert_called_once()
            assert call.call_args.args[:2] == ("POST", "/hook")
            assert "couldn't write" in err.getvalue()

    def test_server_errors_back_off_tool_hooks_and_warn_once(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(client, "STATE", Path(temp)),
            patch.object(client, "server", return_value=("http://bus:7777", "test")),
        ):
            outputs: list[str] = []
            with patch.object(
                client, "hook", side_effect=client.Down("HTTP 500: boom")
            ) as failing:
                for _ in range(2):
                    with (
                        patch.object(
                            client.sys,
                            "stdin",
                            io.StringIO('{"hook_event_name":"PostToolUse"}'),
                        ),
                        patch.object(client.sys, "stdout", io.StringIO()) as out,
                    ):
                        client.run_hook("claude")
                    outputs.append(out.getvalue())
            assert failing.call_count == 1  # the second tool hook skipped the bus
            assert "HTTP 500: boom" in outputs[0]
            assert outputs[1] == ""

    def test_turn_hooks_use_the_short_timeout_while_the_bus_is_down(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(client, "STATE", Path(temp)),
            patch.object(client, "server", return_value=("http://bus:7777", "test")),
        ):
            down: Path = (
                Path(temp)
                / f"down-until-{client.hashlib.sha256(b'http://bus:7777').hexdigest()[:12]}"
            )
            quick: list[bool] = []
            for fresh in (False, True):
                if fresh:
                    down.write_text(str(client.time.time() + 60))
                with (
                    patch.object(
                        client.sys, "stdin", io.StringIO('{"hook_event_name":"Stop"}')
                    ),
                    patch.object(client.sys, "stdout", io.StringIO()),
                    patch.object(client, "hook", return_value=None) as hooked,
                ):
                    client.run_hook("claude")
                quick.append(hooked.call_args.kwargs["quick"])
            assert quick == [False, True]

    def test_output_survives_a_down_marker_that_cant_be_removed(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(client, "STATE", Path(temp)),
            patch.object(client, "server", return_value=("http://bus:7777", "test")),
        ):
            with (
                patch.object(
                    client.sys, "stdin", io.StringIO('{"hook_event_name":"Stop"}')
                ),
                patch.object(client.sys, "stdout", io.StringIO()) as out,
                patch.object(client.sys, "stderr", io.StringIO()) as err,
                patch.object(
                    client, "hook", return_value={"decision": "block", "reason": "mail"}
                ),
                patch.object(
                    Path, "unlink", side_effect=OSError(30, "Read-only file system")
                ),
            ):
                client.run_hook("claude")
            assert json.loads(out.getvalue()) == {"decision": "block", "reason": "mail"}
            assert "couldn't clear" in err.getvalue()

    def test_session_start_sends_its_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with (
                patch.object(client, "STATE", Path(temp)),
                patch.object(client, "call", return_value={}) as call,
            ):
                client.hook(
                    "claude",
                    {
                        "hook_event_name": "SessionStart",
                        "session_id": "s",
                        "cwd": temp,
                        "source": "compact",
                    },
                )
            assert call.call_args.args[2]["source"] == "compact"

    def test_unreadable_hook_input_is_reported_not_raised(self) -> None:
        for raw in ("", "not json", "[1]"):
            with (
                patch.object(client.sys, "stdin", io.StringIO(raw)),
                patch.object(client.sys, "stdout", io.StringIO()) as out,
            ):
                client.run_hook("claude")
            assert "unreadable event" in json.loads(out.getvalue())["systemMessage"]

    def test_tool_input_that_is_not_an_object(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with (
                patch.object(client, "STATE", Path(temp)),
                patch.object(client, "call", return_value={}) as call,
            ):
                client.hook(
                    "codex",
                    {
                        "hook_event_name": "PostToolUse",
                        "session_id": "s",
                        "cwd": temp,
                        "tool_name": "apply_patch",
                        "tool_input": "*** Begin Patch",
                    },
                )
            assert call.call_args.args[2]["target"] == "*** Begin Patch"

    def test_codex_command_is_recorded_in_hook_activity(self, tmp_path: Path) -> None:
        with (
            patch.object(client, "STATE", tmp_path),
            patch.object(client, "call", return_value={}) as call,
        ):
            client.hook(
                "codex",
                {
                    "hook_event_name": "PostToolUse",
                    "session_id": "s",
                    "cwd": str(tmp_path),
                    "tool_name": "exec_command",
                    "tool_input": {"cmd": "cat src/main.py"},
                },
            )
        assert call.call_args.args[2]["target"] == "cat src/main.py"

    @pytest.mark.parametrize("branch", [None, "", "main"])
    def test_branch_timeout_is_omitted_but_detached_head_is_sent(
        self, tmp_path: Path, branch: str | None
    ) -> None:
        with (
            patch.object(client, "STATE", tmp_path),
            patch.object(client, "git", return_value=branch),
            patch.object(client, "call", return_value={}) as call,
        ):
            client.hook(
                "claude",
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "s",
                    "cwd": str(tmp_path),
                },
            )
        body: dict[str, Any] = call.call_args.args[2]
        if branch is None:
            assert "branch" not in body
        else:
            assert body["branch"] == branch

    def test_flags_inside_message_text_are_kept(self) -> None:
        assert client.flags(
            [
                "send",
                "bob",
                "please",
                "use",
                "--all",
                "and",
                "--before",
                "5",
                "now",
                "--board",
            ]
        ) == (
            ["send", "bob", "please", "use", "--all", "and", "--before", "5", "now"],
            {"--board": ""},
        )
        assert client.flags(["send", "bob", "hi", "--request-id", "r1", "--board"]) == (
            ["send", "bob", "hi"],
            {"--board": "", "--request-id": "r1"},
        )
        # only the command's own flags end a message; anything else at the end is text
        assert client.flags(["send", "bob", "see the log with", "--before", "12"]) == (
            ["send", "bob", "see the log with", "--before", "12"],
            {},
        )
        assert client.flags(["broadcast", "use", "--board"]) == (
            ["broadcast", "use", "--board"],
            {},
        )
        assert client.flags(["about", "fixing", "--all"]) == (
            ["about", "fixing", "--all"],
            {},
        )
        assert client.flags(["broadcast", "hi", "--request-id", "r2"]) == (
            ["broadcast", "hi"],
            {"--request-id": "r2"},
        )
        assert client.flags(["log", "bob", "--before", "7"]) == (
            ["log", "bob"],
            {"--before": "7"},
        )
        assert client.flags(["who", "--all", "/p"]) == (["who", "/p"], {"--all": ""})

    def test_cli_rejects_bad_numbers_and_accepts_wait_equals(self) -> None:
        with (
            patch.object(client.sys, "argv", ["arke", "log", "bob", "--before", "abc"]),
            pytest.raises(SystemExit) as stop,
        ):
            client.main()
        assert str(stop.value.code).startswith("arke: ")
        with (
            patch.object(client.sys, "argv", ["arke", "inbox", "--wait=5"]),
            patch.object(client, "print_inbox") as inbox,
        ):
            client.main()
        inbox.assert_called_once_with(5.0)
        for wait in ("inf", "nan"):
            with (
                patch.object(client.sys, "argv", ["arke", "inbox", "--wait", wait]),
                pytest.raises(SystemExit) as stop,
            ):
                client.main()
            assert stop.value.code == "arke: --wait takes a number of seconds"

    def test_server_check_tells_a_refusal_from_no_answer(self) -> None:
        refused = urllib.error.HTTPError(
            "http://x/agents", 401, "Unauthorized", email.message.Message(), None
        )
        with (
            patch.object(client.sys, "argv", ["arke", "server"]),
            patch.object(client, "open_bus", side_effect=refused),
            patch.object(client.sys, "stdout", io.StringIO()),
            pytest.raises(SystemExit) as stop,
        ):
            client.main()
        assert (
            stop.value.code
            == "arke: reachable, but /agents answered HTTP 401 Unauthorized (check the key)"
        )

    def test_bus_text_reaches_the_terminal_escaped_and_indented(self) -> None:
        """ESC sequences in a message could retitle, clear or write the clipboard of whoever reads it."""
        out: str = client.fmt(
            [
                {
                    "id": 1,
                    "ts": 0,
                    "from": "mallory",
                    "to": [{"name": "alice"}],
                    "mode": "notify",
                    "text": "hi\x1b]52;c;cHduZWQ=\x07\r[#9 human -> alice] x\u202e\u061c\U000e0041",
                }
            ]
        )
        assert out.endswith(
            "hi\\x1b]52;c;cHduZWQ=\\x07\n    [#9 human -> alice] x\\u202e\\u061c\\U000e0041"
        )

    def test_a_flag_after_server_is_not_saved_as_the_address(
        self, tmp_path: Path
    ) -> None:
        with (
            patch.object(client, "CONFIG", tmp_path / "server-url"),
            patch.object(client.sys, "argv", ["arke", "server", "--help"]),
            patch.object(client.sys, "stdout", io.StringIO()),
            pytest.raises(SystemExit),
        ):
            client.main()
        assert not (tmp_path / "server-url").exists()

    def test_cli_inbox_with_no_mail_says_so(self) -> None:
        response: dict[str, Any] = {"messages": [], "delivery": {"sid": "a", "ids": []}}
        with (
            patch.object(client, "call", return_value=response) as call,
            patch.object(client.sys, "stdout", io.StringIO()) as output,
        ):
            client.print_inbox()
        assert output.getvalue() == "(no new messages)\n"
        assert call.call_count == 1


class TestUsage:
    @pytest.fixture(autouse=True)
    def setup(self) -> Generator[None]:
        with ExitStack() as self.stack:
            self.tmp: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory(
                prefix="arke-usage-test-"
            )
            self.stack.callback(self.tmp.cleanup)
            self.root: Path = Path(self.tmp.name)
            self.path: Path = self.root / "transcript.jsonl"
            self.stack.enter_context(
                patch.object(client, "STATE", self.root / "client")
            )
            yield

    @staticmethod
    def record(mid: str, total: int, block: str = "text", padding: int = 0) -> bytes:
        return (
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "id": mid,
                        "model": "claude-test",
                        "usage": {
                            "output_tokens": total,
                            "input_tokens": 10,
                            "cache_read_input_tokens": 20,
                            "cache_creation_input_tokens": 30,
                        },
                        "content": [{"type": block, "text": "x" * padding}],
                    },
                }
            )
            + "\n"
        ).encode()

    def append(self, data: bytes) -> None:
        with self.path.open("ab") as f:
            f.write(data)

    def test_content_blocks_count_once_across_persisted_incremental_reads(
        self,
    ) -> None:
        self.path.write_bytes(
            self.record("one", 10, "thinking") + self.record("one", 10)
        )
        assert client.usage("s", str(self.path))["tokens"] == 10
        self.append(self.record("one", 15, "tool_use") + self.record("two", 7))
        u: dict[str, Any] = client.usage("s", str(self.path))
        assert u["tokens"] == 22
        assert u["context"] == 60
        assert u["offset"] == self.path.stat().st_size
        assert client.usage("s", str(self.path))["tokens"] == 22
        self.append(self.record("one", 10))  # late duplicate of an older content block
        assert client.usage("s", str(self.path))["tokens"] == 22

    @pytest.mark.parametrize(
        "field", ["output_tokens", "input_tokens", "cache_read_input_tokens"]
    )
    @pytest.mark.parametrize("bad", [None, "10", -1, True, float("nan")])
    def test_bad_usage_is_skipped_without_losing_later_counts(
        self, field: str, bad: Any
    ) -> None:
        self.path.write_bytes(self.record("one", 1))
        assert client.usage("s", str(self.path))["tokens"] == 1
        broken = json.loads(self.record("bad", 50))
        broken["message"]["usage"][field] = bad
        self.append((json.dumps(broken) + "\n").encode() + self.record("two", 10))
        u = client.usage("s", str(self.path))
        assert (u["tokens"], u["context"], u["offset"]) == (
            11,
            60,
            self.path.stat().st_size,
        )
        self.append(self.record("three", 7))
        assert client.usage("s", str(self.path))["tokens"] == 18
        assert (
            client.usage("s", str(self.path))["tokens"] == 18
        )  # no replay on an unchanged file

    @pytest.mark.parametrize(
        "bad", [[1], {"output_tokens": None}, {"output_tokens": -1}]
    )
    def test_bad_codex_usage_does_not_pin_or_poison_the_cache(self, bad: Any) -> None:
        def record(total: Any, context: int) -> bytes:
            return (
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "total_token_usage": total,
                                "last_token_usage": {"input_tokens": context},
                                "model_context_window": 200000,
                            },
                        },
                    }
                )
                + "\n"
            ).encode()

        self.path.write_bytes(record({"output_tokens": 1}, 100))
        assert client.usage("s", str(self.path))["tokens"] == 1
        self.append(record(bad, 999))
        u = client.usage("s", str(self.path))
        assert (u["tokens"], u["context"], u["offset"]) == (
            1,
            100,
            self.path.stat().st_size,
        )
        self.append(record({"output_tokens": 11}, 200))
        u = client.usage("s", str(self.path))
        assert (u["tokens"], u["context"]) == (11, 200)

    def test_half_written_record_is_not_counted_until_complete(self) -> None:
        self.path.write_bytes(self.record("one", 5))
        u: dict[str, Any] = client.read_usage(client.USAGE_FRESH, self.path)
        line: bytes = self.record("one", 12)
        self.append(line[: len(line) // 2])
        partial: dict[str, Any] = client.read_usage(u, self.path)
        assert (partial["tokens"], partial["offset"]) == (5, u["offset"])
        self.append(line[len(line) // 2 :])
        assert client.read_usage(partial, self.path)["tokens"] == 12

    def test_truncated_transcript_resets_dedup_state(self) -> None:
        self.path.write_bytes(self.record("same-id", 100, padding=2000))
        u: dict[str, Any] = client.read_usage(client.USAGE_FRESH, self.path)
        self.path.write_bytes(self.record("same-id", 3))
        assert client.read_usage(u, self.path)["tokens"] == 3

    def test_different_transcript_path_resets_totals(self) -> None:
        self.path.write_bytes(self.record("same-id", 100))
        u: dict[str, Any] = client.read_usage(client.USAGE_FRESH, self.path)
        other: Path = self.root / "replacement.jsonl"
        other.write_bytes(self.record("same-id", 200))
        assert client.read_usage(u, other)["tokens"] == 200

    def test_fresh_tail_offset_is_preserved_and_aligned(self) -> None:
        first: bytes = self.record("old", 100, padding=2000)
        self.path.write_bytes(first + self.record("new", 30))
        for fresh in (
            client.USAGE_FRESH,
            {k: v for k, v in client.USAGE_FRESH.items() if k != "version"},
        ):
            u: dict[str, Any] = client.read_usage(
                fresh | {"offset": len(first) - 50}, self.path
            )
            assert u["tokens"] == 30
            assert u["offset"] == self.path.stat().st_size

    def test_input_cache_and_global_fresh_state_are_not_mutated(self) -> None:
        self.path.write_bytes(self.record("one", 10))
        u: dict[str, Any] = client.read_usage(client.USAGE_FRESH, self.path)
        before: str = json.dumps(u, sort_keys=True)
        self.append(self.record("one", 15))
        assert client.read_usage(u, self.path)["tokens"] == 15
        assert json.dumps(u, sort_keys=True) == before
        assert "claude_outputs" not in client.USAGE_FRESH

    def test_codex_totals_remain_absolute(self) -> None:
        records: list[dict[str, Any]] = [
            {
                "type": "turn_context",
                "payload": {"model": "codex-test", "effort": "high"},
            }
        ]
        records += [
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "model_context_window": 200000,
                        "total_token_usage": {"output_tokens": n},
                        "last_token_usage": {"input_tokens": 50},
                    },
                },
            }
            for n in (100, 100, 150)
        ]
        self.path.write_text("".join(json.dumps(r) + "\n" for r in records))
        u: dict[str, Any] = client.read_usage(client.USAGE_FRESH, self.path)
        assert (u["tokens"], u["context"], u["window"], u["model"], u["effort"]) == (
            150,
            50,
            200000,
            "codex-test",
            "high",
        )

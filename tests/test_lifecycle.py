"""Agents that die, reboot or restart without telling the bus, Codex daemons that misbehave, and other input that
used to stop wake-ups or lose mail. Temporary state only; no live bus, terminal or Codex daemon."""

from __future__ import annotations

import collections.abc
import json
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import server
import transcripts
import wake


class TestLifecycle:
    @pytest.fixture(autouse=True)
    def setup(self, bus: server.Bus, tmp_path: Path) -> Generator[None]:
        self.bus: server.Bus = bus
        self.root: Path = tmp_path
        with ExitStack() as self.stack:
            yield

    def hook(
        self, sid: str, event: str = "SessionStart", **body: Any
    ) -> dict[str, Any]:
        return self.bus.hook(
            {
                "session_id": sid,
                "event": event,
                "kind": "claude",
                "host": "remote",
                **body,
            }
        )

    def send(self, to: str, text: str = "task") -> None:
        self.bus.send("human", {"to": [to], "text": text})

    # --- agents gone without SessionEnd ---

    def test_a_dead_local_harness_goes_offline_and_its_name_and_mail_pass_on(
        self,
    ) -> None:
        """kill -9, a crash or a killed tmux server send no SessionEnd; a reused pane id kept the record alive."""
        proc: dict[str, str | int] = {
            "comm": "claude",
            "ppid": 1,
            "pgrp": 1,
            "tty": 0,
            "foreground": 0,
            "start": 50,
        }
        with patch.object(server, "process_info", return_value=proc):
            self.hook("a", host=server.LOCAL_HOST, pid=4242, names=["work"])
        self.send("work")
        with patch.object(server, "process_info", return_value=proc):
            self.bus.forget_old()
        assert self.bus.agents["a"]["state"] == "idle", "still running: left alone"
        with patch.object(
            server, "process_info", return_value=proc | {"start": 51}
        ):  # the pid now belongs to another process
            self.bus.forget_old()
        assert self.bus.agents["a"]["state"] == "offline"
        self.hook("b", host=server.LOCAL_HOST, names=["work"])
        assert self.bus.agents["b"]["name"] == "work"
        assert [item[0] for item in self.bus.agents["b"]["inbox"]] == [1]

    def test_a_dead_codex_daemon_takes_its_threads_offline(self) -> None:
        proc: dict[str, str | int] = {
            "comm": "codex",
            "ppid": 1,
            "pgrp": 1,
            "tty": 0,
            "foreground": 0,
            "start": 7,
        }
        with patch.object(server, "process_info", return_value=proc):
            for sid in ("t1", "t2"):
                self.hook(
                    sid,
                    kind="codex",
                    host=server.LOCAL_HOST,
                    pid=900,
                    daemon=True,
                    names=[],
                )
        with patch.object(server, "process_info", return_value=None):
            self.bus.forget_old()
        assert {a["state"] for a in self.bus.agents.values()} == {"offline"}

    def test_unavailable_process_information_is_retried_without_disabling_wakes(
        self,
    ) -> None:
        self.hook(
            "alive",
            kind="codex",
            host=server.LOCAL_HOST,
            pid=os.getpid(),
            daemon=True,
            names=["alive"],
        )
        self.send("alive")
        with patch.object(server, "process_info", return_value={"start": 1}):
            self.hook("gone", host=server.LOCAL_HOST, pid=99_999_999, names=["gone"])
        read: Callable[..., str] = Path.read_text
        for error in (
            PermissionError("unavailable"),
            OSError("temporary I/O failure"),
            ValueError("partial stat"),
        ):

            def unavailable(
                path: Path, *args: Any, error: Exception = error, **kwargs: Any
            ) -> str:
                if str(path) == f"/proc/{os.getpid()}/stat":
                    raise error
                return read(path, *args, **kwargs)

            with patch.object(Path, "read_text", unavailable):
                self.bus.forget_old()
            assert self.bus.agents["alive"]["state"] == "idle"
            assert self.bus.agents["gone"]["state"] == "offline", (
                "other agents are still checked"
            )
        self.bus.forget_old()
        with patch.object(server, "codex_turn", return_value="started") as wake:
            self.bus.wake_pass()
        wake.assert_called_once()
        assert self.bus.agents["alive"]["inbox"] == [[1, "notify", True]]

    def test_unknown_replacement_process_does_not_inherit_verified_identity(
        self,
    ) -> None:
        for pid in (
            100,
            200,
        ):  # a reused PID and a different PID both need a fresh verification
            sid: str = f"thread-{pid}"
            with patch.object(server, "process_info", return_value={"start": 50}):
                self.hook(
                    sid,
                    host=server.LOCAL_HOST,
                    pid=100,
                    names=[sid],
                    process_token=f"{server.LOCAL_HOST}:100:50",
                )
            with patch.object(server, "process_info", return_value=None):
                self.hook(
                    sid,
                    pid=pid,
                    process_token=f"{server.LOCAL_HOST}:{pid}:60",
                    host=server.LOCAL_HOST,
                )
            assert self.bus.agents[sid]["pid_start"] is None
            with patch.object(server, "process_info", return_value={"start": 60}):
                self.hook(
                    sid,
                    pid=pid,
                    process_token=f"{server.LOCAL_HOST}:{pid}:60",
                    host=server.LOCAL_HOST,
                )
            assert self.bus.agents[sid]["pid_start"] == 60

    def test_daemon_restart_rejects_late_hooks_and_keeps_pending_mail_wakeable(
        self,
    ) -> None:
        # Include records saved before daemon hooks supplied process_token, and a missed SessionStart.
        for n, event in enumerate(("SessionStart", "Stop")):
            sid, name = f"thread-{n}", f"work-{n}"
            old_token: str = f"{server.LOCAL_HOST}:100:1000:{sid}"
            new_token: str = f"{server.LOCAL_HOST}:200:2000:{sid}"

            def process_info(pid: int) -> dict[str, int]:
                return {"start": pid * 10}

            with patch.object(server, "process_info", side_effect=process_info):
                self.hook(
                    sid,
                    kind="codex",
                    host=server.LOCAL_HOST,
                    daemon=True,
                    pid=100,
                    process_token=old_token if n else "",
                    names=[name],
                )
                self.hook(
                    sid,
                    event,
                    kind="codex",
                    host=server.LOCAL_HOST,
                    daemon=True,
                    pid=200,
                    process_token=new_token,
                )
            self.send(name)
        self.bus.store.close()
        self.bus = server.Bus(self.root / "state.sqlite3")
        for sid in ("thread-0", "thread-1"):
            for event in ("SessionEnd", "SessionStart", "Stop", "PostToolUse"):
                out: dict[str, Any] = self.hook(
                    sid,
                    event,
                    kind="codex",
                    host=server.LOCAL_HOST,
                    daemon=True,
                    pid=100,
                    process_token=f"{server.LOCAL_HOST}:100:1000:{sid}",
                )
                assert out.get("ignored"), out
            assert self.bus.agents[sid]["pid"] == 200
            assert self.bus.agents[sid]["pid_start"] == 2000
            assert self.bus.agents[sid]["state"] == "idle"
        with patch.object(server, "codex_turn", return_value="started") as wake:
            self.bus.wake_pass()
        assert {call.args[0] for call in wake.call_args_list} == {
            "thread-0",
            "thread-1",
        }
        assert [len(a["inbox"]) for a in self.bus.agents.values()] == [1, 1]

    def test_new_thread_after_daemon_exit_does_not_take_the_resuming_threads_mail(
        self,
    ) -> None:
        def start(sid: str, pid: int) -> dict[str, Any]:
            with patch.object(server, "process_info", return_value={"start": pid * 10}):
                return self.hook(
                    sid,
                    kind="codex",
                    host=server.LOCAL_HOST,
                    daemon=True,
                    names=[],
                    cwd="/repo",
                    pid=pid,
                    process_token=f"{server.LOCAL_HOST}:{pid}:{pid * 10}:{sid}",
                    receipt=True,
                )

        name: Any = start("old-thread", 100)["name"]
        self.send(name, "work for the old conversation")
        with patch.object(server, "process_info", return_value=None):
            self.bus.forget_old()
        assert self.bus.agents["old-thread"]["state"] == "offline"
        self.bus.store.close()
        self.bus = server.Bus(self.root / "state.sqlite3")

        newcomer: dict[str, Any] = start("new-thread", 200)
        assert newcomer["name"] != name
        assert self.bus.agents["new-thread"]["inbox"] == []
        assert self.bus.agents["old-thread"]["inbox"] == [[1, "notify", True]]
        assert "old-thread" not in self.bus.retired
        assert start("old-thread", 200)["name"] == name
        with patch.object(server, "codex_turn", return_value="started") as wake:
            self.bus.wake_pass()
        assert [call.args[0] for call in wake.call_args_list] == ["old-thread"]
        out: dict[str, Any] = self.hook("old-thread", "PostToolUse", receipt=True)
        assert "work for the old conversation" in out["context"]
        assert out["delivery"] == {"sid": "old-thread", "ids": [1]}
        self.bus.acknowledge("old-thread", "", {"ids": [1]})
        assert self.bus.agents["old-thread"]["inbox"] == []

    def test_automatic_name_takeover_requires_neither_session_to_be_a_daemon_thread(
        self, subtests: pytest.Subtests
    ) -> None:
        for old_daemon, new_daemon in ((True, True), (False, True), (True, False)):
            with subtests.test(old_daemon=old_daemon, new_daemon=new_daemon):
                tag: str = f"{old_daemon}-{new_daemon}"
                old, new, name = f"old-{tag}", f"new-{tag}", f"worker-{tag}"
                self.hook(old, kind="codex", daemon=old_daemon, names=[name])
                self.send(name)
                self.hook(old, "SessionEnd")
                self.hook(new, kind="codex", daemon=new_daemon, names=[name])
                assert self.bus.agents[old]["name"] == name
                assert self.bus.agents[new]["name"] != name
                assert self.bus.agents[new]["inbox"] == []
                pending: list[Any] = list(self.bus.agents[old]["inbox"])
                self.bus.rename(
                    new, "", {"name": name}
                )  # deliberate handoff still works
                assert old not in self.bus.agents
                assert self.bus.agents[new]["name"] == name
                assert self.bus.agents[new]["inbox"] == pending

    def test_shared_daemon_token_does_not_transfer_names_mail_or_resume_rights(
        self,
    ) -> None:
        identity: dict[str, Any] = {
            "kind": "codex",
            "daemon": True,
            "pid": 100,
            "process_token": "remote:100:1000",
            "names": ["work"],
        }
        self.hook("a", **identity)
        self.send("work")
        self.hook("b", **identity)
        assert self.bus.agents["a"]["name"] == "work"
        assert self.bus.agents["a"]["inbox"] == [[1, "notify", True]]
        assert self.bus.agents["b"]["inbox"] == []
        self.hook("a", "SessionEnd", **identity)
        resumed_identity: dict[str, Any] = identity | {
            "pid": 200,
            "process_token": "remote:200:2000",
        }
        self.hook("c", **resumed_identity)
        assert self.bus.agents["a"]["inbox"] == [[1, "notify", True]]
        assert self.bus.agents["c"]["inbox"] == []
        self.bus.rename(
            "c", "", {"name": "work"}
        )  # retire the old session only through explicit handoff
        assert self.hook("a", **identity)["ignored"], (
            "another daemon thread cannot authorize this old hook"
        )
        assert set(self.bus.agents) == {"b", "c"}
        assert self.bus.agents["c"]["inbox"] == [[1, "notify", True]]

    @pytest.mark.parametrize("reload", [False, True])
    def test_legacy_terminal_record_cannot_authorize_a_retired_daemon_hook(
        self, reload: bool
    ) -> None:
        identity: dict[str, Any] = {
            "kind": "codex",
            "pid": 100,
            "process_token": "remote:100:1000",
            "receipt": True,
        }
        self.hook("old", **identity, daemon=True, names=["work"])
        self.send("work", "pending work")
        # Older records may lack the daemon flag and use the same unsuffixed token.
        self.hook("legacy", **identity, names=["other"])
        self.hook("old", "SessionEnd", **identity, daemon=True)
        self.hook("replacement", names=["replacement"], receipt=True)
        self.bus.rename("replacement", "", {"name": "work"})
        if reload:
            self.bus.store.close()
            self.bus = server.Bus(self.root / "state.sqlite3")
        for event in ("SessionStart", "PostToolUse", "Stop", "SessionEnd"):
            assert self.hook("old", event, **identity, daemon=True)["ignored"]
        assert set(self.bus.agents) == {"legacy", "replacement"}
        assert self.bus.left["old"] == [identity["process_token"]]
        assert self.bus.agents["replacement"]["inbox"] == [[1, "notify", True]]
        assert self.bus.messages[0]["text"] == "pending work"

    def test_same_pane_handoff_and_resume_preserve_mail_across_restart(self) -> None:
        seat: dict[str, Any] = {
            "tmux": "/sock:%0",
            "tmux_server": "111",
            "receipt": True,
        }
        self.hook("a", **seat, names=["original"], process_token="remote:10:1")
        self.send("original", "already read")
        self.bus.acknowledge("a", "", {"ids": [1]})
        self.bus.rename("a", "", {"name": "chosen"})
        self.send("chosen", "pending before handoff")
        self.hook("b", **seat, names=["different"], process_token="remote:20:1")
        assert set(self.bus.agents) == {"b"}
        assert self.bus.agents["b"]["name"] == "chosen"
        assert self.bus.agents["b"]["inbox"] == [[2, "notify", True]]
        self.send("original", "sent to the old name")
        history: list[Any] = list(self.bus.messages)
        self.bus.store.close()
        self.bus = server.Bus(self.root / "state.sqlite3")
        for event in ("SessionStart", "PostToolUse", "SessionEnd"):
            assert self.hook("a", event, **seat, process_token="remote:10:1")["ignored"]
        # Deliberately resume A inside the process currently running B.
        out: dict[str, Any] = self.hook(
            "a", **seat, names=["original"], process_token="remote:20:1"
        )
        assert "ignored" not in out
        assert set(self.bus.agents) == {"a"}
        assert self.bus.agents["a"]["name"] == "chosen"
        assert "original" in self.bus.agents["a"]["aka"]
        assert self.bus.agents["a"]["inbox"] == [
            [2, "notify", True],
            [3, "notify", True],
        ]
        assert self.bus.messages == history
        self.bus.acknowledge("a", "", {"ids": [2, 3]})
        assert self.bus.agents["a"]["inbox"] == []
        assert self.bus.messages == history

    @pytest.mark.parametrize("leave_default", [False, True])
    def test_same_pane_preserves_projects_and_unread_board_mail(
        self, leave_default: bool
    ) -> None:
        seat: dict[str, Any] = {
            "tmux": "/sock:%0",
            "tmux_server": "111",
            "repo": "repo",
            "receipt": True,
        }
        self.hook("a", **seat, names=["work"], process_token="remote:10:1")
        self.bus.projects("a", "", {"action": "join", "project": "/team"})
        self.bus.projects(
            "a",
            "",
            {"action": "leave" if leave_default else "unsubscribe", "project": "/repo"},
        )
        self.bus.update("a", "", {"about": "review release"})
        self.bus.send(
            "human", {"to": ["/team"], "text": "pending board", "mode": "board"}
        )
        projects: dict[str, bool] = dict(self.bus.agents["a"]["projects"])
        self.hook("b", **seat, names=["different"], process_token="remote:20:1")
        self.bus.store.close()
        self.bus = server.Bus(self.root / "state.sqlite3")
        assert self.bus.agents["b"]["projects"] == projects
        assert self.bus.agents["b"]["default_project"] == "/repo"
        assert self.bus.agents["b"]["about"] == "review release"
        out: dict[str, Any] = self.hook(
            "b", "PostToolUse", **seat, process_token="remote:20:1"
        )
        assert "1 new board post in /team" in out["context"]
        assert out["delivery"] == {"sid": "b", "ids": [1]}
        assert self.bus.agents["b"]["inbox"] == [[1, "board", False]]
        self.bus.acknowledge("b", "", {"ids": [1]})
        assert self.bus.agents["b"]["inbox"] == []
        assert self.bus.messages[0]["text"] == "pending board"

    @pytest.mark.parametrize("event", ["SessionStart", "PostToolUse"])
    def test_known_session_resuming_in_occupied_pane_merges_both_agents(
        self, event: str
    ) -> None:
        common: dict[str, Any] = {"tmux_server": "111", "receipt": True}
        self.hook(
            "a", **common, tmux="/sock:%1", names=["alpha"], process_token="remote:10:1"
        )
        self.bus.projects("a", "", {"action": "join", "project": "/alpha"})
        self.bus.projects("a", "", {"action": "join", "project": "/shared"})
        self.send("alpha", "mail for A")
        self.hook(
            "a", "SessionEnd", **common, tmux="/sock:%1", process_token="remote:10:1"
        )
        self.hook(
            "b", **common, tmux="/sock:%2", names=["beta"], process_token="remote:20:1"
        )
        self.bus.rename("b", "", {"name": "chosen"})
        self.bus.projects("b", "", {"action": "join", "project": "/shared"})
        self.bus.projects("b", "", {"action": "unsubscribe", "project": "/shared"})
        self.bus.projects("b", "", {"action": "join", "project": "/beta"})
        self.bus.update("b", "", {"about": "pane's task"})
        self.send("chosen", "mail for B")
        self.bus.store.close()
        self.bus = server.Bus(self.root / "state.sqlite3")
        self.hook("a", event, **common, tmux="/sock:%2", process_token="remote:30:1")
        assert set(self.bus.agents) == {"a"}
        a: dict[str, Any] = self.bus.agents["a"]
        assert a["name"] == "chosen"
        assert set(a["aka"]) == {"alpha", "beta"}
        assert a["projects"] == {"/alpha": True, "/shared": False, "/beta": True}
        assert a["about"] == "pane's task"
        assert a["inbox"] == [[1, "notify", True], [2, "notify", True]]
        for late_event in ("SessionStart", "PostToolUse", "Stop", "SessionEnd"):
            assert self.hook(
                "b", late_event, **common, tmux="/sock:%2", process_token="remote:20:1"
            )["ignored"]
        self.send("beta", "new mail to an old name")
        assert a["inbox"][-1] == [3, "notify", True]
        self.bus.store.close()
        self.bus = server.Bus(self.root / "state.sqlite3")
        assert self.bus.agents["a"]["inbox"] == a["inbox"]
        assert len(self.bus.messages) == 3

    def test_resumed_pane_handoff_rolls_back_if_saving_fails(self) -> None:
        self.hook("a", names=["alpha"], tmux="/sock:%1", process_token="remote:10:1")
        self.hook("b", names=["beta"], tmux="/sock:%2", process_token="remote:20:1")
        self.bus.rename("b", "", {"name": "chosen"})
        self.bus.projects("b", "", {"action": "join", "project": "/team"})
        self.bus.update("b", "", {"about": "preserve this task"})
        self.send("chosen")
        before: dict[str, Any] = self.bus.snapshot()
        with (
            patch.object(self.bus.store, "commit", side_effect=OSError("disk full")),
            pytest.raises(OSError, match="disk full"),
        ):
            self.hook(
                "a", names=["alpha"], tmux="/sock:%2", process_token="remote:30:1"
            )
        assert self.bus.agents == before["agents"]
        assert self.bus.retired == before["retired"]
        assert self.bus.left == before["left"]
        self.bus.store.close()
        self.bus = server.Bus(self.root / "state.sqlite3")
        assert self.bus.agents["a"]["name"] == "alpha"
        assert self.bus.agents["b"]["name"] == "chosen"
        assert self.bus.agents["b"]["inbox"] == [[1, "notify", True]]
        assert self.bus.agents["b"]["projects"] == {"/team": True}

    @pytest.mark.parametrize("kind", ["nested", "daemon"])
    def test_nonterminal_session_does_not_inherit_a_pane(self, kind: str) -> None:
        self.hook("a", names=["alpha"], tmux="/sock:%1")
        self.bus.projects("a", "", {"action": "join", "project": "/team"})
        self.send("alpha")
        self.hook(
            "b",
            names=["alpha"],
            tmux="/sock:%1",
            nested=kind == "nested",
            daemon=kind == "daemon",
        )
        assert set(self.bus.agents) == {"a", "b"}
        assert self.bus.agents["a"]["inbox"] == [[1, "notify", True]]
        assert self.bus.agents["b"]["inbox"] == []
        assert self.bus.agents["b"]["projects"] == {}

    def test_a_rebooted_machine_takes_its_earlier_agents_offline(self) -> None:
        self.hook("a", names=["alpha"], boot="boot-1")
        self.hook("x", names=["other-host"], host="elsewhere", boot="boot-9")
        self.hook("b", names=["alpha"], boot="boot-2")  # same host, after a reboot
        assert self.bus.agents["b"]["name"] == "alpha", "the name is free again"
        self.hook("c", names=["gamma"], boot="boot-2")
        assert "a" not in self.bus.agents
        assert self.bus.agents["x"]["state"] == "idle", "other machines are unaffected"

    def test_a_new_session_in_the_same_process_replaces_the_old_one(self) -> None:
        """Claude's /clear outside tmux whose SessionEnd was lost: one process runs one session."""
        self.hook("a", names=["claude-v"], process_token="remote:10:1")
        self.send("claude-v")
        self.hook("b", names=["claude-v"], process_token="remote:10:1")
        assert self.bus.agents["b"]["name"] == "claude-v"
        assert [item[0] for item in self.bus.agents["b"]["inbox"]] == [1]

    def test_a_restarted_tmux_server_reuses_pane_ids_but_not_seats(self) -> None:
        """%0 after `tmux kill-server` is a different pane: no takeover of its chosen name or its mail."""
        self.hook("a", names=["alpha"], tmux="/sock:%0", tmux_server="111")
        self.bus.rename("a", "", {"name": "alice-chosen"})
        self.hook("a", "SessionEnd")
        self.send("alice-chosen")
        self.hook("b", names=["beta"], tmux="/sock:%0", tmux_server="222")
        assert self.bus.agents["b"]["name"] == "beta"
        assert self.bus.agents["b"]["inbox"] == []
        self.hook(
            "c", names=["gamma"], tmux="/sock:%0", tmux_server="222"
        )  # a real restart in that pane
        assert "b" not in self.bus.agents, "replaced by the newer session in its pane"

    def test_names_that_dont_fit_are_skipped_not_cut(self) -> None:
        """Cut names of different agents collide, and the newcomer took the other's mail."""
        self.hook("a", cwd="/tmp/experiment-runner-config-1", names=[])
        self.hook("a", "SessionEnd")
        self.send(self.bus.agents["a"]["name"])
        self.hook("b", cwd="/tmp/experiment-runner-config-2", names=["x" * 41])
        assert self.bus.agents["b"]["name"] != self.bus.agents["a"]["name"]
        assert self.bus.agents["b"]["inbox"] == []
        assert self.bus.agents["b"]["name"].endswith("config-2"), self.bus.agents["b"][
            "name"
        ]

    def test_long_automatic_names_survive_collisions_and_receive_separate_mail(
        self,
    ) -> None:
        for n in range(1, 13):  # the suffix grows from one digit to two
            sid: str = str(n)
            out: dict[str, Any] = self.hook(
                sid,
                kind="codex",
                daemon=True,
                names=[],
                cwd="/repo/release-review",
                process_token="remote:100:1000",
            )
            assert re.search(server.NAME_RE, out["name"])
            self.send(out["name"], f"task for thread {n}")
        assert len(self.bus.agents) == 12
        assert len({a["name"] for a in self.bus.agents.values()}) == 12
        assert self.bus.agents["12"]["name"].endswith("-12")
        for n in range(1, 13):
            out = self.hook(str(n), "Stop")
            assert f"task for thread {n}" in out["block"]
            assert self.bus.agents[str(n)]["inbox"] == []

    def test_superseded_session_records_are_kept(self) -> None:
        self.hook("a", names=["alpha"], tmux="/sock:%0")
        self.hook("b", names=["alpha"], tmux="/sock:%0")
        assert "a" in self.bus.retired
        self.bus.left["gone-long-ago"] = ["remote:1:1"]
        self.bus.forget_old()
        assert "a" in self.bus.retired, "recent: kept"
        assert "gone-long-ago" in self.bus.left
        self.bus.retired["a"]["at"] -= 30 * 86400
        self.bus.forget_old()
        assert "a" in self.bus.retired

    # --- input that used to stop every wake-up ---

    def test_a_nul_byte_in_a_hook_field_is_refused(
        self, subtests: pytest.Subtests
    ) -> None:
        for field in ("transcript", "tmux", "cwd"):
            with subtests.test(field=field), pytest.raises(server.HttpError) as caught:
                self.hook("a", **{field: "/x\x00y:%1"})
            assert caught.value.status == 400
        assert self.bus.agents == {}

    def test_one_failing_watch_step_doesnt_stop_the_others(self) -> None:
        ran: list[str] = []

        def step(name: str, fail: bool = False) -> Any:
            def run() -> None:
                ran.append(name)
                if fail:
                    raise ValueError("embedded null byte")

            run.__name__ = name
            return run

        class Stop(Exception):
            pass

        sleeps: collections.abc.Iterator[None | Stop] = iter([None, Stop()])

        def sleep(_: float) -> None:
            if isinstance(nxt := next(sleeps), Exception):
                raise nxt

        with (
            patch.object(self.bus, "forget_old", step("forget_old")),
            patch.object(self.bus, "turn_pass", step("turn_pass", True)),
            patch.object(self.bus, "wake_pass", step("wake_pass")),
            patch.object(server.time, "sleep", sleep),
            patch("builtins.print"),
            pytest.raises(Stop),
        ):
            server.watch_agents(self.bus)
        assert ran == ["forget_old", "turn_pass", "wake_pass"]

    # --- Codex daemon errors ---

    def daemon_thread(self, sid: str, name: str) -> None:
        self.hook(sid, kind="codex", host=server.LOCAL_HOST, daemon=True, names=[name])
        self.send(name)

    def test_codex_daemon_errors_hold_or_fail_without_stopping_other_wakes(
        self,
    ) -> None:
        self.daemon_thread("t1", "cx-1")
        self.daemon_thread("t2", "cx-2")
        calls: list[str] = []

        def refused(thread: str, text: str, **_: Any) -> str:
            calls.append(thread)
            raise wake.CodexRefused("codex turn/start: thread is busy")

        with patch.object(server, "codex_turn", refused):
            self.bus.wake_pass()
        assert sorted(calls) == ["t1", "t2"]
        assert {a["wake"]["status"] for a in self.bus.agents.values()} == {"held"}, (
            "an error reply: it is up"
        )

        calls.clear()

        def broken(thread: str, text: str, **_: Any) -> str:
            calls.append(thread)
            if thread == "t1":
                raise KeyError("status")
            return "started"

        with (
            patch.object(server, "codex_turn", broken),
            patch.object(server, "WAKE_RETRY_S", 0),
        ):
            self.bus.wake_pass()
        assert self.bus.agents["t1"]["wake"]["status"] == "failed"
        assert self.bus.agents["t2"]["wake"]["status"] == "typed", (
            "one odd reply doesn't stop the next wake"
        )

    @pytest.mark.parametrize("hung", [False, True])
    def test_an_unreachable_codex_daemon_stops_queued_calls_and_is_waited_for(
        self, hung: bool
    ) -> None:
        for i in range(12):
            self.hook(
                f"t{i}",
                kind="codex",
                host=server.LOCAL_HOST,
                daemon=True,
                names=[f"cx-{i}"],
            )
        self.bus.send("human", {"to": ["*"], "text": "task"})
        calls: list[str] = []
        waiting: threading.Barrier = threading.Barrier(4, timeout=5)

        def down(thread: str, text: str, **_: Any) -> str:
            calls.append(thread)
            if hung:
                waiting.wait()  # all four requests are in flight before the first timeout
            raise ConnectionRefusedError("no daemon")

        with (
            patch.object(server, "codex_turn", down),
            patch.object(server, "WAKE_RETRY_S", 0),
            patch.object(server, "WAKE_PARALLEL", 4),
        ):
            self.bus.wake_pass()
            assert 1 <= len(calls) <= 4
            if hung:
                assert len(calls) == 4
            assert {a["wake"]["status"] for a in self.bus.agents.values()} == {
                "held"
            }, "Codex may be restarting"
            for a in self.bus.agents.values():
                a["wake"]["down_since"] -= server.CODEX_DOWN_S + 1
            calls.clear()
            self.bus.wake_pass()
            assert 1 <= len(calls) <= 4
        assert {a["wake"]["status"] for a in self.bus.agents.values()} == {"failed"}

    @pytest.mark.parametrize(
        "kinds", [("codex",), ("claude",), ("codex", "claude"), ("claude", "codex")]
    )
    def test_wakes_share_a_bounded_pool_without_one_kind_blocking_the_other(
        self, kinds: tuple[str, ...]
    ) -> None:
        for kind in kinds:
            for i in range(6):
                name: str = f"{kind}-{i}"
                self.hook(
                    name,
                    kind=kind,
                    host=server.LOCAL_HOST,
                    names=[name],
                    daemon=kind == "codex",
                    tmux="" if kind == "codex" else f"/sock:%{i}",
                )
        self.bus.send("human", {"to": ["*"], "text": "task"})
        ready, release = threading.Event(), threading.Event()
        lock: threading.Lock = threading.Lock()
        started: list[str] = []
        active: int = 0
        peak: int = 0

        def attempt(name: str) -> None:
            nonlocal active, peak
            with lock:
                started.append(name)
                active += 1
                peak = max(peak, active)
                if active == 4:
                    ready.set()
            try:
                assert release.wait(5), "test did not release wake attempts"
            finally:
                with lock:
                    active -= 1

        def native(thread: str, text: str, *, before_start: Callable[[], None]) -> str:
            before_start()
            attempt(thread)
            return "started"

        def terminal(name: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
            attempt(name)
            return {"status": "typed", "at": time.time(), "tries": 1}

        with (
            patch.object(server, "WAKE_PARALLEL", 4),
            patch.object(server, "codex_turn", native),
            patch.object(self.bus, "try_wake", terminal),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            run = pool.submit(self.bus.wake_pass)
            try:
                assert ready.wait(5), "wake attempts did not run in parallel"
                assert len(started) == 4
                if len(kinds) == 2:
                    assert sum(n.startswith("codex-") for n in started) == 2
            finally:
                release.set()
            run.result(timeout=10)
        assert peak == 4
        assert sorted(started) == sorted(self.bus.agents)
        assert {a["wake"]["status"] for a in self.bus.agents.values()} == {"typed"}

    # --- busy/idle, inbox, history ---

    def test_a_local_slash_command_doesnt_mark_claude_busy(self) -> None:
        transcript: Path = self.root / "t.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "user",
                    "timestamp": "2026-10-23T10:00:00Z",
                    "message": {
                        "role": "user",
                        "content": "<command-name>/model</command-name>\n<command-message>model</command-message>\n"
                        "<command-args></command-args>",
                    },
                }
            )
            + "\n"
        )
        assert wake.turn_marker(transcript, 0)[1] is None

    def test_intro_counts_board_posts_received_under_an_earlier_name(self) -> None:
        self.hook("a", names=["alice"], repo="p")
        self.bus.send("human", {"to": ["/p"], "text": "board news", "mode": "board"})
        self.bus.rename("a", "", {"name": "bob"})
        assert "/p: 1 recent board posts" in self.bus.intro(self.bus.agents["a"])

    def test_inbox_wait_isnt_ended_or_consumed_by_a_board_post(self) -> None:
        self.hook("a", names=["alice"], repo="p")
        self.bus.send("human", {"to": ["/p"], "text": "board news", "mode": "board"})
        started: float = time.monotonic()
        assert self.bus.inbox(self.bus.agents["a"], 0.3) == []
        assert time.monotonic() - started >= 0.3
        assert len(self.bus.agents["a"]["inbox"]) == 1, (
            "the board note stays for the next hook"
        )

    def test_history_follows_names_including_messages_from_before_any_owner_fields(
        self,
    ) -> None:
        self.hook("a", names=["alice"])
        self.send("alice", "new")
        old: dict[str, Any | int | str] = dict(
            self.bus.messages[0], id=99, text="from an old server"
        )
        for k in ("from_owner", "reach_owners"):
            old.pop(k, None)
        self.bus.messages.append(old)
        assert [m["text"] for m in self.bus.log_view({"agent": "alice"})] == [
            "new",
            "from an old server",
        ]

    def test_a_codex_local_shell_call_shows_its_command(self) -> None:
        rec: dict[str, str | dict[str, str | dict[str, str | list[str]]]] = {
            "timestamp": "2026-10-23T10:00:00Z",
            "type": "response_item",
            "payload": {
                "type": "local_shell_call",
                "call_id": "c1",
                "action": {"type": "exec", "command": ["git", "status"]},
            },
        }
        entry: dict[str, Any] = transcripts.condense_codex(rec)[0]
        assert (entry["target"], entry["command"]) == ("git status", "git status")

    def test_the_mail_notice_says_how_to_run_arke(self) -> None:
        self.hook("a", names=["alice"])
        self.send("alice")
        assert (
            f"`arke` = python3 {server.CLIENT}"
            in self.hook("a", "PostToolUse")["context"]
        )


class TestStartup:
    """main() must not quietly start an empty bus next to existing state: new message ids would reuse old ones."""

    @pytest.fixture(autouse=True)
    def setup(self) -> Generator[None]:
        with ExitStack() as self.stack:
            temporary: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory(
                prefix="arke-startup-test-"
            )
            self.stack.callback(temporary.cleanup)
            self.root: Path = Path(temporary.name)
            self.stack.enter_context(
                patch.dict(os.environ, {"HOME": str(self.root)}, clear=True)
            )
            self.stack.enter_context(
                patch.object(server.Path, "home", return_value=self.root)
            )
            self.stack.enter_context(patch.object(server, "SETTINGS", {}))
            self.stack.enter_context(
                patch.object(server, "STATE_DIR", self.root / "clone/.local/server")
            )
            self.stack.enter_context(
                patch.object(
                    server, "LOG_FILE", self.root / "clone/.local/server/log.jsonl"
                )
            )
            yield

    def test_old_agentbus_state_without_a_migration_is_refused(self) -> None:
        (self.root / ".local/state/agentbus").mkdir(parents=True)
        (self.root / ".local/state/agentbus/state.sqlite3").write_text("")
        with pytest.raises(SystemExit) as caught:
            server.refuse_empty_start()
        assert "migration script" in str(caught.value)
        os.environ["ARKE_DIR"] = str(
            self.root / "new-bus"
        )  # an explicit new bus is fine
        server.refuse_empty_start()

    def test_a_log_without_its_database_is_refused(self) -> None:
        server.LOG_FILE.parent.mkdir(parents=True)
        server.LOG_FILE.write_text("{}\n")
        with pytest.raises(SystemExit):
            server.refuse_empty_start()
        (server.STATE_DIR / "state.sqlite3").write_text("")
        with pytest.raises(SystemExit):  # an emptied database next to old history
            server.refuse_empty_start()
        server.Bus(server.STATE_DIR / "state.sqlite3").store.close()
        server.refuse_empty_start()

    def test_old_environment_names_are_refused(self) -> None:
        os.environ["AGENTBUS_NTFY"] = (
            "http://127.0.0.1:2586"  # copied from the old unit: ntfy would silently be off
        )
        with pytest.raises(SystemExit) as caught:
            server.refuse_empty_start()
        assert "AGENTBUS_NTFY" in str(caught.value)

    def test_a_state_json_cannot_be_silently_replaced_by_an_empty_bus(
        self,
    ) -> None:
        original: str = json.dumps({"agents": {}, "messages": [], "next_id": 4413})
        (self.root / "state.json").write_text(original)
        with pytest.raises(ValueError, match="no committed initialization metadata"):
            server.Bus(self.root / "state.sqlite3")
        assert (self.root / "state.json").read_text() == original

    def test_startup_checks_legacy_json_in_the_configured_directory(self) -> None:
        server.STATE_DIR.mkdir(parents=True)
        (server.STATE_DIR / "state.json").write_text('{"next_id": 4413}')
        os.environ["ARKE_DIR"] = str(server.STATE_DIR)
        with pytest.raises(SystemExit, match="state.json"):
            server.refuse_empty_start()

    def test_an_initialized_database_stays_authoritative_over_old_json(
        self,
    ) -> None:
        bus: server.Bus = server.Bus(self.root / "state.sqlite3")
        with bus.change():
            bus.next_id = 4414
            bus.save()
        bus.store.close()
        (self.root / "state.json").write_text(
            '{"agents": {}, "messages": [], "next_id": 4413}'
        )
        restored: server.Bus = server.Bus(self.root / "state.sqlite3")
        self.stack.callback(restored.store.close)
        assert restored.next_id == 4414

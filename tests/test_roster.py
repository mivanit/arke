"""Project discovery through real HTTP and CLI, with isolated state and no harness probes."""

from __future__ import annotations

import copy
import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import client
import pytest
import roster
import server
import wake

from tests.test_delivery import DeliveryFixture


class TestRoster(DeliveryFixture):
    def add(
        self, name: str, projects: dict[str, bool], *, ended: bool = False
    ) -> server.Agent:
        self.bus.hook(
            {
                "event": "SessionStart",
                "session_id": name,
                "names": [name],
                "host": "remote",
            }
        )
        self.bus.agents[name]["projects"] = projects
        if ended:
            self.bus.hook({"event": "SessionEnd", "session_id": name})
        return self.bus.agents[name]

    def read(self, query: str = "") -> dict[str, Any]:
        return client.call("GET", "/roster" + query)

    def names(self, query: str = "") -> set[str]:
        return {a["name"] for a in self.read(query)["agents"]}

    def test_membership_scope_and_expansion(self) -> None:
        self.bus.agents["a"]["projects"] = {"/app/workers": True, "/extra": True}
        self.add("manager", {"/app": True})
        self.add("peer", {"/app/workers": False, "/extra": True})
        self.add("child", {"/app/workers/review": True})
        self.add("sibling", {"/app/core": True})
        self.add("unrelated", {"/application": True})
        self.add("ended-peer", {"/app/workers": True}, ended=True)
        self.add("ended-elsewhere", {"/elsewhere": True}, ended=True)
        assert self.names() == {"manager", "peer", "child"}
        assert self.read()["ended"] == 1
        assert self.read()["total"] == 4
        assert self.names("?all=1") == {"manager", "peer", "child", "ended-peer"}
        assert self.names("?project=/app/workers") == {"alice", "peer", "child"}
        assert self.names("?global=1") == {
            "alice",
            "manager",
            "peer",
            "child",
            "sibling",
            "unrelated",
        }
        assert len(self.names("?global=1&all=1")) == 8
        assert self.read("?global=1&all=1")["ended"] == 2
        self.bus.projects("a", "", {"project": "/app/workers", "action": "unsubscribe"})
        assert self.names() == {"manager", "peer", "child"}
        self.bus.agents["a"]["cwd"] = "/different/checkout"
        self.bus.agents["a"]["default_project"] = "/app/workers"
        self.bus.projects("a", "", {"project": "/app/workers", "action": "leave"})
        assert self.names() == {"peer"}
        # The original API keeps its global, all-record behavior.
        assert len(client.call("GET", "/agents")) == 8

    def test_footer_hints_only_at_what_is_not_shown(self) -> None:
        """under --global the footer offered `arke who --global --all` twice."""
        self.bus.agents["a"]["projects"] = {"/p": True}
        footers: dict[str, str] = {
            q: roster.render(self.read(q)).splitlines()[-1]
            for q in ("", "?all=1", "?global=1", "?global=1&all=1")
        }
        assert footers == {
            "": "Run `arke who --all` to include ended agents; `arke who --global --all` for the full registry.",
            "?all=1": "Run `arke who --global --all` for the full registry.",
            "?global=1": "Run `arke who --global --all` to include ended agents.",
            "?global=1&all=1": "0 ended agents (listed). Recently seen does not guarantee availability; "
            "busy/idle is the last reported state.",
        }

    def test_no_memberships_human_and_unknown_identity(self) -> None:
        self.add("bob", {"/p": True})
        assert self.names() == set()
        assert "no project memberships" in roster.render(self.read())
        with (
            patch.object(client, "identity", return_value=("", "")),
            patch.object(client, "ancestors", return_value=[]),
            patch.dict(os.environ, {"ARKE_AS": "human"}),
        ):
            assert self.names() == {"alice", "bob"}
            assert self.read()["scope"] == "global"
        with patch.object(client, "identity", return_value=("unknown", "")):
            with pytest.raises(client.BusError, match="unknown session"):
                self.read()
            assert self.names("?global=1") == {"alice", "bob"}
            assert self.names("?project=/p") == {"bob"}

    @pytest.mark.parametrize(
        "query",
        [
            "?project=/",
            "?project=bad",
            "?project=/p&global=1",
            "?all=yes",
            "?global=false",
        ],
    )
    def test_invalid_scope_is_obvious(self, query: str) -> None:
        with pytest.raises(client.BusError, match="400"):
            self.read(query)

    def test_recency_is_advisory_and_ended_takes_precedence(self) -> None:
        self.bus.agents["a"]["projects"] = {"/p": True}
        peer: server.Agent = self.add("bob", {"/p": True})
        self.bus.send("human", {"to": ["bob"], "text": "keep this mail"})
        before: dict[str, Any] = copy.deepcopy(peer)
        self.bus.agents["bob"]["last_active"] = time.time() - server.STALE_S - 1
        assert self.read()["agents"][0]["presence"] == "unconfirmed"
        assert peer["inbox"] == before["inbox"]
        assert peer["state"] == "idle"
        self.bus.send("human", {"to": ["bob"], "text": "still deliver"})
        assert len(peer["inbox"]) == 2
        self.bus.hook({"event": "SessionEnd", "session_id": "bob"})
        assert self.read()["ended"] == 1
        assert self.read("?all=1")["agents"][0]["presence"] == "ended"
        self.bus.hook(
            {"event": "UserPromptSubmit", "session_id": "bob", "receipt": True}
        )
        assert self.read()["agents"][0]["presence"] == "recent"
        assert len(peer["inbox"]) == 2

    def test_roster_updates_are_bounded_quiet_and_never_mail(self) -> None:
        self.bus.agents["a"]["projects"] = {"/p": True}
        start: dict[str, Any] = self.bus.hook(
            {"session_id": "a", "event": "SessionStart"}
        )
        assert "Your project peers: /p" in start["context"]
        peer: server.Agent = self.add("bob", {"/p": True})
        with patch.object(server, "ROSTER_COOLDOWN_S", 0):
            notice: dict[str, Any] = self.bus.hook(
                {"session_id": "a", "event": "PostToolUse", "receipt": True}
            )
            assert "bob" in notice["context"]
            assert "delivery" not in notice
            assert not self.bus.agents["a"]["inbox"]
            peer.update(state="busy", last_active=time.time() - server.STALE_S - 10)
            self.add("elsewhere", {"/elsewhere": True})
            quiet: dict[str, Any] = self.bus.hook(
                {"session_id": "a", "event": "PostToolUse"}
            )
            assert not quiet["context"]
            self.bus.hook({"session_id": "bob", "event": "SessionEnd"})
            for event in ("Stop", "SessionEnd"):
                stopped: dict[str, Any] = self.bus.hook(
                    {"session_id": "a", "event": event}
                )
                assert "block" not in stopped and "context" not in stopped
            ended: dict[str, Any] = self.bus.hook(
                {"session_id": "a", "event": "PostToolUse"}
            )
            assert (
                "1 ended agents" in ended["context"] and "bob" not in ended["context"]
            )
            # A quiet peer's changing age is visible at the next prompt without chatter on every tool.
            self.bus.hook({"session_id": "bob", "event": "Stop"})
            peer["last_active"] = time.time() - server.STALE_S - 10
            prompt: dict[str, Any] = self.bus.hook(
                {"session_id": "a", "event": "UserPromptSubmit"}
            )
            assert "unconfirmed" in prompt["context"]

    def test_cooldown_coalesces_changes_and_next_prompt_recovers_lost_notice(
        self,
    ) -> None:
        self.bus.agents["a"]["projects"] = {"/p": True}
        with patch.object(server.time, "monotonic", return_value=100):
            self.bus.hook({"session_id": "a", "event": "SessionStart"})
            self.add("bob", {"/p": True})
            out: dict[str, Any] = self.bus.hook(
                {"session_id": "a", "event": "PostToolUse"}
            )
            assert not out["context"]
            self.add("charlie", {"/p": True})
        with patch.object(
            server.time, "monotonic", return_value=100 + server.ROSTER_COOLDOWN_S
        ):
            out = self.bus.hook({"session_id": "a", "event": "PostToolUse"})
            assert "bob" in out["context"] and "charlie" in out["context"]
            # Discard that response: the next prompt still gets a current snapshot.
            out = self.bus.hook({"session_id": "a", "event": "UserPromptSubmit"})
            assert "bob" in out["context"] and "charlie" in out["context"]

    def test_large_roster_never_crowds_out_mail_or_probes_harnesses(self) -> None:
        self.bus.agents["a"]["projects"] = {"/p": True}
        for number in range(60):
            peer: server.Agent = self.add(f"peer-{number:03}", {"/p": True})
            peer.update(
                about="x" * 200,
                host=server.LOCAL_HOST,
                daemon=True,
                pid=123,
                pid_start=456,
            )
        text: str = "mail start " + "y" * 10000 + " mail end"
        self.bus.send("human", {"to": ["alice"], "text": text})
        with (
            patch.object(
                server, "codex_turn", side_effect=AssertionError("no Codex probe")
            ),
            patch.object(
                server, "process_info", side_effect=AssertionError("no process probe")
            ),
            patch.object(
                server, "tmux_run", side_effect=AssertionError("no tmux probe")
            ),
            patch.object(
                wake, "process_info", side_effect=AssertionError("no process probe")
            ),
            patch.object(wake, "tmux_run", side_effect=AssertionError("no tmux probe")),
        ):
            assert len(self.read()["agents"]) == 60
            out: dict[str, Any] = self.bus.hook(
                {"session_id": "a", "event": "UserPromptSubmit", "receipt": True}
            )
            # mail is cut to fit the harness's hook limit, with a pointer, and the roster waits
            assert text[:1000] in out["context"]
            assert "(cut; full text: `arke log human`)" in out["context"]
            assert "Your project peers:" not in out["context"]
            assert out["delivery"]["ids"] == [1]
            assert len(self.bus.agents["a"]["inbox"]) == 1
            self.bus.acknowledge("a", "", {"ids": [1]})
            out = self.bus.hook({"session_id": "a", "event": "UserPromptSubmit"})
        assert "more agents" in out["context"]
        advisory: str = out["context"].split("Your project peers:", 1)[1]
        assert len(advisory) + len("Your project peers:") <= server.ROSTER_CONTEXT_CHARS
        assert len(roster.render(self.read())) > server.ROSTER_CONTEXT_CHARS

    @pytest.mark.skipif(
        not shutil.which("curl"),
        reason="curl is needed for the documented HTTP examples",
    )
    def test_python_script_and_curl_use_the_same_endpoint(self) -> None:
        self.bus.agents["a"]["projects"] = {"/p": True}
        self.add("bob", {"/p": True})
        self.add("ended-peer", {"/p": True}, ended=True)
        self.add("elsewhere", {"/elsewhere": True})
        url: str = f"http://127.0.0.1:{self.http.server_port}"
        env: dict[str, str] = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("ARKE_", "AGENTBUS_"))
            and key
            not in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "TMUX", "TMUX_PANE")
        } | {
            "ARKE_CONFIG": os.environ["ARKE_CONFIG"],
            "ARKE_URL": url,
            "ARKE_AS": "alice",
            # Test identities for this private listener, regardless of the harness running pytest.
            "CODEX_THREAD_ID": "a",
            "CLAUDE_CODE_SESSION_ID": "a",
        }
        for flags, query in (
            ([], ""),
            (["--all"], "?all=1"),
            (["--global"], "?global=1"),
            (["--global", "--all"], "?global=1&all=1"),
            (["/p", "--all"], "?project=/p&all=1"),
        ):
            python: subprocess.CompletedProcess[str] = subprocess.run(
                [sys.executable, str(Path(client.__file__)), "who", *flags],
                env=env,
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
            curl: subprocess.CompletedProcess[str] = subprocess.run(
                [
                    "curl",
                    "--fail",
                    "--silent",
                    "--show-error",
                    "--noproxy",
                    "*",
                    "-H",
                    "X-Agent-Session: a",
                    url + "/roster" + query,
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
            assert python.stdout.strip() == roster.render(json.loads(curl.stdout))

    def test_cli_conflicting_or_unknown_arguments_fail(self) -> None:
        for args in (["who", "/p", "--global"], ["who", "--typo"], ["who", "/p", "/q"]):
            with (
                patch.object(sys, "argv", ["arke", *args]),
                patch.object(sys, "stderr", io.StringIO()),
                pytest.raises(SystemExit) as error,
            ):
                client.main()
            assert str(error.value.code).startswith("arke:")

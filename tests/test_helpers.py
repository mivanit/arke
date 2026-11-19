"""Helpers can inspect Arke, but only their registered parent can act on their requests."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import patch

import client
import pytest
import server

from tests.test_delivery import DeliveryFixture


def test_nested_codex_helpers_resolve_to_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client, "STATE", tmp_path)
    monkeypatch.setattr(client, "nearest_session", lambda: "grandchild")
    monkeypatch.setenv("CODEX_THREAD_ID", "grandchild")
    (tmp_path / "thread").mkdir()
    (tmp_path / "thread/grandchild").write_text("child")
    (tmp_path / "thread/child").write_text("parent")
    assert client.identity() == ("parent", "grandchild")
    (tmp_path / "thread/parent").write_text("child")
    with pytest.raises(client.BusError, match="cyclic"):
        client.identity()
    (tmp_path / "thread/parent").write_text("")
    with pytest.raises(client.BusError, match="invalid"):
        client.identity()
    (tmp_path / "thread/parent").unlink()
    (tmp_path / "thread/parent").mkdir()
    with pytest.raises(client.BusError, match="cannot read"):
        client.identity()


def test_claude_helper_marker_is_scoped_to_its_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARKE_PARENT_SESSION", "parent")
    monkeypatch.setenv("ARKE_HELPER_ID", "child")
    monkeypatch.setattr(client, "nearest_session", lambda: "parent")
    assert client.identity() == ("parent", "child")
    monkeypatch.setattr(client, "nearest_session", lambda: "separate-harness")
    assert client.identity() == ("separate-harness", "")


@pytest.mark.parametrize("agent_id", ["child", "teammate@team"])
@pytest.mark.parametrize("tool", ["Bash", "Monitor"])
def test_claude_pretool_marks_only_helper_commands_and_preserves_input(
    tool: str, agent_id: str
) -> None:
    command: str = 'python3 -c \'import os,json; print(json.dumps([os.environ["ARKE_HELPER_ID"], os.environ["ARKE_PARENT_SESSION"]]))\'  # stands in for arke\nexit 7'
    original: dict[str, Any] = {
        "command": command,
        "description": "look at messages",
        **({"timeout": 1000, "run_in_background": False} if tool == "Bash" else {}),
    }
    event: dict[str, Any] = {
        "hook_event_name": "PreToolUse",
        "session_id": "parent",
        "agent_id": agent_id,
        "tool_name": tool,
        "tool_input": original,
    }

    def run(payload: dict[str, Any]) -> str:
        with (
            patch.object(client.sys, "stdin", io.StringIO(json.dumps(payload))),
            patch.object(client.sys, "stdout", io.StringIO()) as output,
            patch.object(client, "call") as call,
        ):
            client.run_hook("claude")
            call.assert_not_called()
            return output.getvalue()

    out: dict[str, Any] = json.loads(run(event))["hookSpecificOutput"]
    assert "permissionDecision" not in out
    updated: dict[str, Any] = out["updatedInput"]
    assert updated | {"command": command} == original
    assert updated["command"].endswith(command)
    result: subprocess.CompletedProcess[str] = subprocess.run(
        ["bash", "-c", updated["command"]],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 7
    assert json.loads(result.stdout) == [agent_id, "parent"]
    assert "ARKE_HELPER_ID" not in os.environ
    assert run(event | {"tool_input": updated}) == ""  # already marked
    assert run(event | {"agent_id": ""}) == ""  # parent
    assert run(event | {"tool_name": "Read"}) == ""
    assert run(event | {"tool_input": {"url": "wss://example.invalid/watch"}}) == ""
    # commands that don't call Arke keep matching the user's permission rules
    for other in ("ls", "git status", "pytest -q tests/", "echo parked markers"):
        assert run(event | {"tool_input": original | {"command": other}}) == "", other
    for calls in (
        "python3 ~/arke/arke/client.py inbox",
        "cd /repo && arke who",
        "python3 client.py send parent hi",
        "python3 ~/old/ab.py send bob hello",
        "cd ~/old && python3 ab.py inbox",
    ):
        assert run(event | {"tool_input": original | {"command": calls}}), calls


@pytest.mark.parametrize(
    ("kind", "event", "output"),
    [
        ("claude", {"agent_id": ""}, ""),  # the main agent's Bash call
        ("claude", {}, "updatedInput"),  # a sub-agent's: marked as a helper
        ("claude", {"hook_event_name": "PostToolUse"}, ""),  # a sub-agent's: dropped
        ("codex", {"hook_event_name": "PostToolUse"}, ""),  # mapped to its parent
    ],
)
def test_hooks_that_skip_the_bus_skip_its_imports(
    kind: str, event: dict[str, Any], output: str, tmp_path: Path
) -> None:
    """Claude runs PreToolUse on every Bash call: answering it must not import the network stack (~100 ms),
    and for Claude, nothing beyond what the answer needs."""
    full: dict[str, Any] = {
        "hook_event_name": "PreToolUse",
        "session_id": "parent",
        "agent_id": "child",
        "tool_name": "Bash",
        "tool_input": {"command": "python3 ~/arke/arke/client.py who"},
    } | event
    result: subprocess.CompletedProcess[str] = subprocess.run(
        [sys.executable, "-X", "importtime", client.__file__, "hook", kind],
        input=json.dumps(full),
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
        env=os.environ
        | {"ARKE_CLIENT_DIR": str(tmp_path), "ARKE_URL": "http://bus.invalid"},
    )
    assert output in result.stdout if output else result.stdout == ""
    # -X importtime prints one line per module: "import time: self | cumulative | name"
    imported: set[str] = {
        line.rsplit("|", 1)[1].strip()
        for line in result.stderr.splitlines()
        if line.startswith("import time:")
    }
    assert not imported & {"http.client", "urllib.request", "urllib.error", "email"}
    if kind == "claude":
        assert not imported & {"settings", "subprocess", "pathlib"}
    else:
        assert (tmp_path / "thread/child").read_text() == "parent"


@pytest.mark.parametrize("args", [["server", "--reset"], ["server", "localhost:8888"]])
def test_helper_cannot_change_saved_server(args: list[str], tmp_path: Path) -> None:
    config: Path = tmp_path / "server-url"
    config.write_text("http://localhost:7777\n")
    with (
        patch.object(client, "identity", return_value=("parent", "child")),
        patch.object(client, "CONFIG", config),
        patch.object(client.sys, "argv", ["arke", *args]),
        pytest.raises(SystemExit, match="sub-agents cannot"),
    ):
        client.main()
    assert config.read_text() == "http://localhost:7777\n"


def test_helper_send_to_old_server_never_falls_back_to_regular_send() -> None:
    error: urllib.error.HTTPError = urllib.error.HTTPError(
        "http://old-server/helper/send",
        404,
        "no route",
        Message(),
        io.BytesIO(b'{"error":"no route POST /helper/send"}'),
    )
    with (
        patch.object(client, "identity", return_value=("a", "child")),
        patch.object(client, "open_bus", side_effect=error) as transport,
        pytest.raises(client.BusError, match="requires an updated Arke server"),
    ):
        client.call("POST", "/send", {"to": ["bob"], "text": "hello"})
    transport.assert_called_once()
    req: urllib.request.Request = transport.call_args.args[0]
    assert req.selector == "/helper/send"
    assert isinstance(req.data, bytes)
    assert json.loads(req.data)["request_id"]


class TestHelperDelivery(DeliveryFixture):
    def raw(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        helper: str = "child",
    ) -> tuple[int, Any]:
        req: urllib.request.Request = urllib.request.Request(
            f"http://127.0.0.1:{self.http.server_port}" + path,
            method=method,
            headers={
                "Content-Type": "application/json",
                "X-Agent-Session": "a",
                "X-Agent-Helper": helper,
            },
            data=json.dumps(body).encode() if body is not None else None,
        )
        try:
            response: Any = urllib.request.urlopen(req, timeout=2)
        except urllib.error.HTTPError as e:
            response = e
        with response:
            status: int | None = response.status
            assert status is not None
            return status, json.load(response)

    @pytest.mark.parametrize(
        "path", ["/inbox", "/inbox?receipt=0", "/%69nbox", "/inbox/"]
    )
    def test_helper_peek_does_not_consume_and_parent_can_ack(self, path: str) -> None:
        message: dict[str, Any] = self.bus.send(
            "human", {"to": ["alice"], "text": "parent must see this"}
        )
        for _ in range(2):
            status, out = self.raw("GET", path)
            assert status == 200
            assert [m["id"] for m in out["messages"]] == [message["id"]]
            assert "delivery" not in out
            assert self.bus.agents["a"]["inbox"]
        with patch.object(client, "identity", return_value=("a", "")):
            client.print_inbox()
        assert not self.bus.agents["a"]["inbox"]

    def test_cli_peek_skips_ack_but_retains_normal_reads(self) -> None:
        self.bus.send("human", {"to": ["alice"], "text": "parent task"})
        with (
            patch.object(client, "identity", return_value=("a", "child")),
            patch.object(client.sys, "stdout", io.StringIO()) as output,
        ):
            client.print_inbox()
            assert "parent task" in output.getvalue()
            assert client.call("GET", "/agents")
            assert client.call("GET", "/log")
            client.acknowledge({"sid": "a", "ids": [1]})
        assert self.bus.agents["a"]["inbox"]

    def test_agent_team_teammate_ids_are_valid_helpers(self) -> None:
        """Claude agent-team teammates are name@team: refusing @ made every arke call of theirs fail."""
        status, out = self.raw("GET", "/inbox", helper="researcher@team")
        assert status == 200, out
        status, out = self.raw(
            "POST",
            "/helper/send",
            {"to": ["bob"], "text": "hi"},
            helper="researcher@team",
        )
        assert status == 200, out
        assert self.bus.messages[-1]["helper"] == "researcher@team"
        for bad in ("a b", "a/b", "x" * 101):
            assert self.raw("GET", "/inbox", helper=bad)[0] == 400

    def test_a_chatty_helper_does_not_use_up_its_parents_sends(self) -> None:
        limit: int = server.LIMITS["dm"][0]
        for i in range(limit):
            self.bus.send("", {"to": ["bob"], "text": f"q{i}"}, sid="a", helper="child")
        with pytest.raises(server.HttpError, match="rate limit") as e:
            self.bus.send(
                "", {"to": ["bob"], "text": "one more"}, sid="a", helper="sibling"
            )
        assert e.value.status == 429
        self.bus.send("", {"to": ["human"], "text": "the parent still sends"}, sid="a")

    @pytest.mark.parametrize(
        "body",
        [
            {"to": ["bob"], "text": "é" * 50_000},
            {"to": ["x" * 500_000], "text": "hello"},
        ],
    )
    def test_helper_size_limit_includes_forwarding_header(
        self, body: dict[str, Any]
    ) -> None:
        before: str = json.dumps(self.bus.agents, sort_keys=True)
        with patch.object(self.bus, "rate_limit") as limited:
            status, out = self.raw("POST", "/helper/send", body)
            assert status == 413, out
            limited.assert_not_called()
        assert not self.bus.messages
        assert self.bus.next_id == 1
        assert json.dumps(self.bus.agents, sort_keys=True) == before

    def test_send_header_also_routes_only_to_parent(self) -> None:
        status, out = self.raw("POST", "/send", {"to": ["*"], "text": "question"})
        assert status == 200
        assert out["forwarded_to_parent"]
        assert out["reach"] == ["alice"]

    @pytest.mark.parametrize(
        "path",
        ["/hook", "/ack", "/name", "/about", "/projects", "/color"],
    )
    def test_helper_writes_rejected_by_client_and_server(self, path: str) -> None:
        before: str = json.dumps(self.bus.agents, sort_keys=True)
        with (
            patch.object(client, "identity", return_value=("a", "child")),
            patch.object(client, "open_bus") as transport,
            pytest.raises(client.BusError, match="read-only Arke access"),
        ):
            client.call("POST", path, {})
        transport.assert_not_called()  # refused by the client itself
        status, out = self.raw("POST", path, {})
        assert status == 403
        assert "read-only" in out["error"]
        assert json.dumps(self.bus.agents, sort_keys=True) == before

    @pytest.mark.parametrize(
        ("to", "mode"),
        [
            (["bob"], "notify"),
            (["*"], "notify"),
            (["/project", "human"], "board"),
            (["unknown"], "notify"),
        ],
    )
    def test_send_delivers_only_to_parent_and_parent_can_forward(
        self, to: list[str], mode: str
    ) -> None:
        self.bus.hook(
            {
                "event": "SessionStart",
                "session_id": "b",
                "names": ["bob"],
                "host": "remote",
            }
        )
        with patch.object(client, "identity", return_value=("a", "child")):
            out: dict[str, Any] = client.call(
                "POST", "/send", {"to": to, "mode": mode, "text": "please review"}
            )
        assert out["forwarded_to_parent"]
        assert out["reach"] == ["alice"]
        assert out["mode"] == "notify"
        assert not self.bus.agents["b"]["inbox"]
        assert set(self.bus.agents) == {"a", "b"}
        m: dict[str, Any] = self.bus.messages[-1]
        assert m["helper"] == "child"
        assert m["requested_to"] == to
        assert m["requested_mode"] == mode
        assert "NOT been contacted" in m["text"]
        assert m["text"].endswith("please review")
        for recipient in to:
            assert recipient in m["text"]
        with patch.object(client, "identity", return_value=("a", "")):
            client.print_inbox()
            client.call("POST", "/send", {"to": ["bob"], "text": "parent approved"})
        assert not self.bus.agents["a"]["inbox"]
        assert self.bus.agents["b"]["inbox"]

    def test_retries_survive_reload_and_helper_request_ids_are_independent(
        self,
    ) -> None:
        body: dict[str, Any] = {"to": ["human"], "text": "hello", "request_id": "same"}
        first: dict[str, Any] = self.bus.send("", body, sid="a", helper="child")
        # Use a second store on the same temporary database to model server reload.
        restored: server.Bus = server.Bus(self.bus.state_file)
        try:
            again: dict[str, Any] = restored.send("", body, sid="a", helper="child")
            assert again["id"] == first["id"]
            assert again["replayed"] and again["forwarded_to_parent"]
            with pytest.raises(server.HttpError, match="different message"):
                restored.send("", body | {"to": ["*"]}, sid="a", helper="child")
            sibling: dict[str, Any] = restored.send("", body, sid="a", helper="sibling")
            parent: dict[str, Any] = restored.send("", body, sid="a")
            assert len({first["id"], sibling["id"], parent["id"]}) == 3
            assert len(restored.agents["a"]["inbox"]) == 2
        finally:
            restored.store.close()

    @pytest.mark.parametrize(
        "command", [["send", "bob", "message"], ["broadcast", "message"]]
    )
    def test_cli_reports_parent_only_delivery(
        self, command: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch.object(client, "identity", return_value=("a", "child")),
            patch.object(client.sys, "argv", ["arke", *command]),
        ):
            client.main()
        assert "request sent only to parent alice" in capsys.readouterr().out

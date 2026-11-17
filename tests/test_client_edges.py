"""Client hook and CLI edge cases: local failures vs an unreachable bus, odd environments and arguments."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from contextlib import ExitStack
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

import client
import pytest


def run(event: dict[str, str], temp: str) -> str:
    out = io.StringIO()
    with (
        patch.object(client, "STATE", Path(temp)),
        patch.object(client, "server", return_value=("http://bus:7777", "t")),
        patch.object(client.sys, "stdin", io.StringIO(json.dumps(event))),
        patch.object(client.sys, "stdout", out),
    ):
        client.run_hook("claude")
    return out.getvalue()


def down_marker(temp: str) -> Path:
    h = client.hashlib.sha256(b"http://bus:7777").hexdigest()[:12]
    return Path(temp) / f"down-until-{h}"


EV = {"hook_event_name": "SessionStart", "session_id": "s"}


# a local failure (no git) still registers, and does not mark the bus down
def test_missing_git_still_registers_and_is_not_down() -> None:
    with tempfile.TemporaryDirectory() as temp:
        real = subprocess.run

        def no_git(cmd: list[str], *a: Any, **kw: Any) -> Any:
            if cmd[0] == "git":
                raise FileNotFoundError(2, "No such file or directory", "git")
            return real(cmd, *a, **kw)

        with (
            patch.object(client.subprocess, "run", side_effect=no_git),
            patch.object(client, "call", return_value={}) as call,
            patch.dict(os.environ, {}, clear=False),
        ):
            run(EV | {"cwd": temp}, temp)
        assert call.called
        assert not down_marker(temp).exists()


def test_missing_git_is_asked_once_per_session() -> None:
    """No git on PATH looked like a timeout: the seat was never cached, so every hook ran git again."""
    with tempfile.TemporaryDirectory() as temp:
        real = subprocess.run
        gits: list[list[str]] = []

        def spy(cmd: list[str], *a: Any, **kw: Any) -> Any:
            if cmd[0] == "git":
                gits.append(cmd)
            return real(cmd, *a, **kw)

        with (
            patch.object(client.subprocess, "run", side_effect=spy),
            patch.object(client, "call", return_value={}) as call,
            patch.dict(os.environ, {"PATH": temp, "TMUX": "", "TMUX_PANE": ""}),
        ):
            for _ in range(2):
                run(EV | {"hook_event_name": "PostToolUse", "cwd": temp}, temp)
        assert call.call_count == 2
        assert len(gits) == 1


def test_a_malformed_usage_record_costs_only_the_numbers() -> None:
    """A null output_tokens raised TypeError on every hook, so the agent never reached the bus again."""
    with tempfile.TemporaryDirectory() as temp:
        tr = Path(temp) / "t.jsonl"
        for record in (
            {
                "type": "assistant",
                "message": {"id": "m", "usage": {"output_tokens": None}},
            },
            {"payload": {"type": "token_count", "info": {"total_token_usage": [1]}}},
        ):
            tr.write_text(json.dumps(record) + "\n")
            with (
                patch.object(client, "STATE", Path(temp)),
                patch.object(client, "call", return_value={}) as call,
                patch.object(client.sys, "stderr", io.StringIO()) as err,
            ):
                client.hook(
                    "claude",
                    {
                        "hook_event_name": "PostToolUse",
                        "session_id": f"s{len(record)}",
                        "cwd": temp,
                        "transcript_path": str(tr),
                    },
                )
            assert call.called
            assert "arke: usage from" in err.getvalue()


def test_unreadable_transcript_still_ends_the_turn() -> None:
    with tempfile.TemporaryDirectory() as temp:
        tr = Path(temp) / "t.jsonl"
        tr.write_text("{}\n")
        with (
            patch.object(client, "call", return_value={}) as call,
            patch.object(Path, "open", side_effect=PermissionError(13, "denied")),
        ):
            run(
                {
                    "hook_event_name": "Stop",
                    "session_id": "s",
                    "cwd": temp,
                    "transcript_path": str(tr),
                },
                temp,
            )
        assert call.called
        assert not down_marker(temp).exists()


# a 4xx is a definite answer, not an outage
def test_http_4xx_does_not_back_off_everyone() -> None:
    with tempfile.TemporaryDirectory() as temp:
        err = urllib.error.HTTPError(
            "http://bus:7777/hook",
            400,
            "bad",
            Message(),
            io.BytesIO(b'{"error":"nope"}'),
        )
        with patch.object(client, "open_bus", side_effect=err):
            out = run(EV | {"cwd": temp}, temp)
        assert "nope" in out
        assert not down_marker(temp).exists()


def test_http_5xx_and_refused_back_off() -> None:
    for exc in (
        urllib.error.HTTPError(
            "http://bus:7777/hook", 503, "x", Message(), io.BytesIO(b'{"error":"busy"}')
        ),
        urllib.error.URLError(ConnectionRefusedError(111, "refused")),
    ):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(client, "open_bus", side_effect=exc):
                run(EV | {"cwd": temp}, temp)
            assert down_marker(temp).exists(), exc


def test_https_without_port_is_443() -> None:
    assert client.normalize("https://bus.example") == "https://bus.example:443"
    assert client.normalize("bus.example").endswith(f":{client.PORT}")


@pytest.mark.parametrize(
    "argv",
    [
        ["send", "bob", "hi", "--request-id"],
        ["send", "bob", "hi", "--request-id", "--board"],
        ["log", "--before"],
        ["log", "bob", "--before"],
    ],
)
def test_valued_flag_without_value_is_refused(argv: list[str]) -> None:
    with (
        patch.object(client.sys, "argv", ["arke", *argv]),
        patch.object(client, "call") as call,
        pytest.raises(SystemExit) as stop,
    ):
        client.main()
    call.assert_not_called()
    assert "needs a value" in str(stop.value.code)


def test_tmux_without_comma_does_not_break_the_hook() -> None:
    with tempfile.TemporaryDirectory() as temp:
        with (
            patch.dict(os.environ, {"TMUX": "weird", "TMUX_PANE": "%1"}),
            patch.object(client, "call", return_value={}) as call,
            patch.object(
                client,
                "seat",
                return_value={
                    "pid": 0,
                    "names": [],
                    "session": "",
                    "repo": "",
                    "nested": False,
                    "daemon": False,
                },
            ),
        ):
            client.hook("claude", EV | {"cwd": temp})
        assert call.call_args.args[2]["tmux"] == "weird:%1"


def test_harness_kind_is_not_a_substring_match() -> None:
    with patch.object(Path, "read_text", return_value="run-claude\n"):
        assert client.harness_kind(5) == ""
    with patch.object(Path, "read_text", return_value="codex-x86_64-un\n"):
        assert client.harness_kind(5) == "codex"


def test_usage_is_sent_on_every_hook_but_session_start_and_end() -> None:
    with tempfile.TemporaryDirectory() as temp:
        tr: Path = Path(temp) / "t.jsonl"
        tr.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"id": "m", "usage": {"output_tokens": 5}},
                }
            )
            + "\n"
        )
        for event, sent in (
            ("SessionStart", False),
            ("PostToolUse", True),
            ("UserPromptSubmit", True),
            ("Stop", True),
            ("SessionEnd", False),
        ):
            with (
                patch.object(client, "STATE", Path(temp)),
                patch.object(client, "call", return_value={}) as call,
            ):
                client.hook(
                    "claude",
                    {
                        "hook_event_name": event,
                        "session_id": "s",
                        "cwd": temp,
                        "transcript_path": str(tr),
                    },
                )
            assert (call.call_args.args[2].get("tokens") == 5) is sent, event


def test_unkeyed_requests_never_go_through_a_proxy() -> None:
    seen: list[str] = []

    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            seen.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    with ThreadingHTTPServer(("127.0.0.1", 0), Proxy) as proxy:
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        try:
            url: str = f"http://127.0.0.1:{proxy.server_port}"
            with (
                patch.object(client, "SETTINGS", {}),
                patch.dict(
                    os.environ,
                    {"http_proxy": url, "no_proxy": "", "NO_PROXY": ""},
                ),
                pytest.raises(urllib.error.URLError),
            ):
                # nothing listens on port 1: only a proxy could answer
                client.open_bus(urllib.request.Request("http://127.0.0.1:1/agents"), 2)
        finally:
            proxy.shutdown()
    assert seen == []
    with (
        patch.object(client.sys, "argv", ["arke", "help"]),
        patch.object(client.sys, "stdout", io.StringIO()),
        patch.object(client, "open_bus", return_value=io.BytesIO(b"guide")) as bus,
    ):
        client.main()
    assert bus.call_args.args[0].full_url.endswith("/help")


def test_session_end_fits_inside_both_harness_limits() -> None:
    for kind, limit in (("claude", 5), ("codex", 3)):
        with (
            patch.object(client.sys, "argv", ["arke", "hooks", kind]),
            patch.object(client.sys, "stdout", io.StringIO()) as out,
        ):
            client.main()
        hooks: Any = json.loads(out.getvalue())["hooks"]
        assert hooks["SessionEnd"][0]["hooks"][0]["timeout"] == limit, kind
        assert "timeout" not in hooks["Stop"][0]["hooks"][0]
    with tempfile.TemporaryDirectory() as temp:
        for event, timeout in (
            ("SessionEnd", client.HOOK_TIMEOUT_S),
            ("Stop", client.TURN_HOOK_TIMEOUT_S),
        ):
            with (
                patch.object(client, "STATE", Path(temp)),
                patch.object(client, "call", return_value={}) as call,
            ):
                client.hook(
                    "codex", {"hook_event_name": event, "session_id": "s", "cwd": temp}
                )
            assert call.call_args.kwargs["timeout"] == timeout, event


# one harness at pid 4242, outside tmux and git
def harness(temp: str) -> ExitStack:
    stack: ExitStack = ExitStack()
    for p in (
        patch.object(client, "STATE", Path(temp)),
        patch.object(client, "harnesses", return_value=[(4242, "claude")]),
        patch.object(client, "is_daemon", return_value=False),
        patch.object(client, "sandbox_init", return_value=""),
        patch.object(client, "git", return_value=""),
        patch.dict(os.environ, {}, clear=True),
    ):
        stack.enter_context(p)
    return stack


@pytest.mark.parametrize("corrupt", ["bytes", "directory"])
def test_a_corrupt_pid_file_is_replaced_not_fatal(corrupt: str) -> None:
    with tempfile.TemporaryDirectory() as temp:
        f: Path = Path(temp) / "pid" / "4242"
        if corrupt == "bytes":
            f.parent.mkdir()
            f.write_bytes(b"\xff\xfe")
        else:
            f.mkdir(parents=True)
        with (
            harness(temp),
            patch.object(client, "call", return_value={}) as call,
            patch.object(client.sys, "stderr", io.StringIO()),
        ):
            assert client.identity()[0] == ""
            client.hook(
                "claude",
                {"hook_event_name": "UserPromptSubmit", "session_id": "s", "cwd": temp},
            )
        assert call.call_args.args[:2] == ("POST", "/hook")
        if corrupt == "bytes":
            assert f.read_text() == "s"
        else:  # the failed rewrite leaves no temporary file behind
            assert sorted(p.name for p in f.parent.iterdir()) == ["4242"]


def test_a_reply_that_is_not_an_object_is_an_outage() -> None:
    with tempfile.TemporaryDirectory() as temp:
        with patch.object(client, "call", return_value=[]):
            out = run(EV | {"cwd": temp}, temp)
        assert "unexpected reply" in json.loads(out)["systemMessage"]
        assert down_marker(temp).exists()


def test_a_malformed_receipt_is_reported_after_the_mail() -> None:
    with tempfile.TemporaryDirectory() as temp:
        with (
            patch.object(
                client, "call", return_value={"context": "mail!", "delivery": "x"}
            ) as call,
            patch.object(client.sys, "stderr", io.StringIO()) as err,
        ):
            out = run(EV | {"cwd": temp}, temp)
        assert json.loads(out)["hookSpecificOutput"]["additionalContext"] == "mail!"
        assert call.call_count == 1  # no ack
        assert "receipt is malformed" in err.getvalue()


def test_seat_asks_tmux_only_inside_tmux() -> None:
    with tempfile.TemporaryDirectory() as temp:
        with (
            harness(temp),
            patch.dict(os.environ, {"TMUX_PANE": "%1"}),
            patch.object(client.subprocess, "run") as tmux,
        ):
            assert client.seat("s", temp)["names"] == []
        tmux.assert_not_called()


# mail reaches the harness with terminal codes escaped (Claude shows a Stop reason on screen); line breaks stay
@pytest.mark.parametrize(
    "event,field", [("SessionStart", "context"), ("Stop", "block")]
)
def test_hook_mail_reaches_the_harness_escaped(event: str, field: str) -> None:
    with tempfile.TemporaryDirectory() as temp:
        with patch.object(
            client, "call", return_value={field: "a\x1b]52;c;eA==\x07\n  b\u202e"}
        ):
            out = json.loads(run(EV | {"hook_event_name": event, "cwd": temp}, temp))
        text = (
            out["reason"]
            if field == "block"
            else out["hookSpecificOutput"]["additionalContext"]
        )
        assert text == "a\\x1b]52;c;eA==\\x07\n  b\\u202e"


def test_escaping_keeps_joiners_that_scripts_and_emoji_need() -> None:
    text = "👨‍💻 می‌خواهم co­op"
    assert client.printable(text) == text


def test_escaping_normalizes_crlf_but_escapes_standalone_carriage_returns() -> None:
    assert (
        client.printable("a\r\nb\rc\r\n\r\n\x1b[2J\r") == "a\nb\\x0dc\n\n\\x1b[2J\\x0d"
    )

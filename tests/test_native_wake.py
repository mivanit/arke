"""Native readiness and dispatch failures use private state; no live harnesses or mailboxes."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import client
import native
import pytest
import server


def agent(bus: server.Bus, **extra: Any) -> server.Agent:
    bus.hook(
        {
            "session_id": "a",
            "event": "SessionStart",
            "kind": "claude",
            "host": server.LOCAL_HOST,
            "names": ["alice"],
            "process_token": "process:1",
            "tmux": "/sock:%1",
            **extra,
        }
    )
    return bus.agents["a"]


@contextmanager
def waiter(
    bus: server.Bus, listener: str = "one", token: str = "process:1", wait: float = 2
) -> Generator[list[Any]]:
    results: list[Any] = []

    def run() -> None:
        try:
            results.append(
                bus.wake_wait(
                    "a", {"process_token": token, "listener": listener, "wait": wait}
                )
            )
        except Exception as exc:  # noqa: BLE001 — recorded for the test thread
            results.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    with bus.cond:
        assert bus.cond.wait_for(
            lambda: "a" in bus.wake_waiters or bool(results), timeout=2
        )
    try:
        yield results
    finally:
        thread.join(3)
        assert not thread.is_alive()


def test_native_offer_is_committed_and_never_consumes_mail(bus: server.Bus) -> None:
    a = agent(bus)
    bus.send("human", {"to": ["alice"], "text": "task"})
    with waiter(bus) as replies, patch.object(bus, "try_wake") as terminal:
        bus.wake_pass()
        persisted = json.loads(
            bus.store.db.execute("SELECT data FROM agents WHERE sid='a'").fetchone()[0]
        )
        assert persisted["wake"]["status"] == "typed"
        assert persisted["wake"]["transport"] == "claude hook"
        assert a["inbox"]
        terminal.assert_not_called()
    assert replies[0]["attempt"] == a["wake"]["attempt"]
    assert replies[0]["notice"].startswith("[arke]")
    assert "delivery" not in replies[0] and "messages" not in replies[0]
    assert not bus.wake_waiters


def test_waiter_rejects_old_generation(bus: server.Bus) -> None:
    agent(bus)
    with waiter(bus) as first:  # a second listener replaces it: test_native_dispatch
        with pytest.raises(server.HttpError, match="no longer owns"):
            bus.wake_wait("a", {"process_token": "old", "listener": "two"})
        bus.hook({"session_id": "a", "event": "SessionEnd"})
    assert isinstance(first[0], server.HttpError)
    assert not bus.wake_waiters


def test_reconnect_cannot_be_removed_by_old_handler(bus: server.Bus) -> None:
    agent(bus)
    with waiter(bus) as old:
        original = bus.wake_waiters["a"]
        with waiter(bus, wait=0.1) as new, bus.cond:
            assert bus.cond.wait_for(
                lambda: bus.wake_waiters.get("a") is not original, 1
            )
    assert isinstance(old[0], server.HttpError)
    assert new == [{"status": "timeout"}]


def test_new_process_replaces_waiter_before_old_handler_cleans_up(
    bus: server.Bus,
) -> None:
    a = agent(bus)
    bus.send("human", {"to": ["alice"], "text": "keep this mail"})
    # Hold the lock through registration and the new request so the old handler cannot clean up first.
    with waiter(bus) as old, bus.cond:
        agent(bus, process_token="process:2")
        assert bus.wake_wait(
            "a", {"process_token": "process:2", "listener": "two", "wait": 0.05}
        ) == {"status": "timeout"}
    assert isinstance(old[0], server.HttpError)
    assert not bus.wake_waiters
    with waiter(bus, listener="two", token="process:2") as new:
        bus.wake_pass()
    assert new[0]["attempt"] == a["wake"]["attempt"]
    assert a[
        "inbox"
    ]  # replacing a listener and offering a notice do not acknowledge mail


def test_expired_listener_uses_terminal(bus: server.Bus) -> None:
    agent(bus)
    with waiter(bus, wait=0.02) as replies:
        pass
    assert replies == [{"status": "timeout"}]
    bus.send("human", {"to": ["alice"], "text": "task"})
    with patch.object(
        bus, "try_wake", return_value={"status": "typed", "at": time.time(), "tries": 1}
    ) as terminal:
        bus.wake_pass()
    terminal.assert_called_once()
    assert bus.agents["a"]["wake"]["transport"] == "tmux"
    assert (
        terminal.call_args.args[3]["fallback"] == "no listener"
    )  # logged with the wake


def test_lost_offer_new_mail_and_restart_keep_grace(bus: server.Bus) -> None:
    a = agent(bus)
    bus.send("human", {"to": ["alice"], "text": "first"})
    with waiter(bus), patch.object(bus, "try_wake") as terminal:
        bus.wake_pass()  # ignore its response, as if the socket closed after dispatch
        attempt, at = a["wake"]["attempt"], a["wake"]["at"]
        bus.send("human", {"to": ["alice"], "text": "second"})
        bus.wake_pass()
        terminal.assert_not_called()
    restarted = server.Bus(bus.state_file)
    try:
        with patch.object(
            restarted,
            "try_wake",
            return_value={"status": "typed", "at": at + 31, "tries": 2},
        ) as terminal:
            restarted.wake_pass()
            assert restarted.agents["a"]["wake"]["attempt"] == attempt
            terminal.assert_not_called()
            with patch.object(server.time, "time", return_value=at + 31):
                restarted.wake_pass()
            terminal.assert_called_once()
            assert terminal.call_args.args[3]["fallback"] == "listener didn't pick up"
            assert len(restarted.agents["a"]["inbox"]) == 2
    finally:
        restarted.store.close()


def test_result_recorded_after_busy_and_ack(bus: server.Bus) -> None:
    a = agent(bus, kind="codex", daemon=True)
    bus.send("human", {"to": ["alice"], "text": "task"})

    def started(*args: Any, **kwargs: Any) -> str:
        persisted = json.loads(
            bus.store.db.execute("SELECT data FROM agents WHERE sid='a'").fetchone()[0]
        )
        assert persisted["wake"]["status"] == "dispatching"
        bus.hook({"session_id": "a", "event": "UserPromptSubmit"})
        bus.acknowledge("a", "", {"ids": [bus.messages[-1]["id"]]})
        return "started"

    with patch.object(server, "codex_turn", side_effect=started):
        bus.wake_pass()
    assert a["wake"]["status"] == "picked up"
    assert a["wake"]["tries"] == 1
    assert not a["inbox"]
    assert not bus.wake_running


def test_concurrent_wake_pass_does_not_dispatch_twice(bus: server.Bus) -> None:
    agent(bus, kind="codex", daemon=True)
    bus.send("human", {"to": ["alice"], "text": "task"})
    entered, release = threading.Event(), threading.Event()

    def slow(*args: Any, **kwargs: Any) -> str:
        entered.set()
        assert release.wait(3)
        return "started"

    with patch.object(server, "codex_turn", side_effect=slow) as dispatch:
        thread = threading.Thread(target=bus.wake_pass)
        thread.start()
        try:
            assert entered.wait(2)
            bus.wake_pass()
            assert dispatch.call_count == 1
        finally:
            release.set()
            thread.join(3)


def test_answering_daemon_resets_outage_clock(bus: server.Bus) -> None:
    a = agent(bus, kind="codex", daemon=True)
    bus.send("human", {"to": ["alice"], "text": "task"})
    now = time.time() + 1
    for offset, answer in [
        (0, OSError("down")),
        (20, "active"),
        (80, OSError("down again")),
    ]:
        with (
            patch.object(server.time, "time", return_value=now + offset),
            patch.object(
                server,
                "codex_turn",
                side_effect=answer if isinstance(answer, Exception) else None,
                return_value=answer,
            ),
        ):
            bus.wake_pass()
        if offset == 20:
            assert "down_since" not in a["wake"]
    assert a["wake"]["status"] == "held"
    assert a["wake"]["down_since"] == now + 80


def test_missing_registration_does_not_create_agent(bus: server.Bus) -> None:
    assert bus.wake_wait("missing", {"process_token": "p", "listener": "l"}) == {
        "status": "unregistered"
    }
    assert not bus.agents
    for wait in [float("nan"), float("inf"), -1, 21]:
        with pytest.raises(server.HttpError):
            bus.wake_wait("a", {"process_token": "p", "listener": "l", "wait": wait})


@pytest.fixture
def listener_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pid").mkdir()
    (tmp_path / "pid" / "123").write_text("sid")
    monkeypatch.setattr(client, "STATE", tmp_path)
    monkeypatch.setattr(client, "harnesses", lambda: [(123, "claude")])
    monkeypatch.setattr(client, "process_token", lambda pid: "token")
    monkeypatch.setattr(client, "identity", lambda: ("sid", ""))
    monkeypatch.setattr(native, "supported", lambda: True)
    monkeypatch.setattr(native, "interactive", lambda pid: True)
    monkeypatch.setattr(
        native.sys,
        "stdin",
        io.StringIO(json.dumps({"session_id": "sid", "hook_event_name": "Stop"})),
    )


def test_listener_only_emits_explicit_notice(
    listener_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    with patch.object(
        client,
        "call",
        side_effect=[
            {"status": "unregistered"},
            {"status": "timeout"},
            {"attempt": "id", "notice": "[arke] mail"},
        ],
    ) as call:
        assert native.listen() == 2
    assert {c.args[1] for c in call.call_args_list} == {"/wake/wait"}
    out = capsys.readouterr()
    assert out.out == "" and out.err == "[arke] mail\n"


@pytest.mark.parametrize(
    "event", [{"agent_id": "helper"}, {"hook_event_name": "PreToolUse"}, []]
)
def test_helper_and_other_events_do_not_wait(listener_env: None, event: Any) -> None:
    with (
        patch.object(native.sys, "stdin", io.StringIO(json.dumps(event))),
        patch.object(client, "call") as call,
    ):
        assert native.listen() == 0
    call.assert_not_called()


def test_old_server_exits_without_waking(
    listener_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    with patch.object(client, "call", side_effect=client.BusError("HTTP 404")):
        assert native.listen() == 0
    assert capsys.readouterr() == ("", "")


def test_listener_retries_startup_conflict(
    listener_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch.object(
            client,
            "call",
            side_effect=[
                client.BusError(
                    "HTTP 409: this listener no longer owns a live local Claude session"
                ),
                {"attempt": "new", "notice": "[arke] mail"},
            ],
        ) as call,
        patch.object(native.time, "sleep") as sleep,
    ):
        assert native.listen() == 2
    assert call.call_count == 2
    sleep.assert_called_once_with(0.2)
    assert capsys.readouterr() == ("", "[arke] mail\n")


def test_listener_startup_conflict_has_a_deadline(
    listener_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch.object(
            client, "call", side_effect=client.BusError("HTTP 409: conflict")
        ) as call,
        patch.object(native, "STARTUP_S", 0.4),
        patch.object(
            native.time, "monotonic", side_effect=[0, 0, 0, 0.2, 0.2, 0.4, 0.4]
        ),
        patch.object(native.time, "sleep") as sleep,
    ):
        assert native.listen() == 0
    assert call.call_count == 3
    assert sleep.call_count == 2
    assert capsys.readouterr() == ("", "")


def test_duplicate_listener_exits_before_network(listener_env: None) -> None:
    with (
        patch.object(native.fcntl, "flock", side_effect=BlockingIOError),
        patch.object(client, "call") as call,
    ):
        assert native.listen() == 0
    call.assert_not_called()


def test_generated_listener_hooks_are_backgrounded(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch.object(native, "supported", return_value=True),
        patch.object(client.sys, "argv", ["client.py", "hooks", "claude"]),
    ):
        client.main()
    hooks = json.loads(capsys.readouterr().out)["hooks"]
    for event, rules in hooks.items():
        listeners = [h for h in rules[0]["hooks"] if h.get("asyncRewake")]
        assert len(listeners) == int(event in ("SessionStart", "Stop", "StopFailure"))
        for h in listeners:
            assert h["async"] is True and h["timeout"] == 86400
            assert "listen claude" in h["command"]


def test_print_mode_and_missing_terminal_never_listen(listener_env: None) -> None:
    with (
        patch.object(native, "interactive", return_value=False),
        patch.object(client, "call") as call,
    ):
        assert native.listen() == 0
    call.assert_not_called()


@pytest.mark.parametrize(
    "arguments,tty,expected",
    [
        (["claude", "--print", "hello"], 123, False),
        (["claude", "-p", "hello"], 123, False),
        (["claude", "--system-prompt", "--", "--print", "hello"], 123, False),
        (["claude", "--append-system-prompt", "--", "-p", "hello"], 123, False),
        (["claude", "--", "--print"], 123, False),
        (["claude", "--resume", "session"], 123, True),
        (["claude"], 0, False),
    ],
)
def test_interactive_process_detection(
    arguments: list[str], tty: int, expected: bool
) -> None:
    master, slave = os.openpty()
    try:
        with (
            patch.object(native, "process_info", return_value={"tty": tty}),
            patch.object(
                native.Path, "read_bytes", return_value="\0".join(arguments).encode()
            ),
            patch.object(native.os, "open", side_effect=lambda *args: os.dup(slave)),
        ):
            assert native.interactive(123) is expected
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize("sink", ["terminal", "file", "pipe", "null"])
def test_stdout_must_be_a_terminal(tmp_path: Path, sink: str) -> None:
    master, slave = os.openpty()
    try:
        with (tmp_path / "output").open("wb") as output:
            destination = {
                "terminal": slave,
                "file": output,
                "pipe": subprocess.PIPE,
                "null": subprocess.DEVNULL,
            }[sink]
            with subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdin=subprocess.DEVNULL,
                stdout=destination,
                stderr=subprocess.DEVNULL,
            ) as process:
                try:
                    # As in tmux, a controlling terminal alone must not override stdout's actual type.
                    with patch.object(
                        native, "process_info", return_value={"tty": 123}
                    ):
                        assert native.interactive(process.pid) is (sink == "terminal")
                finally:
                    process.terminate()
                    process.wait(timeout=5)
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize("error", [FileNotFoundError, PermissionError])
def test_unavailable_stdout_does_not_start_listener(error: type[OSError]) -> None:
    with (
        patch.object(native, "process_info", return_value={"tty": 123}),
        patch.object(native.os, "open", side_effect=error),
    ):
        assert not native.interactive(123)


def test_session_switch_during_response_does_not_emit(
    listener_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    def changed(*args: Any, **kwargs: Any) -> dict[str, str]:
        (client.STATE / "pid" / "123").write_text("new-session")
        return {"attempt": "id", "notice": "[arke] old mail"}

    with patch.object(client, "call", side_effect=changed):
        assert native.listen() == 0
    assert capsys.readouterr() == ("", "")


def test_expired_listener_exits_without_polling(listener_env: None) -> None:
    with patch.object(native, "LIFETIME_S", 0), patch.object(client, "call") as call:
        assert native.listen() == 0
    call.assert_not_called()


def test_unsupported_claude_keeps_only_ordinary_hooks(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch.object(native, "supported", return_value=False),
        patch.object(client.sys, "argv", ["client.py", "hooks", "claude"]),
    ):
        client.main()
    hooks = json.loads(capsys.readouterr().out)["hooks"]
    assert all(len(rules[0]["hooks"]) == 1 for rules in hooks.values())


def test_native_commit_failure_never_publishes_notice(bus: server.Bus) -> None:
    a = agent(bus)
    bus.send("human", {"to": ["alice"], "text": "task"})
    commit = bus.store.commit
    calls = 0

    def fail_offer(state: dict[str, Any]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk full")
        commit(state)

    with (
        waiter(bus, wait=0.1) as replies,
        patch.object(bus.store, "commit", side_effect=fail_offer),
    ):
        bus.wake_pass()
    assert replies == [{"status": "timeout"}]
    assert a["inbox"] and a["wake"]["status"] == "failed"
    assert not bus.wake_running


def test_late_result_cannot_replace_new_attempt(bus: server.Bus) -> None:
    a = agent(bus, kind="codex", daemon=True)
    bus.send("human", {"to": ["alice"], "text": "task"})

    def newer(*args: Any, **kwargs: Any) -> str:
        with bus.change(messages_changed=False):
            a["wake"] = {"attempt": "newer", "status": "held"}
            bus.save()
        return "started"

    with patch.object(server, "codex_turn", side_effect=newer):
        bus.wake_pass()
    assert a["wake"] == {"attempt": "newer", "status": "held"}
    assert not bus.wake_running


def test_ack_after_offer_cancels_waiter_response(bus: server.Bus) -> None:
    a = agent(bus)
    bus.send("human", {"to": ["alice"], "text": "task"})
    with waiter(bus, wait=0.2) as replies, patch.object(bus.cond, "notify_all"):
        bus.wake_pass()
        bus.acknowledge("a", "", {"ids": [bus.messages[-1]["id"]]})
    assert replies == [{"status": "cancelled"}]
    assert not a["inbox"]


def test_receipt_then_new_mail_does_not_wait_for_previous_attempt(
    bus: server.Bus,
) -> None:
    a = agent(bus)
    bus.send("human", {"to": ["alice"], "text": "first"})
    with waiter(bus):
        bus.wake_pass()
    first = a["wake"]["attempt"]
    # The background pass never observes an empty inbox between these requests.
    bus.acknowledge("a", "", {"ids": [bus.messages[-1]["id"]]})
    bus.send("human", {"to": ["alice"], "text": "second"})
    with waiter(bus) as replies, patch.object(bus, "try_wake") as terminal:
        bus.wake_pass()
    terminal.assert_not_called()
    assert replies[0]["attempt"] != first
    assert a["wake"]["tries"] == 1
    assert a["wake"]["transport"] == "claude hook"


def test_failed_wake_logs_its_transport_and_fallback() -> None:
    with patch.object(server, "log") as log:
        server.wake_failed(
            {"transport": "tmux", "fallback": "no listener"}, "alice", "#1", "no pane"
        )
    assert log.call_args.kwargs["via"] == "tmux"
    assert log.call_args.kwargs["fallback"] == "no listener"

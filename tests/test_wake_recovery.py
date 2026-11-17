"""Wake recovery regressions using temporary state and a private Codex daemon; no live agents."""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import server
import wake


@contextmanager
def daemon(
    rpc: Callable[[dict[str, Any], Callable[[dict[str, Any]], None]], Any],
) -> Generator[list[str]]:
    """One private WebSocket connection; rpc returns a result, or closes after accepting turn/start."""
    with tempfile.TemporaryDirectory(prefix="arke-private-daemon-") as directory:
        path: Path = Path(directory) / "control.sock"
        methods: list[str] = []
        errors: list[BaseException] = []
        listener: socket.socket = socket.socket(socket.AF_UNIX)
        listener.bind(str(path))
        listener.listen()
        listener.settimeout(2)

        def serve() -> None:
            try:
                conn, _ = listener.accept()
                with conn:
                    conn.settimeout(2)
                    buf: bytes = b""
                    while b"\r\n\r\n" not in buf:
                        chunk: bytes = conn.recv(4096)
                        if not chunk:
                            return
                        buf += chunk
                    buf = buf.split(b"\r\n\r\n", 1)[1]
                    conn.sendall(
                        b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
                    )

                    def read(n: int) -> bytes:
                        nonlocal buf
                        while len(buf) < n:
                            more: bytes = conn.recv(65536)
                            if not more:
                                raise EOFError
                            buf += more
                        out, buf = buf[:n], buf[n:]
                        return out

                    def send(obj: dict[str, Any]) -> None:
                        data: bytes = json.dumps(obj).encode()
                        size: bytes = (
                            bytes([len(data)])
                            if len(data) < 126
                            else b"\x7e" + len(data).to_bytes(2, "big")
                        )
                        conn.sendall(b"\x81" + size + data)

                    while True:
                        _, size = read(2)
                        n: int = size & 0x7F
                        if n >= 126:
                            n = int.from_bytes(read(2 if n == 126 else 8), "big")
                        mask, data = read(4), read(n)
                        msg: Any = json.loads(
                            bytes(b ^ mask[i % 4] for i, b in enumerate(data))
                        )
                        methods.append(msg["method"])
                        if "id" not in msg:
                            continue
                        result: Any = rpc(msg, send)
                        if result is False:
                            return
                        send({"id": msg["id"], "result": result})
            except (EOFError, BrokenPipeError, ConnectionResetError):
                pass  # the tested client closes after completion, cancellation or its deadline
            except BaseException as exc:  # noqa: BLE001 — re-raised in the test thread below
                errors.append(exc)

        worker: threading.Thread = threading.Thread(target=serve, daemon=True)
        worker.start()
        try:
            with patch.object(wake, "CODEX_SOCK", path):
                yield methods
        finally:
            worker.join(3)
            listener.close()
            if worker.is_alive() or errors:
                raise AssertionError(f"private daemon did not finish cleanly: {errors}")


class TestWakeRecovery:
    @pytest.fixture(autouse=True)
    def setup(self, bus: server.Bus, tmp_path: Path) -> Generator[None]:
        self.bus: server.Bus = bus
        self.root: Path = tmp_path
        with ExitStack() as self.stack:
            self.stack.enter_context(
                patch.object(server, "pane_alive", return_value=True)
            )
            yield

    def agent(self, sid: str = "a", name: str = "alice", **extra: Any) -> server.Agent:
        self.bus.hook(
            {
                "session_id": sid,
                "event": "SessionStart",
                "kind": "codex",
                "host": server.LOCAL_HOST,
                "names": [name],
                "daemon": True,
                "tmux": "/sock:%9",
                "repo": "p",
                **extra,
            }
        )
        return self.bus.agents[sid]

    def send(self, to: str = "alice", **extra: Any) -> None:
        self.bus.send("human", {"to": [to], "text": "pending task", **extra})

    def test_native_cancels_mail_read_or_session_end_during_thread_read(
        self, subtests: pytest.Subtests
    ) -> None:
        for change in ("ack", "end"):
            with subtests.test(change=change):
                self.agent(change, change)
                self.send(change)
                mid: Any = self.bus.messages[-1]["id"]

                def rpc(
                    msg: dict[str, Any],
                    send: Callable[[dict[str, Any]], None],
                    change: str = change,
                    mid: int = mid,
                ) -> Any:
                    if msg["method"] == "thread/read":
                        if change == "ack":
                            self.bus.acknowledge(change, "", {"ids": [mid]})
                        else:
                            self.bus.hook({"session_id": change, "event": "SessionEnd"})
                        return {"thread": {"status": {"type": "idle"}}}
                    return {}

                with (
                    daemon(rpc) as methods,
                    patch.object(self.bus, "try_wake") as fallback,
                ):
                    self.bus.wake_pass()
                assert "thread/read" in methods
                assert "turn/start" not in methods
                fallback.assert_not_called()

    def test_noisy_native_request_times_out_and_next_agent_is_woken(self) -> None:
        a: dict[str, Any] = self.agent()
        self.send()
        self.agent("c", "claude", daemon=False, kind="claude", tmux="/sock:%2")
        self.send("claude")

        def rpc(msg: dict[str, Any], send: Callable[[dict[str, Any]], None]) -> Any:
            if msg["method"] == "thread/read":
                stop: float = time.monotonic() + 5
                while time.monotonic() < stop:
                    send({"method": "thread/status/changed", "params": {}})
                    time.sleep(0.01)
                return {"thread": {"status": {"type": "active"}}}
            return {}

        started: float = time.monotonic()
        with (
            daemon(rpc) as methods,
            patch.object(wake, "CODEX_TIMEOUT_S", 0.15),
            patch.object(
                self.bus, "try_wake", return_value={"status": "held"}
            ) as try_wake,
        ):
            self.bus.wake_pass()
        assert time.monotonic() - started < 2
        assert "thread/read" in methods
        assert try_wake.call_args.args[0] == "claude"
        # a daemon that doesn't answer is waited for (Codex restarting) before the wake counts as failed
        assert a["wake"]["status"] == "held"
        assert "isn't answering" in a["wake"]["detail"]

    def test_lost_native_reply_is_held_and_never_typed(self) -> None:
        """The turn may well have started: typing the notice too would wake it twice, or into another thread."""
        a: dict[str, Any] = self.agent()
        self.send()

        def rpc(msg: dict[str, Any], send: Callable[[dict[str, Any]], None]) -> Any:
            if msg["method"] == "thread/read":
                return {"thread": {"status": {"type": "idle"}}}
            return False if msg["method"] == "turn/start" else {}

        with daemon(rpc) as methods, patch.object(self.bus, "try_wake") as typed:
            self.bus.wake_pass()
        assert "turn/start" in methods
        typed.assert_not_called()
        assert a["wake"]["status"] == "held"

    def test_explicit_non_daemon_resume_uses_its_terminal(self) -> None:
        a: dict[str, Any] = self.agent(process_token="old:100:1")
        self.bus.hook(
            {
                "session_id": "a",
                "event": "SessionStart",
                "daemon": False,
                "process_token": "new:200:1",
                "tmux": "/sock:%3",
            }
        )
        self.send()
        with (
            patch.object(server, "codex_turn") as native,
            patch.object(
                self.bus, "try_wake", return_value={"status": "held"}
            ) as terminal,
        ):
            self.bus.wake_pass()
        assert not a.get("daemon")
        native.assert_not_called()
        assert terminal.call_args.args[1] == "/sock:%3"

    def test_explicit_resume_and_new_harness_reset_exhausted_retry_budget(
        self, subtests: pytest.Subtests
    ) -> None:
        with patch.object(server.time, "time", return_value=1000.0) as clock:
            a: dict[str, Any] = self.agent(process_token="old:100:1")
            self.send()
            with patch.object(server, "codex_turn", return_value="notLoaded") as native:
                for _ in range(4):
                    self.bus.wake_pass()
                    clock.return_value += server.WAKE_FAILED_RETRY_S + 1
            assert native.call_count == 4
            exhausted: dict[str, Any] = dict(a["wake"])
            for event in (
                {"event": "SessionStart"},
                {"event": "Stop", "failed": True, "process_token": "new:200:1"},
            ):
                with subtests.test(event=event):
                    a["wake"] = dict(exhausted)
                    self.bus.hook({"session_id": "a", **event})
                    with patch.object(
                        server, "codex_turn", return_value="started"
                    ) as resumed:
                        self.bus.wake_pass()
                    resumed.assert_called_once()
                    assert a["wake"]["status"] == "typed"
                    assert a["wake"]["retries"] == 0

    def test_new_mail_cannot_keep_restarting_the_initial_batch_wait(self) -> None:
        with (
            patch.object(server.time, "time", return_value=1000.0) as clock,
            patch.object(server, "WAKE_BATCH_S", 1.5),
        ):
            self.agent()
            self.send()
            with patch.object(server, "codex_turn", return_value="started") as native:
                self.bus.wake_pass()
                native.assert_not_called()
                clock.return_value += 1
                self.send()
                self.bus.wake_pass()
                native.assert_not_called()
                clock.return_value += 1
                self.send()
                self.bus.wake_pass()
                native.assert_called_once()

    @pytest.mark.parametrize("prevented", [True, False])
    def test_stop_summary_recovers_missed_hook_and_wakes_unread_mail(
        self, prevented: bool
    ) -> None:
        transcript: Path = self.root / "claude.jsonl"
        a: server.Agent = self.agent(
            kind="claude", daemon=False, transcript=str(transcript)
        )
        self.bus.hook({"session_id": "a", "event": "UserPromptSubmit"})
        self.send()
        unread: list[Any] = list(a["inbox"])
        # Arke missed Stop; another hook's continue:false produces a true flag.
        # False also covers an ordinary completed stop, so both must recover.
        summary: dict[str, Any] = {
            "type": "system",
            "subtype": "stop_hook_summary",
            "timestamp": "2099-01-01T00:00:00Z",
            "preventedContinuation": prevented,
            "hookErrors": [],
        }
        transcript.write_text(json.dumps(summary) + "\n")
        self.bus.turn_pass()
        assert a["state"] == "idle"
        with patch.object(
            self.bus,
            "try_wake",
            return_value={"status": "typed", "at": time.time(), "tries": 1},
        ) as wake:
            self.bus.wake_pass()
            wake.assert_called_once()
        assert a["inbox"] == unread
        # If Claude continues, its subsequent reply must restore busy state.
        with transcript.open("a") as output:
            output.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2099-01-01T00:00:01Z",
                        "message": {"model": "claude", "content": []},
                    }
                )
                + "\n"
            )
        self.bus.turn_pass()
        assert a["state"] == "busy"

    @pytest.mark.parametrize("split", [False, True])
    @pytest.mark.parametrize("prevented", [False, True])
    def test_stop_summary_of_a_blocking_stop_keeps_the_turn_busy(
        self, split: bool, prevented: bool
    ) -> None:
        transcript: Path = self.root / "claude.jsonl"
        a: server.Agent = self.agent(
            kind="claude", daemon=False, transcript=str(transcript)
        )
        self.bus.hook({"session_id": "a", "event": "UserPromptSubmit"})
        # Our Stop hook handed over mail and blocked: Claude writes the reason as a meta user record, then the
        # summary (also listing it in hookErrors), and keeps working.
        feedback: dict[str, Any] = {
            "type": "user",
            "isMeta": True,
            "timestamp": "2099-01-01T00:00:00Z",
            "message": {
                "role": "user",
                "content": "Stop hook feedback:\nMessages via arke ...",
            },
        }
        summary: dict[str, Any] = {
            "type": "system",
            "subtype": "stop_hook_summary",
            "timestamp": "2099-01-01T00:00:00Z",
            "preventedContinuation": False,
            "hookErrors": ["Messages via arke ..."],
        }
        summary["preventedContinuation"] = prevented
        transcript.write_text(json.dumps(feedback) + "\n")
        if split:
            self.bus.turn_pass()
            self.bus.turn_pass()  # no new records yet
            assert a["state"] == "busy"
            with transcript.open("a") as f:
                f.write(json.dumps(summary)[:20])
            self.bus.turn_pass()  # a partially written summary must also wait
        with transcript.open("a") as f:
            f.write(json.dumps(summary)[20 if split else 0 :] + "\n")
        self.bus.turn_pass()
        assert a["state"] == ("idle" if prevented else "busy")
        assert a["turn_offset"] == transcript.stat().st_size
        if not prevented:
            self.send()
            with patch.object(self.bus, "try_wake") as wake:
                self.bus.wake_pass()
                wake.assert_not_called()
        # A later, ordinary Stop must not inherit the earlier hook's blocking result.
        summary.update(
            timestamp="2099-01-01T00:00:01Z", hookErrors=[], preventedContinuation=False
        )
        with transcript.open("a") as f:
            f.write(json.dumps(summary) + "\n")
        self.bus.turn_pass()
        assert a["state"] == "idle"

    def test_a_failed_stop_hook_leaves_the_agent_idle(self) -> None:
        """A Stop hook that exits 1 (a moved clone) is listed in hookErrors too, but the turn has ended."""
        transcript: Path = self.root / "claude.jsonl"
        a: server.Agent = self.agent(
            kind="claude", daemon=False, transcript=str(transcript)
        )
        self.bus.hook({"session_id": "a", "event": "UserPromptSubmit"})
        self.bus.hook({"session_id": "a", "event": "Stop"})
        summary: dict[str, Any] = {
            "type": "system",
            "subtype": "stop_hook_summary",
            "timestamp": "2099-01-01T00:00:00Z",
            "preventedContinuation": False,
            "hookErrors": ["Exit code 1"],
        }
        transcript.write_text(json.dumps(summary) + "\n")
        self.bus.turn_pass()
        assert a["state"] == "idle"

    def test_new_mail_right_after_a_notice_waits_for_the_ack_window(self) -> None:
        with patch.object(server.time, "time", return_value=1000.0) as clock:
            self.agent()
            self.send()
            with patch.object(server, "codex_turn", return_value="started") as native:
                self.bus.wake_pass()
                clock.return_value += 2
                self.send()  # the agent hasn't picked up the first notice yet
                self.bus.wake_pass()
                assert native.call_count == 1  # not a second notice 2 s after the first
                clock.return_value += server.WAKE_ACK_S + 1
                self.bus.wake_pass()
                assert native.call_count == 2  # unanswered: notified again, about both

    def test_session_start_after_compaction_keeps_the_turn_busy(self) -> None:
        self.agent(kind="claude", daemon=False)
        self.bus.hook({"session_id": "a", "event": "UserPromptSubmit"})
        self.bus.hook({"session_id": "a", "event": "SessionStart", "source": "compact"})
        assert self.bus.agents["a"]["state"] == "busy"
        self.bus.hook({"session_id": "a", "event": "SessionStart", "source": "clear"})
        assert self.bus.agents["a"]["state"] == "idle"

    def test_one_agents_wake_error_does_not_stop_the_others(self) -> None:
        for sid, pane in (("a", 1), ("b", 2)):
            self.agent(sid, sid, kind="claude", daemon=False, tmux=f"/sock:%{pane}")
            self.bus.hook({"session_id": sid, "event": "Stop"})
            self.send(sid)
        typed: dict[str, Any] = {"status": "typed", "at": time.time(), "tries": 1}
        with (
            patch.object(
                self.bus, "try_wake", side_effect=[RuntimeError("odd screen"), typed]
            ) as wake,
            patch.object(server.traceback, "print_exc"),
        ):
            self.bus.wake_pass()
        assert wake.call_count == 2
        statuses: list[str] = [self.bus.agents[s]["wake"]["status"] for s in "ab"]
        assert sorted(statuses) == ["failed", "typed"]
        assert any(
            "RuntimeError: odd screen" in self.bus.agents[s]["wake"].get("detail", "")
            for s in "ab"
        )

    def test_one_transcript_that_breaks_the_scan_doesnt_stop_the_others(
        self,
    ) -> None:
        marker: str = json.dumps(
            {
                "type": "system",
                "subtype": "stop_hook_summary",
                "timestamp": "2099-01-01T00:00:00Z",
            }
        )
        for sid in "ab":
            (self.root / f"{sid}.jsonl").write_text(marker + "\n")
            self.agent(sid, sid, transcript=str(self.root / f"{sid}.jsonl"))
            self.bus.hook({"session_id": sid, "event": "UserPromptSubmit"})
        scan: Any = wake.turn_marker

        def odd(path: Path, offset: int) -> Any:
            if path.name == "a.jsonl":
                raise RuntimeError("odd transcript")
            return scan(path, offset)

        with patch.object(server, "turn_marker", side_effect=odd):
            self.bus.turn_pass()
        a, b = self.bus.agents["a"], self.bus.agents["b"]
        assert (a["state"], b["state"]) == ("busy", "idle")
        assert a["turn_offset"] == len(marker) + 1  # skipped, not failed on every pass
        assert "RuntimeError: odd transcript" in server.LOG_FILE.read_text()

    def test_transcript_path_change_resets_offset_and_rejects_old_scan(
        self,
    ) -> None:
        old, new = self.root / "old.jsonl", self.root / "new.jsonl"
        old.write_text("{}\n" * 100)
        a: dict[str, Any] = self.agent(transcript=str(old))
        self.bus.turn_pass()
        assert a["turn_offset"] == 300
        marker: dict[str, str] = {
            "type": "system",
            "subtype": "stop_hook_summary",
            "timestamp": "2099-01-01T00:00:00Z",
        }
        new.write_text(json.dumps(marker) + "\n" + "{}\n" * 200)

        def scan(path: Path, offset: int) -> tuple[int, None]:
            self.bus.hook(
                {"session_id": "a", "event": "PostToolUse", "transcript": str(new)}
            )
            return 300, None

        with patch.object(server, "turn_marker", side_effect=scan):
            self.bus.turn_pass()
        assert a.get("turn_offset", 0) == 0
        assert a["state"] == "busy"
        self.bus.turn_pass()
        assert a["state"] == "idle"

    def test_offline_board_only_agent_can_expire_after_archival(self) -> None:
        a: dict[str, Any] = self.agent()
        self.send("/p", mode="board")
        self.bus.hook({"session_id": "a", "event": "SessionEnd"})
        with self.bus.change():
            a["last_active"] = time.time() - server.OFFLINE_DROP_S - 1
            self.bus.save()
        self.bus.forget_old()
        assert "a" in self.bus.agents
        job: dict[str, Any] = self.bus.maintenance.start(7)
        deadline: float = time.monotonic() + 5
        while job["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
            job = self.bus.maintenance.status(job["id"])
        assert job["status"] == "complete", job
        assert (Path(job["backup"]) / "state.sqlite3.gz").is_file()
        assert "a" not in self.bus.agents

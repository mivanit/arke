"""Client/server delivery checks on a private loopback listener with temporary state."""

from __future__ import annotations

import io
import json
import shutil
import socket
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Generator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch

import client
import pytest
import server


def git(repo: Path, *args: str) -> None:
    """git in a throwaway repo, ignoring the user's hooks, signing and identity."""
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.quotepath=true",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            *args,
        ],
        check=True,
        capture_output=True,
        timeout=5,
    )


class TestListener:
    def test_unsupported_http_method_cannot_emit_terminal_controls(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        class KeyedHandler(server.Handler):
            key = "test-key"

        method = b"\x1b]0;INJECTED\x07"
        with server.BusHTTPServer(("127.0.0.1", 0), KeyedHandler) as listener:
            worker = threading.Thread(
                target=listener.serve_forever,
                kwargs={"poll_interval": 0.01},
                daemon=True,
            )
            worker.start()
            try:
                with socket.create_connection(
                    ("127.0.0.1", listener.server_port), timeout=5
                ) as connection:
                    connection.sendall(
                        method + b" / HTTP/1.1\r\nHost: localhost\r\n\r\n"
                    )
                    reply = b"".join(iter(lambda: connection.recv(4096), b""))
                assert reply.startswith(b"HTTP/1.0 501 ")
            finally:
                listener.shutdown()
                worker.join()
        captured = capsys.readouterr()
        assert "INJECTED" in captured.out
        assert "\\x1b" in captured.out and "\\x07" in captured.out
        assert "\x1b" not in captured.out + captured.err
        assert "\x07" not in captured.out + captured.err

    def test_hook_burst_connects_while_accept_loop_is_busy(self) -> None:
        # No accept loop yet: each connection must fit in the pending queue, as during a hook burst.
        with (
            server.BusHTTPServer(("127.0.0.1", 0), server.Handler) as listener,
            ExitStack() as connections,
        ):
            for _ in range(52):
                connections.enter_context(
                    socket.create_connection(
                        ("127.0.0.1", listener.server_port), timeout=1
                    )
                )

    def test_a_malformed_request_line_gets_an_error_reply(self) -> None:
        """Logging the error needed self.path, which a request line that doesn't parse never sets: no reply at all.
        The real Handler: the delivery fixture's handler doesn't log."""
        with server.BusHTTPServer(("127.0.0.1", 0), server.Handler) as listener:
            threading.Thread(
                target=listener.serve_forever,
                kwargs={"poll_interval": 0.01},
                daemon=True,
            ).start()
            try:
                for line in (b"GET / HTTP/1.1 extra", b"GET / HTTP/9.x"):
                    with socket.create_connection(
                        ("127.0.0.1", listener.server_port), timeout=5
                    ) as s:
                        s.sendall(line + b"\r\n\r\n")
                        reply: bytes = b"".join(iter(lambda: s.recv(4096), b""))
                    assert b"Error code: 400" in reply, (
                        line
                    )  # the bare HTTP/0.9-style error page
            finally:
                listener.shutdown()


class DeliveryFixture:
    @pytest.fixture(autouse=True)
    def setup_delivery(self, bus: server.Bus, tmp_path: Path) -> Generator[None]:
        self.bus: server.Bus = bus
        self.root: Path = tmp_path
        with ExitStack() as self.stack:
            self.stack.enter_context(patch.object(client, "STATE", tmp_path / "client"))
            self.stack.enter_context(patch.object(client, "HOST", "remote"))
            self.bus.hook(
                {
                    "event": "SessionStart",
                    "session_id": "a",
                    "names": ["alice"],
                    "host": "remote",
                }
            )
            self.cut_off: set[str] = set()
            cut_off: set[str] = self.cut_off

            class Handler(server.Handler):
                def log_request(
                    self, code: int | str = "-", size: int | str = "-"
                ) -> None:
                    pass

                def reply(
                    self,
                    status: int,
                    obj: object,
                    ctype: str = "application/json",
                ) -> None:
                    if self.path in cut_off:
                        self.send_response(status)
                        self.send_header("Content-Type", ctype)
                        self.send_header("Content-Length", "100")
                        self.end_headers()
                        self.wfile.write(b'{"truncated":')
                        return
                    super().reply(status, obj, ctype)

            Handler.bus = self.bus
            self.handler: type[server.Handler] = Handler
            self.http: server.BusHTTPServer = server.BusHTTPServer(
                ("127.0.0.1", 0), Handler
            )
            self.thread: threading.Thread = threading.Thread(
                target=self.http.serve_forever,
                kwargs={"poll_interval": 0.01},
                daemon=True,
            )
            self.thread.start()
            self.stack.callback(self.stop)
            self.stack.enter_context(
                patch.object(
                    client,
                    "server",
                    return_value=(f"http://127.0.0.1:{self.http.server_port}", "test"),
                )
            )
            self.stack.enter_context(
                patch.object(client, "identity", return_value=("a", ""))
            )
            self.stack.enter_context(
                patch.object(
                    client,
                    "seat",
                    return_value={
                        "pid": 1,
                        "names": [],
                        "repo": "p",
                        "nested": True,
                        "daemon": False,
                    },
                )
            )
            self.stack.enter_context(
                patch.object(client, "process_token", return_value="")
            )
            yield

    def stop(self) -> None:
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(2)

    def assert_delivery_after_output(self, subtests: pytest.Subtests) -> None:
        class Output(io.StringIO):
            def __init__(self, bus: server.Bus) -> None:
                super().__init__()
                self.bus: server.Bus = bus
                self.pending_at_flush: list[int] = []

            def flush(self) -> None:
                self.pending_at_flush.append(len(self.bus.agents["a"]["inbox"]))

        for hook in (True, False):
            with subtests.test(hook=hook):
                self.bus.send("human", {"to": ["alice"], "text": "one task"})
                with patch.object(client.sys, "stdout", Output(self.bus)) as output:
                    if hook:
                        event: dict[str, str | dict[str, Any]] = {
                            "session_id": "a",
                            "hook_event_name": "PostToolUse",
                            "tool_input": {},
                        }
                        with patch.object(
                            client.sys, "stdin", io.StringIO(json.dumps(event))
                        ):
                            client.run_hook("claude")
                    else:
                        client.print_inbox()
                assert "one task" in output.getvalue()
                assert "_delivery" not in output.getvalue()
                assert output.pending_at_flush == [1]
                assert self.bus.agents["a"]["inbox"] == []


class TestDeliveryIntegration(DeliveryFixture):
    def test_bulk_file_sizes_preserve_commas_and_other_path_characters(
        self,
    ) -> None:
        root: Path = self.root
        self.bus.agents["a"].update(host=server.LOCAL_HOST, cwd=str(root))
        files: dict[str, int] = {}
        for name in ("report,a.py", "space & plus+.py", "unicodé.py"):
            path: Path = root / name
            path.write_bytes(name.encode())
            files[str(path)] = len(name.encode())
        query: str = urllib.parse.urlencode([("path", path) for path in files])
        assert client.call("GET", "/filesizes?" + query) == files

    def test_a_chunked_body_is_refused_not_read_as_empty(self) -> None:
        """Without Content-Length the body went unread, so the request ran with every field at its default."""
        with socket.create_connection(
            ("127.0.0.1", self.http.server_port), timeout=5
        ) as s:
            body: bytes = b'{"color":"red"}'
            s.sendall(
                b"POST /color HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Agent-As: human\r\n"
                b"Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
                + f"{len(body):x}\r\n".encode()
                + body
                + b"\r\n0\r\n\r\n"
            )
            assert s.recv(100).startswith(b"HTTP/1.0 411 ")

    def test_no_request_can_start_a_backup_or_trim(self) -> None:
        """/maintenance used to back up and, by default, trim history for anyone who claimed to be the person."""
        self.bus.send("human", {"to": ["alice"], "text": "kept"})
        url: str = f"http://127.0.0.1:{self.http.server_port}/maintenance"
        for data in (b"{}", None):
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json", "X-Agent-As": "human"},
            )
            with pytest.raises(urllib.error.HTTPError) as refused:
                urllib.request.urlopen(req, timeout=5)
            assert refused.value.code == 404
        assert len(self.bus.messages) == 1
        assert not (self.bus.state_file.parent / "backups").exists()
        assert not hasattr(self.bus, "maintenance")

    def test_human_sends_store_only_a_known_source(self) -> None:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.http.server_port}/send",
            data=json.dumps({"to": ["alice"], "text": "hi"}).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Agent-As": "human",
                "X-Agent-Via": "v" * 5000,
            },
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            mid: int = json.loads(r.read())["id"]
        stored: Any = self.bus.message(mid)
        assert stored["via"] == "shell"
        req.headers["X-agent-via"] = (
            "console"  # urllib stores header names capitalize()d
        )
        req.data = json.dumps({"to": ["alice"], "text": "from the page"}).encode()
        with urllib.request.urlopen(req, timeout=5) as r:
            mid = json.loads(r.read())["id"]
        stored = self.bus.message(mid) or {}
        assert (
            stored["via"] == "console"
        )  # the "(web console)" tag in agents' mail depends on it

    def test_a_tmux_header_without_a_pane_gets_the_409(self) -> None:
        self.bus.agents["a"]["tmux"] = "/sock:%1"
        with pytest.raises(server.HttpError) as refused:
            self.bus.sender("a", "", "garbage")
        assert refused.value.status == 409

    def test_slow_pane_probes_do_not_block_hooks_or_delay_delivery(
        self, subtests: pytest.Subtests
    ) -> None:
        """Status and recipient checks probe panes outside the bus lock; the answer is advisory."""
        self.bus.agents["a"].update(
            host=server.LOCAL_HOST, tmux="/fake:%1", tmux_session="test"
        )
        self.bus.hook(
            {
                "event": "SessionStart",
                "session_id": "b",
                "names": ["bob"],
                "host": "remote",
            }
        )
        self.bus.send("human", {"to": ["bob"], "text": "mail for the concurrent hook"})
        for method, path, body, alive in (
            ("GET", "/agents", None, True),
            ("GET", "/agents/alice", None, True),
            ("GET", "/resolve?to=alice", None, False),
            (
                "POST",
                "/send",
                {"to": ["alice"], "text": "commit before probing"},
                False,
            ),
        ):
            entered, release = threading.Event(), threading.Event()

            def slow(
                *args: Any,
                entered: threading.Event = entered,
                release: threading.Event = release,
                alive: bool = alive,
            ) -> bool:
                entered.set()
                assert release.wait(5), "pane probe was not released"
                return alive

            with (
                subtests.test(path=path),
                patch.object(server, "pane_alive", side_effect=slow),
                ThreadPoolExecutor(max_workers=1) as pool,
            ):
                pending: Future[Any] = pool.submit(
                    client.call, method, path, body, sid="b"
                )
                try:
                    assert entered.wait(2)
                    hook: Any = client.call(
                        "POST",
                        "/hook",
                        {"event": "PostToolUse", "session_id": "b", "receipt": True},
                        timeout=1,
                    )
                    assert "mail for the concurrent hook" in hook["context"]
                    if method == "POST":
                        inbox: Any = client.call("GET", "/inbox?receipt=1", timeout=1)
                        assert [m["text"] for m in inbox["messages"]] == [
                            "commit before probing"
                        ]
                    assert not pending.done()
                finally:
                    release.set()
                out: Any = pending.result(timeout=3)
                if path.startswith("/agents"):
                    alice: Any = (
                        out
                        if path == "/agents/alice"
                        else next(a for a in out if a["name"] == "alice")
                    )
                    assert alice["reachable"] == "yes"
                else:
                    assert out["reach"] == ["alice"]
                    assert [
                        (a["name"], a["reachable"]) for a in out["unreachable"]
                    ] == [("alice", "gone")]

    def test_hook_and_cli_keep_mail_pending_until_output(
        self, subtests: pytest.Subtests
    ) -> None:
        self.assert_delivery_after_output(subtests)

    def test_analytics_pages_load_the_shared_scripts(self) -> None:
        base: str = f"http://127.0.0.1:{self.http.server_port}"
        scripts: tuple[str, ...] = (
            "graph-layout.js",
            "analytics-shared.js",
            "analytics-page.js",
        )
        for page in ("analytics", "analytics3d"):
            with urllib.request.urlopen(f"{base}/{page}", timeout=1) as response:
                html: Any = response.read().decode()
                for script in scripts:
                    assert f'<script src="{script}"></script>' in html
        for script in scripts:
            with urllib.request.urlopen(f"{base}/{script}", timeout=1) as response:
                assert response.headers.get_content_type() == "text/javascript"
                assert response.read() == (server.WEB / script).read_bytes()

    def test_a_server_bug_is_answered_not_a_dropped_connection(self) -> None:
        request: urllib.request.Request = urllib.request.Request(
            f"http://127.0.0.1:{self.http.server_port}/send",
            method="POST",
            data=json.dumps({"to": ["alice"], "text": "x"}).encode(),
            headers={"X-Agent-As": "human", "Content-Type": "application/json"},
        )
        with (
            patch.object(
                self.bus, "send", side_effect=TypeError("injected server bug")
            ),
            patch("traceback.print_exc"),
            pytest.raises(urllib.error.HTTPError) as caught,
        ):
            urllib.request.urlopen(request, timeout=2)
        with caught.value as response:
            assert response.code == 500
            assert "TypeError" in json.loads(response.read())["error"]

    def test_a_lone_surrogate_is_refused_and_agents_still_load(self) -> None:
        """ "\\ud800" parses as JSON, was saved, and then broke /agents for everyone."""
        base: str = f"http://127.0.0.1:{self.http.server_port}"
        request: urllib.request.Request = urllib.request.Request(
            f"{base}/hook",
            method="POST",
            data=json.dumps(
                {"session_id": "s", "event": "SessionStart", "tmux": "/tmp/s\ud800:%1"}
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=2)
        with caught.value as response:
            assert response.code == 400
        assert "s" not in self.bus.agents
        with urllib.request.urlopen(f"{base}/agents", timeout=2) as response:
            assert response.status == 200

    def test_a_key_error_from_inside_the_bus_is_printed(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch.object(self.bus, "list_agents", side_effect=KeyError("state")),
            pytest.raises(client.BusError, match="HTTP 400"),
        ):
            client.call("GET", "/agents")
        assert "KeyError: 'state'" in capsys.readouterr().err  # a server bug surfaces

    @pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
    def test_git_from_a_subdirectory_reports_the_working_tree_root(
        self, subtests: pytest.Subtests
    ) -> None:
        repo: Path = self.root / "repo"
        repo.mkdir()

        git(repo, "init", "-q")
        source: Path = repo / "src/lib/parser.py"
        source.parent.mkdir(parents=True)
        source.write_text("print(1)\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "fixture")
        worktree: Path = self.root / "worktree"
        git(repo, "worktree", "add", "--detach", str(worktree))
        for tree in (repo, worktree):
            with subtests.test(tree=tree.name):
                cwd: Path = tree / "src/lib"
                self.bus.hook(
                    {
                        "event": "SessionStart",
                        "session_id": "a",
                        "host": server.LOCAL_HOST,
                        "cwd": str(cwd),
                    }
                )
                history: Any = client.call(
                    "GET", "/git?cwd=" + urllib.parse.quote(str(cwd), safe="")
                )
                assert history["repo"] == str(tree)
                assert len(history["commits"]) == 1
                assert history["commits"][0]["files"] == [
                    {"path": "src/lib/parser.py", "add": 1, "del": 0}
                ]
                assert (
                    Path(history["repo"]) / history["commits"][0]["files"][0]["path"]
                    == cwd / "parser.py"
                )
                with pytest.raises(
                    client.BusError, match="HTTP 400.*local agent's working directory"
                ):
                    client.call(
                        "GET", "/git?cwd=" + urllib.parse.quote(str(tree), safe="")
                    )

    @pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
    def test_git_preserves_filenames_and_uses_rename_destinations(self) -> None:
        repo: Path = self.root / "repo"
        repo.mkdir()

        git(repo, "init", "-q")
        paths: list[str] = [
            "My Report.md",
            "café.py",
            "tab\tname.py",
            "line\nname.py",
            "carriage\rname.py",
            "control\x1e\x1fname.py",
            "before.py",
            "src/before.py",
        ]
        for name in paths:
            f: Path = repo / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("print(1)\n")
        (repo / "binary.dat").write_bytes(b"\x00\xff")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "initial")
        git(repo, "mv", "before.py", "after.py")
        git(repo, "mv", "src/before.py", "src/after.py")
        git(repo, "commit", "-qm", "rename")
        git(repo, "commit", "--allow-empty", "-qm", "empty")
        self.bus.hook(
            {
                "event": "SessionStart",
                "session_id": "a",
                "host": server.LOCAL_HOST,
                "cwd": str(repo),
            }
        )
        history: Any = client.call(
            "GET", "/git?cwd=" + urllib.parse.quote(str(repo), safe="")
        )
        commits: Any = history["commits"]
        assert [c["subject"] for c in commits] == ["empty", "rename", "initial"]
        assert commits[0]["files"] == []
        assert {f["path"]: (f["add"], f["del"]) for f in commits[1]["files"]} == {
            "after.py": (0, 0),
            "src/after.py": (0, 0),
        }
        assert {
            f["path"]: (f["add"], f["del"]) for f in commits[2]["files"]
        } == dict.fromkeys(paths, (1, 0)) | {"binary.dat": (None, None)}

    @pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
    def test_git_in_a_non_repository_returns_a_bad_request(self) -> None:
        cwd: Path = self.root / "not-a-repo"
        cwd.mkdir()
        self.bus.hook(
            {
                "event": "SessionStart",
                "session_id": "a",
                "host": server.LOCAL_HOST,
                "cwd": str(cwd),
            }
        )
        with pytest.raises(client.BusError, match="HTTP 400.*git root.*failed"):
            client.call("GET", "/git?cwd=" + urllib.parse.quote(str(cwd), safe=""))

    def test_horatio_history_must_be_horatio_json(self) -> None:
        """On a machine where :8080 is some other web page, its HTML was passed on as JSON."""

        class Page(server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html>not horatio</html>")

            def log_message(self, format: str, *args: Any) -> None:
                pass

        with server.ThreadingHTTPServer(("127.0.0.1", 0), Page) as other:
            threading.Thread(target=other.serve_forever, daemon=True).start()
            with (
                patch.object(
                    server, "HORATIO_URL", f"http://127.0.0.1:{other.server_port}"
                ),
                pytest.raises(urllib.error.HTTPError) as caught,
            ):
                urllib.request.urlopen(
                    f"http://127.0.0.1:{self.http.server_port}/horatio/history",
                    timeout=2,
                )
            other.shutdown()
        with caught.value as response:
            assert response.code == 502

    def test_horatio_is_off_by_default_and_says_so(self) -> None:
        with (
            patch.object(server, "HORATIO_URL", ""),
            pytest.raises(urllib.error.HTTPError) as caught,
        ):
            urllib.request.urlopen(
                f"http://127.0.0.1:{self.http.server_port}/horatio/history", timeout=2
            )
        with caught.value as response:
            assert response.code == 404
            assert "not configured" in json.loads(response.read())["error"]

    def test_public_help_uses_generic_paths(self) -> None:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.http.server_port}/help", timeout=2
        ) as response:
            text: Any = response.read().decode()
        assert "python3 <clone>/arke/client.py" in text
        assert server.CLIENT not in text
        assert "{client}" not in text
        assert "{dir}" not in text

    def test_http_send_requires_a_list_of_recipient_names(
        self, subtests: pytest.Subtests
    ) -> None:
        self.bus.hook(
            {
                "event": "SessionStart",
                "session_id": "sender",
                "names": ["bob"],
                "host": "remote",
            }
        )
        for name in [*"alice", "42"]:
            self.bus.hook(
                {
                    "event": "SessionStart",
                    "session_id": "letter-" + name,
                    "names": [name],
                    "host": "remote",
                }
            )
        for to in (
            "alice",
            {"alice": True},
            42,
            False,
            None,
            ["alice", 42],
            ["alice", None],
        ):
            with subtests.test(to=to), pytest.raises(client.BusError, match="HTTP 400"):
                client.call(
                    "POST", "/send", {"to": to, "text": "only for Alice"}, sid="sender"
                )
            assert self.bus.messages == []
            assert self.bus.next_id == 1
            assert all(not a["inbox"] for a in self.bus.agents.values())
        sent: Any = client.call(
            "POST", "/send", {"to": [" alice "], "text": "only for Alice"}, sid="sender"
        )
        assert sent["reach"] == ["alice"]
        assert [entry[0] for entry in self.bus.agents["a"]["inbox"]] == [sent["id"]]

    def test_the_client_gives_every_send_a_request_id(self) -> None:
        self.bus.hook({"event": "SessionStart", "session_id": "b", "names": ["bob"]})
        client.call("POST", "/send", {"to": ["alice"], "text": "x"}, sid="b")
        assert self.bus.messages[-1]["request_id"]

    def test_a_post_body_must_be_one_json_object_within_the_limit(self) -> None:
        base: str = f"http://127.0.0.1:{self.http.server_port}"
        for data, status in ((b"[1]", 400), (b'{"text": "too long"}', 413)):
            request: urllib.request.Request = urllib.request.Request(
                f"{base}/send",
                method="POST",
                data=data,
                headers={"X-Agent-As": "human", "Content-Type": "application/json"},
            )
            with (
                patch.object(server, "MAX_BODY", 10),
                pytest.raises(urllib.error.HTTPError) as caught,
            ):
                urllib.request.urlopen(request, timeout=2)
            with caught.value as response:
                assert response.code == status
        assert self.bus.messages == []

    def test_truncated_committed_ack_keeps_hook_output_successful(self) -> None:
        self.bus.send("human", {"to": ["alice"], "text": "do not lose this task"})
        self.cut_off.add("/ack")
        event: dict[str, str | dict[str, Any]] = {
            "session_id": "a",
            "hook_event_name": "PostToolUse",
            "tool_input": {},
        }
        with (
            patch.object(client.sys, "stdin", io.StringIO(json.dumps(event))),
            patch.object(client.sys, "stdout", io.StringIO()) as output,
            patch.object(client.sys, "stderr", io.StringIO()) as warning,
        ):
            client.run_hook(
                "codex"
            )  # must return normally even though the committed ACK response is incomplete
        assert (
            "do not lose this task"
            in json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"]
        )
        assert "acknowledgment failed" in warning.getvalue()
        assert self.bus.agents["a"]["inbox"] == []

    def test_truncated_send_and_error_response_keep_retry_id(
        self, subtests: pytest.Subtests
    ) -> None:
        self.bus.hook(
            {
                "event": "SessionStart",
                "session_id": "b",
                "names": ["bob"],
                "host": "remote",
            }
        )
        self.cut_off.add("/send")
        for target in ("bob", "missing-agent"):
            with subtests.test(target=target), pytest.raises(client.BusError) as caught:
                client.call(
                    "POST",
                    "/send",
                    {
                        "to": [target],
                        "text": "one task",
                        "request_id": "retry-" + target,
                    },
                )
            assert "--request-id retry-" + target in str(caught.value)
        self.cut_off.clear()
        retry: Any = client.call(
            "POST",
            "/send",
            {"to": ["bob"], "text": "one task", "request_id": "retry-bob"},
        )
        assert retry["replayed"]
        assert len(self.bus.messages) == 1

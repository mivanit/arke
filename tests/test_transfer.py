"""Big answers over slow links: gzip for clients that accept it, a send limit that grows with the answer, and
procs events cut to their totals for the 2D analytics page."""

from __future__ import annotations

import gzip
import json
import os
import socket
import threading
import time
from collections.abc import Generator
from typing import Any
from unittest.mock import patch

import pytest
import server


@pytest.fixture
def port(bus: server.Bus) -> Generator[int]:
    """The real Handler (its reply() is what's under test), quiet."""

    class Handler(server.Handler):
        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            pass

    Handler.bus = bus
    with server.BusHTTPServer(("127.0.0.1", 0), Handler) as http:
        threading.Thread(
            target=http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()
        try:
            yield http.server_port
        finally:
            http.shutdown()


def get(
    port: int, path: str, headers: dict[str, str] | None = None, pause: float = 0
) -> tuple[dict[str, str], bytes]:
    """A raw GET that reads to EOF, so a cut-off body shows up as fewer bytes than Content-Length.
    pause: wait before reading, with a tiny receive buffer, so the server's send has to wait for us."""
    s: socket.socket = socket.socket()
    # before connect, or the window is already set
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    with s:
        s.settimeout(60)
        s.connect(("127.0.0.1", port))
        request: list[str] = [f"GET {path} HTTP/1.1", f"Host: 127.0.0.1:{port}"]
        request += [f"{k}: {v}" for k, v in (headers or {}).items()]
        s.sendall(("\r\n".join(request) + "\r\n\r\n").encode())
        time.sleep(pause)
        raw: bytes = b"".join(iter(lambda: s.recv(65536), b""))
    head: bytes
    body: bytes
    head, _, body = raw.partition(b"\r\n\r\n")
    fields: dict[str, str] = dict(
        line.split(": ", 1) for line in head.decode().split("\r\n")[1:]
    )
    return fields, body


def write_events(count: int, payload: int) -> None:
    """Events with random (incompressible) text, appended straight to the log."""
    with server.LOG_FILE.open("a") as f:
        for i in range(count):
            f.write(
                json.dumps(
                    {"ts": i, "event": "tool", "target": os.urandom(payload).hex()}
                )
                + "\n"
            )


def test_accepts_gzip() -> None:
    for header, expected in [
        ("gzip, deflate, br, zstd", True),  # Firefox
        ("GZIP; q=0.5", True),
        ("br, zstd", False),
        ("gzip;q=0", False),
        ("gzip;q=0.0", False),
        ("gzip;q=junk", False),
        ("", False),
    ]:
        assert server.accepts_gzip(header) is expected, header


def test_big_answers_are_gzipped_only_for_clients_that_accept_it(port: int) -> None:
    write_events(200, 50)
    path: str = "/events?limit=1000"
    plain_fields: dict[str, str]
    plain: bytes
    plain_fields, plain = get(port, path)
    assert "Content-Encoding" not in plain_fields
    assert len(plain) == int(plain_fields["Content-Length"])

    fields: dict[str, str]
    body: bytes
    fields, body = get(port, path, {"Accept-Encoding": "gzip, deflate, br"})
    assert fields["Content-Encoding"] == "gzip"
    assert "Accept-Encoding" in fields["Vary"]
    assert len(body) == int(fields["Content-Length"]) < len(plain)
    assert gzip.decompress(body) == plain

    refused: dict[str, str] = get(port, path, {"Accept-Encoding": "gzip;q=0"})[0]
    assert "Content-Encoding" not in refused


def test_small_answers_are_not_gzipped(port: int) -> None:
    fields: dict[str, str] = get(port, "/events", {"Accept-Encoding": "gzip"})[0]
    assert "Content-Encoding" not in fields


def test_a_big_answer_to_a_slow_reader_is_not_cut_off(port: int) -> None:
    """The 30 s socket limit covers a whole send, so a 30 MB events page to a slow link was cut off, and Firefox
    took the truncated JSON as the whole answer. Here the limit is 0.5 s and the reader waits 2 s before reading
    a 16 MB answer (more than the kernel buffers hold), so the send must wait for it."""
    write_events(40000, 200)
    with patch.object(server.Handler, "timeout", 0.5):
        fields: dict[str, str]
        body: bytes
        fields, body = get(port, "/events?limit=50000", pause=2)
    assert len(body) == int(fields["Content-Length"]) > 16_000_000
    assert len(json.loads(body)["events"]) == 40000


def test_procs_totals_keep_only_each_agents_cpu_and_memory(port: int) -> None:
    full: dict[str, Any] = {
        "ts": 5.0,
        "event": "procs",
        "agents": {
            "alice": {
                "cpu": 12.5,
                "rss_mb": 300,
                "n": 4,
                "top": [{"pid": 1, "cmd": "python3"}],
            },
            "bob": "not an object",
        },
        "started": [{"pid": 2, "agent": "alice"}],
        "ended": [],
    }
    tool: dict[str, Any] = {
        "ts": 6.0,
        "event": "tool",
        "agent": "alice",
        "target": "a.py",
    }
    server.LOG_FILE.write_text(json.dumps(full) + "\n" + json.dumps(tool) + "\n")

    # unchanged unless asked
    assert server.read_events(0, set(), 10)["events"] == [full, tool]
    assert server.read_events(0, set(), 10, procs_totals=True)["events"] == [
        {
            "ts": 5.0,
            "event": "procs",
            "agents": {"alice": {"cpu": 12.5, "rss_mb": 300}},
        },
        tool,
    ]
    body: bytes = get(port, "/events?procs=totals")[1]
    assert json.loads(body)["events"][0]["agents"] == {
        "alice": {"cpu": 12.5, "rss_mb": 300}
    }
    body = get(port, "/events?procs=all")[1]
    assert json.loads(body)["error"] == "procs must be totals or absent"

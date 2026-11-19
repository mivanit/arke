"""Opt-in key enforcement and delivery, on temporary files and loopback listeners only."""

from __future__ import annotations

import http.client
import io
import json
import os
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import patch

import client
import pytest
import server
import settings

from tests import test_delivery, test_settings

KEY: str = "A" * 43  # fixture, never used on a real bus


class TestAuthentication(test_delivery.DeliveryFixture):
    @pytest.fixture(autouse=True)
    def setup_auth(self, setup_delivery: None) -> None:
        assets: Path = self.root / "web"
        assets.mkdir()
        for path, name in server.PAGES.items():
            if path.startswith("/vendor/"):
                (assets / name).write_text("// test fixture\n")
        self.stack.enter_context(patch.object(server, "ASSETS", assets))
        self.keyfile: Path = self.root / "key"
        self.keyfile.write_text(KEY + "\n")
        self.keyfile.chmod(0o600)
        self.base: str = f"http://127.0.0.1:{self.http.server_port}"
        self.handler.key = KEY
        self.config: dict[str, dict[str, str]] = {
            "client": {"url": self.base, "key_file": str(self.keyfile)}
        }
        self.stack.enter_context(patch.object(client, "SETTINGS", self.config))

    def request(
        self,
        method: str,
        path: str,
        key: str = "",
        body: dict[str, Any] | None = None,
    ) -> tuple[int, Message[str, str], bytes]:
        headers: dict[str, str] = {
            "X-Agent-Session": "a",
            "Content-Type": "application/json",
        }
        if key:
            headers["Authorization"] = "Bearer " + key
        req: urllib.request.Request = urllib.request.Request(
            self.base + path,
            method=method,
            headers=headers,
            data=json.dumps(body).encode() if body is not None else None,
        )
        try:
            result: Any = urllib.request.urlopen(req, timeout=2)
        except urllib.error.HTTPError as e:
            result = e
        with result:
            status: int | None = result.status
            assert status is not None
            return status, result.headers, result.read()

    def test_every_private_route_rejects_before_reading_or_mutating_bus(
        self, subtests: pytest.Subtests
    ) -> None:
        reads: list[str] = [
            "/agents",
            "/roster",
            "/roster?global=1&all=1",
            "/agents/alice",
            "/agents/alice/transcript",
            "/projects",
            "/resolve",
            "/inbox",
            "/log",
            "/events",
            "/fileinfo",
            "/filesizes",
            "/git",
            "/machine",
            "/horatio/history",
            "/%61gents",
            "//agents",
            "/unknown",
            "/vendor/../arke/settings.py",
        ]
        writes: list[str] = [
            "/hook",
            "/send",
            "/ack",
            "/projects",
            "/name",
            "/about",
            "/color",
        ]
        self.bus.send("human", {"to": ["alice"], "text": "still queued"})
        before: dict[str, Any] = self.bus.agents["a"].copy()
        for method, paths in [("GET", reads), ("POST", writes)]:
            for path in paths:
                with subtests.test(method=method, path=path):
                    status, headers, body = self.request(
                        method, path, body={} if method == "POST" else None
                    )
                    assert status == 401
                    assert "Access-Control-Allow-Origin" not in headers
                    assert headers["Cache-Control"] == "no-store"
                    assert KEY.encode() not in body
        assert self.bus.agents["a"] == before
        assert len(self.bus.messages) == 1
        assert self.bus.agents["a"]["inbox"]
        # A failed hook still emits valid harness output and leaves the message queued.
        self.keyfile.write_text("B" * 43)
        with (
            patch.object(
                client.sys,
                "stdin",
                io.StringIO('{"session_id":"a","hook_event_name":"PostToolUse"}'),
            ),
            patch.object(client.sys, "stdout", io.StringIO()) as output,
        ):
            client.run_hook("claude")
        assert "401" in output.getvalue()
        json.loads(output.getvalue())
        assert self.bus.agents["a"]["inbox"]

    def test_valid_key_delivers_and_acknowledges_after_output(
        self, subtests: pytest.Subtests
    ) -> None:
        # Reuse the delivery regression with authentication enabled on both sides.
        self.assert_delivery_after_output(subtests)

    def test_bad_send_is_rejected_and_same_id_can_be_retried(self) -> None:
        self.keyfile.write_text("B" * 43)
        body: dict[str, list[str] | str] = {
            "to": ["human"],
            "text": "once",
            "request_id": "retry-auth",
        }
        with pytest.raises(client.BusError, match="HTTP 401") as error:
            client.call("POST", "/send", body)
        assert "outcome unknown" not in str(error.value)
        assert not self.bus.messages
        self.keyfile.write_text(KEY)
        first: Any = client.call("POST", "/send", body)
        second: Any = client.call("POST", "/send", body)
        assert first["id"] == second["id"]
        assert len(self.bus.messages) == 1

    def test_only_static_pages_and_help_are_public(self) -> None:
        for path in [*server.PAGES, "/help"]:
            status, headers, body = self.request("GET", path)
            assert status == 200, path
            assert KEY.encode() not in body
            assert str(server.HERE).encode() not in body
            if path in ("/", "/analytics", "/analytics3d"):
                assert headers["X-Frame-Options"] == "DENY"
        assert self.request("OPTIONS", "/send")[0] == 403
        (server.ASSETS / "OrbitControls.js").unlink()
        status, _, body = self.request("GET", "/vendor/OrbitControls.js")
        assert status == 404
        assert b"--assets-only" in body

    def test_malformed_and_duplicate_authorization_is_rejected(self) -> None:
        for values in [
            ("Basic " + KEY,),
            ("Bearer " + "B" * 43,),
            ("Bearer é",),
            ("Bearer " + KEY, "Bearer " + KEY),
        ]:
            connection: http.client.HTTPConnection = http.client.HTTPConnection(
                "127.0.0.1", self.http.server_port, timeout=2
            )
            try:
                connection.putrequest("GET", "/agents")
                for value in values:
                    connection.putheader("Authorization", value)
                connection.endheaders()
                response: http.client.HTTPResponse = connection.getresponse()
                assert response.status == 401
                response.read()
            finally:
                connection.close()
        assert self.request("GET", "/agents", KEY)[0] == 200

    def test_disabled_allows_clients_but_not_cors_even_with_unusable_key_file(
        self,
    ) -> None:
        self.handler.key = settings.server_key({"key_file": "/does/not/exist"})
        assert self.request("GET", "/agents")[0] == 200
        status, headers, _ = self.request("OPTIONS", "/send")
        assert status == 403
        assert "Access-Control-Allow-Origin" not in headers
        with patch.object(client, "SETTINGS", {}):
            assert client.call("GET", "/agents")[0]["name"] == "alice"

    def test_key_config_failure_or_wrong_destination_never_sends(
        self, subtests: pytest.Subtests
    ) -> None:
        variants: list[dict[str, str]] = [
            {"key_file": str(self.keyfile)},
            {"url": "http://other.invalid:7777", "key_file": str(self.keyfile)},
            {"url": self.base, "key_file": str(self.root / "absent")},
        ]
        for value in variants:
            with (
                subtests.test(value=value),
                patch.object(client, "SETTINGS", {"client": value}),
                patch.object(urllib.request, "build_opener") as opener,
                patch.object(urllib.request, "urlopen") as anonymous,
            ):
                with pytest.raises(client.BusError):
                    client.call("GET", "/agents")
                opener.assert_not_called()
                anonymous.assert_not_called()
        with patch.object(urllib.request, "build_opener") as opener:
            with pytest.raises(client.BusError):
                # No explicit port means HTTP port 80, not Arke's default port.
                client.open_bus(urllib.request.Request("http://127.0.0.1/agents"), 2)
            opener.assert_not_called()

    def test_redirects_and_proxy_environment_cannot_forward_key(self) -> None:
        seen: list[Any] = []
        destination: str = self.base + "/agents"

        class Redirect(server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                seen.append(self.headers.get("Authorization"))
                self.send_response(302)
                self.send_header("Location", destination)
                self.end_headers()

        with server.BusHTTPServer(("127.0.0.1", 0), Redirect) as redirect:
            thread: threading.Thread = threading.Thread(
                target=redirect.serve_forever,
                kwargs={"poll_interval": 0.01},
                daemon=True,
            )
            thread.start()
            try:
                base: str = f"http://127.0.0.1:{redirect.server_port}"
                config: dict[str, dict[str, str]] = {
                    "client": {"url": base, "key_file": str(self.keyfile)}
                }
                with (
                    patch.object(client, "SETTINGS", config),
                    patch.object(self.bus, "list_agents") as reached,
                    pytest.raises(urllib.error.HTTPError) as error,
                ):
                    client.open_bus(urllib.request.Request(base + "/redirect"), 2)
                assert error.value.code == 302
                error.value.close()
                reached.assert_not_called()
                assert seen == ["Bearer " + KEY]
                with patch.dict(
                    os.environ,
                    {
                        "http_proxy": base,
                        "HTTP_PROXY": base,
                        "no_proxy": "",
                        "NO_PROXY": "",
                    },
                ):
                    assert client.call("GET", "/agents")[0]["name"] == "alice"
                assert len(seen) == 1, "proxy never received the request"
            finally:
                redirect.shutdown()
                thread.join(2)


class TestBrowserOrigin(test_delivery.DeliveryFixture):
    def request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        request_headers: dict[str, str | Any] = {
            "Content-Type": "application/json",
            "X-Agent-Session": "a",
            "Authorization": "Bearer " + KEY,
        }
        request_headers.update(headers or {})
        connection: http.client.HTTPConnection = http.client.HTTPConnection(
            "127.0.0.1", self.http.server_port, timeout=2
        )
        try:
            connection.request(
                method,
                path,
                body=json.dumps(body).encode() if body is not None else None,
                headers=request_headers,
            )
            response: http.client.HTTPResponse = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_other_origins_cannot_read_consume_mail_or_mutate_with_key_on_or_off(
        self, subtests: pytest.Subtests
    ) -> None:
        base: str = f"http://127.0.0.1:{self.http.server_port}"
        self.bus.send("human", {"to": ["alice"], "text": "keep this queued"})
        headers: list[dict[str, str]] = [
            {"Origin": "http://elsewhere.invalid"},
            {"Origin": "null"},
            {"Origin": base + ".evil.invalid"},
            {"Sec-Fetch-Site": "cross-site"},
            {"Sec-Fetch-Site": "same-site"},
            {"Origin": base, "Sec-Fetch-Site": "same-site"},
            {"Origin": "http://elsewhere.invalid", "Sec-Fetch-Site": "same-origin"},
            # DNS rebinding: the attacker's domain now resolves to the bus, so the page looks same-origin
            {
                "Host": "rebound.invalid",
                "Origin": "http://rebound.invalid",
                "Sec-Fetch-Site": "same-origin",
            },
            {"Host": "rebound.invalid", "Sec-Fetch-Site": "same-origin"},
            {"Host": "rebound.invalid", "Origin": "http://rebound.invalid"},
            # what a rebinding page really sends: plain http to a non-local name carries no browser headers
            {"Host": "rebound.invalid"},
        ]
        for key in ("", KEY):
            self.handler.key = key
            for header in headers:
                for method, path, body in [
                    ("GET", "/agents", None),
                    ("GET", "/inbox", None),
                    ("POST", "/hook", {"event": "PostToolUse", "session_id": "a"}),
                    ("POST", "/ack", {"ids": [1]}),
                    ("POST", "/send", {"to": ["human"], "text": "unwanted"}),
                ]:
                    with subtests.test(key=bool(key), header=header, path=path):
                        status, response_headers, _ = self.request(
                            method, path, header, body
                        )
                        assert status == 403
                        assert "Access-Control-Allow-Origin" not in response_headers
                        assert (
                            response_headers["Cross-Origin-Resource-Policy"]
                            == "same-origin"
                        )
                        assert self.bus.agents["a"]["inbox"]
                        assert len(self.bus.messages) == 1

    def test_same_origin_cli_and_proxy_requests_still_work(
        self, subtests: pytest.Subtests
    ) -> None:
        base: str = f"http://127.0.0.1:{self.http.server_port}"
        headers: list[dict[str, Any] | dict[str, str]] = [
            {},
            {"Origin": base},
            {"Sec-Fetch-Site": "none"},
            {"Sec-Fetch-Site": "same-origin"},
            {"Origin": base, "Sec-Fetch-Site": "same-origin"},
            {
                "Host": "bus.example",
                "Origin": "https://bus.example",
                "Sec-Fetch-Site": "same-origin",
            },
        ]
        for key in ("", KEY):
            self.handler.key = key
            # a reverse proxy's name must be listed in server.browser_hosts
            for header in headers:
                with (
                    subtests.test(key=bool(key), header=header),
                    patch.object(server, "BROWSER_HOSTS", ["bus.example"]),
                ):
                    assert self.request("GET", "/agents", header)[0] == 200
                    assert (
                        self.request(
                            "POST",
                            "/hook",
                            header,
                            {
                                "event": "PostToolUse",
                                "session_id": "a",
                                "receipt": True,
                            },
                        )[0]
                        == 200
                    )
            # Public page navigation is allowed, but another site cannot embed the page.
            status, response_headers, _ = self.request(
                "GET", "/", {"Sec-Fetch-Site": "cross-site"}
            )
            assert status == 200
            assert response_headers["X-Frame-Options"] == "DENY"

    def test_deeply_nested_json_is_a_bad_request(self) -> None:
        connection: http.client.HTTPConnection = http.client.HTTPConnection(
            "127.0.0.1", self.http.server_port, timeout=5
        )
        try:
            connection.request(
                "POST",
                "/send",
                body=b"[" * 100_000,
                headers={"Content-Type": "application/json", "X-Agent-As": "human"},
            )
            response: http.client.HTTPResponse = connection.getresponse()
            assert response.status == 400
            # RecursionError ("nested too deeply") or, where the parser's stack allows 100k levels, the missing value
            assert json.loads(response.read())["error"].startswith("bad request")
        finally:
            connection.close()

    def test_simple_post_bodies_are_rejected_even_without_browser_headers(
        self, subtests: pytest.Subtests
    ) -> None:
        self.bus.send("human", {"to": ["alice"], "text": "still unread"})
        for key in ("", KEY):
            self.handler.key = key
            for content_type in (
                "",
                "text/plain",
                "text/plain; charset=UTF-8",
                "application/x-www-form-urlencoded",
                "multipart/form-data; boundary=fixture",
            ):
                with subtests.test(key=bool(key), content_type=content_type):
                    status, _, _ = self.request(
                        "POST",
                        "/hook",
                        {"Content-Type": content_type},
                        {"event": "PostToolUse", "session_id": "a"},
                    )
                    assert status == 415
                    assert self.bus.agents["a"]["inbox"]
            assert (
                self.request(
                    "POST",
                    "/hook",
                    {"Content-Type": "application/json; charset=utf-8"},
                    {"event": "PostToolUse", "session_id": "a", "receipt": True},
                )[0]
                == 200
            )
            status, headers, _ = self.request(
                "OPTIONS",
                "/hook",
                {
                    "Origin": "http://elsewhere.invalid",
                    "Access-Control-Request-Method": "POST",
                },
            )
            assert status == 403
            assert not any(h.startswith("Access-Control-") for h in headers)


class TestKeyInstallation(test_settings.InstallationFixture):
    @pytest.mark.skipif(
        not (shutil.which("curl")),
        reason="curl is needed for the documented HTTP example",
    )
    def test_toml_enables_check_in_actual_process_and_curl_reads_key_from_stdin(
        self,
    ) -> None:
        key: Path = self.clone / ".local/key"
        key.write_text(KEY + "\n")
        key.chmod(0o600)
        self.config.write_text(
            '[server]\nstate_dir="state"\nexperimental_require_key=true\n'
            '[client]\nkey_file=".local/key"\n'
        )
        with self.running_server() as (base, output):
            with pytest.raises(urllib.error.HTTPError) as anonymous:
                urllib.request.urlopen(base + "/agents", timeout=2)
            assert anonymous.value.code == 401
            anonymous.value.close()
            self.config.write_text(
                self.config.read_text() + f'url="{base}"\n'
            )  # appended to [client]
            result: subprocess.CompletedProcess[str] = self.run_script(
                "arke/client.py", "server", check=True
            )
            assert "reachable (key sent)" in result.stdout
            result = subprocess.run(
                [
                    "curl",
                    "--fail",
                    "--silent",
                    "--show-error",
                    "--noproxy",
                    "*",
                    "--header",
                    "@-",
                    base + "/agents",
                ],
                input="Authorization: Bearer " + KEY + "\n",
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout) == []
        assert "enabled (no guarantees)" in output[0]
        assert KEY not in output[0]

    def test_enabled_missing_or_bad_key_does_not_touch_service_or_database(
        self,
    ) -> None:
        self.config.write_text("[server]\nexperimental_require_key=true\n")
        self.unit.parent.mkdir(parents=True)
        self.unit.write_text("existing service\n")
        key: Path = self.clone / ".local/key"
        for value, mode in [
            (None, 0o600),
            (KEY, 0o644),
            ("short", 0o600),
            ("A" * 1000, 0o600),
        ]:
            if value is not None:
                key.write_text(value)
                key.chmod(mode)
            for script, args in [
                ("install-service.py", ["--replace"]),
                ("arke/server.py", []),
            ]:
                result: subprocess.CompletedProcess[str] = self.run_script(
                    script, *args
                )
                assert result.returncode != 0
                assert KEY not in result.stdout + result.stderr
                assert not (self.clone / ".local/server").exists()
                assert self.unit.read_text() == "existing service\n"
                assert not self.commands.exists()
        key.unlink()
        os.mkfifo(key, 0o600)
        result = self.run_script("arke/server.py")
        assert result.returncode != 0, "a non-regular key must not hang startup"

    def test_bad_client_key_keeps_hook_output_valid(self) -> None:
        self.config.write_text(
            '[client]\nurl="http://127.0.0.1:1"\nkey_file="missing"\n'
        )
        self.env["ARKE_URL"] = "http://127.0.0.1:1"
        result: subprocess.CompletedProcess[str] = self.run_script(
            "arke/client.py",
            "hook",
            "claude",
            input='{"session_id":"fixture","hook_event_name":"PostToolUse"}',
        )
        assert result.returncode == 0, result.stderr
        assert "key configuration" in result.stdout
        json.loads(result.stdout)

"""TOML, service installation and documented HTTP commands, using temporary clones and state."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import runpy
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import client
import pytest
import settings

ROOT: Path = Path(__file__).resolve().parents[1]


class TestSettings:
    @pytest.fixture(autouse=True)
    def setup(self) -> Generator[None]:
        with ExitStack() as self.stack:
            temporary: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory(
                prefix="arke-settings-"
            )
            self.stack.callback(temporary.cleanup)
            self.root: Path = Path(temporary.name)
            self.stack.enter_context(patch.object(settings, "ROOT", self.root))
            self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
            yield

    def test_missing_default_is_optional_but_explicit_file_is_required(
        self,
    ) -> None:
        assert settings.load() == {}
        with (
            patch.dict(os.environ, {"ARKE_CONFIG": str(self.root / "missing.toml")}),
            pytest.raises(FileNotFoundError),
        ):
            settings.load()

    def test_invalid_settings_are_rejected_with_the_file_path(
        self, subtests: pytest.Subtests
    ) -> None:
        config: Path = self.root / "config.toml"
        self.stack.enter_context(patch.dict(os.environ, {"ARKE_CONFIG": str(config)}))
        for text in (
            "[broken",
            "[server]\nprot=1234",
            "[client]\nurl=false",
            "[server]\nport=true",
            "[server]\nport=0",
            "[server]\nport=65536",
            "[server]\nprocess_interval=nan",
            "[server]\nstale_seconds=-1",
            "[server]\ntranscript_roots=[123]",
            '[server]\nstate_dir=""',
            "[other]\nx=1",
        ):
            with subtests.test(text=text):
                config.write_text(text)
                with pytest.raises(ValueError, match=str(config)):
                    settings.load()
        config.write_text("server = 1")
        with pytest.raises(ValueError, match=r"must be a \[server\] table"):
            settings.load()

    def test_relative_paths_use_the_clone_and_tildes_expand(self) -> None:
        assert settings.path("data/server") == self.root / "data/server"
        assert settings.path("~/transcripts") == Path.home() / "transcripts"

    def test_transcript_roots_are_resolved(self) -> None:
        """a symlinked ~/.claude refused every transcript (resolved paths vs unresolved roots)."""
        (self.root / "real").mkdir()
        (self.root / "link").symlink_to(self.root / "real")
        config: Path = self.root / "roots.toml"
        config.write_text(
            f"[server]\ntranscript_roots=[{json.dumps(str(self.root / 'link'))}]\n"
        )
        result: subprocess.CompletedProcess[str] = subprocess.run(
            [
                sys.executable,
                "-c",
                "import transcripts; print(*transcripts.TRANSCRIPT_ROOTS)",
            ],
            cwd=ROOT / "arke",
            env=os.environ | {"ARKE_CONFIG": str(config)},
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        assert result.stdout.strip() == str((self.root / "real").resolve())

    def test_pytest_ignores_installed_configuration(self) -> None:
        config: Path = self.root / "installed.toml"
        config.write_text(
            '[server]\nport=8888\n[client]\nurl="http://configured:8888"\n'
        )
        result: subprocess.CompletedProcess[str] = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "tests/test_client.py::TestConfiguration::test_default_server_is_local_on_any_machine",
            ],
            cwd=ROOT,
            env=os.environ | {"ARKE_CONFIG": str(config), "ARKE_PORT": "9999"},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_url_precedence_and_reset_fall_back_to_toml(self) -> None:
        saved: Path = self.root / "server-url"
        with (
            patch.object(client, "SETTINGS", {"client": {"url": "toml-bus:8888"}}),
            patch.object(client, "CONFIG", saved),
            patch.object(client, "STATE", self.root / "client"),
        ):
            assert client.server()[0] == "http://toml-bus:8888"
            saved.write_text("saved-bus:9999")
            assert client.server()[0] == "http://saved-bus:9999"
            with patch.dict(os.environ, {"ARKE_URL": "environment-bus:7778"}):
                assert client.server() == ("http://environment-bus:7778", "$ARKE_URL")
            with (
                patch.object(sys, "argv", ["client.py", "server", "--reset"]),
                patch("builtins.print"),
            ):
                client.main()
            assert client.server()[0] == "http://toml-bus:8888"


class InstallationFixture:
    @pytest.fixture(autouse=True)
    def setup_installation(self) -> Generator[None]:
        with ExitStack() as self.stack:
            temporary: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory(
                prefix="arke-install-"
            )
            self.stack.callback(temporary.cleanup)
            self.root: Path = Path(temporary.name)
            self.clone: Path = self.root / "clone with 'quotes' % and $dollars"
            shutil.copytree(
                ROOT / "arke",
                self.clone / "arke",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            for name in ("install-service.py", "arke.example.toml"):
                shutil.copyfile(ROOT / name, self.clone / name)
            self.config: Path = self.clone / ".local/config.toml"
            self.config.parent.mkdir()
            self.env: dict[str, str] = {
                k: v
                for k, v in os.environ.items()
                if not k.startswith(("ARKE_", "AGENTBUS_"))
            }
            self.env["XDG_CONFIG_HOME"] = str(self.root / "user-config")
            self.unit: Path = self.root / "user-config/systemd/user/arke.service"
            self.commands: Path = self.root / "systemctl.jsonl"
            fake: Path = self.root / "bin/systemctl"
            fake.parent.mkdir()
            fake.write_text(
                f"#!{sys.executable}\nimport json,sys\nwith open({str(self.commands)!r}, 'a') as f:\n"
                "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            )
            fake.chmod(0o755)
            self.env["PATH"] = str(fake.parent) + os.pathsep + self.env.get("PATH", "")
            yield

    @contextmanager
    def running_server(self) -> Generator[tuple[str, list[str]]]:
        """Run the clone's server on a free port until it answers; yields its URL and, after the stop, its output.
        The server is started from an unrelated working directory and must stop cleanly."""
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port: int = reserve.getsockname()[1]
        # the [server] section gets this run's port, replacing any earlier one
        text: str = re.sub(r"(?m)^port=\d+\n", "", self.config.read_text())
        self.config.write_text(
            text.replace("[server]\n", f"[server]\nport={port}\n", 1)
        )
        url: str = f"http://127.0.0.1:{port}"
        log: list[str] = []
        with (self.root / "server-output").open("w+") as output:
            process: subprocess.Popen[bytes] = subprocess.Popen(
                [sys.executable, str(self.clone / "arke/server.py")],
                cwd=self.root,
                env=self.env,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        output.seek(0)
                        pytest.fail(output.read())
                    try:
                        urllib.request.urlopen(url + "/agents", timeout=0.2).close()
                        break
                    except urllib.error.HTTPError as e:  # it answers, if only to refuse
                        e.close()
                        break
                    except OSError:
                        time.sleep(0.05)
                else:
                    pytest.fail("server did not start")
                yield url, log
            finally:
                process.terminate()
                process.wait(timeout=5)
                output.seek(0)
                log.append(output.read())
        assert process.returncode == 0, log[0]  # a clean stop, not a failed unit

    def run_script(
        self, script: str, *args: str, check: bool = False, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        command: list[str] = [sys.executable, str(self.clone / script), *args]
        if script == "install-service.py":
            # Service/unit checks stay offline; AssetDownloadTests checks the downloader separately.
            command = [
                sys.executable,
                "-c",
                (
                    "import runpy,sys\nfrom typing import Any\nfrom unittest.mock import patch\n"
                    "sys.argv=sys.argv[1:]\ng: dict[str, Any]=runpy.run_path(sys.argv[0])\n"
                    "def skip_download() -> None: pass\n"
                    "with patch.dict(g['main'].__globals__, download_assets=skip_download): g['main']()\n"
                ),
                str(self.clone / script),
                *args,
            ]
        return subprocess.run(
            command,
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=15,
            check=check,
            **kwargs,
        )


class TestInstallation(InstallationFixture):
    def test_install_initializes_config_and_starts_using_actual_clone_path(
        self,
    ) -> None:
        result: subprocess.CompletedProcess[str] = self.run_script(
            "install-service.py", check=True
        )
        assert str(self.unit) in result.stdout
        assert self.config.read_text() == (self.clone / "arke.example.toml").read_text()
        content: str = self.unit.read_text()
        assert (
            str(self.clone).replace("%", "%%").replace("$", "$$") + "/arke/server.py"
            in content
        )
        assert "%h/arke/" not in content
        calls: list[Any] = [
            json.loads(line) for line in self.commands.read_text().splitlines()
        ]
        assert calls == [
            ["--user", "daemon-reload"],
            ["--user", "enable", "arke.service"],
            ["--user", "restart", "arke.service"],
        ]
        if shutil.which("systemd-analyze"):
            check: subprocess.CompletedProcess[str] = subprocess.run(
                ["systemd-analyze", "--user", "verify", str(self.unit)],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            assert check.returncode == 0, check.stderr

    def test_reinstall_preserves_config_and_backs_up_changed_unit_without_starting(
        self,
    ) -> None:
        self.config.write_text("[server]\nport=8899\n")
        self.unit.parent.mkdir(parents=True)
        original: Path = self.root / "previous.service"
        original.write_text("old custom service\n")
        self.unit.symlink_to(original)
        self.run_script("install-service.py", "--replace", "--no-start", check=True)
        assert self.config.read_text() == "[server]\nport=8899\n"
        assert original.read_text() == "old custom service\n"
        assert self.unit.with_suffix(".service.bak").read_text() == original.read_text()
        assert not self.unit.is_symlink()
        assert not self.commands.exists()

    def test_custom_service_is_preserved_without_explicit_replacement(
        self, subtests: pytest.Subtests
    ) -> None:
        self.unit.parent.mkdir(parents=True)
        original: str = (
            f'[Service]\nEnvironment="ARKE_DIR={self.root}/existing-state"\n'
            f"ExecStart={sys.executable} {self.clone}/arke/server.py --tailscale\n"
        )
        self.unit.write_text(original)
        for args in ((), ("--no-start",)):
            with subtests.test(args=args):
                result: subprocess.CompletedProcess[str] = self.run_script(
                    "install-service.py", *args
                )
                assert result.returncode != 0
                assert "--replace" in result.stderr
                assert str(self.config) in result.stderr
                assert self.unit.read_text() == original
                assert not self.config.exists()
                assert not self.commands.exists()
                assert not self.unit.with_suffix(".service.bak").exists()

    def test_identical_reinstall_starts_without_replacement_flag(self) -> None:
        self.run_script("install-service.py", "--no-start", check=True)
        original: str = self.unit.read_text()
        self.run_script("install-service.py", check=True)
        assert self.unit.read_text() == original
        calls: list[Any] = [
            json.loads(line) for line in self.commands.read_text().splitlines()
        ]
        assert calls == [
            ["--user", "daemon-reload"],
            ["--user", "enable", "arke.service"],
            ["--user", "restart", "arke.service"],
        ]
        assert not self.unit.with_suffix(".service.bak").exists()

    def test_moving_installed_clone_preserves_messages_receipts_and_projects(
        self,
    ) -> None:
        self.run_script("install-service.py", "--no-start", check=True)
        self.config.write_text(
            '[server]\nstate_dir=".local/server"\n[client]\nstate_dir=".local/client"\n'
        )

        def request(
            url: str, route: str, body: dict[str, Any] | None = None, sid: str = ""
        ) -> Any:
            req = urllib.request.Request(
                url + route,
                data=json.dumps(body).encode() if body is not None else None,
                headers={
                    "Content-Type": "application/json",
                    **({"X-Agent-Session": sid} if sid else {"X-Agent-As": "human"}),
                },
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                return json.load(response)

        with self.running_server() as (url, _):
            request(
                url,
                "/hook",
                {
                    "event": "SessionStart",
                    "session_id": "upgrade-agent",
                    "names": ["alice"],
                    "host": "remote",
                },
            )
            request(
                url,
                "/projects",
                {"project": "/release", "action": "join"},
                "upgrade-agent",
            )
            request(url, "/send", {"to": ["alice"], "text": "already read"})
            delivered = request(url, "/inbox?receipt=1", sid="upgrade-agent")
            request(url, "/ack", {"ids": delivered["delivery"]["ids"]}, "upgrade-agent")
            request(
                url,
                "/send",
                {
                    "to": ["alice"],
                    "text": "still unread",
                    "request_id": "upgrade-message",
                },
            )
            before = request(url, "/log")
            pending = request(url, "/inbox?receipt=1", sid="upgrade-agent")
            projects = request(url, "/agents")[0]["projects"]
        config = self.config.read_bytes()
        unit = self.unit.read_bytes()
        old_clone = self.clone
        original_database = old_clone / ".local/server/state.sqlite3"
        original_digest = hashlib.sha256(original_database.read_bytes()).digest()
        self.clone = self.root / "new standalone clone"
        shutil.copytree(old_clone, self.clone)
        self.config = self.clone / ".local/config.toml"
        refused = self.run_script("install-service.py", "--no-start")
        assert refused.returncode != 0 and "--replace" in refused.stderr
        assert self.unit.read_bytes() == unit
        self.run_script("install-service.py", "--replace", "--no-start", check=True)
        assert self.config.read_bytes() == config
        assert self.unit.with_suffix(".service.bak").read_bytes() == unit
        assert str(self.clone / "arke/server.py") in self.unit.read_text()
        assert not self.commands.exists(), (
            "--no-start must not touch the running service"
        )
        with self.running_server() as (url, _):
            assert request(url, "/log") == before
            assert request(url, "/inbox?receipt=1", sid="upgrade-agent") == pending
            agent = request(url, "/agents")[0]
            assert agent["name"] == "alice" and agent["projects"] == projects
            request(
                url,
                "/send",
                {
                    "to": ["alice"],
                    "text": "still unread",
                    "request_id": "upgrade-message",
                },
            )
            assert request(url, "/log") == before, "upgrade lost send deduplication"
            request(url, "/ack", {"ids": pending["delivery"]["ids"]}, "upgrade-agent")
            assert (
                request(url, "/inbox?receipt=1", sid="upgrade-agent")["messages"] == []
            )
        assert (
            hashlib.sha256(original_database.read_bytes()).digest() == original_digest
        ), "original data must remain unchanged for rollback"

    def test_explicit_replacement_keeps_migrated_settings_and_restarts(
        self,
    ) -> None:
        config: str = (
            f'[server]\nstate_dir="{self.root}/existing-state"\ntailscale=true\n'
        )
        self.config.write_text(config)
        self.unit.parent.mkdir(parents=True)
        original: str = "[Service]\nExecStart=/old/arke/server.py --tailscale\n"
        self.unit.write_text(original)
        self.run_script("install-service.py", "--replace", check=True)
        assert self.config.read_text() == config
        assert self.unit.with_suffix(".service.bak").read_text() == original
        assert self.unit.read_text() != original
        calls: list[Any] = [
            json.loads(line) for line in self.commands.read_text().splitlines()
        ]
        assert calls[-1] == ["--user", "restart", "arke.service"]

    def test_bad_config_preserves_unit_and_hook_does_not_block(self) -> None:
        self.config.write_text("[server]\nport=false\n")
        self.unit.parent.mkdir(parents=True)
        self.unit.write_text("existing unit")
        assert self.run_script("install-service.py").returncode != 0
        assert self.unit.read_text() == "existing unit"
        assert not self.commands.exists()
        hook: subprocess.CompletedProcess[str] = self.run_script(
            "arke/client.py", "hook", "claude", input="{}", check=True
        )
        assert "configuration error" in json.loads(hook.stdout)["systemMessage"]
        server: subprocess.CompletedProcess[str] = self.run_script("arke/server.py")
        assert server.returncode != 0
        assert str(self.config) in server.stderr

    @pytest.mark.parametrize("existing", ["same", "custom", "symlink"])
    def test_failed_unit_write_preserves_existing_service(self, existing: str) -> None:
        import signal

        resource = pytest.importorskip("resource")
        self.run_script("install-service.py", "--no-start", check=True)
        original_path: Path = self.root / "original.service"
        if existing != "same":
            self.unit.unlink()
            original_path.write_text("[Service]\nExecStart=/old/arke\n")
            if existing == "symlink":
                self.unit.symlink_to(original_path)
            else:
                self.unit.write_bytes(original_path.read_bytes())
        original: bytes = self.unit.read_bytes()

        def limit_writes() -> None:
            # Permit a backup of a custom unit, but fail while writing the longer replacement.
            limit: int = 0 if existing == "same" else len(original)
            signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
            resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))

        result = self.run_script(
            "install-service.py", "--replace", preexec_fn=limit_writes
        )
        assert result.returncode != 0 and "File too large" in result.stderr
        assert self.unit.read_bytes() == original
        assert self.unit.is_symlink() == (existing == "symlink")
        if existing != "same":
            assert original_path.read_bytes() == original
            assert self.unit.with_suffix(".service.bak").read_bytes() == original
        assert not self.commands.exists()
        assert not list(self.unit.parent.glob(".install-*"))

    def test_custom_config_is_carried_into_service_and_hooks(self) -> None:
        custom: Path = self.root / "custom 'config' %.toml"
        custom.write_text('[client]\nurl="custom-bus:8888"\n')
        self.env["ARKE_CONFIG"] = str(custom)
        self.run_script("install-service.py", "--no-start", check=True)
        assert (
            'Environment="ARKE_CONFIG=' + str(custom).replace("%", "%%") + '"'
            in self.unit.read_text()
        )
        result: subprocess.CompletedProcess[str] = self.run_script(
            "arke/client.py", "hooks", "codex", check=True
        )
        hook: Any = json.loads(result.stdout)["hooks"]["SessionStart"][0]["hooks"][0][
            "command"
        ]
        assert shlex.split(hook.rpartition("; exec ")[2])[:2] == [
            "env",
            f"ARKE_CONFIG={custom}",
        ]

    def test_server_and_client_read_toml_from_unrelated_working_directory(
        self,
    ) -> None:
        self.config.write_text("""[server]
port=8899
state_dir="state"
log_file="events/log.jsonl"
ntfy_url="http://notify:2586"
horatio_url="http://metrics:8080"
stale_seconds=120
process_interval=20
transcript_roots=["transcripts"]
[client]
state_dir="client-state"
""")
        script: str = """import json,client,server,transcripts
print(json.dumps([client.server()[0],str(client.STATE),str(server.STATE_DIR),str(server.LOG_FILE),
server.NTFY_URL,server.HORATIO_URL,server.STALE_S,server.PROC_S,list(map(str,transcripts.TRANSCRIPT_ROOTS))]))"""
        probe: Path = self.clone / "arke/probe.py"
        probe.write_text(script)
        result: Any = json.loads(self.run_script("arke/probe.py", check=True).stdout)
        assert result == [
            "http://127.0.0.1:8899",
            str(self.clone / "client-state"),
            str(self.clone / "state"),
            str(self.clone / "events/log.jsonl"),
            "http://notify:2586",
            "http://metrics:8080",
            120,
            20,
            [str(self.clone / "transcripts")],
        ]
        self.env.update(
            ARKE_PORT="9900", ARKE_DIR=str(self.root / "override"), ARKE_NTFY=""
        )
        result = json.loads(self.run_script("arke/probe.py", check=True).stdout)
        assert result[0] == "http://127.0.0.1:9900"
        assert result[2] == str(self.root / "override")
        assert result[4] == ""

    def test_a_broken_event_log_doesnt_stop_the_server_starting_or_stopping(
        self,
    ) -> None:
        log: Path = self.root / "log.jsonl"
        self.config.write_text(f'[server]\nstate_dir="state"\nlog_file="{log}"\n')
        with self.running_server():
            pass  # creates the database
        log.unlink()
        log.mkdir()  # the event log can't be written now
        with self.running_server() as (_, output):
            pass
        assert "IsADirectoryError" in output[0]  # reported, but the bus still runs

    def test_help_and_unknown_flags_never_start_a_server(self) -> None:
        # a started server would block until run_script's timeout
        shown: subprocess.CompletedProcess[str] = self.run_script(
            "arke/server.py", "--help"
        )
        assert shown.returncode == 0 and "--tailscale" in shown.stdout
        assert self.run_script("arke/server.py", "--tailscal").returncode == 2

    @pytest.mark.skipif(
        not (shutil.which("curl")),
        reason="curl is needed for the documented HTTP examples",
    )
    def test_toml_server_accepts_curl_discovery_send_board_and_backup(
        self,
    ) -> None:
        self.config.write_text('[server]\nstate_dir="state"\n')
        with self.running_server() as (bus_url, _):

            def curl(
                route: str,
                payload: dict[str, Any] | None = None,
                identity: str = "human",
            ) -> Any:
                args: list[str] = [
                    "curl",
                    "--fail",
                    "--silent",
                    "--show-error",
                    "-H",
                    "X-Agent-As: " + identity,
                ]
                if payload is not None:
                    args += [
                        "-H",
                        "Content-Type: application/json",
                        "-d",
                        json.dumps(payload),
                    ]
                result: subprocess.CompletedProcess[str] = subprocess.run(
                    [*args, bus_url + route],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                assert result.returncode == 0, result.stderr
                return json.loads(result.stdout)

            curl(
                "/hook",
                {
                    "event": "SessionStart",
                    "session_id": "test-session",
                    "names": ["alice"],
                    "host": "remote",
                },
            )
            assert curl("/agents")[0]["name"] == "alice"
            curl("/projects", {"project": "/demo", "action": "join"}, "alice")
            curl(
                "/send",
                {
                    "to": ["alice"],
                    "text": "Please review the change.",
                    "request_id": "example-send",
                },
            )
            curl(
                "/send",
                {"to": ["/demo"], "text": "Review complete.", "mode": "board"},
            )
            assert curl("/log?project=/demo")


def test_installer_refuses_old_python() -> None:
    with (
        patch.object(sys, "version_info", (3, 11, 9)),
        pytest.raises(SystemExit, match=r"needs Python 3\.12"),
    ):
        runpy.run_path(str(ROOT / "install-service.py"))


class TestAssetDownload:
    @pytest.fixture(autouse=True)
    def setup(self) -> Generator[None]:
        with ExitStack() as self.stack:
            temporary: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory(
                prefix="arke-assets-"
            )
            self.stack.callback(temporary.cleanup)
            self.root: Path = Path(temporary.name)
            self.installer: Any = runpy.run_path(str(ROOT / "install-service.py"))[
                "main"
            ].__globals__
            files: Any = self.installer["ASSET_FILES"]
            self.data: dict[Any, Any] = {
                url: ("fixture " + name).encode() for name, (url, _) in files.items()
            }
            self.files: dict[Any, tuple[Any, str]] = {
                name: (url, hashlib.sha256(self.data[url]).hexdigest())
                for name, (url, _) in files.items()
            }
            self.stack.enter_context(
                patch.dict(self.installer, ROOT=self.root, ASSET_FILES=self.files)
            )
            self.stack.enter_context(patch("builtins.print"))
            yield

    def test_downloads_verified_assets_reuses_cache_and_repairs_damage(
        self,
    ) -> None:
        def download(url: str, timeout: float) -> io.BytesIO:
            assert timeout == 30
            return io.BytesIO(self.data[url])

        with patch("urllib.request.urlopen", side_effect=download) as fetch:
            self.installer["download_assets"]()
            assert fetch.call_count == len(self.files)
            for name, (url, _) in self.files.items():
                assert (self.root / ".local/web" / name).read_bytes() == self.data[url]
            self.installer["download_assets"]()
            assert fetch.call_count == len(self.files), (
                "reinstall works without another download"
            )
            (self.root / ".local/web/OrbitControls.js").write_bytes(b"damaged")
            self.installer["download_assets"]()
            assert fetch.call_count == len(self.files) + 1
            assert (
                self.root / ".local/web/OrbitControls.js"
            ).read_bytes() == self.data[self.files["OrbitControls.js"][0]]

    def test_failed_download_preserves_previous_file(self) -> None:
        target: Path = self.root / ".local/web/three.module.min.js"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"previous version")
        with (
            patch("urllib.request.urlopen", return_value=io.BytesIO(b"wrong download")),
            pytest.raises(ValueError, match="checksum mismatch"),
        ):
            self.installer["download_assets"]()
        with (
            patch("urllib.request.urlopen", side_effect=OSError("offline")),
            pytest.raises(OSError, match="offline"),
        ):
            self.installer["download_assets"]()
        assert target.read_bytes() == b"previous version"
        assert list(target.parent.iterdir()) == [target]

    def test_assets_only_does_not_read_config_or_install_a_service(self) -> None:

        def skip_download() -> None:
            return None

        with (
            patch.object(sys, "argv", ["install-service.py", "--assets-only"]),
            patch.object(settings, "load", side_effect=AssertionError("config read")),
            patch("subprocess.run") as command,
            patch.dict(self.installer, download_assets=skip_download),
        ):
            self.installer["main"]()
        command.assert_not_called()
        assert list(self.root.iterdir()) == []

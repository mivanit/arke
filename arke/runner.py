"""Process ownership for native adapters, optionally behind a container boundary.

Only adapter commands/events and scoped bus calls cross this connection. The
worker never receives web credentials or access to the conversation database.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import uuid
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

from .harnesses import Event, Launch

MAX_FRAME = 2 * 1024 * 1024
WORKER_CLOSE_TIMEOUT = 22


def validate_isolation(launch: Launch, protected: Sequence[Path]) -> None:
    if launch.runner != "container":
        raise ValueError("shared chat requires runner = 'container'")
    if launch.native_home is None or not launch.native_home.is_dir():
        raise ValueError("container profiles need an existing dedicated native_home")
    workspace, home = launch.cwd.resolve(), launch.native_home.resolve()
    if Path.home().resolve().is_relative_to(home) or home in (
        Path.home().resolve() / ".codex",
        Path.home().resolve() / ".claude",
    ):
        raise ValueError("use a dedicated native_home, not the owner's existing home")
    if not workspace.is_dir():
        raise ValueError("workspace does not exist")
    if workspace.is_relative_to(home) or home.is_relative_to(workspace):
        raise ValueError("workspace and native_home must be separate directories")
    for mount in (workspace, home):
        for path in protected:
            path = path.resolve()
            if path.is_relative_to(mount) or mount.is_relative_to(path):
                raise ValueError(f"worker mount overlaps private service state: {path}")
        if "," in str(mount):
            raise ValueError("container mount paths cannot contain commas")
    if not launch.image or launch.image.startswith("-"):
        raise ValueError("configure a trusted worker image")


class Runner:
    """Adapter proxy with one owned process group/container and bounded shutdown."""

    def __init__(self, launch: Launch) -> None:
        self.launch = launch
        self.process: asyncio.subprocess.Process | None = None
        self.reader: asyncio.Task[None] | None = None
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.events: asyncio.Queue[Event | Exception] = asyncio.Queue(maxsize=64)
        self.callbacks: set[asyncio.Task[None]] = set()
        self.sequence = 0
        self.container: str | None = None
        self._closing: asyncio.Task[None] | None = None

    def command(self) -> list[str]:
        if self.launch.runner == "local":
            return [sys.executable, "-m", "arke.worker"]
        if self.launch.runner != "container":
            raise ValueError("runner must be local or container")
        validate_isolation(self.launch, ())
        assert self.launch.native_home is not None
        self.container = "arke-worker-" + uuid.uuid4().hex
        return [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "-i",
            "--init",
            "--name",
            self.container,
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=256m",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--pids-limit=256",
            "--memory=2g",
            "--cpus=2",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--env",
            "HOME=/home/agent",
            "--env",
            "CODEX_HOME=/home/agent/.codex",
            "--env",
            "CLAUDE_CONFIG_DIR=/home/agent/.claude",
            "--mount",
            f"type=bind,src={self.launch.cwd.resolve()},dst=/workspace",
            "--mount",
            f"type=bind,src={self.launch.native_home.resolve()},dst=/home/agent",
            "--workdir",
            "/workspace",
            "--entrypoint",
            "/app/.venv/bin/python",
            self.launch.image,
            "-m",
            "arke.worker",
        ]

    async def _write(self, value: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise ConnectionError("worker is not running")
        self.process.stdin.write((json.dumps(value) + "\n").encode())
        await self.process.stdin.drain()

    async def _call(self, method: str, params: dict[str, Any]) -> Any:
        self.sequence += 1
        number = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[number] = future
        try:
            await self._write({"id": number, "method": method, "params": params})
            return await future
        finally:
            self.pending.pop(number, None)

    async def _bus(self, message: dict[str, Any]) -> None:
        try:
            if self.launch.bus_call is None:
                raise ValueError("this worker has no bus capability")
            # Bus operations commit synchronously. Do not detach them into
            # cancellable to_thread tasks that could outlive the bus database.
            result = self.launch.bus_call(message["tool"], message["data"])
            reply = {"result": result}
        except Exception as exc:  # noqa: BLE001 — forward failures across the worker boundary
            reply = {"error": str(exc)}
        with contextlib.suppress(ConnectionError, BrokenPipeError):
            await self._write({"bus_reply": message["bus_request"], **reply})

    async def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        failure: Exception = ConnectionError("native worker disconnected")
        try:
            while line := await self.process.stdout.readline():
                value = json.loads(line)
                if "event" in value:
                    self.events.put_nowait(Event(**value["event"]))
                elif "failure" in value:
                    self.events.put_nowait(RuntimeError(value["failure"]))
                elif "bus_request" in value:
                    # A worker can use only its own injected mailbox capability.
                    if len(self.callbacks) >= 16:
                        await self._write(
                            {
                                "bus_reply": value["bus_request"],
                                "error": "too many bus requests",
                            }
                        )
                        continue
                    task = asyncio.create_task(self._bus(value))
                    self.callbacks.add(task)
                    task.add_done_callback(self.callbacks.discard)
                else:
                    future = self.pending.get(value.get("id"))
                    if future is not None and not future.done():
                        if "error" in value:
                            future.set_exception(RuntimeError(value["error"]))
                        else:
                            future.set_result(value.get("result"))
        except Exception as exc:  # noqa: BLE001 — forward failures across the worker boundary
            failure = exc
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(failure)
            if self.events.full():
                self.events.get_nowait()
            self.events.put_nowait(failure)

    async def open(self, native_id: str | None) -> str | None:
        self.process = await asyncio.create_subprocess_exec(
            *self.command(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            limit=MAX_FRAME,
        )
        self.reader = asyncio.create_task(self._read())
        launch = {
            "harness": self.launch.harness,
            "cwd": "/workspace" if self.container else str(self.launch.cwd),
            "model": self.launch.model,
            "instructions": self.launch.instructions,
            "mcp_servers": self.launch.mcp_servers,
            "executable": self.launch.executable,
        }
        async with asyncio.timeout(90):
            return await self._call(
                "open",
                {
                    "launch": launch,
                    "native_id": native_id,
                    "bus": self.launch.bus_call is not None,
                },
            )

    async def run(self, text: str) -> AsyncIterator[Event]:
        await self._call("run", {"text": text})
        while True:
            event = await self.events.get()
            if isinstance(event, Exception):
                raise event
            yield event
            if event.kind == "completed":
                return

    async def interrupt(self) -> None:
        await self._call("interrupt", {})

    async def respond(self, request_id: str, answer: dict[str, Any]) -> None:
        await self._call("respond", {"request_id": request_id, "answer": answer})

    async def close(self) -> None:
        # A cancelled caller must not cancel the task that owns process cleanup.
        if self._closing is None:
            self._closing = asyncio.create_task(self._close())
        await asyncio.shield(self._closing)

    async def _close(self) -> None:
        if self.process is None:
            return
        try:
            if self.process.returncode is None:
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(WORKER_CLOSE_TIMEOUT):
                        await self._call("close", {})
                        if self.process.stdin:
                            self.process.stdin.close()
                        await self.process.wait()
        finally:
            # Stop the process group, including tools left by the CLI, and reap
            # our owned worker. Orphaned descendants are reaped by the OS/init.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
            removal_error = None
            try:
                async with asyncio.timeout(2):
                    await self.process.wait()
            except TimeoutError:
                removal_error = RuntimeError("could not confirm worker process exit")
            if self.container:
                cleanup = await asyncio.create_subprocess_exec(
                    "docker",
                    "rm",
                    "-f",
                    self.container,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    async with asyncio.timeout(5):
                        _, stderr = await cleanup.communicate()
                    if cleanup.returncode and b"No such container" not in stderr:
                        removal_error = RuntimeError(
                            "could not confirm worker container removal"
                        )
                except TimeoutError:
                    cleanup.kill()
                    await cleanup.wait()
                    removal_error = RuntimeError("worker container removal timed out")
            if self.reader:
                self.reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.reader
            for task in list(self.callbacks):
                task.cancel()
            await asyncio.gather(*self.callbacks, return_exceptions=True)
            if removal_error:
                raise removal_error

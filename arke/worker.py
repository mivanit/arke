"""Private stdio adapter host. No HTTP server, web keys, or conversation database."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import struct
import sys
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from .harnesses import Adapter, Launch, adapter
from .harnesses.events import READ_BATCH
from .wire import MAX_FRAME, PipeWriter, encode_frame


async def serve() -> None:
    output = PipeWriter()
    await asyncio.get_running_loop().connect_write_pipe(
        lambda: output, sys.stdout.buffer
    )
    emit = output.send
    reader = asyncio.StreamReader(limit=MAX_FRAME)
    protocol = asyncio.StreamReaderProtocol(reader)
    await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)
    native: Adapter | None = None
    turn: asyncio.Task[None] | None = None
    turn_finished = True
    tasks: set[asyncio.Task[None]] = set()
    replies: dict[int, asyncio.Future[Any]] = {}
    sequence = 0
    socket_server: asyncio.Server | None = None
    closing = False

    async def bus_connection(
        stream: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        nonlocal sequence
        number = None
        try:
            peer = writer.get_extra_info("socket")
            _, uid, _ = struct.unpack(
                "3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            if uid != os.getuid():
                raise PermissionError("bus socket belongs to another execution user")
            async with asyncio.timeout(30):
                message = json.loads(await stream.readline())
                sequence += 1
                number = sequence
                future = asyncio.get_running_loop().create_future()
                replies[number] = future
                await emit(
                    {
                        "bus_request": number,
                        "tool": message["tool"],
                        "data": message["data"],
                    }
                )
                result = await future
                response = {"result": result}
        except Exception as exc:  # noqa: BLE001 — forward failures across the worker boundary
            response = {"error": str(exc)[:8192]}
        finally:
            if number is not None:
                replies.pop(number, None)
        writer.write(encode_frame(response))
        with contextlib.suppress(ConnectionError):
            await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def run(text: str) -> None:
        nonlocal turn_finished
        assert native is not None
        try:
            terminal = None
            async for event in native.run(text):
                if event.kind == "completed":
                    terminal = event
                else:
                    await emit({"event": asdict(event)})
            if terminal is None:
                raise ConnectionError("native stream ended without a turn result")
            # Native cleanup is finished. A following run may wait for the final
            # output flush, but must never overlap the previous native iterator.
            turn_finished = True
            await emit({"event": asdict(terminal)})
        except Exception as exc:  # noqa: BLE001 — forward failures across the worker boundary
            turn_finished = True
            await emit({"failure": str(exc)[:8192]})

    async def dispatch(value: dict[str, Any]) -> None:
        nonlocal native, turn, turn_finished, socket_server, closing
        try:
            method, params = value["method"], value["params"]
            result: Any = None
            if closing:
                raise RuntimeError("worker is stopping")
            if method == "open":
                if native is not None:
                    raise RuntimeError("worker already opened")
                launch = Launch(
                    **(params["launch"] | {"cwd": Path(params["launch"]["cwd"])})
                )
                if params.get("bus"):
                    socket_path = "@arke-bus-" + uuid.uuid4().hex
                    socket_server = await asyncio.start_unix_server(
                        bus_connection, path="\0" + socket_path[1:], limit=MAX_FRAME
                    )
                    if "arke" in launch.mcp_servers:
                        raise ValueError(
                            "the dashboard reserves the MCP server name arke"
                        )
                    launch = replace(
                        launch,
                        mcp_servers=launch.mcp_servers
                        | {
                            "arke": {
                                "command": sys.executable,
                                "args": ["-m", "arke.bus_tools"],
                                "env": {"ARKE_MANAGED_SOCKET": socket_path},
                            }
                        },
                    )
                native = adapter(launch)
                result = await native.open(params["native_id"])
            elif native is None:
                raise RuntimeError("worker has not opened")
            elif method == "run":
                if turn is not None and not turn.done():
                    if not turn_finished:
                        raise RuntimeError("a turn is already running")
                    await turn
                turn_finished = False
                turn = asyncio.create_task(run(params["text"]))
            elif method == "interrupt":
                await native.interrupt()
            elif method == "respond":
                await native.respond(params["request_id"], params["answer"])
            elif method == "close":
                closing = True
                others = [task for task in tasks if task is not asyncio.current_task()]
                for task in others:
                    task.cancel()
                await asyncio.gather(*others, return_exceptions=True)
                if turn is not None and not turn.done():
                    turn.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await turn
                await native.close()
                native = None
            else:
                raise ValueError("unknown worker operation")
            await emit({"id": value["id"], "result": result})
        except Exception as exc:  # noqa: BLE001 — forward failures across the worker boundary
            await emit({"id": value["id"], "error": str(exc)[:8192]})

    batch = 0
    try:
        while line := await reader.readline():
            if len(line) > MAX_FRAME:
                raise ValueError("worker protocol message exceeds the frame limit")
            batch += 1
            if batch >= READ_BATCH:
                batch = 0
                await asyncio.sleep(0)
            value = json.loads(line)
            if "bus_reply" in value:
                future = replies.get(value["bus_reply"])
                if future is not None and not future.done():
                    if "error" in value:
                        future.set_exception(RuntimeError(value["error"]))
                    else:
                        future.set_result(value["result"])
                continue
            task = asyncio.create_task(dispatch(value))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    finally:
        if socket_server is not None:
            socket_server.close()
            await socket_server.wait_closed()
        for task in tasks:
            task.cancel()
        if turn is not None:
            turn.cancel()
        await asyncio.gather(*tasks, *([turn] if turn else []), return_exceptions=True)
        try:
            if native is not None:
                await native.close()
        finally:
            output.close()


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()

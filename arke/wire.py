"""Small shared limits for the private worker's newline-delimited JSON protocol."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

MAX_FRAME = 2 * 1024 * 1024


def encode_frame(value: dict[str, Any]) -> bytes:
    frame = (json.dumps(value) + "\n").encode()
    if len(frame) > MAX_FRAME:
        raise ValueError("worker protocol message exceeds the 2 MiB frame limit")
    return frame


class PipeWriter(asyncio.Protocol):
    """An owned, bounded write pipe; backpressure never blocks the event loop."""

    def __init__(self) -> None:
        self.transport: asyncio.WriteTransport | None = None
        self.failure: Exception | None = None
        self.ready = asyncio.Event()
        self.ready.set()
        self.lock = asyncio.Lock()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = cast(asyncio.WriteTransport, transport)
        self.transport.set_write_buffer_limits(high=64 * 1024)

    def pause_writing(self) -> None:
        self.ready.clear()

    def resume_writing(self) -> None:
        self.ready.set()

    def connection_lost(self, exc: Exception | None) -> None:
        self.failure = exc or ConnectionError("worker output pipe closed")
        self.ready.set()

    async def send(self, value: dict[str, Any]) -> None:
        async with self.lock:
            if self.failure is not None:
                raise self.failure
            if self.transport is None:
                raise ConnectionError("worker output pipe is not connected")
            self.transport.write(encode_frame(value))
            await self.ready.wait()
            if self.failure is not None:
                raise self.failure

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()

"""Burst output, control progress, and explicit bounded failure without models."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from arke.harnesses import Event, Launch
from arke.harnesses.events import EventOverflow, event_buffer, native_buffer
from arke.runner import Runner
from arke.wire import MAX_FRAME, PipeWriter, encode_frame


def test_preview_bursts_coalesce_without_crossing_durable_records() -> None:
    async def scenario() -> None:
        buffer = event_buffer()
        for _ in range(1000):
            buffer.put_nowait(Event("delta", {"text": "x"}))
        buffer.put_nowait(Event("message", {"text": "authoritative"}))
        for _ in range(1000):
            buffer.put_nowait(Event("delta", {"text": "y"}))
        buffer.put_nowait(Event("completed", {"status": "completed"}))
        assert [await buffer.get() for _ in range(4)] == [
            Event("delta", {"text": "x" * 1000}),
            Event("message", {"text": "authoritative"}),
            Event("delta", {"text": "y" * 1000}),
            Event("completed", {"status": "completed"}),
        ]
        assert buffer.bytes == 0 and buffer.failure is None

    asyncio.run(scenario())


def test_preview_memory_bounded_and_native_turns_do_not_merge() -> None:
    async def scenario() -> None:
        buffer = native_buffer()
        for text, turn in [("a" * 150_000, "one"), ("tail", "one"), ("b", "two")]:
            buffer.put_nowait(
                {
                    "method": "item/agentMessage/delta",
                    "params": {"threadId": "thread", "turnId": turn, "delta": text},
                }
            )
        first, second = await buffer.get(), await buffer.get()
        assert isinstance(first, dict) and isinstance(second, dict)
        assert len(first["params"]["delta"]) == 100_000
        assert first["params"]["delta"].endswith("tail")
        assert second["params"]["delta"] == "b"

    asyncio.run(scenario())


def test_durable_overflow_preserves_accepted_records_and_separate_failure() -> None:
    async def scenario() -> None:
        buffer = event_buffer(max_bytes=180)
        accepted = Event("message", {"text": "kept"})
        terminal = Event("completed", {"status": "completed"})
        buffer.put_nowait(accepted)
        buffer.put_nowait(terminal)
        with pytest.raises(EventOverflow):
            buffer.put_nowait(Event("tool", {"details": "x" * 200}))
        buffer.fail(ConnectionError("later EOF must not replace the original failure"))
        assert await buffer.get() == accepted
        assert await buffer.get() == terminal
        assert isinstance(await buffer.get(), EventOverflow)
        assert buffer.bytes == 0

    asyncio.run(scenario())


def test_runner_reader_handles_bursts_with_concurrent_consumer(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner = Runner(Launch("codex", tmp_path))
        source = asyncio.StreamReader()
        frames: list[dict[str, Any]] = [
            {"event": {"kind": "delta", "data": {"text": "x"}}} for _ in range(500)
        ]
        frames += [
            {"event": {"kind": "message", "data": {"text": str(i)}}} for i in range(200)
        ]
        frames.append({"event": {"kind": "completed", "data": {"status": "completed"}}})
        source.feed_data(b"".join(encode_frame(frame) for frame in frames))
        source.feed_eof()
        runner.process = cast(
            asyncio.subprocess.Process, SimpleNamespace(stdout=source)
        )
        received = []

        async def consume() -> None:
            while True:
                event = await runner.events.get()
                assert isinstance(event, Event)
                received.append(event)
                if event.kind == "completed":
                    return

        await asyncio.wait_for(asyncio.gather(runner._read(), consume()), 2)
        assert (
            "".join(e.data["text"] for e in received if e.kind == "delta") == "x" * 500
        )
        assert [e.data["text"] for e in received if e.kind == "message"] == [
            str(i) for i in range(200)
        ]
        with pytest.raises(ConnectionError, match="disconnected"):
            await runner._call("interrupt", {})

    asyncio.run(scenario())


def test_control_reply_does_not_wait_for_event_consumer(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner = Runner(Launch("codex", tmp_path))
        runner.events = event_buffer(max_items=3)
        runner.events.put_nowait(Event("message", {"text": "one"}))
        runner.events.put_nowait(Event("approval", {"request_id": "pending"}))
        source = asyncio.StreamReader()
        frames: list[dict[str, Any]] = [
            {"event": {"kind": "delta", "data": {"text": "x"}}} for _ in range(500)
        ]
        frames.append({"id": 42, "result": {"ok": True}})
        source.feed_data(b"".join(encode_frame(frame) for frame in frames))
        source.feed_eof()
        runner.process = cast(
            asyncio.subprocess.Process, SimpleNamespace(stdout=source)
        )
        answer = asyncio.get_running_loop().create_future()
        runner.pending[42] = answer
        reading = asyncio.create_task(runner._read())
        # Deliberately consume no events until the control response is delivered.
        assert await asyncio.wait_for(answer, 2) == {"ok": True}
        await reading
        assert (await runner.events.get()) == Event("message", {"text": "one"})
        assert (await runner.events.get()) == Event(
            "approval", {"request_id": "pending"}
        )
        assert (await runner.events.get()) == Event("delta", {"text": "x" * 500})

    asyncio.run(scenario())


def test_wire_rejects_oversized_encoded_frames() -> None:
    assert len(encode_frame({"text": "normal"})) < MAX_FRAME
    with pytest.raises(ValueError, match="frame limit"):
        encode_frame({"text": "\x00" * MAX_FRAME})


def test_worker_pipe_backpressure_keeps_event_loop_responsive() -> None:
    async def scenario() -> None:
        protocol = PipeWriter()
        frames: list[bytes] = []

        class Transport:
            def set_write_buffer_limits(self, **limits: Any) -> None:
                pass

            def write(self, frame: bytes) -> None:
                frames.append(frame)
                protocol.pause_writing()

        protocol.connection_made(cast(asyncio.BaseTransport, Transport()))
        sending = asyncio.create_task(protocol.send({"event": "buffered"}))
        await asyncio.sleep(0)
        assert not sending.done()
        # Control tasks still run while the output pipe is not writable.
        controlled = asyncio.Event()
        asyncio.get_running_loop().call_soon(controlled.set)
        await asyncio.wait_for(controlled.wait(), 1)
        protocol.resume_writing()
        await sending
        assert json.loads(frames[0]) == {"event": "buffered"}

    asyncio.run(scenario())


def test_bus_callback_is_awaited_and_oversized_reply_is_explicit(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        called = False

        async def callback(tool: str, data: dict[str, Any]) -> dict[str, str]:
            nonlocal called
            await asyncio.sleep(0)
            called = True
            return {"text": "\x00" * MAX_FRAME}

        frames: list[bytes] = []

        class Input:
            def write(self, value: bytes) -> None:
                frames.append(value)

            async def drain(self) -> None:
                pass

        runner = Runner(Launch("codex", tmp_path, bus_call=callback))
        runner.process = cast(
            asyncio.subprocess.Process, SimpleNamespace(stdin=Input())
        )
        await runner._bus({"bus_request": 1, "tool": "inbox", "data": {}})
        assert called
        reply = json.loads(frames[0])
        assert reply["bus_reply"] == 1 and "exceeds" in reply["error"]
        assert len(frames[0]) < 1000

    asyncio.run(scenario())

"""Owned blocking bus work: cancellation of a caller never loses a running write."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any


class BusBusy(RuntimeError):
    pass


class BusExecution:
    def __init__(self) -> None:
        # Long polls have separate capacity so they cannot occupy tool/write slots.
        self.executors = (
            ThreadPoolExecutor(max_workers=4, thread_name_prefix="arke-bus"),
            ThreadPoolExecutor(max_workers=8, thread_name_prefix="arke-bus-wait"),
        )
        self.pending: tuple[set[Future[Any]], set[Future[Any]]] = (set(), set())
        self.lock = threading.Lock()
        self.closing = False

    def begin_shutdown(self) -> None:
        with self.lock:
            self.closing = True

    def _finished(self, future: Future[Any], lane: int) -> None:
        with self.lock:
            self.pending[lane].discard(future)

    async def call(
        self, function: Callable[..., Any], *args: Any, long_poll: bool = False
    ) -> Any:
        lane = int(long_poll)
        with self.lock:
            if self.closing:
                raise BusBusy("the bus is stopping")
            if len(self.pending[lane]) >= (8 if long_poll else 32):
                raise BusBusy("the bus is busy; retry with the same request ID")
            future = self.executors[lane].submit(function, *args)
            self.pending[lane].add(future)
        # Add outside the lock: add_done_callback executes inline for finished work.
        future.add_done_callback(lambda done: self._finished(done, lane))
        waiting = asyncio.wrap_future(future)
        waiting.add_done_callback(
            lambda done: None if done.cancelled() else done.exception()
        )
        return await asyncio.shield(waiting)

    async def drain(self, timeout: float = 30) -> None:
        self.begin_shutdown()
        with self.lock:
            pending = [
                asyncio.wrap_future(future) for lane in self.pending for future in lane
            ]
        if pending:
            done, unfinished = await asyncio.wait(pending, timeout=timeout)
            for future in done:
                if not future.cancelled():
                    future.exception()
            for future in unfinished:
                future.add_done_callback(
                    lambda done: None if done.cancelled() else done.exception()
                )
            if unfinished:
                raise RuntimeError(
                    "bus operations did not stop; retaining its database lock"
                )
        for executor in self.executors:
            executor.shutdown(wait=False, cancel_futures=False)

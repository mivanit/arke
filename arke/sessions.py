"""Durable conversations, independent of HTTP, tmux, and the agent bus.

One process owns a database. One coroutine owns each native connection. Browser
connections merely observe it. Only explicit resume releases recovered queues.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import inspect
import json
import logging
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .harnesses import Adapter, Event, Launch
from .harnesses.base import native_data_paths
from .runner import Runner

INTERRUPT_TIMEOUT = 30.0
INTERRUPT_COMPLETION_TIMEOUT = 15.0
SHUTDOWN_GRACE = 5.0
CLEANUP_TIMEOUT = 30.0
CANCEL_GRACE = 2.0


class Conflict(ValueError):
    """An operation targets stale state or an unresolved outcome."""


@dataclass
class Runtime:
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    adapter: Adapter | None = None
    active: str | None = None
    started: bool = False
    redirect: str | None = None
    interrupting: bool = False
    control: asyncio.Task[None] | None = None
    cleanup: asyncio.Task[None] | None = None
    turn_done: asyncio.Event = field(default_factory=asyncio.Event)
    closing: bool = False
    stopping: bool = False
    generation: str = field(default_factory=lambda: uuid.uuid4().hex)
    approvals: dict[str, dict[str, Any]] = field(default_factory=dict)
    preview: str = ""
    revision: int = 0


class Conversations:
    def __init__(
        self,
        path: Path,
        profiles: dict[str, Launch],
        factory: Callable[[Launch], Adapter] = Runner,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_paths = (
            path.resolve(),
            path.with_suffix(".lock").resolve(),
            Path(str(path) + "-wal").resolve(),
            Path(str(path) + "-shm").resolve(),
        )
        self._lock = path.with_suffix(".lock").open("a+")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            raise RuntimeError(f"another conversation manager owns {path}") from None
        try:
            self.db = sqlite3.connect(path)
            path.chmod(0o600)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, profile TEXT NOT NULL,
                    harness TEXT NOT NULL, cwd TEXT NOT NULL,
                    native_id TEXT, paused INTEGER NOT NULL, state TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
                    runner TEXT NOT NULL DEFAULT 'local', native_home TEXT, native_roots TEXT
                );
                CREATE TABLE IF NOT EXISTS requests (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, conversation TEXT NOT NULL,
                    id TEXT NOT NULL, text TEXT NOT NULL, mode TEXT NOT NULL,
                    origin TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL,
                    UNIQUE(conversation, id)
                );
                CREATE TABLE IF NOT EXISTS history (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, conversation TEXT NOT NULL,
                    request TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS request_queue ON requests(conversation, status, seq);
                CREATE INDEX IF NOT EXISTS history_page ON history(conversation, seq);
            """)
            with self.db:
                columns = {
                    row[1]
                    for row in self.db.execute("PRAGMA table_info(conversations)")
                }
                if "runner" not in columns:
                    self.db.execute(
                        "ALTER TABLE conversations ADD COLUMN runner TEXT NOT NULL DEFAULT 'local'"
                    )
                if "native_home" not in columns:
                    self.db.execute(
                        "ALTER TABLE conversations ADD COLUMN native_home TEXT"
                    )
                if "native_roots" not in columns:
                    self.db.execute(
                        "ALTER TABLE conversations ADD COLUMN native_roots TEXT"
                    )
                self.db.execute(
                    "UPDATE requests SET status='uncertain' WHERE status IN ('starting', 'running')"
                )
                self.db.execute("UPDATE conversations SET paused=1, state='paused'")
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            self._lock.close()
            raise
        self.profiles = profiles
        self.factory = factory
        self.prepare_launch: Callable[[str, Launch], Launch | Awaitable[Launch]] = (
            lambda cid, launch: launch
        )
        self.runtime: dict[str, Runtime] = {}
        self.closed = False
        self._close_task: asyncio.Task[None] | None = None

    def _row(self, cid: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM conversations WHERE id=?", (cid,)
        ).fetchone()
        if row is None:
            raise KeyError("conversation not found")
        return dict(row)

    def _rt(self, cid: str) -> Runtime:
        self._row(cid)
        return self.runtime.setdefault(cid, Runtime())

    def _change(self, cid: str, **fields: Any) -> None:
        if fields:
            with self.db:
                self.db.execute(
                    "UPDATE conversations SET "
                    + ",".join(k + "=?" for k in fields)
                    + " WHERE id=?",
                    (*fields.values(), cid),
                )
        rt = self._rt(cid)
        rt.revision += 1
        rt.changed.set()
        rt.changed = asyncio.Event()

    def create(self, profile: str, name: str = "") -> str:
        if self.closed:
            raise Conflict("manager is stopping")
        with self.db:
            return self._create(profile, name)

    def _create(self, profile: str, name: str) -> str:
        if self.closed:
            raise Conflict("manager is stopping")
        if profile not in self.profiles:
            raise ValueError("unknown launch profile")
        if not self.profiles[profile].cwd.is_dir():
            raise ValueError("workspace does not exist")
        if len(name) > 100:
            raise ValueError("name must be at most 100 characters")
        cid = uuid.uuid4().hex
        launch = self.profiles[profile]
        self.db.execute(
            "INSERT INTO conversations (id,name,profile,harness,cwd,native_id,paused,state,error,created,runner,native_home,native_roots) "
            "VALUES (?, ?, ?, ?, ?, NULL, 0, 'idle', '', ?, ?, ?, ?)",
            (
                cid,
                name or profile,
                profile,
                launch.harness,
                str(launch.cwd.resolve()),
                time.time(),
                launch.runner,
                str(launch.native_home.resolve())
                if launch.native_home is not None
                else None,
                json.dumps([str(path.resolve()) for path in native_data_paths(launch)]),
            ),
        )
        return cid

    def get_or_create_shared(self, profile: str, name: str = "") -> str:
        """Sharing is an explicit persisted identity, never inferred from a profile."""
        if self.closed:
            raise Conflict("manager is stopping")
        with self.db:
            shared = self.db.execute(
                "SELECT value FROM metadata WHERE key='shared_conversation'"
            ).fetchone()
            if shared:
                row = self._row(shared[0])
                launch = self.profiles.get(profile)
                if (
                    row["profile"] != profile
                    or launch is None
                    or not self._matches_launch(row, launch)
                ):
                    raise ValueError(
                        "the shared conversation's profile, workspace, or execution binding changed; restore its configuration or use a fresh state directory"
                    )
                return shared[0]
            cid = self._create(profile, name)
            self.db.execute(
                "INSERT INTO metadata(key,value) VALUES ('shared_conversation',?)",
                (cid,),
            )
            return cid

    @staticmethod
    def _matches_launch(row: dict[str, Any], launch: Launch) -> bool:
        same_binding = (
            row["harness"],
            row["cwd"],
            row["runner"],
            row["native_home"],
        ) == (
            launch.harness,
            str(launch.cwd.resolve()),
            launch.runner,
            str(launch.native_home.resolve())
            if launch.native_home is not None
            else None,
        )

        if not same_binding:
            return False
        # Old rows did not record environment-selected native roots. Preserve the
        # known binding without inventing historical locations during migration.
        return row["native_roots"] is None or json.loads(row["native_roots"]) == [
            str(path.resolve()) for path in native_data_paths(launch)
        ]

    def _launch(self, row: dict[str, Any]) -> Launch:
        launch = self.profiles.get(row["profile"])
        if launch is None:
            raise ValueError("launch profile was removed; restore it before resuming")
        if not self._matches_launch(row, launch):
            raise ValueError(
                "this conversation's harness, workspace, or execution binding changed; restore its profile or create a fresh conversation"
            )
        return launch

    def list_conversations(self) -> list[dict[str, Any]]:
        return [
            self.snapshot(row[0], history_limit=0)
            for row in self.db.execute("SELECT id FROM conversations ORDER BY created")
        ]

    def history(
        self, cid: str, before: int | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        self._row(cid)
        rows = self.db.execute(
            "SELECT * FROM history WHERE conversation=? AND seq<? ORDER BY seq DESC LIMIT ?",
            (cid, before if before is not None else 2**63 - 1, min(max(limit, 0), 200)),
        ).fetchall()
        return [dict(row) | {"data": json.loads(row["data"])} for row in reversed(rows)]

    def snapshot(self, cid: str, history_limit: int = 100) -> dict[str, Any]:
        row = self._row(cid)
        # Native references and launch arguments never cross the browser API.
        row.pop("native_id")
        row.pop("runner")
        row.pop("native_home")
        row.pop("native_roots")
        rt = self._rt(cid)
        launch = self.profiles.get(row["profile"])
        return row | {
            "paused": bool(row["paused"]),
            "held": self.db.execute(
                "SELECT 1 FROM requests WHERE conversation=? AND status='held' LIMIT 1",
                (cid,),
            ).fetchone()
            is not None,
            "profile_available": launch is not None,
            "active": rt.active,
            "stopping": rt.stopping,
            "revision": f"{rt.generation}:{rt.revision}",
            "preview": rt.preview,
            "approvals": [
                {k: v for k, v in a.items() if k != "native_request"}
                for a in rt.approvals.values()
            ],
            "queue": [
                dict(r)
                for r in self.db.execute(
                    "SELECT id,text,status,origin,mode FROM requests WHERE conversation=? AND status IN ('queued','held','starting','running','uncertain') ORDER BY seq",
                    (cid,),
                )
            ],
            "history": self.history(cid, limit=history_limit),
        }

    def _record(self, cid: str, request: str, event: Event) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO history(conversation,request,kind,data,created) VALUES (?,?,?,?,?)",
                (cid, request, event.kind, json.dumps(event.data), time.time()),
            )

    def submit(
        self,
        cid: str,
        request_id: str,
        text: str,
        *,
        redirect: bool = False,
        target: str | None = None,
        origin: str = "human",
    ) -> dict[str, Any]:
        if self.closed:
            raise Conflict("manager is stopping")
        self._row(cid)
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text.encode()) > 100_000
        ):
            raise ValueError("message must contain 1 to 100000 UTF-8 bytes")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
            raise ValueError("a stable request ID is required")
        mode = "redirect" if redirect else "send"
        old = self.db.execute(
            "SELECT * FROM requests WHERE conversation=? AND id=?", (cid, request_id)
        ).fetchone()
        if old:
            if (old["text"], old["mode"], old["origin"]) != (text, mode, origin):
                raise Conflict("request ID was already used for different content")
            return {"id": request_id, "status": old["status"]}
        rt = self._preflight(cid)
        if redirect and (
            not target
            or target != rt.active
            or rt.redirect
            or rt.interrupting
            or rt.stopping
        ):
            raise Conflict(
                "the targeted turn is no longer active or is already being interrupted"
            )
        if not redirect and (
            self.db.execute(
                "SELECT count(*) FROM requests WHERE conversation=? AND status IN ('queued','held')",
                (cid,),
            ).fetchone()[0]
            >= 100
        ):
            raise Conflict("queue is full; discard pending messages first")
        with self.db:
            if redirect:
                self.db.execute(
                    "UPDATE requests SET status='held' WHERE conversation=? AND status='queued'",
                    (cid,),
                )
            self.db.execute(
                "INSERT INTO requests(conversation,id,text,mode,origin,status,created) VALUES (?,?,?,?,?,'queued',?)",
                (cid, request_id, text, mode, origin, time.time()),
            )
        # The commit above is the acceptance boundary. A later scheduling
        # failure must not turn this durable request into a definite rejection.
        try:
            if redirect:
                rt.redirect = request_id
                if rt.started:
                    self._schedule_interrupt(cid)
            self._ensure_worker(cid)
            rt.wake.set()
            self._change(cid)
        except Exception as exc:  # noqa: BLE001 — retain accepted requests when execution scheduling fails
            rt.redirect = None
            self._change(
                cid,
                paused=1,
                state="paused",
                error=f"Message accepted, but execution could not be scheduled: {exc}",
            )
        return {"id": request_id, "status": "queued"}

    def _preflight(self, cid: str) -> Runtime:
        """Known rejection checks must run before changing durable request state."""
        rt = self._rt(cid)
        if (
            rt.cleanup is not None
            and rt.cleanup.done()
            and (rt.cleanup.cancelled() or rt.cleanup.exception() is not None)
        ):
            raise Conflict(
                "native cleanup failed; inspect execution before restarting the service"
            )
        return rt

    def _ensure_worker(self, cid: str) -> None:
        rt = self._rt(cid)
        if rt.stopping:
            # The retiring task still owns this runtime. Accepted input remains
            # paused; only explicit Resume after retirement may start it again.
            return
        if rt.task is None:
            rt.closing = False
            rt.cleanup = None
            coroutine = self._worker(cid)
            try:
                task = asyncio.create_task(coroutine)
            except BaseException:
                coroutine.close()
                raise
            rt.task = task
            task.add_done_callback(lambda done: self._worker_finished(cid, done))

    def _worker_finished(self, cid: str, task: asyncio.Task[None]) -> None:
        """Publish readiness only after this actor has relinquished ownership."""
        rt = self.runtime[cid]
        if rt.task is not task:
            return
        if not task.cancelled():
            task.exception()  # retrieve failures; cleanup below determines restart safety
        if rt.cleanup is not None and (
            not rt.cleanup.done()
            or rt.cleanup.cancelled()
            or rt.cleanup.exception() is not None
        ):
            # Retain the failed execution's ownership and error for inspection.
            rt.stopping = True
            self._change(cid)
            return
        rt.task = None
        rt.stopping = False
        rt.active = None
        rt.approvals.clear()
        rt.redirect = None
        rt.started = False
        rt.turn_done.set()
        self._change(cid, state="paused" if self._row(cid)["paused"] else "idle")

    def resume(self, cid: str) -> None:
        if self.closed:
            raise Conflict("manager is stopping")
        self._launch(self._row(cid))
        rt = self._preflight(cid)
        if rt.stopping:
            raise Conflict("execution is still stopping; wait for cleanup to finish")
        if rt.active or rt.interrupting:
            raise Conflict("wait for the active turn to finish")
        if self.db.execute(
            "SELECT 1 FROM requests WHERE conversation=? AND status='uncertain'", (cid,)
        ).fetchone():
            raise Conflict(
                "inspect the native conversation and resolve uncertain requests before resuming"
            )
        self._ensure_worker(cid)
        with self.db:
            self.db.execute(
                "UPDATE requests SET status='queued' WHERE conversation=? AND status='held'",
                (cid,),
            )
        self._change(cid, paused=0, state="idle", error="")
        rt.wake.set()

    def discard(self, cid: str, request_id: str) -> None:
        self._row(cid)
        rt = self._rt(cid)
        with self.db:
            result = self.db.execute(
                "UPDATE requests SET status='discarded' WHERE conversation=? AND id=? AND status IN ('queued','held')",
                (cid, request_id),
            )
        if not result.rowcount:
            raise Conflict("only queued or held messages can be discarded")
        if rt.redirect == request_id:
            rt.redirect = None
        self._change(cid)

    def resolve(self, cid: str, request_id: str) -> None:
        """Owner acknowledges an unknown outcome after inspecting native history; never retry it."""
        self._row(cid)
        with self.db:
            result = self.db.execute(
                "UPDATE requests SET status='resolved' WHERE conversation=? AND id=? AND status='uncertain'",
                (cid, request_id),
            )
        if not result.rowcount:
            raise Conflict("request is not uncertain")
        self._record(
            cid,
            request_id,
            Event(
                "notice",
                {
                    "text": "Owner resolved the uncertain outcome; request will not be replayed."
                },
            ),
        )
        self._change(cid)

    def _schedule_interrupt(self, cid: str) -> None:
        rt = self._rt(cid)
        if rt.interrupting:
            return
        rt.interrupting = True
        self._change(cid, state="interrupting")
        coroutine = self._interrupt(cid)
        try:
            rt.control = asyncio.create_task(coroutine)
        except BaseException:
            coroutine.close()
            rt.interrupting = False
            raise

    async def _interrupt(self, cid: str) -> None:
        rt = self._rt(cid)
        try:
            if rt.turn_done.is_set():
                return
            assert rt.adapter is not None
            await asyncio.wait_for(rt.adapter.interrupt(), INTERRUPT_TIMEOUT)
            # A native ACK only confirms receipt. Do not run the redirect until
            # the old turn iterator has ended, including its transport cleanup.
            await asyncio.wait_for(rt.turn_done.wait(), INTERRUPT_COMPLETION_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 — preserve uncertain native outcomes, never replay
            if rt.turn_done.is_set():
                return
            rt.redirect = None
            self._uncertain(
                cid, f"Could not confirm interruption: {exc or type(exc).__name__}"
            )
            if rt.task is not None:
                rt.task.cancel()
        finally:
            rt.interrupting = False
            rt.wake.set()
            self._change(cid)

    async def answer(self, cid: str, approval_id: str, answer: dict[str, Any]) -> None:
        rt = self._rt(cid)
        approval = rt.approvals.get(approval_id)
        if (
            approval is None
            or approval["request"] != rt.active
            or rt.adapter is None
            or rt.interrupting
            or rt.stopping
            or self.closed
        ):
            raise Conflict("this approval is no longer pending")
        # Remove before awaiting native I/O: duplicate clicks cannot approve twice.
        rt.approvals.pop(approval_id)
        try:
            await rt.adapter.respond(approval["native_request"], answer)
        except Exception:
            if rt.active == approval["request"]:
                rt.approvals[approval_id] = approval
            raise
        finally:
            self._change(cid)

    async def _worker(self, cid: str) -> None:
        rt = self._rt(cid)
        try:
            while not rt.closing:
                rt.wake.clear()
                row = self._row(cid)
                job = None
                if not rt.interrupting:
                    if rt.redirect:
                        job = self.db.execute(
                            "SELECT * FROM requests WHERE conversation=? AND id=? AND status='queued'",
                            (cid, rt.redirect),
                        ).fetchone()
                        rt.redirect = None
                    elif not row["paused"]:
                        job = self.db.execute(
                            "SELECT * FROM requests WHERE conversation=? AND status='queued' ORDER BY seq LIMIT 1",
                            (cid,),
                        ).fetchone()
                if job is None:
                    await rt.wake.wait()
                    continue
                rt.active, rt.started = job["id"], False
                rt.turn_done.clear()
                rt.preview = ""
                with self.db:
                    self.db.execute(
                        "UPDATE requests SET status='starting' WHERE conversation=? AND id=?",
                        (cid, rt.active),
                    )
                self._change(cid, state="starting", error="")
                launch = self._launch(row)
                if rt.adapter is None:
                    prepared = self.prepare_launch(cid, launch)
                    launch = (
                        await prepared if inspect.isawaitable(prepared) else prepared
                    )
                    launch = replace(
                        launch,
                        protected_paths=tuple(
                            dict.fromkeys((*launch.protected_paths, *self.state_paths))
                        ),
                    )
                    rt.adapter = self.factory(launch)
                    native_id = await rt.adapter.open(row["native_id"])
                    self._change(cid, native_id=native_id)
                self._record(
                    cid,
                    rt.active,
                    Event("user", {"text": job["text"], "origin": job["origin"]}),
                )
                # Mark before crossing the native write boundary. A lost acknowledgement is not a safe retry.
                with self.db:
                    self.db.execute(
                        "UPDATE requests SET status='running' WHERE conversation=? AND id=?",
                        (cid, rt.active),
                    )
                terminal = False
                async for event in rt.adapter.run(job["text"]):
                    if event.kind == "started":
                        rt.started = True
                        self._change(cid, state="running")
                        if rt.redirect or rt.closing:
                            self._schedule_interrupt(cid)
                    elif event.kind == "session":
                        self._change(cid, native_id=event.data["native_id"])
                    elif event.kind == "delta":
                        rt.preview = (rt.preview + event.data.get("text", ""))[
                            -100_000:
                        ]
                    elif event.kind == "approval":
                        aid = uuid.uuid4().hex
                        rt.approvals[aid] = {
                            "id": aid,
                            "request": rt.active,
                            "native_request": event.data["request_id"],
                            "kind": event.data["kind"],
                            "details": event.data["details"],
                        }
                    elif event.kind == "approval_resolved":
                        rt.approvals = {
                            k: v
                            for k, v in rt.approvals.items()
                            if v["native_request"] != event.data["request_id"]
                        }
                    elif event.kind == "completed":
                        terminal = True
                        status = event.data.get("status", "failed")
                        if status not in ("completed", "interrupted"):
                            status = "failed"
                            # Claude reports interruption as an error result. A pending explicit redirect may proceed.
                            if not rt.redirect:
                                self._change(cid, paused=1)
                        with self.db:
                            self.db.execute(
                                "UPDATE requests SET status=? WHERE conversation=? AND id=?",
                                (status, cid, rt.active),
                            )
                        self._record(cid, rt.active, event)
                    else:
                        self._record(cid, rt.active, event)
                        if event.kind == "message":
                            rt.preview = ""
                    self._change(cid)
                if not terminal:
                    raise ConnectionError("native stream ended without a turn result")
                rt.turn_done.set()
                rt.active, rt.started, rt.preview = None, False, ""
                rt.approvals.clear()
                self._change(
                    cid, state="paused" if self._row(cid)["paused"] else "idle"
                )
        except asyncio.CancelledError:
            self._uncertain(
                cid,
                self._row(cid)["error"]
                or "Execution stopped during the turn; inspect its outcome before resuming.",
            )
            raise
        except Exception as exc:  # noqa: BLE001 — preserve uncertain native outcomes, never replay
            self._uncertain(cid, str(exc))
        finally:
            rt.stopping = True
            self._change(cid)
            rt.cleanup = asyncio.create_task(self._cleanup(cid))
            try:
                await asyncio.shield(rt.cleanup)
            except asyncio.CancelledError:
                # Cancellation during cleanup must not abandon the child process.
                await asyncio.shield(rt.cleanup)
                raise

    async def _cleanup(self, cid: str) -> None:
        rt = self._rt(cid)
        if rt.control is not None and not rt.control.done():
            rt.control.cancel()
            done, _ = await asyncio.wait({rt.control}, timeout=CANCEL_GRACE)
            if not done:
                raise RuntimeError(
                    "interrupt task did not stop; database ownership retained"
                )
            with contextlib.suppress(asyncio.CancelledError):
                rt.control.result()
        if rt.adapter is not None:
            closing = asyncio.create_task(rt.adapter.close())
            done, _ = await asyncio.wait({closing}, timeout=CLEANUP_TIMEOUT)
            if not done:
                closing.cancel()
                done, _ = await asyncio.wait({closing}, timeout=CANCEL_GRACE)
            if not done:
                # A broken injected adapter must not let us release the database
                # lock while its cleanup is still alive. Production runners own
                # forced process termination and must satisfy this close contract.
                raise RuntimeError(
                    "adapter cleanup did not stop; database ownership retained"
                )
            try:
                closing.result()
            except asyncio.CancelledError:
                self._change(
                    cid, paused=1, error="Native cleanup exceeded its deadline"
                )
                raise RuntimeError(
                    "adapter cleanup timed out; database ownership retained"
                ) from None
            except Exception:
                logging.getLogger(__name__).exception(
                    "closing native conversation %s", cid
                )
                self._change(
                    cid,
                    paused=1,
                    error="Native cleanup failed; inspect the execution process",
                )
                raise
        rt.adapter = None
        # The actor is still awaiting this cleanup task. Its done callback owns
        # clearing active state and publishing readiness for a replacement.

    def _uncertain(self, cid: str, error: str) -> None:
        rt = self._rt(cid)
        if rt.active:
            with self.db:
                # Failures before submission are safe to leave queued; failures after it require review.
                self.db.execute(
                    "UPDATE requests SET status=CASE status WHEN 'starting' THEN 'queued' ELSE 'uncertain' END WHERE conversation=? AND id=? AND status IN ('starting','running')",
                    (cid, rt.active),
                )
        self._change(cid, paused=1, state="paused", error=error)

    async def close(self) -> None:
        """Close once; cancellation of one caller does not abandon owned resources."""
        if self._close_task is None:
            self.closed = True
            self._close_task = asyncio.create_task(self._close())
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            # The enclosing service must not close its bus while native workers
            # are still shutting down. Finish owned cleanup before cancellation
            # is allowed to unwind into the next resource's finally block.
            await asyncio.shield(self._close_task)
            raise

    async def _close(self) -> None:
        tasks = []
        for cid, rt in self.runtime.items():
            rt.closing = True
            rt.wake.set()
            if rt.active and rt.started:
                self._schedule_interrupt(cid)
            if rt.task is not None:
                tasks.append(rt.task)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_GRACE)
            for task in pending:
                task.cancel()
            _, pending = await asyncio.wait(
                tasks, timeout=CLEANUP_TIMEOUT + CANCEL_GRACE + 1
            )
            if pending:
                raise RuntimeError(
                    "conversation tasks did not stop; database ownership retained"
                )
            await asyncio.gather(*tasks, return_exceptions=True)
        # Failed cleanup retains ownership rather than allowing another manager
        # to resume a native session whose execution may still be alive.
        for rt in self.runtime.values():
            if rt.cleanup is not None:
                rt.cleanup.result()
            rt.changed.set()
        self.db.close()
        self._lock.close()

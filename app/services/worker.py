from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

log = logging.getLogger(__name__)

JobKind = Literal["upload", "refresh", "convert", "send", "remove"]
JobState = Literal["queued", "running", "done", "failed"]
BookState = Literal["pending", "running", "done", "skipped", "failed"]

MAX_JOB_HISTORY = 100


@dataclass
class BookProgress:
    book_id: int
    title: str
    state: BookState = "pending"
    message: str | None = None


@dataclass
class Job:
    id: str
    kind: JobKind
    state: JobState
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    book_ids: list[int]
    params: dict
    progress: list[BookProgress] = field(default_factory=list)
    summary: str | None = None


JobHandler = Callable[[Job], Awaitable[None]]


def _now() -> datetime:
    return datetime.now(UTC)


class Worker:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._handlers: dict[JobKind, JobHandler] = {}
        self._task: asyncio.Task | None = None
        self._stopping = False

    def register_handler(self, kind: JobKind, handler: JobHandler) -> None:
        self._handlers[kind] = handler

    def enqueue(self, kind: JobKind, book_ids: list[int], params: dict | None = None) -> Job:
        job = Job(
            id=uuid.uuid4().hex,
            kind=kind,
            state="queued",
            created_at=_now(),
            started_at=None,
            finished_at=None,
            book_ids=list(book_ids),
            params=dict(params or {}),
            progress=[BookProgress(book_id=b, title="") for b in book_ids],
        )
        self._jobs[job.id] = job
        self._evict_terminal()
        self._queue.put_nowait(job)
        return job

    def _evict_terminal(self) -> None:
        """Evict the oldest done/failed jobs to keep _jobs within cap.

        Never evicts queued or running jobs — those are still visible to /jobs
        and must survive history rotation until they reach a terminal state.
        """
        excess = len(self._jobs) - MAX_JOB_HISTORY
        if excess <= 0:
            return
        terminal = ("done", "failed")
        for job_id in list(self._jobs.keys()):
            if excess <= 0:
                break
            if self._jobs[job_id].state in terminal:
                del self._jobs[job_id]
                excess -= 1
        # If we still exceed cap, every entry is non-terminal — log and accept.
        if len(self._jobs) > MAX_JOB_HISTORY:
            log.warning(
                "job history at %d (cap %d) — all active, eviction deferred",
                len(self._jobs), MAX_JOB_HISTORY,
            )

    def get_job(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list_jobs(self, limit: int = 100) -> list[Job]:
        return list(reversed(list(self._jobs.values())))[:limit]

    async def _run(self) -> None:
        while not self._stopping:
            try:
                job = await self._queue.get()
            except asyncio.CancelledError:
                break
            job.state = "running"
            job.started_at = _now()
            handler = self._handlers.get(job.kind)
            try:
                if handler is None:
                    raise RuntimeError(f"no handler for kind {job.kind!r}")
                await handler(job)
                job.state = "done"
            except Exception as exc:
                log.exception("job %s failed", job.id)
                job.state = "failed"
                job.summary = job.summary or f"{type(exc).__name__}: {exc}"
            finally:
                job.finished_at = _now()
                self._queue.task_done()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._run(), name="worker")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is None:
            return
        # Drain anything still queued so task_done counts balance; cancel the task.
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task
        self._task = None

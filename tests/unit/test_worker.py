from __future__ import annotations

import asyncio

import pytest

from app.services.worker import MAX_JOB_HISTORY, Worker


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("predicate did not become true")


async def test_strict_serial_execution():
    worker = Worker()
    events: list[tuple[str, str, float]] = []

    async def handler(job):
        events.append((job.id, "start", asyncio.get_running_loop().time()))
        await asyncio.sleep(0.05)
        events.append((job.id, "end", asyncio.get_running_loop().time()))

    worker.register_handler("upload", handler)
    worker.start()
    try:
        jobs = [worker.enqueue("upload", [i], {}) for i in range(3)]
        await _wait_until(lambda: all(j.state == "done" for j in jobs))
    finally:
        await worker.stop()

    # Build the expected interleave: j1 start/end, j2 start/end, j3 start/end
    job_ids = [j.id for j in jobs]
    seq = [(jid, phase) for jid, phase, _t in events]
    assert seq == [
        (job_ids[0], "start"),
        (job_ids[0], "end"),
        (job_ids[1], "start"),
        (job_ids[1], "end"),
        (job_ids[2], "start"),
        (job_ids[2], "end"),
    ]
    # And there is no overlap: each end precedes the next start.
    times = {(jid, phase): t for jid, phase, t in events}
    assert times[(job_ids[0], "end")] <= times[(job_ids[1], "start")]
    assert times[(job_ids[1], "end")] <= times[(job_ids[2], "start")]


async def test_state_transitions_observable():
    worker = Worker()
    saw_running = asyncio.Event()
    finish = asyncio.Event()

    async def handler(_job):
        saw_running.set()
        await finish.wait()

    worker.register_handler("upload", handler)
    worker.start()
    try:
        job = worker.enqueue("upload", [1], {})
        assert job.state == "queued"

        await asyncio.wait_for(saw_running.wait(), timeout=1.0)
        assert worker.get_job(job.id).state == "running"
        assert worker.get_job(job.id).started_at is not None

        finish.set()
        await _wait_until(lambda: worker.get_job(job.id).state == "done")
        assert worker.get_job(job.id).finished_at is not None
    finally:
        finish.set()
        await worker.stop()


async def test_handler_exception_marks_failed_and_keeps_running():
    worker = Worker()

    async def handler(job):
        if job.params.get("crash"):
            raise RuntimeError("boom")

    worker.register_handler("upload", handler)
    worker.start()
    try:
        bad = worker.enqueue("upload", [1], {"crash": True})
        good = worker.enqueue("upload", [2], {})
        await _wait_until(lambda: bad.state == "failed" and good.state == "done")
    finally:
        await worker.stop()

    assert "RuntimeError" in (bad.summary or "")
    assert good.state == "done"


def test_fifo_eviction_only_evicts_terminal_jobs():
    """Running and queued jobs must survive history rotation."""
    worker = Worker()
    ids = [worker.enqueue("upload", [i], {}).id for i in range(MAX_JOB_HISTORY + 5)]

    # Nothing terminal yet → cap is exceeded but no eviction.
    assert len(worker._jobs) == MAX_JOB_HISTORY + 5
    for kept in ids:
        assert worker.get_job(kept) is not None

    # Mark the oldest 10 as done. Next enqueue exceeds cap by 6 (106 total) and
    # should evict the 6 oldest terminal entries — leaving 100.
    for old in ids[:10]:
        worker.get_job(old).state = "done"

    new_job = worker.enqueue("upload", [999], {})

    assert len(worker._jobs) == MAX_JOB_HISTORY  # cap exactly satisfied
    for evicted in ids[:6]:
        assert worker.get_job(evicted) is None
    for kept in ids[6:]:
        assert worker.get_job(kept) is not None
    assert worker.get_job(new_job.id) is not None


def test_fifo_eviction_defers_when_all_active():
    """If every entry is queued/running, allow exceeding the cap rather than evict live jobs."""
    worker = Worker()
    ids = [worker.enqueue("upload", [i], {}).id for i in range(MAX_JOB_HISTORY + 3)]

    assert len(worker._jobs) == MAX_JOB_HISTORY + 3
    for kept in ids:
        assert worker.get_job(kept) is not None


async def test_per_book_progress_visible_during_run():
    worker = Worker()
    proceed = asyncio.Event()
    halfway = asyncio.Event()

    async def handler(job):
        for i, bp in enumerate(job.progress):
            bp.state = "running"
            bp.message = f"working on {bp.book_id}"
            if i == 0:
                halfway.set()
                await proceed.wait()
            bp.state = "done"

    worker.register_handler("upload", handler)
    worker.start()
    try:
        job = worker.enqueue("upload", [10, 11], {})
        await asyncio.wait_for(halfway.wait(), timeout=1.0)
        snapshot = worker.get_job(job.id)
        assert snapshot.progress[0].state == "running"
        assert snapshot.progress[0].message == "working on 10"

        proceed.set()
        await _wait_until(lambda: worker.get_job(job.id).state == "done")
    finally:
        proceed.set()
        await worker.stop()


def test_list_jobs_returns_most_recent_first():
    worker = Worker()
    jobs = [worker.enqueue("upload", [i], {}) for i in range(3)]

    listed = worker.list_jobs(limit=10)

    assert [j.id for j in listed] == [j.id for j in reversed(jobs)]


@pytest.mark.parametrize("kind", ["upload", "refresh", "convert", "send", "remove"])
async def test_missing_handler_fails_job_with_message(kind):
    worker = Worker()
    worker.start()
    try:
        job = worker.enqueue(kind, [1], {})
        await _wait_until(lambda: job.state == "failed")
        assert "no handler" in (job.summary or "")
    finally:
        await worker.stop()

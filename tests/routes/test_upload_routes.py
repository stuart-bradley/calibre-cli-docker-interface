from __future__ import annotations

import asyncio
import subprocess

from app.services import calibre_cli


async def _wait_for_job(client, kind: str, timeout: float = 2.0) -> dict:
    worker = client.app.state.worker
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for job in worker.list_jobs(100):
            if job.kind == kind and job.state in ("done", "failed"):
                return job
        await asyncio.sleep(0.02)
    raise AssertionError(f"no {kind} job completed within {timeout}s")


def test_upload_creates_a_single_job(client, monkeypatch):
    def fake_run(argv, *, input=None):
        return subprocess.CompletedProcess(argv, 0, "Added book ids: 42\n", "")

    monkeypatch.setattr(calibre_cli, "_run", fake_run)

    resp = client.post(
        "/upload",
        files=[
            ("files", ("a.epub", b"epub-bytes-a", "application/epub+zip")),
            ("files", ("b.epub", b"epub-bytes-b", "application/epub+zip")),
            ("files", ("c.epub", b"epub-bytes-c", "application/epub+zip")),
        ],
    )

    assert resp.status_code == 200  # followed redirect to /jobs
    jobs = client.app.state.worker.list_jobs(100)
    upload_jobs = [j for j in jobs if j.kind == "upload"]
    assert len(upload_jobs) == 1
    assert len(upload_jobs[0].params["files"]) == 3


def test_upload_reports_duplicates_in_summary(client, monkeypatch):
    """Duplicates are signalled via stdout at exit 0 — must NOT branch on exit code."""

    call_count = {"n": 0}

    def fake_run(argv, *, input=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return subprocess.CompletedProcess(argv, 0, "Added book ids: 42\n", "")
        return subprocess.CompletedProcess(
            argv, 0,
            f"{calibre_cli.DUPLICATE_MARKER}\n/tmp/dup.epub\n",
            "",
        )

    monkeypatch.setattr(calibre_cli, "_run", fake_run)

    client.post("/upload", files=[
        ("files", ("a.epub", b"a", "application/epub+zip")),
        ("files", ("dup.epub", b"dup", "application/epub+zip")),
    ])

    # Poll the worker until the upload job finishes.
    import time
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        jobs = [j for j in client.app.state.worker.list_jobs(100) if j.kind == "upload"]
        if jobs and jobs[0].state in ("done", "failed"):
            job = jobs[0]
            assert job.state == "done"
            assert "duplicates 1" in (job.summary or "")
            return
        time.sleep(0.02)
    raise AssertionError("upload job did not complete in time")

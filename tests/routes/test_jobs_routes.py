from __future__ import annotations

import time


def _wait_done(client, kind: str, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for j in client.app.state.worker.list_jobs(100):
            if j.kind == kind and j.state in ("done", "failed"):
                return j
        time.sleep(0.02)
    raise AssertionError(f"no {kind} job completed in time")


def test_batch_creates_one_job_with_all_book_ids(client):
    resp = client.post(
        "/batch/refresh?mode=fill_blanks",
        data={"book_id": ["1", "2", "3"]},
    )

    assert resp.status_code == 200
    refresh_jobs = [j for j in client.app.state.worker.list_jobs(100) if j.kind == "refresh"]
    assert len(refresh_jobs) == 1
    assert refresh_jobs[0].book_ids == [1, 2, 3]
    assert refresh_jobs[0].params == {"mode": "fill_blanks"}


def test_batch_refresh_rejects_invalid_mode(client):
    resp = client.post(
        "/batch/refresh?mode=bogus",
        data={"book_id": ["1"]},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_batch_convert_lifecycle(client, monkeypatch):
    """Mock convert_book → done; assert job transitions queued → done."""
    import subprocess

    from app.services import calibre_cli

    def fake_run(argv, *, input=None):
        # ebook-convert is invoked with src + dst; create dst so add_format step passes.
        if argv[0] == "ebook-convert":
            from pathlib import Path
            Path(argv[2]).write_bytes(b"converted")
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(calibre_cli, "_run", fake_run)

    resp = client.post(
        "/batch/convert?target=AZW3",
        data={"book_id": ["3"]},  # A Wizard of Earthsea, has EPUB + MOBI
    )
    assert resp.status_code == 200

    job = _wait_done(client, "convert")
    assert job.state == "done"
    assert len(job.book_ids) == 1


def test_jobs_listing_page(client):
    client.post("/batch/refresh?mode=fill_blanks", data={"book_id": ["1"]})

    resp = client.get("/jobs")

    assert resp.status_code == 200
    assert "refresh" in resp.text


def test_job_fragment_returns_html(client):
    resp = client.post(
        "/batch/refresh?mode=fill_blanks",
        data={"book_id": ["1"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    job = client.app.state.worker.list_jobs(1)[0]
    frag = client.get(f"/jobs/{job.id}/fragment")

    assert frag.status_code == 200
    assert "<tr" in frag.text
    assert "refresh" in frag.text

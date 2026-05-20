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
        "/batch/refresh",
        data={"book_id": ["1", "2", "3"], "mode": "fill_blanks"},
    )

    assert resp.status_code == 200
    refresh_jobs = [j for j in client.app.state.worker.list_jobs(100) if j.kind == "refresh"]
    assert len(refresh_jobs) == 1
    assert refresh_jobs[0].book_ids == [1, 2, 3]
    assert refresh_jobs[0].params == {"mode": "fill_blanks", "fetch_covers": True}


def test_batch_refresh_default_fetch_covers_is_true(client):
    """Backwards-compat: omitting fetch_covers defaults to True."""
    resp = client.post(
        "/batch/refresh",
        data={"book_id": ["1"], "mode": "fill_blanks"},
    )
    assert resp.status_code == 200
    job = [j for j in client.app.state.worker.list_jobs(100) if j.kind == "refresh"][0]
    assert job.params["fetch_covers"] is True


def test_batch_refresh_with_overwrite_and_no_covers(client):
    resp = client.post(
        "/batch/refresh",
        data={"book_id": ["1"], "mode": "overwrite", "fetch_covers": "false"},
    )
    assert resp.status_code == 200
    job = [j for j in client.app.state.worker.list_jobs(100) if j.kind == "refresh"][0]
    assert job.params == {"mode": "overwrite", "fetch_covers": False}


def test_batch_refresh_rejects_invalid_mode(client):
    resp = client.post(
        "/batch/refresh",
        data={"book_id": ["1"], "mode": "bogus"},
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
        "/batch/convert",
        data={"book_id": ["3"], "target": "AZW3"},  # A Wizard of Earthsea, has EPUB + MOBI
    )
    assert resp.status_code == 200

    job = _wait_done(client, "convert")
    assert job.state == "done"
    assert len(job.book_ids) == 1


def test_convert_dialog_excludes_targets_present_in_all_books(client):
    """Books 1 and 2 (both have EPUB) → EPUB should be excluded; AZW3 + MOBI
    available (book 2 is EPUB-only, book 1 has EPUB+AZW3 → MOBI missing from
    both; AZW3 missing from book 2)."""
    resp = client.post(
        "/batch/convert/dialog",
        data={"book_id": ["1", "2"]},
    )
    assert resp.status_code == 200
    html = resp.text
    assert 'value="AZW3"' in html
    assert 'value="MOBI"' in html
    assert 'value="EPUB"' not in html


def test_convert_dialog_marks_will_skip_and_will_convert(client):
    """Book 1 has EPUB+AZW3, book 2 has EPUB only. For target AZW3:
    book 1 = will skip, book 2 = will convert."""
    resp = client.post(
        "/batch/convert/dialog",
        data={"book_id": ["1", "2"]},
    )
    assert resp.status_code == 200
    azw3_table_start = resp.text.index('data-target="AZW3"')
    azw3_section = resp.text[azw3_table_start : azw3_table_start + 2000]
    assert "Children of Time" in azw3_section
    assert "already has AZW3" in azw3_section
    assert "will convert" in azw3_section


def test_convert_dialog_empty_state_when_all_formats_present(client, monkeypatch):
    """When every selected book has all three target formats, the dialog should
    render NO form and NO submit button — just an empty-state message."""
    from dataclasses import replace

    from app.services import db as real_db

    real_get_book = real_db.get_book

    def get_book_all_formats(library_path, book_id):
        book = real_get_book(library_path, book_id)
        if book is None:
            return None
        return replace(book, formats=["EPUB", "AZW3", "MOBI"])

    monkeypatch.setattr("app.routes.jobs.db.get_book", get_book_all_formats)

    resp = client.post(
        "/batch/convert/dialog",
        data={"book_id": ["1", "2"]},
    )
    assert resp.status_code == 200
    assert "All selected books already have all available target formats" in resp.text
    assert '<form method="post" action="/batch/convert"' not in resp.text
    assert 'type="submit"' not in resp.text


def test_jobs_listing_page(client):
    client.post("/batch/refresh", data={"book_id": ["1"], "mode": "fill_blanks"})

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

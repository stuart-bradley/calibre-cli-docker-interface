"""End-to-end handler tests against the fake MTP backend.

These drive the real handler (``handle_send`` / ``handle_remove``) via the
batch routes, using a synthesised MOBI plus a real JPEG cover so the full
chain runs:

    POST /batch/send → worker → handle_send →
        calibre_cli.read_mobi_identity (parses EXTH 113/501) →
        calibre_cli.make_kindle_thumbnail (Pillow resize) →
        mtp_helper.send + mtp_helper.send_thumbnail →
        FakeMtpBackend (records to in-memory tree)

The fake device tree is the assertion surface — we inspect what would have
landed on the Kindle.
"""

from __future__ import annotations

import time
from io import BytesIO
from pathlib import Path

from PIL import Image

from app.services import mtp_helper
from tests.fakes.books import make_minimal_mobi


def _wait_for_job(client, kind: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = [j for j in client.app.state.worker.list_jobs(100) if j.kind == kind]
        if jobs and jobs[0].state in ("done", "failed"):
            return jobs[0]
        time.sleep(0.02)
    raise AssertionError(f"{kind} job did not complete within {timeout}s")


def _prefer_azw3(client) -> None:
    """Put AZW3 first in device_format_order so handle_send picks it and
    skips the on-the-fly EPUB→AZW3 conversion (which would invoke real
    Calibre)."""
    order = client.app.state.settings.device_format_order
    order.clear()
    order.extend(["AZW3", "MOBI", "EPUB", "PDF"])


def _replace_book_1_with_valid_mobi(
    library: Path, uuid: str, cdetype: str = "EBOK"
) -> Path:
    """Overwrite the fixture's 4-byte stub AZW3 with a real MOBI that
    ``read_mobi_identity`` can parse. Returns the path."""
    p = library / "Adrian Tchaikovsky" / "Children of Time (1)" / "Children of Time.azw3"
    p.write_bytes(make_minimal_mobi(uuid, cdetype))
    return p


def _replace_book_1_cover_with_real_jpeg(library: Path) -> Path:
    p = library / "Adrian Tchaikovsky" / "Children of Time (1)" / "cover.jpg"
    buf = BytesIO()
    Image.new("RGB", (400, 600), color=(120, 80, 40)).save(buf, format="JPEG")
    p.write_bytes(buf.getvalue())
    return p


def test_handle_send_uploads_book_and_sidecar_thumbnail(client, fake_mtp_backend, library):
    """Sending an AZW3-with-EXTH book uploads both the book file and the
    sidecar thumbnail to the fake device."""
    _prefer_azw3(client)
    _replace_book_1_with_valid_mobi(library, uuid="cot-test-uuid-001")
    _replace_book_1_cover_with_real_jpeg(library)

    resp = client.post("/batch/send", data={"book_id": ["1"]})
    assert resp.status_code == 200

    job = _wait_for_job(client, "send")
    assert job.state == "done"
    assert all(p.state == "done" for p in job.progress), job.progress

    device = fake_mtp_backend.devices[0]
    docs_id = device.find_by_name(mtp_helper._MTP_PARENT_ROOT, "documents")
    thumbs_id = device.find_by_name(
        device.find_by_name(mtp_helper._MTP_PARENT_ROOT, "system"), "thumbnails"
    )

    assert "Children of Time.azw3" in device.filenames_in(docs_id)
    assert (
        "thumbnail_cot-test-uuid-001_EBOK_portrait.jpg"
        in device.filenames_in(thumbs_id)
    )

    # Optimistic update made the badge appear immediately.
    assert "Children of Time.azw3" in client.app.state.device_state.on_device_filenames


def test_handle_remove_clears_book_and_sidecar_thumbnail(client, fake_mtp_backend, library):
    """After a send, a remove deletes both the book file and the sidecar
    thumbnail, and clears the on_device_filenames entry."""
    _prefer_azw3(client)
    _replace_book_1_with_valid_mobi(library, uuid="cot-test-uuid-002")
    _replace_book_1_cover_with_real_jpeg(library)

    # First send the book.
    client.post("/batch/send", data={"book_id": ["1"]})
    _wait_for_job(client, "send")

    device = fake_mtp_backend.devices[0]
    docs_id = device.find_by_name(mtp_helper._MTP_PARENT_ROOT, "documents")
    thumbs_id = device.find_by_name(
        device.find_by_name(mtp_helper._MTP_PARENT_ROOT, "system"), "thumbnails"
    )
    assert "Children of Time.azw3" in device.filenames_in(docs_id)
    assert (
        "thumbnail_cot-test-uuid-002_EBOK_portrait.jpg"
        in device.filenames_in(thumbs_id)
    )

    # Now remove it.
    resp = client.post("/batch/remove", data={"book_id": ["1"]})
    assert resp.status_code == 200

    job = _wait_for_job(client, "remove")
    assert job.state == "done"
    assert all(p.state == "done" for p in job.progress), job.progress

    assert "Children of Time.azw3" not in device.filenames_in(docs_id)
    assert (
        "thumbnail_cot-test-uuid-002_EBOK_portrait.jpg"
        not in device.filenames_in(thumbs_id)
    )
    assert "Children of Time.azw3" not in client.app.state.device_state.on_device_filenames


def test_handle_send_continues_when_cover_is_invalid(client, fake_mtp_backend, library):
    """If the cover.jpg is unreadable, the book still uploads — thumbnail
    failure is best-effort and must not fail the send."""
    _prefer_azw3(client)
    _replace_book_1_with_valid_mobi(library, uuid="cot-no-cover-003")
    # Leave the cover.jpg as the 4-byte stub; Pillow will refuse to open it.

    client.post("/batch/send", data={"book_id": ["1"]})
    job = _wait_for_job(client, "send")

    assert job.state == "done"
    assert all(p.state == "done" for p in job.progress)

    device = fake_mtp_backend.devices[0]
    docs_id = device.find_by_name(mtp_helper._MTP_PARENT_ROOT, "documents")
    thumbs_id = device.find_by_name(
        device.find_by_name(mtp_helper._MTP_PARENT_ROOT, "system"), "thumbnails"
    )

    assert "Children of Time.azw3" in device.filenames_in(docs_id)
    # No thumbnail uploaded because the cover was unreadable.
    assert (
        "thumbnail_cot-no-cover-003_EBOK_portrait.jpg"
        not in device.filenames_in(thumbs_id)
    )


def test_handle_send_continues_when_backend_send_fails(client, fake_mtp_backend, library):
    """If the backend rejects the book send, the per-book progress marks
    failed but the job itself completes (other books would still try)."""
    _prefer_azw3(client)
    _replace_book_1_with_valid_mobi(library, uuid="cot-send-fails-004")
    _replace_book_1_cover_with_real_jpeg(library)
    fake_mtp_backend.fail_next_send = "simulated storage full"

    client.post("/batch/send", data={"book_id": ["1"]})
    job = _wait_for_job(client, "send")

    assert job.state == "done"
    assert job.progress[0].state == "failed"
    assert "storage full" in (job.progress[0].message or "")

    device = fake_mtp_backend.devices[0]
    docs_id = device.find_by_name(mtp_helper._MTP_PARENT_ROOT, "documents")
    assert "Children of Time.azw3" not in device.filenames_in(docs_id)
    # No optimistic update because the send raised.
    assert "Children of Time.azw3" not in client.app.state.device_state.on_device_filenames

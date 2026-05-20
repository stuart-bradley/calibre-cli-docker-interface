"""Worker job handlers — wired in main.py at app startup."""

from __future__ import annotations

import logging
from pathlib import Path

from app.config import Settings
from app.services import calibre_cli, db, mtp_helper, snapshot
from app.services.worker import Job, Worker

log = logging.getLogger(__name__)


def _snapshot(settings: Settings) -> None:
    snapshot.snapshot_if_needed(
        settings.library_path,
        settings.data_path,
        settings.snapshot_retention_days,
        tz=settings.tz,
    )


def _title(settings: Settings, book_id: int) -> str:
    book = db.get_book(settings.library_path, book_id)
    return book.title if book else f"<book {book_id}>"


def make_handlers(settings: Settings) -> dict:
    async def handle_upload(job: Job) -> None:
        _snapshot(settings)
        paths: list[Path] = [Path(p) for p in job.params.get("files", [])]
        added = 0
        duplicates = 0
        for path, bp in zip(paths, job.progress, strict=False):
            bp.title = path.name
            bp.state = "running"
            result = calibre_cli.add_book(settings.library_path, path)
            if result.added:
                bp.state = "done"
                bp.message = f"added id {result.book_id}"
                added += 1
            elif result.duplicate:
                bp.state = "skipped"
                bp.message = "duplicate"
                duplicates += 1
            else:
                bp.state = "failed"
                bp.message = result.message
        job.summary = f"added {added}, duplicates {duplicates}, total {len(paths)}"

    async def handle_refresh(job: Job) -> None:
        _snapshot(settings)
        mode = job.params.get("mode", "fill_blanks")
        for bp in job.progress:
            bp.title = _title(settings, bp.book_id)
            bp.state = "running"
            result = calibre_cli.refresh_metadata(
                settings.library_path, bp.book_id,
                mode=mode, sources=settings.metadata_sources,
            )
            bp.message = result.message
            bp.state = "done" if result.state == "fetched" else result.state
        done = sum(1 for p in job.progress if p.state == "done")
        job.summary = f"refreshed {done} of {len(job.progress)}"

    async def handle_convert(job: Job) -> None:
        _snapshot(settings)
        target = job.params.get("target", "EPUB")

        def resolver(book_id: int, fmt: str):
            return db.get_format_path(settings.library_path, book_id, fmt)

        for bp in job.progress:
            book = db.get_book(settings.library_path, bp.book_id)
            bp.title = book.title if book else f"<{bp.book_id}>"
            if book is None:
                bp.state = "failed"
                bp.message = "book not found"
                continue
            bp.state = "running"
            result = calibre_cli.convert_book(
                settings.library_path, bp.book_id, target,
                available_formats=book.formats, source_path_resolver=resolver,
            )
            bp.message = result.message
            if result.state == "done":
                bp.state = "done"
            elif result.state == "no_source":
                bp.state = "skipped"
            else:
                bp.state = "failed"
        done = sum(1 for p in job.progress if p.state == "done")
        job.summary = f"converted {done} of {len(job.progress)}"

    async def handle_send(job: Job) -> None:
        # No DB mutation, no snapshot needed.
        order = [f.upper() for f in settings.device_format_order]
        for bp in job.progress:
            book = db.get_book(settings.library_path, bp.book_id)
            bp.title = book.title if book else f"<{bp.book_id}>"
            bp.state = "running"
            if book is None:
                bp.state = "failed"
                bp.message = "book not found"
                continue
            chosen = next((f for f in order if f in book.formats), None)
            if chosen is None:
                bp.state = "skipped"
                bp.message = "no compatible format"
                continue
            src = db.get_format_path(settings.library_path, bp.book_id, chosen)
            if src is None:
                bp.state = "failed"
                bp.message = f"{chosen} file missing on disk"
                continue
            dest_name = src.name
            try:
                await mtp_helper.send(src, dest_name)
                bp.state = "done"
                bp.message = f"sent {chosen}"
            except mtp_helper.MTPHelperError as exc:
                bp.state = "failed"
                bp.message = str(exc)
        sent = sum(1 for p in job.progress if p.state == "done")
        skipped = sum(1 for p in job.progress if p.state == "skipped")
        job.summary = f"sent {sent}, skipped {skipped}, of {len(job.progress)}"

    async def handle_remove(job: Job) -> None:
        for bp in job.progress:
            book = db.get_book(settings.library_path, bp.book_id)
            bp.title = book.title if book else f"<{bp.book_id}>"
            bp.state = "running"
            # We use the same dest_name convention as send (the source filename).
            order = [f.upper() for f in settings.device_format_order]
            if book is None:
                bp.state = "failed"
                bp.message = "book not found"
                continue
            chosen = next((f for f in order if f in book.formats), None)
            src = db.get_format_path(settings.library_path, bp.book_id, chosen) if chosen else None
            if src is None:
                bp.state = "skipped"
                bp.message = "no known filename on device"
                continue
            try:
                await mtp_helper.remove(src.name)
                bp.state = "done"
            except mtp_helper.MTPHelperError as exc:
                bp.state = "failed"
                bp.message = str(exc)
        done = sum(1 for p in job.progress if p.state == "done")
        job.summary = f"removed {done} of {len(job.progress)}"

    return {
        "upload": handle_upload,
        "refresh": handle_refresh,
        "convert": handle_convert,
        "send": handle_send,
        "remove": handle_remove,
    }


def register_handlers(worker: Worker, settings: Settings) -> None:
    for kind, handler in make_handlers(settings).items():
        worker.register_handler(kind, handler)

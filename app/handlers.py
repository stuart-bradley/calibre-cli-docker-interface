"""Worker job handlers — wired in main.py at app startup.

All blocking Calibre subprocess calls and synchronous file IO are dispatched
via asyncio.to_thread so the worker doesn't starve the event loop (the device
poller and HTMX progress fragments share it).
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from app.config import Settings
from app.services import calibre_cli, db, mtp_helper, snapshot
from app.services.worker import Job, JobHandler, JobKind, Worker


async def _snapshot(settings: Settings) -> None:
    await asyncio.to_thread(
        snapshot.snapshot_if_needed,
        settings.library_path,
        settings.data_path,
        settings.snapshot_retention_days,
        tz=settings.tz,
    )


async def _title(settings: Settings, book_id: int) -> str:
    book = await asyncio.to_thread(db.get_book, settings.library_path, book_id)
    return book.title if book else f"<book {book_id}>"


def _device_filename(settings: Settings, book) -> tuple[Path | None, str | None]:
    """Pick a compatible format and resolve the on-disk source path.

    Single source of truth for both send and remove handlers — they must agree
    on the filename or remove will target the wrong dest_name.
    """
    if book is None:
        return None, None
    order = [f.upper() for f in settings.device_format_order]
    chosen = next((f for f in order if f in book.formats), None)
    if chosen is None:
        return None, None
    src = db.get_format_path(settings.library_path, book.id, chosen)
    return src, chosen


def make_handlers(settings: Settings) -> dict[JobKind, JobHandler]:
    async def handle_upload(job: Job) -> None:
        await _snapshot(settings)
        paths: list[Path] = [Path(p) for p in job.params.get("files", [])]
        spool_dir = job.params.get("spool_dir")
        added = 0
        duplicates = 0
        for path, bp in zip(paths, job.progress, strict=False):
            bp.title = path.name
            bp.state = "running"
            result = await asyncio.to_thread(calibre_cli.add_book, settings.library_path, path)
            if result.added:
                bp.book_id = result.book_id or bp.book_id
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
        if spool_dir:
            await asyncio.to_thread(shutil.rmtree, spool_dir, ignore_errors=True)
        skipped_pre = len(job.params.get("skipped", []))
        job.summary = (
            f"added {added}, duplicates {duplicates}, total {len(paths)}"
            + (f"; pre-rejected {skipped_pre}" if skipped_pre else "")
        )

    async def handle_refresh(job: Job) -> None:
        await _snapshot(settings)
        mode = job.params.get("mode", "fill_blanks")
        for bp in job.progress:
            bp.title = await _title(settings, bp.book_id)
            bp.state = "running"
            result = await asyncio.to_thread(
                calibre_cli.refresh_metadata,
                settings.library_path, bp.book_id,
                mode=mode, sources=settings.metadata_sources,
            )
            bp.message = result.message
            bp.state = "done" if result.state == "fetched" else result.state
        done = sum(1 for p in job.progress if p.state == "done")
        job.summary = f"refreshed {done} of {len(job.progress)}"

    async def handle_convert(job: Job) -> None:
        await _snapshot(settings)
        target = job.params.get("target", "EPUB")

        def resolver(book_id: int, fmt: str):
            return db.get_format_path(settings.library_path, book_id, fmt)

        for bp in job.progress:
            book = await asyncio.to_thread(db.get_book, settings.library_path, bp.book_id)
            bp.title = book.title if book else f"<{bp.book_id}>"
            if book is None:
                bp.state = "failed"
                bp.message = "book not found"
                continue
            bp.state = "running"
            result = await asyncio.to_thread(
                calibre_cli.convert_book,
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
        # No DB mutation — no snapshot needed.
        for bp in job.progress:
            book = await asyncio.to_thread(db.get_book, settings.library_path, bp.book_id)
            bp.title = book.title if book else f"<{bp.book_id}>"
            bp.state = "running"
            if book is None:
                bp.state = "failed"
                bp.message = "book not found"
                continue
            src, chosen = await asyncio.to_thread(_device_filename, settings, book)
            if chosen is None:
                bp.state = "skipped"
                bp.message = "no compatible format"
                continue
            if src is None:
                bp.state = "failed"
                bp.message = f"{chosen} file missing on disk"
                continue
            try:
                await mtp_helper.send(src, src.name)
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
            book = await asyncio.to_thread(db.get_book, settings.library_path, bp.book_id)
            bp.title = book.title if book else f"<{bp.book_id}>"
            bp.state = "running"
            if book is None:
                bp.state = "failed"
                bp.message = "book not found"
                continue
            src, chosen = await asyncio.to_thread(_device_filename, settings, book)
            if chosen is None:
                bp.state = "skipped"
                bp.message = "no known filename on device"
                continue
            # Use the same naming convention as send. src may be None (file
            # deleted locally after a send) — that's fine, we only need the name.
            dest_name = src.name if src is not None else f"{book.title}.{chosen.lower()}"
            try:
                await mtp_helper.remove(dest_name)
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

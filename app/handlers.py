"""Worker job handlers — wired in main.py at app startup.

All blocking Calibre subprocess calls and synchronous file IO are dispatched
via asyncio.to_thread so the worker doesn't starve the event loop (the device
poller and HTMX progress fragments share it).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from app.config import Settings
from app.services import calibre_cli, db, mtp_helper, snapshot
from app.services.worker import Job, JobHandler, JobKind, Worker
from app.state import DeviceState

log = logging.getLogger(__name__)


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


# Formats we want present on every uploaded book so the on-device library
# (Amazon-native AZW3/MOBI) and other readers (raw EPUB) both work without
# on-the-fly conversion at send time.
_AUTOCONVERT_TARGETS: tuple[str, ...] = ("AZW3", "MOBI", "EPUB")


async def _autoconvert_all_formats(settings: Settings, book_id: int) -> list[str]:
    """Convert ``book_id`` into every target format in ``_AUTOCONVERT_TARGETS``
    that isn't already present. Returns the list of formats actually produced.
    Errors are logged and skipped — a missing format here is not fatal to the
    upload job; the user just loses the future-send acceleration.
    """
    book = await asyncio.to_thread(db.get_book, settings.library_path, book_id)
    if book is None:
        return []
    existing = {f.upper() for f in book.formats}

    def resolver(bid: int, fmt: str):
        return db.get_format_path(settings.library_path, bid, fmt)

    produced: list[str] = []
    for target in _AUTOCONVERT_TARGETS:
        if target in existing:
            continue
        result = await asyncio.to_thread(
            calibre_cli.convert_book,
            settings.library_path,
            book_id,
            target,
            available_formats=sorted(existing | set(produced)),
            source_path_resolver=resolver,
        )
        if result.state == "done":
            produced.append(target)
        else:
            log.info("autoconvert book %s -> %s: %s", book_id, target, result.message)
    return produced


def make_handlers(settings: Settings, device_state: DeviceState) -> dict[JobKind, JobHandler]:
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
            if result.added and result.book_id is not None:
                bp.book_id = result.book_id
                bp.state = "done"
                bp.message = f"added id {result.book_id}"
                added += 1
                # Eagerly convert into every reader-compatible format that
                # isn't already present, so future sends never need on-the-fly
                # conversion (the Kindle indexer skips EPUB — see handle_send).
                produced = await _autoconvert_all_formats(settings, result.book_id)
                if produced:
                    bp.message += f"; converted to {', '.join(produced)}"
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
        job.summary = f"added {added}, duplicates {duplicates}, total {len(paths)}" + (
            f"; pre-rejected {skipped_pre}" if skipped_pre else ""
        )

    async def handle_refresh(job: Job) -> None:
        await _snapshot(settings)
        mode = job.params.get("mode", "fill_blanks")
        fetch_covers = job.params.get("fetch_covers", True)
        for bp in job.progress:
            bp.title = await _title(settings, bp.book_id)
            bp.state = "running"
            result = await asyncio.to_thread(
                calibre_cli.refresh_metadata,
                settings.library_path,
                bp.book_id,
                mode=mode,
                sources=settings.metadata_sources,
                fetch_covers=fetch_covers,
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
                settings.library_path,
                bp.book_id,
                target,
                available_formats=book.formats,
                source_path_resolver=resolver,
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
            # Kindle library indexer skips EPUB; convert to AZW3 transiently
            # before sending so the book actually appears in the library UI.
            # Upload-time autoconvert means most books already have AZW3 in
            # the library and this branch is rarely taken — but it's a
            # necessary fallback for books that only have EPUB.
            send_src = src
            send_label = f"sent {chosen}"
            converted_tmp: Path | None = None
            if chosen.upper() == "EPUB":
                converted_tmp = await asyncio.to_thread(
                    calibre_cli.convert_to_temp_file, src, "AZW3"
                )
                if converted_tmp is None:
                    bp.state = "failed"
                    bp.message = "EPUB→AZW3 conversion failed"
                    continue
                send_src = converted_tmp
                send_label = "converted EPUB→AZW3, sent"
            try:
                await mtp_helper.send(send_src, send_src.name)
                # Optimistic cache update — the poller no longer re-lists files
                # while the device is on the bus (see services.device), so the
                # "on-device" badge for the book the user just sent would
                # otherwise not appear until the next replug.
                device_state.on_device_filenames.add(send_src.name)
                bp.state = "done"
                bp.message = send_label
            except mtp_helper.MTPHelperError as exc:
                log.warning("send failed for book %s (%s): %s", bp.book_id, bp.title, exc)
                bp.state = "failed"
                bp.message = str(exc)
            finally:
                if converted_tmp is not None:
                    await asyncio.to_thread(shutil.rmtree, converted_tmp.parent, ignore_errors=True)
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
            # Use the same naming convention as send. EPUB is converted to
            # AZW3 by handle_send before reaching the device, so the on-device
            # filename always carries the AZW3 extension. src may be None
            # (file deleted locally after a send) — that's fine, we only need
            # the name.
            on_device_ext = "azw3" if chosen.upper() == "EPUB" else chosen.lower()
            if src is not None:
                dest_name = src.with_suffix(f".{on_device_ext}").name
            else:
                dest_name = f"{book.title}.{on_device_ext}"
            try:
                await mtp_helper.remove(dest_name)
                device_state.on_device_filenames.discard(dest_name)
                bp.state = "done"
            except mtp_helper.MTPHelperError as exc:
                log.warning("remove failed for book %s (%s): %s", bp.book_id, bp.title, exc)
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


def register_handlers(worker: Worker, settings: Settings, device_state: DeviceState) -> None:
    for kind, handler in make_handlers(settings, device_state).items():
        worker.register_handler(kind, handler)

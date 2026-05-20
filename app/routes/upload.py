from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse

router = APIRouter()
log = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".epub", ".azw3", ".mobi", ".pdf", ".kepub", ".cbz", ".cbr", ".fb2", ".lit"}
MAX_FILE_BYTES = 200 * 1024 * 1024          # 200 MB per file
MAX_REQUEST_BYTES = 1024 * 1024 * 1024      # 1 GB per request


def _sanitise_filename(raw: str) -> str | None:
    """Reduce raw Content-Disposition filename to a safe basename.

    Returns None if the result would be empty, a dotfile, or have a disallowed
    extension. Strips any directory components — Path(raw).name discards `../`.
    """
    name = Path(raw).name.strip()
    if not name or name.startswith("."):
        return None
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        return None
    return name


@router.post("/upload")
async def upload(request: Request, files: list[UploadFile]):
    spool_dir = Path(tempfile.mkdtemp(prefix="cwc-upload-"))
    spool_root = spool_dir.resolve()
    saved: list[Path] = []
    skipped: list[str] = []
    total_bytes = 0

    try:
        for upload in files:
            if not upload.filename:
                continue
            safe = _sanitise_filename(upload.filename)
            if safe is None:
                skipped.append(f"{upload.filename} (rejected: unsafe name or unsupported format)")
                continue

            dest = (spool_dir / safe).resolve()
            if not dest.is_relative_to(spool_root):
                skipped.append(f"{upload.filename} (rejected: traversal)")
                continue

            file_bytes = 0
            with dest.open("wb") as fh:
                while True:
                    chunk = await upload.read(64 * 1024)
                    if not chunk:
                        break
                    file_bytes += len(chunk)
                    total_bytes += len(chunk)
                    if file_bytes > MAX_FILE_BYTES:
                        dest.unlink(missing_ok=True)
                        raise HTTPException(413, f"{safe} exceeds {MAX_FILE_BYTES} bytes")
                    if total_bytes > MAX_REQUEST_BYTES:
                        dest.unlink(missing_ok=True)
                        raise HTTPException(413, f"upload total exceeds {MAX_REQUEST_BYTES} bytes")
                    fh.write(chunk)
            saved.append(dest)
    except HTTPException:
        # Tear down the partial spool before bubbling.
        for p in saved:
            p.unlink(missing_ok=True)
        spool_dir.rmdir()
        raise

    if not saved:
        spool_dir.rmdir()
        log.info("upload received no acceptable files (%d rejected)", len(skipped))
        return RedirectResponse("/jobs", status_code=303)

    worker = request.app.state.worker
    worker.enqueue(
        "upload",
        book_ids=[0] * len(saved),            # placeholder; real ids assigned by handler
        params={"files": [str(p) for p in saved], "spool_dir": str(spool_dir), "skipped": skipped},
    )

    return RedirectResponse("/jobs", status_code=303)

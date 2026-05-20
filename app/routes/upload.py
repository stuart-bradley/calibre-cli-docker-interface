from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.responses import RedirectResponse

from app.config import Settings, get_settings

router = APIRouter()


@router.post("/upload")
async def upload(
    request: Request,
    files: list[UploadFile],
    settings: Settings = Depends(get_settings),
):
    spool_dir = Path(tempfile.mkdtemp(prefix="cwc-upload-"))
    saved: list[Path] = []
    for upload in files:
        if not upload.filename:
            continue
        dest = spool_dir / upload.filename
        with dest.open("wb") as fh:
            shutil.copyfileobj(upload.file, fh)
        saved.append(dest)

    worker = request.app.state.worker
    # Single job covering all uploaded files (batch-first design, R2).
    worker.enqueue(
        "upload",
        book_ids=list(range(len(saved))),
        params={"files": [str(p) for p in saved]},
    )

    return RedirectResponse("/jobs", status_code=303)

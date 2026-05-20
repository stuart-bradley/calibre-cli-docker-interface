from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import Settings, get_settings
from app.services import db
from app.templating import templates

router = APIRouter()

_CONVERT_TARGETS = ("EPUB", "AZW3", "MOBI")


def _book_ids(book_id: list[int]) -> list[int]:
    return list(dict.fromkeys(book_id))  # dedupe, preserve order


@router.post("/batch/refresh")
def batch_refresh(
    request: Request,
    book_id: list[int] = Form(...),
    mode: str = Form("fill_blanks"),
    fetch_covers: bool = Form(True),
):
    if mode not in ("fill_blanks", "overwrite"):
        raise HTTPException(400, "invalid mode")
    worker = request.app.state.worker
    worker.enqueue(
        "refresh",
        book_ids=_book_ids(book_id),
        params={"mode": mode, "fetch_covers": fetch_covers},
    )
    return RedirectResponse("/jobs", status_code=303)


@router.post("/batch/convert")
def batch_convert(
    request: Request,
    book_id: list[int] = Form(...),
    target: str = Form("EPUB"),
):
    if target not in _CONVERT_TARGETS:
        raise HTTPException(400, "invalid target")
    worker = request.app.state.worker
    worker.enqueue("convert", book_ids=_book_ids(book_id), params={"target": target})
    return RedirectResponse("/jobs", status_code=303)


@router.post("/batch/convert/dialog", response_class=HTMLResponse)
def batch_convert_dialog(
    request: Request,
    book_id: list[int] = Form(...),
    settings: Settings = Depends(get_settings),
):
    ids = _book_ids(book_id)
    books = []
    formats_per_book: dict[int, set[str]] = {}
    for bid in ids:
        book = db.get_book(settings.library_path, bid)
        if book is None:
            continue
        books.append(book)
        formats_per_book[book.id] = {f.upper() for f in book.formats}

    if not books:
        available_targets: list[str] = []
        per_target_preview: dict[str, list[dict]] = {}
    else:
        common = set.intersection(*formats_per_book.values()) if formats_per_book else set()
        available_targets = [t for t in _CONVERT_TARGETS if t not in common]

        def _action(book_id: int, target: str) -> str:
            if target in formats_per_book[book_id]:
                return f"already has {target}"
            return "will convert"

        per_target_preview = {
            target: [
                {"book_id": b.id, "title": b.title, "action": _action(b.id, target)} for b in books
            ]
            for target in available_targets
        }

    return templates.TemplateResponse(
        request,
        "_fragments/convert_dialog.html",
        {
            "books": books,
            "available_targets": available_targets,
            "per_target_preview": per_target_preview,
        },
    )


@router.post("/batch/send")
def batch_send(request: Request, book_id: list[int] = Form(...)):
    worker = request.app.state.worker
    worker.enqueue("send", book_ids=_book_ids(book_id), params={})
    return RedirectResponse("/jobs", status_code=303)


@router.post("/batch/remove")
def batch_remove(request: Request, book_id: list[int] = Form(...)):
    worker = request.app.state.worker
    worker.enqueue("remove", book_ids=_book_ids(book_id), params={})
    return RedirectResponse("/jobs", status_code=303)


@router.get("/jobs", response_class=HTMLResponse)
def list_jobs(request: Request):
    worker = request.app.state.worker
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {"jobs": worker.list_jobs(100), "device": request.app.state.device_state.detect},
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def get_job(job_id: str, request: Request):
    worker = request.app.state.worker
    job = worker.get_job(job_id)
    if job is None:
        raise HTTPException(404)
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {"jobs": [job], "device": request.app.state.device_state.detect},
    )


@router.get("/jobs/{job_id}/fragment", response_class=HTMLResponse)
def job_fragment(job_id: str, request: Request):
    worker = request.app.state.worker
    job = worker.get_job(job_id)
    if job is None:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "_fragments/job_row.html", {"job": job})

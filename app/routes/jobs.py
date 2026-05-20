from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.templating import templates

router = APIRouter()


def _book_ids(book_id: list[int]) -> list[int]:
    return list(dict.fromkeys(book_id))   # dedupe, preserve order


@router.post("/batch/refresh")
def batch_refresh(
    request: Request,
    book_id: list[int] = Form(...),
    mode: str = "fill_blanks",
):
    if mode not in ("fill_blanks", "overwrite"):
        raise HTTPException(400, "invalid mode")
    worker = request.app.state.worker
    worker.enqueue("refresh", book_ids=_book_ids(book_id), params={"mode": mode})
    return RedirectResponse("/jobs", status_code=303)


@router.post("/batch/convert")
def batch_convert(
    request: Request,
    book_id: list[int] = Form(...),
    target: str = "EPUB",
):
    if target not in ("EPUB", "AZW3", "MOBI"):
        raise HTTPException(400, "invalid target")
    worker = request.app.state.worker
    worker.enqueue("convert", book_ids=_book_ids(book_id), params={"target": target})
    return RedirectResponse("/jobs", status_code=303)


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

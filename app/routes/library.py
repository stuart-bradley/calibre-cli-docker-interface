from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse

from app.config import Settings, get_settings
from app.services import db
from app.state import DeviceState

router = APIRouter()

PER_PAGE_COOKIE = "calibre_web_cli_per_page"
COOKIE_MAX_AGE = 365 * 24 * 3600


def _resolve_per_page(
    request: Request, query_value: int | None, default: int,
) -> tuple[int, bool]:
    if query_value is not None:
        return max(1, query_value), True
    cookie_value = request.cookies.get(PER_PAGE_COOKIE)
    if cookie_value and cookie_value.isdigit():
        return max(1, int(cookie_value)), False
    return default, False


def _all_authors_and_tags(library_path):
    with db.connect(library_path) as conn:
        authors = [r["name"] for r in conn.execute("SELECT name FROM authors ORDER BY sort")]
        tags = [r["name"] for r in conn.execute("SELECT name FROM tags ORDER BY name")]
    return authors, tags


@router.get("/", response_class=HTMLResponse)
def list_view(
    request: Request,
    response: Response,
    q: str | None = None,
    author: str | None = None,
    tag: str | None = None,
    series: str | None = None,
    format: str | None = None,
    sort: str = "title",
    page: int = Query(1, ge=1),
    per_page: int | None = Query(None, ge=1, le=200),
    settings: Settings = Depends(get_settings),
):
    from app.main import templates  # avoid circular import

    resolved_per_page, set_cookie = _resolve_per_page(request, per_page, settings.page_size)

    books, total = db.list_books(
        settings.library_path,
        q=q, author=author, tag=tag, series=series, format=format,
        sort=sort, page=page, per_page=resolved_per_page,
    )
    total_pages = max(1, (total + resolved_per_page - 1) // resolved_per_page)
    all_authors, all_tags = _all_authors_and_tags(settings.library_path)

    device_state: DeviceState = request.app.state.device_state
    on_device_ids: set[int] = set()
    if device_state.on_device_filenames and books:
        wanted = device_state.on_device_filenames
        for b in books:
            for fmt in b.formats:
                candidate = db.get_format_path(settings.library_path, b.id, fmt)
                if candidate is not None and candidate.name in wanted:
                    on_device_ids.add(b.id)
                    break

    def query_str(**overrides):
        params = {
            "q": q or "", "author": author or "", "tag": tag or "",
            "series": series or "", "format": format or "", "sort": sort,
            "page": page, "per_page": resolved_per_page,
        }
        params.update(overrides)
        cleaned = {k: v for k, v in params.items() if v not in (None, "")}
        return urlencode(cleaned)

    html = templates.TemplateResponse(
        request,
        "library.html",
        {
            "books": books,
            "total": total,
            "total_pages": total_pages,
            "page": page,
            "per_page": resolved_per_page,
            "q": q, "author": author, "tag": tag,
            "sort": sort,
            "all_authors": all_authors,
            "all_tags": all_tags,
            "device": device_state.detect,
            "on_device_ids": on_device_ids,
            "query_str": query_str,
        },
    )
    if set_cookie:
        html.set_cookie(
            PER_PAGE_COOKIE, str(resolved_per_page),
            max_age=COOKIE_MAX_AGE, samesite="lax", path="/",
        )
    return html


@router.get("/book/{book_id}", response_class=HTMLResponse)
def detail_view(
    book_id: int,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    from app.main import templates

    book = db.get_book(settings.library_path, book_id)
    if book is None:
        raise HTTPException(404)

    device_state: DeviceState = request.app.state.device_state
    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "book": book,
            "device": device_state.detect,
        },
    )

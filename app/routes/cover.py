from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import FileResponse

from app.config import Settings, get_settings
from app.services import db

router = APIRouter()


def _placeholder_svg(book_id: int, title: str) -> bytes:
    safe = (title or f"#{book_id}").replace("&", "&amp;").replace("<", "&lt;")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 180 270" width="180" height="270">'
        f'  <rect width="180" height="270" fill="#d1d5db"/>'
        f'  <text x="50%" y="50%" text-anchor="middle" dominant-baseline="middle"'
        f'        font-family="system-ui, sans-serif" font-size="12" fill="#4b5563">'
        f'    {safe[:80]}'
        f'  </text>'
        f'</svg>'
    ).encode()


@router.get("/cover/{book_id}")
def cover(
    book_id: int,
    request: Request,
    if_none_match: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
):
    cover_path = db.get_cover_path(settings.library_path, book_id)
    if cover_path is not None:
        stat = cover_path.stat()
        etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
        if if_none_match == etag:
            return Response(status_code=304)
        return FileResponse(
            cover_path,
            media_type="image/jpeg",
            headers={"ETag": etag, "Cache-Control": "public, max-age=86400"},
        )

    book = db.get_book(settings.library_path, book_id)
    title = book.title if book else f"#{book_id}"
    etag = f'"placeholder-{book_id}"'
    if if_none_match == etag:
        return Response(status_code=304)
    return Response(
        content=_placeholder_svg(book_id, title),
        media_type="image/svg+xml",
        headers={"ETag": etag, "Cache-Control": "public, max-age=300"},
    )

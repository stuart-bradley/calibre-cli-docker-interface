from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

SortKey = Literal["title", "author", "date_added"]

_SORT_SQL: dict[SortKey, str] = {
    "title": "b.sort COLLATE NOCASE ASC",
    "author": "b.author_sort COLLATE NOCASE ASC",
    "date_added": "b.timestamp DESC",
}


@dataclass(frozen=True)
class Book:
    id: int
    title: str
    authors: list[str]
    tags: list[str]
    series: str | None
    series_index: float | None
    formats: list[str]
    path: str
    has_cover: bool
    timestamp: datetime
    pubdate: datetime | None
    identifiers: dict[str, str] = field(default_factory=dict)


@contextmanager
def connect(library_path: Path) -> Iterator[sqlite3.Connection]:
    db = (library_path / "metadata.db").resolve()
    uri = f"file:{db}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _parse_dt(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _book_from_row(row: sqlite3.Row, conn: sqlite3.Connection) -> Book:
    book_id = row["id"]
    authors = [
        r["name"]
        for r in conn.execute(
            "SELECT a.name FROM authors a "
            "JOIN books_authors_link bal ON bal.author = a.id "
            "WHERE bal.book = ? ORDER BY a.sort",
            (book_id,),
        )
    ]
    tags = [
        r["name"]
        for r in conn.execute(
            "SELECT t.name FROM tags t "
            "JOIN books_tags_link btl ON btl.tag = t.id "
            "WHERE btl.book = ? ORDER BY t.name",
            (book_id,),
        )
    ]
    series_row = conn.execute(
        "SELECT s.name FROM series s "
        "JOIN books_series_link bsl ON bsl.series = s.id "
        "WHERE bsl.book = ?",
        (book_id,),
    ).fetchone()
    formats = [
        r["format"].upper()
        for r in conn.execute(
            "SELECT format FROM data WHERE book = ? ORDER BY format",
            (book_id,),
        )
    ]
    identifiers = {
        r["type"]: r["val"]
        for r in conn.execute("SELECT type, val FROM identifiers WHERE book = ?", (book_id,))
    }
    return Book(
        id=book_id,
        title=row["title"],
        authors=authors,
        tags=tags,
        series=series_row["name"] if series_row else None,
        series_index=row["series_index"] if series_row else None,
        formats=formats,
        path=row["path"],
        has_cover=bool(row["has_cover"]),
        timestamp=_parse_dt(row["timestamp"]) or datetime.min,
        pubdate=_parse_dt(row["pubdate"]),
        identifiers=identifiers,
    )


def list_books(
    library_path: Path,
    *,
    q: str | None = None,
    author: str | None = None,
    tag: str | None = None,
    series: str | None = None,
    format: str | None = None,
    sort: SortKey = "title",
    page: int = 1,
    per_page: int = 48,
) -> tuple[list[Book], int]:
    where: list[str] = []
    params: list[object] = []

    if q:
        where.append(
            "(b.title LIKE ? OR EXISTS ("
            " SELECT 1 FROM books_authors_link bal "
            " JOIN authors a ON a.id = bal.author "
            " WHERE bal.book = b.id AND a.name LIKE ?))"
        )
        like = f"%{q}%"
        params.extend([like, like])

    if author:
        where.append(
            "EXISTS (SELECT 1 FROM books_authors_link bal "
            " JOIN authors a ON a.id = bal.author "
            " WHERE bal.book = b.id AND a.name = ?)"
        )
        params.append(author)

    if tag:
        where.append(
            "EXISTS (SELECT 1 FROM books_tags_link btl "
            " JOIN tags t ON t.id = btl.tag "
            " WHERE btl.book = b.id AND t.name = ?)"
        )
        params.append(tag)

    if series:
        where.append(
            "EXISTS (SELECT 1 FROM books_series_link bsl "
            " JOIN series s ON s.id = bsl.series "
            " WHERE bsl.book = b.id AND s.name = ?)"
        )
        params.append(series)

    if format:
        where.append(
            "EXISTS (SELECT 1 FROM data d WHERE d.book = b.id AND d.format = ?)"
        )
        params.append(format.upper())

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    order_sql = _SORT_SQL.get(sort, _SORT_SQL["title"])
    page = max(page, 1)
    per_page = max(per_page, 1)
    offset = (page - 1) * per_page

    with connect(library_path) as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM books b {where_sql}", params
        ).fetchone()["n"]

        rows = conn.execute(
            f"SELECT * FROM books b {where_sql} "
            f"ORDER BY {order_sql} LIMIT ? OFFSET ?",
            [*params, per_page, offset],
        ).fetchall()

        books = [_book_from_row(r, conn) for r in rows]

    return books, total


def get_book(library_path: Path, book_id: int) -> Book | None:
    with connect(library_path) as conn:
        row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        if row is None:
            return None
        return _book_from_row(row, conn)


def get_format_path(library_path: Path, book_id: int, format: str) -> Path | None:
    with connect(library_path) as conn:
        row = conn.execute(
            "SELECT b.path, d.name FROM books b "
            "JOIN data d ON d.book = b.id "
            "WHERE b.id = ? AND d.format = ?",
            (book_id, format.upper()),
        ).fetchone()
    if row is None:
        return None
    candidate = library_path / row["path"] / f"{row['name']}.{format.lower()}"
    return candidate if candidate.exists() else None


def get_cover_path(library_path: Path, book_id: int) -> Path | None:
    with connect(library_path) as conn:
        row = conn.execute(
            "SELECT path, has_cover FROM books WHERE id = ?", (book_id,)
        ).fetchone()
    if row is None or not row["has_cover"]:
        return None
    cover = library_path / row["path"] / "cover.jpg"
    return cover if cover.exists() else None

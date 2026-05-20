from __future__ import annotations

import sqlite3
from collections import defaultdict
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
    # Calibre's `books.timestamp` is not indexed in the stock schema, but `books.id`
    # is monotonic with insert time so it's a free index-backed proxy for "recently
    # added" that scales as the library grows.
    "date_added": "b.id DESC",
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
    format_filenames: dict[str, str]  # FORMAT -> {data.name}.{ext.lower()}
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


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


def _fetch_book_aux(
    conn: sqlite3.Connection, book_ids: list[int],
) -> tuple[dict[int, list[str]], dict[int, list[str]], dict[int, tuple[str, float | None]],
           dict[int, list[tuple[str, str]]], dict[int, dict[str, str]]]:
    """Single-pass fetch of authors/tags/series/data/identifiers for a page of books.

    Returns five dicts keyed by book id. Each is the only query of its kind for
    the whole page, eliminating the N+1 _book_from_row pattern.
    """
    authors: dict[int, list[str]] = defaultdict(list)
    tags: dict[int, list[str]] = defaultdict(list)
    series: dict[int, tuple[str, float | None]] = {}
    data: dict[int, list[tuple[str, str]]] = defaultdict(list)
    identifiers: dict[int, dict[str, str]] = defaultdict(dict)

    if not book_ids:
        return authors, tags, series, data, identifiers

    ph = _placeholders(len(book_ids))
    params = list(book_ids)

    for r in conn.execute(
        f"SELECT bal.book AS book, a.name AS name FROM authors a "
        f"JOIN books_authors_link bal ON bal.author = a.id "
        f"WHERE bal.book IN ({ph}) ORDER BY a.sort",
        params,
    ):
        authors[r["book"]].append(r["name"])

    for r in conn.execute(
        f"SELECT btl.book AS book, t.name AS name FROM tags t "
        f"JOIN books_tags_link btl ON btl.tag = t.id "
        f"WHERE btl.book IN ({ph}) ORDER BY t.name",
        params,
    ):
        tags[r["book"]].append(r["name"])

    for r in conn.execute(
        f"SELECT bsl.book AS book, s.name AS name, b.series_index AS series_index "
        f"FROM series s "
        f"JOIN books_series_link bsl ON bsl.series = s.id "
        f"JOIN books b ON b.id = bsl.book "
        f"WHERE bsl.book IN ({ph})",
        params,
    ):
        series[r["book"]] = (r["name"], r["series_index"])

    for r in conn.execute(
        f"SELECT book, format, name FROM data WHERE book IN ({ph}) ORDER BY format",
        params,
    ):
        data[r["book"]].append((r["format"].upper(), r["name"]))

    for r in conn.execute(
        f"SELECT book, type, val FROM identifiers WHERE book IN ({ph})", params,
    ):
        identifiers[r["book"]][r["type"]] = r["val"]

    return authors, tags, series, data, identifiers


def _book_from_row(row: sqlite3.Row, aux: tuple) -> Book:
    authors, tags, series, data, identifiers = aux
    book_id = row["id"]
    formats_data = data.get(book_id, [])
    format_filenames = {fmt: f"{name}.{fmt.lower()}" for fmt, name in formats_data}
    series_pair = series.get(book_id)
    return Book(
        id=book_id,
        title=row["title"],
        authors=authors.get(book_id, []),
        tags=tags.get(book_id, []),
        series=series_pair[0] if series_pair else None,
        series_index=series_pair[1] if series_pair else None,
        formats=[fmt for fmt, _ in formats_data],
        format_filenames=format_filenames,
        path=row["path"],
        has_cover=bool(row["has_cover"]),
        timestamp=_parse_dt(row["timestamp"]) or datetime.min,
        pubdate=_parse_dt(row["pubdate"]),
        identifiers=identifiers.get(book_id, {}),
    )


def list_books(
    library_path: Path,
    *,
    q: str | None = None,
    author: str | None = None,
    tag: str | None = None,
    series: str | None = None,
    format: str | None = None,
    sort: SortKey = "date_added",
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

        aux = _fetch_book_aux(conn, [r["id"] for r in rows])
        books = [_book_from_row(r, aux) for r in rows]

    return books, total


def get_book(library_path: Path, book_id: int) -> Book | None:
    with connect(library_path) as conn:
        row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        if row is None:
            return None
        aux = _fetch_book_aux(conn, [book_id])
        return _book_from_row(row, aux)


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


def search_suggestions(
    library_path: Path, prefix: str, limit: int = 5,
) -> tuple[list[str], list[str]]:
    """Return ([titles], [authors]) matching prefix (case-insensitive).

    Each table is searched on both its display column (title/name) AND its
    sort column so 'Tch' finds 'Adrian Tchaikovsky' (sort='Tchaikovsky, Adrian')
    and 'Wizard' finds 'A Wizard of Earthsea' (sort='Wizard of Earthsea, A').
    """
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"{escaped}%"
    with connect(library_path) as conn:
        titles = [
            r["title"] for r in conn.execute(
                "SELECT title FROM books "
                "WHERE (title LIKE ? ESCAPE '\\' OR sort LIKE ? ESCAPE '\\') "
                "COLLATE NOCASE ORDER BY sort LIMIT ?",
                (like, like, limit),
            )
        ]
        authors = [
            r["name"] for r in conn.execute(
                "SELECT name FROM authors "
                "WHERE (name LIKE ? ESCAPE '\\' OR sort LIKE ? ESCAPE '\\') "
                "COLLATE NOCASE ORDER BY sort LIMIT ?",
                (like, like, limit),
            )
        ]
    return titles, authors


def get_cover_path(library_path: Path, book_id: int) -> Path | None:
    with connect(library_path) as conn:
        row = conn.execute(
            "SELECT path, has_cover FROM books WHERE id = ?", (book_id,)
        ).fetchone()
    if row is None or not row["has_cover"]:
        return None
    cover = library_path / row["path"] / "cover.jpg"
    return cover if cover.exists() else None

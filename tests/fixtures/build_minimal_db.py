"""Build a small Calibre-like metadata.db for tests.

Run once to produce `tests/fixtures/metadata_minimal.db`. The .db is committed
so test runs don't depend on this script — keep it around for reproducibility
when the schema or sample data needs adjusting.

Calibre schema reference: https://manual.calibre-ebook.com/db_api.html
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "metadata_minimal.db"
LIBRARY_ROOT = HERE / "library_minimal"


SCHEMA = """
CREATE TABLE books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL DEFAULT 'Unknown' COLLATE NOCASE,
    sort TEXT COLLATE NOCASE,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    pubdate TIMESTAMP DEFAULT '0101-01-01 00:00:00+00:00',
    series_index REAL NOT NULL DEFAULT 1.0,
    author_sort TEXT COLLATE NOCASE,
    isbn TEXT DEFAULT '' COLLATE NOCASE,
    lccn TEXT DEFAULT '' COLLATE NOCASE,
    path TEXT NOT NULL DEFAULT '',
    flags INTEGER NOT NULL DEFAULT 1,
    uuid TEXT,
    has_cover BOOL DEFAULT 0,
    last_modified TIMESTAMP NOT NULL DEFAULT '2000-01-01 00:00:00+00:00'
);

CREATE TABLE authors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE,
    sort TEXT COLLATE NOCASE,
    link TEXT NOT NULL DEFAULT ''
);

CREATE TABLE books_authors_link (
    id INTEGER PRIMARY KEY,
    book INTEGER NOT NULL,
    author INTEGER NOT NULL,
    UNIQUE(book, author)
);

CREATE TABLE tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE,
    UNIQUE (name)
);

CREATE TABLE books_tags_link (
    id INTEGER PRIMARY KEY,
    book INTEGER NOT NULL,
    tag INTEGER NOT NULL,
    UNIQUE(book, tag)
);

CREATE TABLE series (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE,
    sort TEXT COLLATE NOCASE,
    link TEXT NOT NULL DEFAULT ''
);

CREATE TABLE books_series_link (
    id INTEGER PRIMARY KEY,
    book INTEGER NOT NULL,
    series INTEGER NOT NULL,
    UNIQUE(book)
);

CREATE TABLE data (
    id INTEGER PRIMARY KEY,
    book INTEGER NOT NULL,
    format TEXT NOT NULL COLLATE NOCASE,
    uncompressed_size INTEGER NOT NULL,
    name TEXT NOT NULL,
    UNIQUE(book, format)
);

CREATE TABLE identifiers (
    id INTEGER PRIMARY KEY,
    book INTEGER NOT NULL,
    type TEXT NOT NULL DEFAULT 'isbn' COLLATE NOCASE,
    val TEXT NOT NULL COLLATE NOCASE,
    UNIQUE(book, type)
);

CREATE TABLE comments (
    id INTEGER PRIMARY KEY,
    book INTEGER NOT NULL,
    text TEXT NOT NULL COLLATE NOCASE,
    UNIQUE(book)
);
"""


def main() -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()
    LIBRARY_ROOT.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)

    authors = [
        ("Adrian Tchaikovsky", "Tchaikovsky, Adrian"),
        ("Ursula K. Le Guin", "Le Guin, Ursula K."),
    ]
    for name, sort in authors:
        conn.execute("INSERT INTO authors(name, sort) VALUES (?, ?)", (name, sort))

    tags = ["sci-fi", "fantasy", "novella", "anthology"]
    for name in tags:
        conn.execute("INSERT INTO tags(name) VALUES (?)", (name,))

    series = [("Children of Time", "Children of Time"), ("Earthsea", "Earthsea")]
    for name, sort in series:
        conn.execute("INSERT INTO series(name, sort) VALUES (?, ?)", (name, sort))

    books = [
        # (title, sort, author_id, author_sort, tag_ids, series_id, series_index,
        #  has_cover, formats, identifiers, timestamp)
        (
            "Children of Time",
            "Children of Time",
            1,
            "Tchaikovsky, Adrian",
            [1],
            1,
            1.0,
            True,
            ["EPUB", "AZW3"],
            {"isbn": "9781447273288"},
            "2024-01-15 09:00:00+00:00",
        ),
        (
            "Children of Ruin",
            "Children of Ruin",
            1,
            "Tchaikovsky, Adrian",
            [1],
            1,
            2.0,
            True,
            ["EPUB"],
            {"isbn": "9781509865888"},
            "2024-03-02 10:30:00+00:00",
        ),
        (
            "A Wizard of Earthsea",
            "Wizard of Earthsea, A",
            2,
            "Le Guin, Ursula K.",
            [2],
            2,
            1.0,
            True,
            ["EPUB", "MOBI"],
            {"isbn": "9780553262506"},
            "2023-11-20 14:00:00+00:00",
        ),
        (
            "The Tombs of Atuan",
            "Tombs of Atuan, The",
            2,
            "Le Guin, Ursula K.",
            [2],
            2,
            2.0,
            False,
            ["EPUB"],
            {},
            "2023-12-05 16:00:00+00:00",
        ),
        (
            "The Left Hand of Darkness",
            "Left Hand of Darkness, The",
            2,
            "Le Guin, Ursula K.",
            [1, 2],
            None,
            1.0,
            True,
            ["EPUB", "PDF"],
            {"isbn": "9780441478125"},
            "2024-05-10 11:00:00+00:00",
        ),
    ]

    for (
        title,
        sort,
        author_id,
        author_sort,
        tag_ids,
        series_id,
        series_index,
        has_cover,
        formats,
        identifiers,
        ts,
    ) in books:
        author_name = authors[author_id - 1][0]
        path = f"{author_name}/{title} ({0})"  # patched below
        cur = conn.execute(
            "INSERT INTO books(title, sort, author_sort, path, has_cover, "
            "series_index, timestamp, pubdate, uuid, last_modified) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                title,
                sort,
                author_sort,
                path,
                int(has_cover),
                series_index,
                ts,
                ts,
                f"uuid-{title.lower().replace(' ', '-')}",
                ts,
            ),
        )
        book_id = cur.lastrowid
        # patch path with real book_id
        true_path = f"{author_name}/{title} ({book_id})"
        conn.execute("UPDATE books SET path = ? WHERE id = ?", (true_path, book_id))

        conn.execute(
            "INSERT INTO books_authors_link(book, author) VALUES (?, ?)",
            (book_id, author_id),
        )
        for tag_id in tag_ids:
            conn.execute(
                "INSERT INTO books_tags_link(book, tag) VALUES (?, ?)",
                (book_id, tag_id),
            )
        if series_id is not None:
            conn.execute(
                "INSERT INTO books_series_link(book, series) VALUES (?, ?)",
                (book_id, series_id),
            )
        for fmt in formats:
            conn.execute(
                "INSERT INTO data(book, format, uncompressed_size, name) VALUES (?, ?, ?, ?)",
                (book_id, fmt, 123_456, title),
            )
        for typ, val in identifiers.items():
            conn.execute(
                "INSERT INTO identifiers(book, type, val) VALUES (?, ?, ?)",
                (book_id, typ, val),
            )

        # Create the on-disk book folder and stub files so get_format_path /
        # get_cover_path can be exercised against this fixture library.
        book_dir = LIBRARY_ROOT / true_path
        book_dir.mkdir(parents=True, exist_ok=True)
        for fmt in formats:
            (book_dir / f"{title}.{fmt.lower()}").write_bytes(b"stub")
        if has_cover:
            (book_dir / "cover.jpg").write_bytes(b"stub-cover")

    conn.commit()
    conn.close()
    print(f"wrote {DB_PATH} and {LIBRARY_ROOT}")


if __name__ == "__main__":
    main()

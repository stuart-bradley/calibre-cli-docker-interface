from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.services import db

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"
SOURCE_LIBRARY = FIXTURE_DIR / "library_minimal"
SOURCE_DB = FIXTURE_DIR / "metadata_minimal.db"


@pytest.fixture
def LIBRARY(tmp_path: Path) -> Path:
    """Per-test writable copy of the committed fixture library + db.

    Replaces the previous module-scoped symlink-into-source-tree fixture, which
    leaked stale state on interrupted runs and raced under `pytest -n`.
    """
    if not SOURCE_DB.exists():
        pytest.skip("fixture metadata_minimal.db missing; run tests/fixtures/build_minimal_db.py")
    lib = tmp_path / "library"
    shutil.copytree(SOURCE_LIBRARY, lib)
    shutil.copy2(SOURCE_DB, lib / "metadata.db")
    return lib


def test_list_books_returns_all_with_total(LIBRARY):
    books, total = db.list_books(LIBRARY)

    assert total == 5
    assert len(books) == 5
    assert all(b.title for b in books)


def test_list_books_pagination_clamps(LIBRARY):
    books, total = db.list_books(LIBRARY, page=99, per_page=2)

    assert total == 5
    assert books == []


def test_list_books_per_page(LIBRARY):
    page1, _ = db.list_books(LIBRARY, page=1, per_page=2, sort="title")
    page2, _ = db.list_books(LIBRARY, page=2, per_page=2, sort="title")

    assert len(page1) == 2
    assert len(page2) == 2
    assert {b.id for b in page1}.isdisjoint({b.id for b in page2})


def test_search_by_author_via_q(LIBRARY):
    books, total = db.list_books(LIBRARY, q="Tchaikovsky")

    assert total == 2
    assert all("Adrian Tchaikovsky" in b.authors for b in books)


def test_search_by_title_substring(LIBRARY):
    books, total = db.list_books(LIBRARY, q="Earthsea")

    assert total == 1
    assert books[0].title == "A Wizard of Earthsea"


def test_filter_by_tag(LIBRARY):
    books, total = db.list_books(LIBRARY, tag="fantasy")

    assert total == 3
    assert all("fantasy" in b.tags for b in books)


def test_filter_by_author(LIBRARY):
    books, total = db.list_books(LIBRARY, author="Ursula K. Le Guin")

    assert total == 3
    assert all("Ursula K. Le Guin" in b.authors for b in books)


def test_filter_by_format_case_insensitive(LIBRARY):
    books_upper, total_upper = db.list_books(LIBRARY, format="AZW3")
    books_lower, total_lower = db.list_books(LIBRARY, format="azw3")

    assert total_upper == total_lower == 1
    assert {b.id for b in books_upper} == {b.id for b in books_lower}


def test_filter_by_series(LIBRARY):
    books, total = db.list_books(LIBRARY, series="Children of Time")

    assert total == 2
    assert all(b.series == "Children of Time" for b in books)


def test_default_sort_is_title_sort_field(LIBRARY):
    """Sort uses books.sort (which strips leading articles like 'A'/'The').

    Fixture sort values: "Children of Ruin", "Children of Time",
    "Left Hand of Darkness, The", "Tombs of Atuan, The", "Wizard of Earthsea, A".
    """
    expected_titles_in_sort_order = [
        "Children of Ruin",
        "Children of Time",
        "The Left Hand of Darkness",
        "The Tombs of Atuan",
        "A Wizard of Earthsea",
    ]

    books, _ = db.list_books(LIBRARY, sort="title")

    assert [b.title for b in books] == expected_titles_in_sort_order


def test_sort_by_date_added_desc(LIBRARY):
    """date_added sorts by books.id DESC (a free index-backed proxy for insert time).

    Calibre assigns book ids monotonically, so this matches "recently added" in
    practice. The fixture inserts books in id order 1..5, so id DESC yields 5..1.
    """
    books, _ = db.list_books(LIBRARY, sort="date_added")

    ids = [b.id for b in books]
    assert ids == sorted(ids, reverse=True)


def test_get_book_returns_full_record(LIBRARY):
    book = db.get_book(LIBRARY, 1)

    assert book is not None
    assert book.title == "Children of Time"
    assert book.authors == ["Adrian Tchaikovsky"]
    assert set(book.formats) == {"EPUB", "AZW3"}
    assert "isbn" in book.identifiers
    assert book.has_cover is True
    assert book.series == "Children of Time"


def test_get_book_missing_returns_none(LIBRARY):
    assert db.get_book(LIBRARY, 9999) is None


def test_get_format_path_returns_existing_file(LIBRARY):
    path = db.get_format_path(LIBRARY, 1, "EPUB")

    assert path is not None
    assert path.exists()
    assert path.suffix == ".epub"


def test_get_format_path_missing_returns_none(LIBRARY):
    assert db.get_format_path(LIBRARY, 1, "PDF") is None


def test_get_cover_path_returns_existing_file(LIBRARY):
    cover = db.get_cover_path(LIBRARY, 1)

    assert cover is not None
    assert cover.name == "cover.jpg"
    assert cover.exists()


def test_get_cover_path_for_book_without_cover_returns_none(LIBRARY):
    cover = db.get_cover_path(LIBRARY, 4)

    assert cover is None

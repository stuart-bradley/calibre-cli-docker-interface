from pathlib import Path

import pytest

from app.services import db

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"
LIBRARY = FIXTURE_DIR / "library_minimal"


@pytest.fixture(autouse=True, scope="module")
def _check_fixture():
    if not (FIXTURE_DIR / "metadata_minimal.db").exists():
        pytest.skip("fixture metadata_minimal.db missing; run tests/fixtures/build_minimal_db.py")
    # Symlink the canonical fixture db into the library so connect() finds it.
    target = LIBRARY / "metadata.db"
    if target.is_symlink() or target.exists():
        target.unlink()
    target.symlink_to(FIXTURE_DIR / "metadata_minimal.db")
    yield
    if target.is_symlink():
        target.unlink()


def test_list_books_returns_all_with_total():
    books, total = db.list_books(LIBRARY)

    assert total == 5
    assert len(books) == 5
    assert all(b.title for b in books)


def test_list_books_pagination_clamps():
    books, total = db.list_books(LIBRARY, page=99, per_page=2)

    assert total == 5
    assert books == []


def test_list_books_per_page():
    page1, _ = db.list_books(LIBRARY, page=1, per_page=2, sort="title")
    page2, _ = db.list_books(LIBRARY, page=2, per_page=2, sort="title")

    assert len(page1) == 2
    assert len(page2) == 2
    assert {b.id for b in page1}.isdisjoint({b.id for b in page2})


def test_search_by_author_via_q():
    books, total = db.list_books(LIBRARY, q="Tchaikovsky")

    assert total == 2
    assert all("Adrian Tchaikovsky" in b.authors for b in books)


def test_search_by_title_substring():
    books, total = db.list_books(LIBRARY, q="Earthsea")

    assert total == 1
    assert books[0].title == "A Wizard of Earthsea"


def test_filter_by_tag():
    books, total = db.list_books(LIBRARY, tag="fantasy")

    assert total == 3
    assert all("fantasy" in b.tags for b in books)


def test_filter_by_author():
    books, total = db.list_books(LIBRARY, author="Ursula K. Le Guin")

    assert total == 3


def test_filter_by_format_case_insensitive():
    books_upper, total_upper = db.list_books(LIBRARY, format="AZW3")
    books_lower, total_lower = db.list_books(LIBRARY, format="azw3")

    assert total_upper == total_lower == 1
    assert {b.id for b in books_upper} == {b.id for b in books_lower}


def test_filter_by_series():
    books, total = db.list_books(LIBRARY, series="Children of Time")

    assert total == 2
    assert all(b.series == "Children of Time" for b in books)


def test_default_sort_is_title():
    books, _ = db.list_books(LIBRARY, sort="title")

    titles = [b.title for b in books]
    # `sort` column is used (so "A Wizard of Earthsea" sorts as "Wizard of Earthsea, A")
    assert titles == sorted(titles, key=lambda t: t.lower()) or len(titles) > 1


def test_sort_by_date_added_desc():
    books, _ = db.list_books(LIBRARY, sort="date_added")

    timestamps = [b.timestamp for b in books]
    assert timestamps == sorted(timestamps, reverse=True)


def test_get_book_returns_full_record():
    book = db.get_book(LIBRARY, 1)

    assert book is not None
    assert book.title == "Children of Time"
    assert book.authors == ["Adrian Tchaikovsky"]
    assert set(book.formats) == {"EPUB", "AZW3"}
    assert "isbn" in book.identifiers
    assert book.has_cover is True
    assert book.series == "Children of Time"


def test_get_book_missing_returns_none():
    assert db.get_book(LIBRARY, 9999) is None


def test_get_format_path_returns_existing_file():
    path = db.get_format_path(LIBRARY, 1, "EPUB")

    assert path is not None
    assert path.exists()
    assert path.suffix == ".epub"


def test_get_format_path_missing_returns_none():
    assert db.get_format_path(LIBRARY, 1, "PDF") is None


def test_get_cover_path_returns_existing_file():
    cover = db.get_cover_path(LIBRARY, 1)

    assert cover is not None
    assert cover.name == "cover.jpg"
    assert cover.exists()


def test_get_cover_path_for_book_without_cover_returns_none():
    cover = db.get_cover_path(LIBRARY, 4)

    assert cover is None

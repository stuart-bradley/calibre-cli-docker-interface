from __future__ import annotations

from app.routes.library import PER_PAGE_COOKIE


def test_list_view_renders_all_books(client):
    resp = client.get("/")

    assert resp.status_code == 200
    assert "Children of Time" in resp.text
    assert "A Wizard of Earthsea" in resp.text
    assert "The Left Hand of Darkness" in resp.text


def test_search_filters_results(client):
    resp = client.get("/?q=Tchaikovsky")

    assert resp.status_code == 200
    assert "Children of Time" in resp.text
    assert "Children of Ruin" in resp.text
    assert "A Wizard of Earthsea" not in resp.text


def test_filter_by_tag(client):
    resp = client.get("/?tag=fantasy")

    assert resp.status_code == 200
    assert "A Wizard of Earthsea" in resp.text
    # Children of Time is sci-fi only, should not appear under fantasy.
    assert "Children of Time" not in resp.text


def test_per_page_cookie_set_when_query_param_provided(client):
    resp = client.get("/?per_page=12")

    assert resp.cookies.get(PER_PAGE_COOKIE) == "12"


def test_per_page_cookie_respected_on_next_request(client):
    """5 books / 2 per page = 3 pages; template emits 'Page 1 of 3'."""
    client.get("/?per_page=2")
    resp = client.get("/")

    assert "Page 1 of 3" in resp.text


def test_detail_view(client):
    resp = client.get("/book/1")

    assert resp.status_code == 200
    assert "Children of Time" in resp.text
    assert "EPUB" in resp.text
    assert "AZW3" in resp.text


def test_detail_view_404_for_missing_book(client):
    resp = client.get("/book/9999")

    assert resp.status_code == 404


def test_default_sort_is_date_added_newest_first(client):
    """date_added sorts by b.id DESC; fixture insertion order id 1..5 means
    the newest insert (id 5: 'The Left Hand of Darkness') comes first and the
    oldest (id 1: 'Children of Time') comes last."""
    resp = client.get("/")

    assert resp.status_code == 200
    newest = resp.text.index("The Left Hand of Darkness")
    oldest = resp.text.index("Children of Time")
    assert newest < oldest


def test_upload_modal_present_in_header(client):
    """Upload books trigger lives in the base header and modal is included on every page."""
    resp = client.get("/")

    assert resp.status_code == 200
    assert "Upload books" in resp.text
    assert '<dialog id="modal-upload"' in resp.text


def test_old_details_upload_block_removed(client):
    """The old <details><summary>Upload books</summary>… block is gone from library."""
    resp = client.get("/")

    assert resp.status_code == 200
    assert "<summary>Upload books</summary>" not in resp.text


def test_upload_modal_present_on_jobs_page_too(client):
    """Modal is included via base.html so it's available everywhere."""
    resp = client.get("/jobs")

    assert resp.status_code == 200
    assert '<dialog id="modal-upload"' in resp.text


def test_search_suggestions_below_min_length_returns_empty(client):
    """Fewer than 2 characters → no suggestions (avoid noisy queries on every keystroke)."""
    resp = client.get("/search/suggestions?q=t")

    assert resp.status_code == 200
    assert "<li" not in resp.text


def test_search_suggestions_matches_titles_and_authors(client):
    """'Children' prefix-matches two book titles in the fixture."""
    resp = client.get("/search/suggestions?q=Children")

    assert resp.status_code == 200
    assert "Title: Children of Time" in resp.text
    assert "Title: Children of Ruin" in resp.text


def test_search_suggestions_author_match(client):
    resp = client.get("/search/suggestions?q=Tch")

    assert resp.status_code == 200
    assert "Author: Adrian Tchaikovsky" in resp.text


def test_search_suggestions_no_match_returns_empty(client):
    """Prefix that matches nothing → empty fragment so :empty CSS hides dropdown."""
    resp = client.get("/search/suggestions?q=zzzzz")

    assert resp.status_code == 200
    assert "<li" not in resp.text


def test_library_page_has_autocomplete_attrs(client):
    resp = client.get("/")

    assert resp.status_code == 200
    assert 'hx-get="/search/suggestions"' in resp.text
    assert 'id="search-suggestions"' in resp.text


def test_author_dropdown_removed(client):
    """Author dropdown was replaced by search autocomplete."""
    resp = client.get("/")

    assert resp.status_code == 200
    assert '<select name="author"' not in resp.text


def test_sort_dropdown_uses_human_labels(client):
    """Sort dropdown options should display human labels, not raw enum values."""
    resp = client.get("/")

    assert resp.status_code == 200
    assert ">Date Added<" in resp.text
    assert ">Title<" in resp.text
    assert ">Author<" in resp.text
    assert ">date_added<" not in resp.text


def test_explicit_sort_title_still_works(client):
    """Control: ?sort=title sorts alphabetically by b.sort, so 'Children of
    Ruin' (sort key 'Children of Ruin') comes before 'A Wizard of Earthsea'
    (sort key 'Wizard of Earthsea, A')."""
    resp = client.get("/?sort=title")

    assert resp.status_code == 200
    children = resp.text.index("Children of Ruin")
    wizard = resp.text.index("A Wizard of Earthsea")
    assert children < wizard

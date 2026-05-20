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

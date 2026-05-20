from __future__ import annotations


def test_cover_returns_image_for_book_with_cover(client):
    resp = client.get("/cover/1")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.headers["etag"]
    assert "max-age=86400" in resp.headers["cache-control"]
    assert resp.content == b"stub-cover"


def test_cover_returns_placeholder_svg_when_missing(client):
    # Book #4 has has_cover=False in the fixture.
    resp = client.get("/cover/4")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/svg+xml"
    assert b"<svg" in resp.content
    assert resp.headers["etag"] == '"placeholder-4"'


def test_cover_304_with_matching_etag(client):
    first = client.get("/cover/1")
    etag = first.headers["etag"]

    cached = client.get("/cover/1", headers={"if-none-match": etag})

    assert cached.status_code == 304


def test_cover_placeholder_304_with_matching_etag(client):
    cached = client.get("/cover/4", headers={"if-none-match": '"placeholder-4"'})

    assert cached.status_code == 304

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.services import calibre_cli

LIB = Path("/tmp/fake-library")


@pytest.fixture
def run_calls():
    return []


@pytest.fixture
def stub_run(monkeypatch, run_calls):
    """Replace _run with a stub returning queued CompletedProcess objects."""

    queue: list[subprocess.CompletedProcess] = []

    def enqueue(*procs: subprocess.CompletedProcess) -> None:
        queue.extend(procs)

    def fake_run(argv, *, input=None):
        run_calls.append(argv)
        if not queue:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return queue.pop(0)

    monkeypatch.setattr(calibre_cli, "_run", fake_run)
    return enqueue


def _cp(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _argv_after(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag and i + 1 < len(argv)]


# --- add_book -----------------------------------------------------------------


def test_add_book_success(stub_run, run_calls):
    stub_run(_cp(stdout="Added book ids: 42\n"))

    result = calibre_cli.add_book(LIB, Path("/tmp/x.epub"))

    assert result.added is True
    assert result.duplicate is False
    assert result.book_id == 42
    assert run_calls[0][:2] == ["calibredb", "add"]
    assert "/tmp/x.epub" in run_calls[0]


def test_add_book_duplicate_via_stdout_not_exit_code(stub_run):
    # Critical: Calibre returns exit 0 for dupes and signals via stdout.
    stub_run(_cp(stdout=calibre_cli.DUPLICATE_MARKER + "\nfoo.epub\n", returncode=0))

    result = calibre_cli.add_book(LIB, Path("/tmp/foo.epub"))

    assert result.duplicate is True
    assert result.added is False
    assert result.book_id is None


def test_add_book_failure(stub_run):
    stub_run(_cp(stderr="No such file", returncode=1))

    result = calibre_cli.add_book(LIB, Path("/tmp/missing.epub"))

    assert result.added is False
    assert result.duplicate is False
    assert "No such file" in result.message


# --- refresh_metadata ---------------------------------------------------------


_OPF_WITH_TITLE_AND_AUTHOR = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title>Existing Title</dc:title>
    <dc:creator>Existing Author</dc:creator>
  </metadata>
</package>
"""

_OPF_EMPTY = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title></dc:title>
  </metadata>
</package>
"""

_OPF_FETCHED = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title>Fetched Title</dc:title>
    <dc:creator>Fetched Author</dc:creator>
    <dc:description>A great book.</dc:description>
    <dc:publisher>Tor</dc:publisher>
    <meta name="calibre:series" content="My Series"/>
    <dc:subject>sci-fi</dc:subject>
  </metadata>
</package>
"""


def test_refresh_fill_blanks_skips_present_fields(stub_run, run_calls):
    stub_run(
        _cp(stdout=_OPF_WITH_TITLE_AND_AUTHOR),  # show_metadata
        _cp(stdout=_OPF_FETCHED),                # fetch-ebook-metadata
        _cp(returncode=0),                        # set_metadata
    )

    result = calibre_cli.refresh_metadata(
        LIB, 7, mode="fill_blanks", sources=["Amazon", "Google"]
    )

    assert result.state == "fetched"
    set_metadata_argv = run_calls[-1]
    fields = _argv_after(set_metadata_argv, "--field")
    field_names = [f.split(":", 1)[0] for f in fields]
    assert "title" not in field_names      # present in existing → skip
    assert "authors" not in field_names    # present in existing → skip
    assert "comments" in field_names       # absent in existing → apply
    assert "series" in field_names
    assert "publisher" in field_names
    assert "tags" in field_names


def test_refresh_overwrite_passes_all_fields(stub_run, run_calls):
    stub_run(
        _cp(stdout=_OPF_WITH_TITLE_AND_AUTHOR),
        _cp(stdout=_OPF_FETCHED),
        _cp(returncode=0),
    )

    result = calibre_cli.refresh_metadata(
        LIB, 7, mode="overwrite", sources=["Amazon", "Google"]
    )

    assert result.state == "fetched"
    set_metadata_argv = run_calls[-1]
    field_names = [f.split(":", 1)[0] for f in _argv_after(set_metadata_argv, "--field")]
    assert {"title", "authors", "comments", "series", "publisher", "tags"} <= set(field_names)


def test_refresh_no_match(stub_run):
    stub_run(
        _cp(stdout=_OPF_EMPTY),
        _cp(stdout="", stderr="No results found", returncode=1),
    )

    result = calibre_cli.refresh_metadata(
        LIB, 7, mode="fill_blanks", sources=["Amazon"]
    )

    assert result.state == "no_match"


# --- convert_book -------------------------------------------------------------


def test_convert_argv_shape(stub_run, run_calls, tmp_path, monkeypatch):
    src = tmp_path / "in.epub"
    src.write_bytes(b"epub")

    def resolver(book_id, fmt):
        assert (book_id, fmt) == (7, "EPUB")
        return src

    # The conversion produces a file under a temp dir; stub _run to create it.
    def fake_run(argv, *, input=None):
        run_calls.append(argv)
        if argv[0] == "ebook-convert":
            Path(argv[2]).write_bytes(b"converted")
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(calibre_cli, "_run", fake_run)

    result = calibre_cli.convert_book(
        LIB, 7, "AZW3", available_formats=["EPUB"], source_path_resolver=resolver
    )

    assert result.state == "done"
    convert_argv = next(a for a in run_calls if a[0] == "ebook-convert")
    assert convert_argv[1] == str(src)
    assert convert_argv[2].endswith(".azw3")
    add_format_argv = next(a for a in run_calls if a[:2] == ["calibredb", "add_format"])
    assert "7" in add_format_argv


def test_convert_no_source_format():
    def resolver(*_args):
        return None

    result = calibre_cli.convert_book(
        LIB, 7, "EPUB", available_formats=["EPUB"], source_path_resolver=resolver
    )

    assert result.state == "no_source"


# --- logging ------------------------------------------------------------------


def test_run_logs_argv(caplog, monkeypatch):
    # Replace subprocess.run so the real one isn't invoked.
    monkeypatch.setattr(
        calibre_cli.subprocess, "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, "", ""),
    )

    with caplog.at_level("INFO", logger=calibre_cli.log.name):
        calibre_cli._run(["calibredb", "add", "x.epub"])

    assert any("calibredb add x.epub" in r.message for r in caplog.records)

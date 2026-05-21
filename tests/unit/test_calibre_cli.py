from __future__ import annotations

import subprocess
import tempfile
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
        _cp(stdout=_OPF_FETCHED),  # fetch-ebook-metadata
        _cp(returncode=0),  # set_metadata
    )

    result = calibre_cli.refresh_metadata(LIB, 7, mode="fill_blanks", sources=["Amazon", "Google"])

    assert result.state == "fetched"
    set_metadata_argv = run_calls[-1]
    fields = _argv_after(set_metadata_argv, "--field")
    field_names = [f.split(":", 1)[0] for f in fields]
    assert "title" not in field_names  # present in existing → skip
    assert "authors" not in field_names  # present in existing → skip
    assert "comments" in field_names  # absent in existing → apply
    assert "series" in field_names
    assert "publisher" in field_names
    assert "tags" in field_names


def test_refresh_overwrite_passes_all_fields(stub_run, run_calls):
    stub_run(
        _cp(stdout=_OPF_WITH_TITLE_AND_AUTHOR),
        _cp(stdout=_OPF_FETCHED),
        _cp(returncode=0),
    )

    result = calibre_cli.refresh_metadata(LIB, 7, mode="overwrite", sources=["Amazon", "Google"])

    assert result.state == "fetched"
    set_metadata_argv = run_calls[-1]
    field_names = [f.split(":", 1)[0] for f in _argv_after(set_metadata_argv, "--field")]
    assert {"title", "authors", "comments", "series", "publisher", "tags"} <= set(field_names)


def _refresh_with_cover_stub(monkeypatch, run_calls, *, existing_cover: Path | None):
    """Stub `_run` so that fetch-ebook-metadata writes a fake cover when --cover= is passed.
    Also stub db.get_cover_path to control "book has cover already" state."""
    procs = {
        "show_metadata": _cp(stdout=_OPF_WITH_TITLE_AND_AUTHOR),
        "fetch": _cp(stdout=_OPF_FETCHED),
        "set_cover": _cp(returncode=0),
        "set_metadata": _cp(returncode=0),
    }

    def fake_run(argv, *, input=None):
        run_calls.append(argv)
        if argv[:2] == ["calibredb", "show_metadata"]:
            return procs["show_metadata"]
        if argv[0] == "fetch-ebook-metadata":
            cover_flag = next((a for a in argv if a.startswith("--cover=")), None)
            if cover_flag:
                Path(cover_flag.split("=", 1)[1]).write_bytes(b"\xff\xd8\xff\xe0fake")
            return procs["fetch"]
        if argv[:2] == ["calibredb", "set_cover"]:
            return procs["set_cover"]
        if argv[:2] == ["calibredb", "set_metadata"]:
            return procs["set_metadata"]
        return _cp(returncode=0)

    monkeypatch.setattr(calibre_cli, "_run", fake_run)

    from app.services import db

    monkeypatch.setattr(db, "get_cover_path", lambda lp, bid: existing_cover)


def test_refresh_fetch_covers_true_passes_cover_flag(monkeypatch, run_calls, tmp_path):
    _refresh_with_cover_stub(monkeypatch, run_calls, existing_cover=None)

    calibre_cli.refresh_metadata(
        LIB,
        7,
        mode="fill_blanks",
        sources=["Amazon"],
        fetch_covers=True,
    )

    fetch_argv = next(a for a in run_calls if a[0] == "fetch-ebook-metadata")
    assert any(a.startswith("--cover=") for a in fetch_argv)


def test_refresh_fetch_covers_false_omits_cover_flag(monkeypatch, run_calls, tmp_path):
    _refresh_with_cover_stub(monkeypatch, run_calls, existing_cover=None)

    calibre_cli.refresh_metadata(
        LIB,
        7,
        mode="fill_blanks",
        sources=["Amazon"],
        fetch_covers=False,
    )

    fetch_argv = next(a for a in run_calls if a[0] == "fetch-ebook-metadata")
    assert not any(a.startswith("--cover=") for a in fetch_argv)
    assert not any(a[:2] == ["calibredb", "set_cover"] for a in run_calls)


def test_refresh_fill_blanks_skips_set_cover_when_cover_exists(monkeypatch, run_calls, tmp_path):
    fake_cover = tmp_path / "existing-cover.jpg"
    fake_cover.write_bytes(b"existing")
    _refresh_with_cover_stub(monkeypatch, run_calls, existing_cover=fake_cover)

    calibre_cli.refresh_metadata(
        LIB,
        7,
        mode="fill_blanks",
        sources=["Amazon"],
        fetch_covers=True,
    )

    assert not any(a[:2] == ["calibredb", "set_cover"] for a in run_calls)


def test_refresh_fill_blanks_applies_cover_when_missing(monkeypatch, run_calls):
    _refresh_with_cover_stub(monkeypatch, run_calls, existing_cover=None)

    result = calibre_cli.refresh_metadata(
        LIB,
        7,
        mode="fill_blanks",
        sources=["Amazon"],
        fetch_covers=True,
    )

    assert any(a[:2] == ["calibredb", "set_cover"] for a in run_calls)
    assert "cover applied" in result.message


def test_refresh_overwrite_always_applies_cover(monkeypatch, run_calls, tmp_path):
    fake_cover = tmp_path / "existing-cover.jpg"
    fake_cover.write_bytes(b"existing")
    _refresh_with_cover_stub(monkeypatch, run_calls, existing_cover=fake_cover)

    result = calibre_cli.refresh_metadata(
        LIB,
        7,
        mode="overwrite",
        sources=["Amazon"],
        fetch_covers=True,
    )

    assert any(a[:2] == ["calibredb", "set_cover"] for a in run_calls)
    assert "cover applied" in result.message


def test_refresh_no_match(stub_run):
    stub_run(
        _cp(stdout=_OPF_EMPTY),
        _cp(stdout="", stderr="No results found", returncode=1),
    )

    result = calibre_cli.refresh_metadata(LIB, 7, mode="fill_blanks", sources=["Amazon"])

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


# --- convert_to_temp_file -----------------------------------------------------


def test_convert_to_temp_file_returns_path_with_preserved_stem(tmp_path, monkeypatch, run_calls):
    src = tmp_path / "Some Book - An Author.epub"
    src.write_bytes(b"epub bytes")

    def fake_run(argv, *, input=None):
        run_calls.append(argv)
        if argv[0] == "ebook-convert":
            # Simulate a successful conversion by creating the output file.
            Path(argv[2]).write_bytes(b"azw3 bytes")
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(calibre_cli, "_run", fake_run)

    out = calibre_cli.convert_to_temp_file(src, "AZW3")

    assert out is not None
    assert out.exists()
    # Same stem as the source, AZW3 extension, in a fresh temp dir.
    assert out.name == "Some Book - An Author.azw3"
    assert out.parent != src.parent
    assert tempfile.gettempdir() in str(out)
    convert_argv = run_calls[0]
    assert convert_argv[0] == "ebook-convert"
    assert convert_argv[1] == str(src)
    assert convert_argv[2] == str(out)


def test_convert_to_temp_file_passes_sibling_cover(tmp_path, monkeypatch, run_calls):
    """Calibre stores ``cover.jpg`` next to the book file in each library
    directory. The converter must pass it through ``--cover=`` so the
    Kindle library tile renders the right artwork instead of a placeholder."""
    src = tmp_path / "Book - Author.epub"
    src.write_bytes(b"epub")
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")

    def fake_run(argv, *, input=None):
        run_calls.append(argv)
        if argv[0] == "ebook-convert":
            Path(argv[2]).write_bytes(b"azw3")
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(calibre_cli, "_run", fake_run)

    out = calibre_cli.convert_to_temp_file(src, "AZW3")

    assert out is not None
    convert_argv = run_calls[0]
    assert f"--cover={cover}" in convert_argv


def test_convert_to_temp_file_omits_cover_when_missing(tmp_path, monkeypatch, run_calls):
    src = tmp_path / "Book - Author.epub"
    src.write_bytes(b"epub")

    def fake_run(argv, *, input=None):
        run_calls.append(argv)
        if argv[0] == "ebook-convert":
            Path(argv[2]).write_bytes(b"azw3")
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(calibre_cli, "_run", fake_run)

    out = calibre_cli.convert_to_temp_file(src, "AZW3")

    assert out is not None
    convert_argv = run_calls[0]
    assert not any(a.startswith("--cover=") for a in convert_argv)


def test_convert_to_temp_file_returns_none_on_nonzero_exit(tmp_path, monkeypatch, run_calls):
    src = tmp_path / "Book.epub"
    src.write_bytes(b"epub")

    def fake_run(argv, *, input=None):
        run_calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "boom")

    monkeypatch.setattr(calibre_cli, "_run", fake_run)

    assert calibre_cli.convert_to_temp_file(src, "AZW3") is None


def test_convert_to_temp_file_returns_none_on_empty_output(tmp_path, monkeypatch, run_calls):
    """ebook-convert can exit 0 but leave an empty file when the source is
    malformed in a way the converter mishandles silently. Treat zero-byte
    output as failure so the caller doesn't ship junk to the device."""
    src = tmp_path / "Book.epub"
    src.write_bytes(b"epub")

    def fake_run(argv, *, input=None):
        run_calls.append(argv)
        if argv[0] == "ebook-convert":
            Path(argv[2]).touch()
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(calibre_cli, "_run", fake_run)

    assert calibre_cli.convert_to_temp_file(src, "AZW3") is None


def test_convert_to_temp_file_cleans_up_failed_tmpdir(tmp_path, monkeypatch, run_calls):
    """No partial tmp directory should be left behind on failure."""
    src = tmp_path / "Book.epub"
    src.write_bytes(b"epub")

    seen_tmpdirs: list[Path] = []

    real_mkdtemp = tempfile.mkdtemp

    def tracking_mkdtemp(*a, **k):
        d = real_mkdtemp(*a, **k)
        seen_tmpdirs.append(Path(d))
        return d

    monkeypatch.setattr(calibre_cli.tempfile, "mkdtemp", tracking_mkdtemp)
    monkeypatch.setattr(
        calibre_cli,
        "_run",
        lambda argv, *, input=None: subprocess.CompletedProcess(argv, 1, "", "boom"),
    )

    assert calibre_cli.convert_to_temp_file(src, "AZW3") is None
    assert seen_tmpdirs, "test setup: mkdtemp should have been called"
    for d in seen_tmpdirs:
        assert not d.exists()


# --- logging ------------------------------------------------------------------


def test_run_logs_argv(caplog, monkeypatch):
    # Replace subprocess.run so the real one isn't invoked.
    monkeypatch.setattr(
        calibre_cli.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, "", ""),
    )

    with caplog.at_level("INFO", logger=calibre_cli.log.name):
        calibre_cli._run(["calibredb", "add", "x.epub"])

    assert any("calibredb add x.epub" in r.message for r in caplog.records)

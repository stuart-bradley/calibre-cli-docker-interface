from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree as ET

log = logging.getLogger(__name__)

# Exact substring Calibre emits to stdout (exit code 0) when a book matches an
# existing record by title+authors. Source: calibre/gui2/add.py and
# calibre/db/cli/cmd_add.py — stable across recent releases.
DUPLICATE_MARKER = "The following books were not added as they already exist in the database"

_ADDED_RE = re.compile(r"Added book ids?:\s*([\d,\s]+)", re.IGNORECASE)


RefreshMode = Literal["fill_blanks", "overwrite"]
ConvertTarget = Literal["EPUB", "AZW3", "MOBI"]


@dataclass(frozen=True)
class AddResult:
    added: bool
    duplicate: bool
    book_id: int | None
    message: str


@dataclass(frozen=True)
class RefreshResult:
    book_id: int
    state: Literal["fetched", "no_match", "error"]
    message: str


@dataclass(frozen=True)
class ConvertResult:
    book_id: int
    state: Literal["done", "no_source", "error"]
    message: str


def _run(argv: list[str], *, input: str | None = None) -> subprocess.CompletedProcess:
    log.info("calibre: %s", " ".join(argv))
    return subprocess.run(argv, capture_output=True, text=True, input=input, check=False)


def add_book(library_path: Path, file_path: Path) -> AddResult:
    proc = _run(
        [
            "calibredb",
            "add",
            "--library-path",
            str(library_path),
            str(file_path),
        ]
    )

    out = proc.stdout or ""
    err = proc.stderr or ""
    combined = out + err

    if DUPLICATE_MARKER in combined:
        return AddResult(added=False, duplicate=True, book_id=None, message=combined.strip())

    match = _ADDED_RE.search(combined)
    if match:
        first_id = int(match.group(1).split(",")[0].strip())
        return AddResult(
            added=True,
            duplicate=False,
            book_id=first_id,
            message=f"Added book id {first_id}",
        )

    msg = combined.strip()
    if proc.returncode != 0:
        return AddResult(
            added=False,
            duplicate=False,
            book_id=None,
            message=msg or f"calibredb add exit {proc.returncode}",
        )

    return AddResult(
        added=False,
        duplicate=False,
        book_id=None,
        message=msg or "no book id returned",
    )


def add_format(library_path: Path, book_id: int, file_path: Path) -> bool:
    proc = _run(
        [
            "calibredb",
            "add_format",
            "--library-path",
            str(library_path),
            str(book_id),
            str(file_path),
        ]
    )
    return proc.returncode == 0


def show_metadata_opf(library_path: Path, book_id: int) -> str:
    proc = _run(
        [
            "calibredb",
            "show_metadata",
            "--library-path",
            str(library_path),
            "--as-opf",
            str(book_id),
        ]
    )
    return proc.stdout or ""


# OPF fields we care about for fill-blanks decisions. Mapped to calibredb
# set_metadata --field names.
_OPF_FIELDS = {
    "title": ".//{http://purl.org/dc/elements/1.1/}title",
    "authors": ".//{http://purl.org/dc/elements/1.1/}creator",
    "comments": ".//{http://purl.org/dc/elements/1.1/}description",
    "series": ".//{http://www.idpf.org/2007/opf}meta[@name='calibre:series']",
    "publisher": ".//{http://purl.org/dc/elements/1.1/}publisher",
    "tags": ".//{http://purl.org/dc/elements/1.1/}subject",
}


def _present_fields_from_opf(opf_xml: str) -> set[str]:
    present: set[str] = set()
    if not opf_xml.strip():
        return present
    try:
        root = ET.fromstring(opf_xml)
    except ET.ParseError:
        return present
    for field, xpath in _OPF_FIELDS.items():
        node = root.find(xpath)
        if node is None:
            continue
        if field == "series":
            if node.get("content"):
                present.add(field)
        elif (node.text or "").strip():
            present.add(field)
    return present


def _fetch_metadata_opf(
    book_id: int,
    sources: list[str],
    opf_hint: str,
    *,
    cover_dest: Path | None = None,
) -> tuple[str | None, str]:
    args = ["fetch-ebook-metadata", "--opf"]
    if cover_dest is not None:
        args.append(f"--cover={cover_dest}")
    for src in sources:
        args.extend(["--allowed-plugin", src])

    # fetch-ebook-metadata needs query hints; pull title + authors from the existing OPF.
    title, authors = _title_and_authors_from_opf(opf_hint)
    if title:
        args.extend(["--title", title])
    if authors:
        args.extend(["--authors", " & ".join(authors)])

    proc = _run(args)
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return None, (proc.stderr or proc.stdout or "no metadata returned").strip()
    return proc.stdout, "fetched"


def set_cover(library_path: Path, book_id: int, cover_path: Path) -> bool:
    proc = _run(
        [
            "calibredb",
            "set_cover",
            "--library-path",
            str(library_path),
            str(book_id),
            str(cover_path),
        ]
    )
    return proc.returncode == 0


def _title_and_authors_from_opf(opf_xml: str) -> tuple[str | None, list[str]]:
    if not opf_xml.strip():
        return None, []
    try:
        root = ET.fromstring(opf_xml)
    except ET.ParseError:
        return None, []
    title_node = root.find(".//{http://purl.org/dc/elements/1.1/}title")
    title = (title_node.text or "").strip() if title_node is not None else None
    authors = [
        (n.text or "").strip()
        for n in root.findall(".//{http://purl.org/dc/elements/1.1/}creator")
        if (n.text or "").strip()
    ]
    return title, authors


def _set_metadata_argv(library_path: Path, book_id: int, fields: dict[str, str]) -> list[str]:
    argv = [
        "calibredb",
        "set_metadata",
        "--library-path",
        str(library_path),
        str(book_id),
    ]
    for name, value in fields.items():
        argv.extend(["--field", f"{name}:{value}"])
    return argv


def refresh_metadata(
    library_path: Path,
    book_id: int,
    *,
    mode: RefreshMode,
    sources: list[str],
    fetch_covers: bool = True,
) -> RefreshResult:
    existing_opf = show_metadata_opf(library_path, book_id)

    with tempfile.TemporaryDirectory() as tmp:
        cover_dest = Path(tmp) / f"{book_id}-cover.jpg" if fetch_covers else None
        fetched_opf, fetch_message = _fetch_metadata_opf(
            book_id,
            sources,
            existing_opf,
            cover_dest=cover_dest,
        )
        if fetched_opf is None:
            lower = fetch_message.lower()
            no_match = "no results" in lower or "no metadata" in lower
            state = "no_match" if no_match else "error"
            return RefreshResult(book_id=book_id, state=state, message=fetch_message)

        fetched_title, fetched_authors = _title_and_authors_from_opf(fetched_opf)
        fetched_fields: dict[str, str] = {}
        if fetched_title:
            fetched_fields["title"] = fetched_title
        if fetched_authors:
            fetched_fields["authors"] = " & ".join(fetched_authors)

        try:
            root = ET.fromstring(fetched_opf)
            desc = root.find(".//{http://purl.org/dc/elements/1.1/}description")
            if desc is not None and (desc.text or "").strip():
                fetched_fields["comments"] = (desc.text or "").strip()
            publisher = root.find(".//{http://purl.org/dc/elements/1.1/}publisher")
            if publisher is not None and (publisher.text or "").strip():
                fetched_fields["publisher"] = (publisher.text or "").strip()
            series = root.find(".//{http://www.idpf.org/2007/opf}meta[@name='calibre:series']")
            if series is not None and series.get("content"):
                fetched_fields["series"] = series.get("content", "")
            subjects = [
                (s.text or "").strip()
                for s in root.findall(".//{http://purl.org/dc/elements/1.1/}subject")
                if (s.text or "").strip()
            ]
            if subjects:
                fetched_fields["tags"] = ", ".join(subjects)
        except ET.ParseError:
            pass

        if mode == "fill_blanks":
            present = _present_fields_from_opf(existing_opf)
            to_apply = {k: v for k, v in fetched_fields.items() if k not in present}
        else:
            to_apply = fetched_fields

        cover_applied = False
        if fetch_covers and cover_dest is not None and cover_dest.exists():
            # In fill_blanks: only set cover if the book has none.
            # In overwrite: always replace.
            from app.services import db as _db

            should_apply = mode == "overwrite" or _db.get_cover_path(library_path, book_id) is None
            if should_apply:
                cover_applied = set_cover(library_path, book_id, cover_dest)

        if not to_apply and not cover_applied:
            return RefreshResult(
                book_id=book_id,
                state="fetched",
                message="no new fields to apply",
            )

        if to_apply:
            argv = _set_metadata_argv(library_path, book_id, to_apply)
            proc = _run(argv)
            if proc.returncode != 0:
                msg = (proc.stderr or proc.stdout).strip()
                return RefreshResult(book_id=book_id, state="error", message=msg)

        parts = []
        if to_apply:
            parts.append(f"updated {len(to_apply)} fields")
        if cover_applied:
            parts.append("cover applied")
        return RefreshResult(
            book_id=book_id,
            state="fetched",
            message="; ".join(parts) or "nothing to do",
        )


def _pick_source_format(formats: list[str], target: ConvertTarget) -> str | None:
    preferred_order = ["EPUB", "AZW3", "MOBI", "PDF"]
    for fmt in preferred_order:
        if fmt != target and fmt in formats:
            return fmt
    return None


def convert_book(
    library_path: Path,
    book_id: int,
    target: ConvertTarget,
    *,
    available_formats: list[str],
    source_path_resolver,
) -> ConvertResult:
    source_fmt = _pick_source_format([f.upper() for f in available_formats], target)
    if source_fmt is None:
        return ConvertResult(
            book_id=book_id,
            state="no_source",
            message="no convertible source format",
        )

    src = source_path_resolver(book_id, source_fmt)
    if src is None or not Path(src).exists():
        return ConvertResult(
            book_id=book_id,
            state="error",
            message=f"source {source_fmt} file missing on disk",
        )

    with tempfile.TemporaryDirectory() as tmp:
        dst = Path(tmp) / f"{book_id}.{target.lower()}"
        proc = _run(["ebook-convert", str(src), str(dst)])
        if proc.returncode != 0 or not dst.exists():
            return ConvertResult(
                book_id=book_id,
                state="error",
                message=(proc.stderr or proc.stdout or "ebook-convert failed").strip(),
            )

        if not add_format(library_path, book_id, dst):
            return ConvertResult(book_id=book_id, state="error", message="add_format failed")

    return ConvertResult(
        book_id=book_id,
        state="done",
        message=f"converted {source_fmt} → {target}",
    )

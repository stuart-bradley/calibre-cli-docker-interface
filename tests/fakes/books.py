"""Synthesised book files used by tests.

These don't reach real Calibre — they're just byte sequences the helpers in
``app.services.calibre_cli`` can parse (EXTH records 113/501, etc.).
"""

from __future__ import annotations

import struct


def make_minimal_mobi(uuid: str | None, cdetype: str | None) -> bytes:
    """Build a MOBI file just big enough that ``read_mobi_identity`` parses
    EXTH 113 and 501 out of it.

    Layout: 78-byte PalmDB header (with type=BOOK creator=MOBI), one
    record-info entry pointing at the PalmDOC + MOBI header + EXTH blob.
    """
    palmdoc = b"\x00" * 16
    exth_records = b""
    count = 0
    if uuid is not None:
        body = uuid.encode("utf-8")
        exth_records += struct.pack(">II", 113, 8 + len(body)) + body
        count += 1
    if cdetype is not None:
        body = cdetype.encode("utf-8")
        exth_records += struct.pack(">II", 501, 8 + len(body)) + body
        count += 1
    exth_header_len = 12 + len(exth_records)
    exth = b"EXTH" + struct.pack(">II", exth_header_len, count) + exth_records
    # MOBI header: only sig, header_length (+4) and exth_flag (+0x70) get
    # parsed. Pad to 0x80 bytes total.
    mobi_header_len = 0x80
    mobi = bytearray(b"MOBI" + struct.pack(">I", mobi_header_len))
    mobi += b"\x00" * (mobi_header_len - len(mobi))
    # exth_flag bit 0x40 at offset 0x70
    mobi[0x70:0x74] = struct.pack(">I", 0x40)
    rec0 = bytes(palmdoc + bytes(mobi) + exth)
    rec0_off = 88
    palmdb = bytearray(b"\x00" * 78)
    palmdb[60:68] = b"BOOKMOBI"
    palmdb[76:78] = struct.pack(">H", 1)
    record_info = struct.pack(">I", rec0_off) + b"\x00\x00\x00\x00"
    gap = b"\x00\x00"
    return bytes(palmdb) + record_info + gap + rec0

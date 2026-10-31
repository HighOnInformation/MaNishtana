"""Layer 1 of binary comparison: tell a rebuild from a change, with no tools.

Public interface:
    normalize(data, ignore_strings=None) -> (bytes, format)   build noise set to zero
    summarize(path_a, path_b, ignore_strings=None) -> BinarySummary
    BinarySummary   namedtuple(format, size_a, size_b, sha_a, sha_b,
                               regions, regions_total, strings_added,
                               strings_removed, strings_added_total,
                               strings_removed_total, code_regions=None)

`normalize` is what makes the core trick work for binaries. `prepare_copy` writes
its output into the normalized copy, so two builds of the same code that differ
only in link time, checksum and build ID become byte-identical there, and the
second `git diff` drops them — "noise only" keeps its one definition.

Only fields that are noise by construction are touched, and only in formats this
module can parse with certainty (PE and ELF). Anything malformed or unknown is
returned unchanged with format None: guessing at an unknown layout could erase a
real difference, which is the one mistake this tool must not make.

`ignore_strings` is the binary counterpart of a rule's `ignore_lines`: regexes the
user declares for build-specific text (version stamps, __DATE__/__TIME__, build
paths). Each match inside a printable string is zeroed on both sides. In PE and
ELF files, strings that lie in executable sections are never touched: there a
"string" is instruction bytes that happen to be printable, and zeroing them could
hide a real code change.

`summarize` describes what is left for a binary that still differs. Standard
library only, as everywhere in this package.
"""

import collections
import hashlib
import re
import struct

BinarySummary = collections.namedtuple(
    "BinarySummary",
    "format size_a size_b sha_a sha_b regions regions_total strings_added "
    "strings_removed strings_added_total strings_removed_total code_regions",
    defaults=(None,),
)

_BLOCK = 4096
_MAX_REGIONS = 5
_MAX_STRINGS = 20
# Eight, not the usual four or six: shorter printable runs are mostly instruction
# bytes that happen to be ASCII, and they bury the real string changes.
_STRING_RE = re.compile(rb"[\x20-\x7e]{8,}")

# PE debug directory entry types whose payload is per-link noise.
_DEBUG_CODEVIEW = 2
_DEBUG_REPRO = 16

# ELF
_SHT_NOTE = 7
_SHT_NOBITS = 8
_SHF_EXECINSTR = 0x4
_PT_LOAD = 1
_PT_NOTE = 4
_PF_X = 0x1
_NT_GNU_BUILD_ID = 3

# PE section characteristics
_SCN_CNT_CODE = 0x20
_SCN_MEM_EXECUTE = 0x20000000

# Candidate strings for ignore_strings: printable runs of 4+, the shortest a
# version or date fragment can usefully be.
_RUN_RE = re.compile(rb"[\x20-\x7e]{4,}")


def normalize(data, ignore_strings=None):
    """Return (data with build noise zeroed, "pe" | "elf" | None).

    `ignore_strings` is a list of regexes (str); see the module docstring.
    """
    buf = bytearray(data)
    kind = "pe" if buf[:2] == b"MZ" else "elf" if buf[:4] == b"\x7fELF" else None
    fmt = None
    try:
        if kind == "pe" and _pe(buf):
            fmt = "pe"
        elif kind == "elf" and _elf(buf):
            fmt = "elf"
    except (struct.error, IndexError, ValueError):
        buf, fmt = bytearray(data), None
    if ignore_strings:
        # By magic, not by whether the noise fields parsed: a PE or ELF with a
        # header we cannot follow still runs, and its code must stay protected.
        # Treating it as an unknown format would expose its code to the patterns.
        _ignore_strings(buf, kind, ignore_strings)
    return bytes(buf), fmt


def summarize(path_a, path_b, ignore_strings=None):
    """Describe how two binaries differ. Either path may be None (added/deleted).

    The hashes identify the files as they are; the regions are found on the
    normalized bytes, so a link stamp does not show up as a differing region next
    to the code change the reader is actually looking for.
    """
    data_a = _read(path_a)
    data_b = _read(path_b)
    fmt = _format_of(data_b if data_b is not None else data_a)

    regions, total, code_regions = [], 0, None
    if data_a is not None and data_b is not None:
        regions, blocks = _regions(normalize(data_a, ignore_strings)[0],
                                   normalize(data_b, ignore_strings)[0])
        total = len(blocks)
        code_regions = _count_in_code(blocks, data_a, data_b)

    strings_a = _strings(data_a)
    strings_b = _strings(data_b)
    added = sorted(strings_b - strings_a)
    removed = sorted(strings_a - strings_b)

    return BinarySummary(
        format=fmt,
        size_a=None if data_a is None else len(data_a),
        size_b=None if data_b is None else len(data_b),
        sha_a=None if data_a is None else hashlib.sha256(data_a).hexdigest(),
        sha_b=None if data_b is None else hashlib.sha256(data_b).hexdigest(),
        regions=regions,
        regions_total=total,
        strings_added=added[:_MAX_STRINGS],
        strings_removed=removed[:_MAX_STRINGS],
        strings_added_total=len(added),
        strings_removed_total=len(removed),
        code_regions=code_regions,
    )


# --- internals: PE -----------------------------------------------------------


def _pe(buf):
    """Zero the PE noise fields in place. False if this is not a PE we understand."""
    pe = _u32(buf, 0x3C)
    if buf[pe:pe + 4] != b"PE\x00\x00":
        return False
    coff = pe + 4
    sections_count = _u16(buf, coff + 2)
    optional_size = _u16(buf, coff + 16)
    opt = coff + 20
    magic = _u16(buf, opt)
    if magic == 0x10B:
        dirs_count_at, dirs_at = opt + 92, opt + 96
    elif magic == 0x20B:
        dirs_count_at, dirs_at = opt + 108, opt + 112
    else:
        return False

    _zero(buf, coff + 4, 4)            # COFF TimeDateStamp
    _zero(buf, opt + 64, 4)            # CheckSum

    sections = _pe_sections(buf, opt + optional_size, sections_count)
    dirs_count = _u32(buf, dirs_count_at)

    # Export directory (index 0): its TimeDateStamp is link time too.
    if dirs_count > 0:
        rva, size = struct.unpack_from("<II", buf, dirs_at)
        off = _rva_to_offset(sections, rva)
        if rva and size >= 8 and off is not None:
            _zero(buf, off + 4, 4)

    # Debug directory (index 6).
    if dirs_count > 6:
        rva, size = struct.unpack_from("<II", buf, dirs_at + 6 * 8)
        off = _rva_to_offset(sections, rva)
        if rva and off is not None:
            for entry in range(off, off + size - 27, 28):
                _zero(buf, entry + 4, 4)                       # TimeDateStamp
                kind, data_size, _addr, raw = struct.unpack_from("<IIII", buf, entry + 12)
                if not raw or raw + data_size > len(buf):
                    continue
                if kind == _DEBUG_CODEVIEW and buf[raw:raw + 4] == b"RSDS":
                    # GUID, age, and the PDB path: the path is wherever the
                    # build ran, so two builds in two directories differ there.
                    _zero(buf, raw + 4, data_size - 4)
                elif kind == _DEBUG_REPRO:
                    _zero(buf, raw, data_size)                 # the reproducible-build hash
    return True


def _ignore_strings(buf, fmt, patterns):
    """Zero every match of the patterns inside printable runs, outside code."""
    regexes = [re.compile(p.encode("utf-8")) for p in patterns]
    try:
        code = _code_ranges(buf, fmt)
    except (struct.error, IndexError, ValueError):
        code = [(0, len(buf))]       # cannot tell code from data: touch nothing
    for run in _RUN_RE.finditer(buf):
        start, stop = run.span()
        if any(start < hi and lo < stop for lo, hi in code):
            continue
        for regex in regexes:
            # Match the run on its own, so ^ and $ mean the string's start and end
            # (with finditer's pos argument, ^ would only ever match at byte 0).
            text = bytes(buf[start:stop])
            for match in regex.finditer(text):
                _zero(buf, start + match.start(), match.end() - match.start())


def _code_ranges(buf, fmt):
    """File ranges that may hold executable code. Raises when it cannot tell.

    The caller treats an exception as "everything is code" and touches nothing,
    so every doubt here resolves towards protecting more bytes, never fewer:

      PE   every section marked executable or code, widened to the 0x200 file
           alignment the loader rounds to, plus the headers themselves.
      ELF  the union of executable PT_LOAD segments (what the loader really maps
           executable) and SHF_EXECINSTR sections (what a relocatable .o has).
           Section headers the loader ignores can be garbage; if they cannot be
           read, the segments alone decide. Older layouts map read-only data in
           the executable segment too, which only means fewer strings qualify.
    """
    if fmt == "pe":
        pe = _u32(buf, 0x3C)
        if bytes(buf[pe:pe + 4]) != b"PE\x00\x00":
            raise ValueError("no PE signature")
        count = _u16(buf, pe + 6)
        table = pe + 24 + _u16(buf, pe + 20)
        ranges = [(0, table + count * 40)]
        for index in range(count):
            at = table + index * 40
            raw_size, raw_off = struct.unpack_from("<II", buf, at + 16)
            flags = _u32(buf, at + 36)
            if flags & (_SCN_MEM_EXECUTE | _SCN_CNT_CODE):
                low = raw_off & ~0x1FF
                high = (raw_off + raw_size + 0x1FF) & ~0x1FF
                ranges.append((low, high))
        return ranges
    if fmt == "elf":
        if buf[4] not in (1, 2) or buf[5] not in (1, 2):
            raise ValueError("unknown ELF class or byte order")
        end = "<" if buf[5] == 1 else ">"
        wide = buf[4] == 2
        if wide:
            phoff, shoff = struct.unpack_from(end + "QQ", buf, 0x20)
            phentsize, phnum, shentsize, shnum = struct.unpack_from(end + "HHHH", buf, 0x36)
        else:
            phoff, shoff = struct.unpack_from(end + "II", buf, 0x1C)
            phentsize, phnum, shentsize, shnum = struct.unpack_from(end + "HHHH", buf, 0x2A)
        ranges = _elf_exec_segments(buf, end, wide, phoff, phentsize, phnum)
        try:
            ranges += _elf_exec_sections(buf, end, wide, shoff, shentsize, shnum)
        except (struct.error, IndexError, ValueError):
            if not ranges:
                raise             # neither table readable: cannot tell
        return ranges
    return []


def _elf_exec_segments(buf, end, wide, phoff, phentsize, phnum):
    ranges = []
    for index in range(phnum if phoff else 0):
        at = phoff + index * phentsize
        if wide:
            kind, flags = struct.unpack_from(end + "II", buf, at)
            offset, = struct.unpack_from(end + "Q", buf, at + 8)
            size, = struct.unpack_from(end + "Q", buf, at + 32)
        else:
            kind, offset = struct.unpack_from(end + "II", buf, at)
            size, = struct.unpack_from(end + "I", buf, at + 16)
            flags, = struct.unpack_from(end + "I", buf, at + 24)
        if kind == _PT_LOAD and flags & _PF_X:
            ranges.append((offset, offset + size))
    return ranges


def _elf_exec_sections(buf, end, wide, shoff, shentsize, shnum):
    ranges = []
    for index in range(shnum if shoff else 0):
        at = shoff + index * shentsize
        if wide:
            kind, = struct.unpack_from(end + "I", buf, at + 4)
            flags, = struct.unpack_from(end + "Q", buf, at + 8)
            offset, size = struct.unpack_from(end + "QQ", buf, at + 24)
        else:
            kind, flags = struct.unpack_from(end + "II", buf, at + 4)
            offset, size = struct.unpack_from(end + "II", buf, at + 16)
        if flags & _SHF_EXECINSTR and kind != _SHT_NOBITS:
            ranges.append((offset, offset + size))
    return ranges


def _pe_sections(buf, table, count):
    """[(virtual address, virtual size, raw size, raw offset)] for each section."""
    result = []
    for index in range(count):
        at = table + index * 40
        vsize, vaddr, raw_size, raw_off = struct.unpack_from("<IIII", buf, at + 8)
        result.append((vaddr, vsize, raw_size, raw_off))
    return result


def _rva_to_offset(sections, rva):
    for vaddr, vsize, raw_size, raw_off in sections:
        if vaddr <= rva < vaddr + max(vsize, raw_size):
            return rva - vaddr + raw_off
    return None


# --- internals: ELF ----------------------------------------------------------


def _elf(buf):
    """Zero the build-id and debuglink CRC in place. False if not an ELF we understand.

    Both the section headers and the program headers are walked: a stripped binary
    may have no section table, and a build-id note found both ways is the same bytes,
    so zeroing it twice is harmless.
    """
    klass, order = buf[4], buf[5]
    if klass not in (1, 2) or order not in (1, 2):
        return False
    end = "<" if order == 1 else ">"
    wide = klass == 2

    if wide:
        phoff, shoff = struct.unpack_from(end + "QQ", buf, 0x20)
        phentsize, phnum, shentsize, shnum, shstrndx = struct.unpack_from(end + "HHHHH", buf, 0x36)
    else:
        phoff, shoff = struct.unpack_from(end + "II", buf, 0x1C)
        phentsize, phnum, shentsize, shnum, shstrndx = struct.unpack_from(end + "HHHHH", buf, 0x2A)

    for index in range(phnum if phoff else 0):
        at = phoff + index * phentsize
        if wide:
            kind, = struct.unpack_from(end + "I", buf, at)
            offset, = struct.unpack_from(end + "Q", buf, at + 8)
            size, = struct.unpack_from(end + "Q", buf, at + 32)
        else:
            kind, offset = struct.unpack_from(end + "II", buf, at)
            size, = struct.unpack_from(end + "I", buf, at + 16)
        if kind == _PT_NOTE:
            _elf_notes(buf, end, offset, size)

    if not shoff or not shnum:
        return True
    headers = []
    for index in range(shnum):
        at = shoff + index * shentsize
        if wide:
            name, kind = struct.unpack_from(end + "II", buf, at)
            offset, size = struct.unpack_from(end + "QQ", buf, at + 24)
        else:
            name, kind = struct.unpack_from(end + "II", buf, at)
            offset, size = struct.unpack_from(end + "II", buf, at + 16)
        headers.append((name, kind, offset, size))
    names_offset = headers[shstrndx][2] if shstrndx < len(headers) else None
    for name, kind, offset, size in headers:
        if kind == _SHT_NOTE:
            _elf_notes(buf, end, offset, size)
        elif names_offset is not None and _cstring(buf, names_offset + name) == b".gnu_debuglink":
            if size >= 4 and offset + size <= len(buf):
                _zero(buf, offset + size - 4, 4)               # CRC32 of the debug file
    return True


def _elf_notes(buf, end, offset, size):
    """Zero the descriptor of every GNU build-id note in [offset, offset+size)."""
    at, stop = offset, min(offset + size, len(buf))
    while at + 12 <= stop:
        namesz, descsz, kind = struct.unpack_from(end + "III", buf, at)
        name_at = at + 12
        desc_at = name_at + _align4(namesz)
        nxt = desc_at + _align4(descsz)
        if nxt > stop or nxt <= at:
            return
        if kind == _NT_GNU_BUILD_ID and bytes(buf[name_at:name_at + namesz]) == b"GNU\x00":
            _zero(buf, desc_at, descsz)
        at = nxt


def _cstring(buf, at):
    stop = buf.find(b"\x00", at)
    return bytes(buf[at:stop]) if stop >= 0 else b""


def _align4(value):
    return (value + 3) & ~3


# --- internals: shared -------------------------------------------------------


def _u16(buf, at):
    return struct.unpack_from("<H", buf, at)[0]


def _u32(buf, at):
    return struct.unpack_from("<I", buf, at)[0]


def _zero(buf, at, length):
    """Zero a span, refusing silently to write past the end of the file."""
    if at < 0 or length <= 0 or at + length > len(buf):
        return
    buf[at:at + length] = b"\x00" * length


def _read(path):
    if path is None:
        return None
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def _format_of(data):
    if data is None:
        return None
    return normalize(data)[1]


def _regions(data_a, data_b):
    """Differing byte regions at the same offsets: ([(start, end)], total count).

    Comparison is block by block first and byte by byte only inside the first few
    differing regions, so a large binary whose contents shifted (every block
    differs) costs a fast block scan, not a Python loop over every byte. The
    price is granularity: differences within the same or adjacent 4 KiB blocks
    are one region, trimmed to the first and last differing byte. A size
    difference counts as one region covering the tail.
    """
    common = min(len(data_a), len(data_b))
    blocks = []
    for start in range(0, common, _BLOCK):
        stop = min(start + _BLOCK, common)
        if data_a[start:stop] != data_b[start:stop]:
            if blocks and blocks[-1][1] == start:
                blocks[-1] = (blocks[-1][0], stop)
            else:
                blocks.append((start, stop))
    if len(data_a) != len(data_b):
        blocks.append((common, max(len(data_a), len(data_b))))

    exact = []
    for start, stop in blocks[:_MAX_REGIONS]:
        if start >= common:
            exact.append((start, stop))
            continue
        first = start
        while first < stop and data_a[first] == data_b[first]:
            first += 1
        last = stop
        while last > first and data_a[last - 1] == data_b[last - 1]:
            last -= 1
        exact.append((first, last))
    return exact, blocks


def _count_in_code(blocks, data_a, data_b):
    """How many differing regions overlap code on either side; None if unknown.

    The report uses it to tell "the bytes that differ are code, yet no engine
    found a changed function" from "the difference is in data". Unknown (None)
    for formats without code ranges or headers that cannot be read.
    """
    code = []
    for data in (data_a, data_b):
        kind = "pe" if data[:2] == b"MZ" else "elf" if data[:4] == b"\x7fELF" else None
        if kind is None:
            return None
        try:
            code.extend(_code_ranges(bytearray(data), kind))
        except (struct.error, IndexError, ValueError):
            return None
    return sum(1 for start, stop in blocks
               if any(start < hi and lo < stop for lo, hi in code))


def _strings(data):
    if data is None:
        return set()
    return set(match.decode("ascii") for match in _STRING_RE.findall(data))

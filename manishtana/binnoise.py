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
import difflib
import hashlib
import re
import struct

BinarySummary = collections.namedtuple(
    "BinarySummary",
    "format size_a size_b sha_a sha_b regions regions_total strings_added "
    "strings_removed strings_added_total strings_removed_total code_regions toolchain dependencies revisions "
    "layout_noise deletions",
    defaults=(None, None, None, None, 0, ()),
)

# Round 1 finding 2: the compiler or runtime that built a binary leaves its name in
# it ("GNU C99 13.2.0 ...", "GCC: (Rev2, ...) 13.2.0", "rustc 1.82.0 ..."). A new one
# is not a source change, so these strings are taken out of the string lists and
# reported apart, one line per tool, so GCC 13 -> 14 is a single labelled change.
_TOOLCHAIN_RES = (
    (None, re.compile(r"^'?(GNU C\+\+\d*|GNU C\d*) (\d+\.\d+(?:\.\d+)?)(?: (.*))?$")),
    ("GCC", re.compile(r"^GCC: (\(.*?\) ?\d+\.\d+(?:\.\d+)?)")),
    # anchored, so a message such as "requires rustc 1.70 or newer" stays a string
    ("clang", re.compile(r"^(?:[A-Za-z][A-Za-z0-9. -]*? )?clang version (\d+\.\d+(?:\.\d+)?)(?: \(.*)?$")),
    ("rustc", re.compile(r"^rustc (\d+\.\d+(?:\.\d+)?)(?: \([0-9a-f]{7,}[^)]*\))?$")),
)


def _toolchain_of(text):
    """(key, value) pairs of one identification string, or None."""
    for key, pattern in _TOOLCHAIN_RES:
        found = pattern.search(text)
        if found:
            if key is None:  # the dialect is part of the compiler's own name
                pairs = [(found.group(1), found.group(2))]
                if found.group(3):  # same version, other flags is still a build change
                    pairs.append((found.group(1) + " flags", found.group(3)))
                return pairs
            return [(key, found.group(1))]
    return None


def _toolchain_changes(tool_a, tool_b):
    """Lines like 'GNU C99: 13.2.0 -> 14.2.0' for the tools whose version changed."""
    def by_key(strings):
        out = {}
        for text in strings:
            for key, value in _toolchain_of(text):
                out.setdefault(key, set()).add(value)
        return out
    old, new = by_key(tool_a), by_key(tool_b)
    lines = []
    for key in sorted(set(old) | set(new)):
        before, after = old.get(key, set()), new.get(key, set())
        if before == after:
            continue
        if before and after:
            lines.append("%s: %s -> %s" % (key, ", ".join(sorted(before)),
                                           ", ".join(sorted(after))))
        elif after:
            lines.append("%s: added %s" % (key, ", ".join(sorted(after))))
        else:
            lines.append("%s: removed %s" % (key, ", ".join(sorted(before))))
    return lines


_RUSTC_PATH_RE = re.compile(rb"/rustc/([0-9a-f]{40})/")
# Debian builds against its own rust source package, whose path names the version.
_RUSTC_SRC_RE = re.compile(rb"/usr/src/rustc-(\d+\.\d+\.\d+)/")


def _rustc_commit_change(data_a, data_b):
    """Rust embeds '/rustc/<commit>/library/...' in its panic locations, inside packed
    literals, so a different compiler shows up only as scattered string noise."""
    def commits(data):
        return set(m.decode("ascii")[:12] for m in _RUSTC_PATH_RE.findall(data or b""))
    def versions(data):
        return set(m.decode("ascii") for m in _RUSTC_SRC_RE.findall(data or b""))
    lines = []
    old, new = versions(data_a), versions(data_b)
    if old and new and old != new:
        lines.append("rustc: %s -> %s" % (", ".join(sorted(old)), ", ".join(sorted(new))))
    old, new = commits(data_a), commits(data_b)
    if old and new and old != new:
        lines.append("rustc commit: %s -> %s" % (", ".join(sorted(old)), ", ".join(sorted(new))))
    return lines


# A Rust binary quotes its crates' source paths in its panic locations, either from
# cargo's registry ("/registry/src/<index>/<crate>-<version>/") or from a distro's
# vendored copy ("/usr/share/cargo/registry/<crate>-<version>/"). A Go binary embeds
# its module list ("dep<TAB>path<TAB>version") and, when built from a checkout, the
# revision ("vcs.revision=<sha>"). Those are dependency updates, not the project's.
_CRATE_RE = re.compile(
    rb"(?:/registry/src/[^/\x00-\x20]+/|/usr/share/cargo/registry/)"
    rb"([A-Za-z0-9_][A-Za-z0-9_.-]*?)-(\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?)(?:\+[0-9A-Za-z.]+)?/")
_GO_DEP_RE = re.compile(rb"dep\t([^\s\x00]+)\t(v[^\s\x00]+)")
_GO_REV_RE = re.compile(rb"vcs\.revision=([0-9a-f]{40})")


def _dependency_changes(data_a, data_b):
    """Lines like 'clap: 4.5.1 -> 4.5.2', 'foo: added 1.0.0', 'bar: removed 0.2.0'."""
    def found(data):
        out = {}
        for regex in (_CRATE_RE, _GO_DEP_RE):
            for name, version in regex.findall(data or b""):
                out.setdefault(name.decode("ascii"), set()).add(version.decode("ascii"))
        return out
    old, new = found(data_a), found(data_b)
    lines = []
    for name in sorted(set(old) | set(new)):
        before, after = old.get(name, set()), new.get(name, set())
        if before == after:
            continue
        if before and after:
            lines.append("%s: %s -> %s" % (name, ", ".join(sorted(before)), ", ".join(sorted(after))))
        elif after:
            lines.append("%s: added %s" % (name, ", ".join(sorted(after))))
        else:
            lines.append("%s: removed %s" % (name, ", ".join(sorted(before))))
    return lines


def _build_revisions(data_a, data_b):
    """(old, new) git revision a Go binary was built from, or None if same or unknown."""
    def revision(data):
        found = _GO_REV_RE.search(data or b"")
        return found.group(1).decode("ascii") if found else None
    old, new = revision(data_a), revision(data_b)
    return (old, new) if old and new and old != new else None


_BLOCK = 4096
_MAX_REGIONS = 5
_MAX_STRINGS = 20
# Eight, not the usual four or six: shorter printable runs are mostly instruction
# bytes that happen to be ASCII, and they bury the real string changes.
_STRING_RE = re.compile(rb"[\x20-\x7e]{8,}")
# Two short shapes are worth the exception, because they are what a release
# changes: a version ("1.7.1") and a terminal escape ("\x1b[0;37m", a colour).
# Each must stand alone as a C string (NUL or file start before, NUL after),
# which instruction bytes spelling "1.2.3" by chance rarely do. Go and Rust pack
# their strings without NULs; there a short version sits in a longer run instead.
_SHORT_RE = re.compile(rb"(?<![\x01-\xff])(\x1b?[\x20-\x7e]{3,7})\x00")
_LABEL_RE = re.compile(rb"[A-Z]{3,}:[ ]*\Z")   # "ERROR: ", "FATAL: ": a label a release may change
_VERSION_RE = re.compile(rb"v?\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.]+)?\Z")
_ESCAPE_RE = re.compile(rb"\x1b\[[0-9;]*[A-Za-z]\Z")
# Long near-copies (see _excerpt_pairs): strings over _EXCERPT_OVER characters
# whose similarity is at least _EXCERPT_RATIO are cut to _EXCERPT_CONTEXT
# characters each side of every difference, each piece at most _EXCERPT_PIECE.
_EXCERPT_OVER = 120
_EXCERPT_RATIO = 0.6
_EXCERPT_CONTEXT = 24
_EXCERPT_PIECE = 160

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
        norm_a = normalize(data_a, ignore_strings)[0]
        norm_b = normalize(data_b, ignore_strings)[0]
        regions, blocks = _regions(norm_a, norm_b)
        total = len(blocks)
        code_regions = _count_in_code(blocks, norm_a, norm_b)

    strings_a = _strings(data_a)
    strings_b = _strings(data_b)
    tool_a = {text for text in strings_a if _toolchain_of(text)}
    tool_b = {text for text in strings_b if _toolchain_of(text)}
    strings_a, strings_b = strings_a - tool_a, strings_b - tool_b
    new_b, new_a, layout, deletions = _drop_layout_noise(strings_b - strings_a, strings_a - strings_b,
                                              strings_a, strings_b)
    added, removed = _excerpt_pairs(_ranked(new_b), _ranked(new_a))

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
        toolchain=_toolchain_changes(tool_a, tool_b) + _rustc_commit_change(data_a, data_b),
        dependencies=_dependency_changes(data_a, data_b),
        revisions=_build_revisions(data_a, data_b),
        layout_noise=layout,
        deletions=sorted(deletions),
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
    """How many differing regions have differing bytes in code; None if unknown.

    The report uses it to tell "the bytes that differ are code, yet no engine
    found a changed function" from "the difference is in data". Two things keep
    it from over-claiming:

      * Each 4 KiB-granular region is trimmed to its first and last differing
        byte before the test, so a data change that merely shares a block with
        the end of .text is not code.
      * It asks _exact_code_ranges, not _code_ranges. The latter errs wide on
        purpose (headers, alignment padding, read-only data in an executable
        segment) because there a wrong "code" only means less normalization;
        here a wrong "code" sends the reader to a disassembler for a version
        field.

    Unknown (None) for formats without code ranges or headers that cannot be read.
    """
    code = []
    for data in (data_a, data_b):
        kind = "pe" if data[:2] == b"MZ" else "elf" if data[:4] == b"\x7fELF" else None
        if kind is None:
            return None
        try:
            code.extend(_exact_code_ranges(bytearray(data), kind))
        except (struct.error, IndexError, ValueError):
            return None
    count = 0
    common = min(len(data_a), len(data_b))
    for start, stop in blocks:
        if not any(start < hi and lo < stop for lo, hi in code):
            continue
        if start < common:
            stop = min(stop, common)
            while start < stop and data_a[start] == data_b[start]:
                start += 1
            while stop > start and data_a[stop - 1] == data_b[stop - 1]:
                stop -= 1
        if any(start < hi and lo < stop for lo, hi in code):
            count += 1
    return count


def _exact_code_ranges(buf, fmt):
    """Where code is, as precisely as the file says: for describing, not protecting.

    PE: executable sections' raw data, no headers, no alignment widening.
    ELF: SHF_EXECINSTR sections when the section table can be read; otherwise the
    executable segments (wider, but the best the file offers).
    """
    if fmt == "pe":
        pe = _u32(buf, 0x3C)
        if bytes(buf[pe:pe + 4]) != b"PE\x00\x00":
            raise ValueError("no PE signature")
        count = _u16(buf, pe + 6)
        table = pe + 24 + _u16(buf, pe + 20)
        ranges = []
        for index in range(count):
            at = table + index * 40
            raw_size, raw_off = struct.unpack_from("<II", buf, at + 16)
            if _u32(buf, at + 36) & (_SCN_MEM_EXECUTE | _SCN_CNT_CODE):
                ranges.append((raw_off, raw_off + raw_size))
        return ranges
    if fmt == "elf":
        end = "<" if buf[5] == 1 else ">"
        wide = buf[4] == 2
        if wide:
            phoff, shoff = struct.unpack_from(end + "QQ", buf, 0x20)
            phentsize, phnum, shentsize, shnum = struct.unpack_from(end + "HHHH", buf, 0x36)
        else:
            phoff, shoff = struct.unpack_from(end + "II", buf, 0x1C)
            phentsize, phnum, shentsize, shnum = struct.unpack_from(end + "HHHH", buf, 0x2A)
        try:
            sections = _elf_exec_sections(buf, end, wide, shoff, shentsize, shnum)
        except (struct.error, IndexError, ValueError):
            sections = []
        return sections or _elf_exec_segments(buf, end, wide, phoff, phentsize, phnum)
    return []


def _ranked(strings):
    """Sort strings so the ones a reviewer reads come first.

    Only a few are listed, and alphabetical order put symbol-table entries and
    instruction bytes that happen to be ASCII ahead of the sentences. Classes:
    0 a version, an escape, or a sentence-like string (a space between letters);
    1 anything else; 2 a lookalike of instruction bytes or padding (under half
    letters, or at most three distinct characters). Inside a class: alphabetical,
    so the order is stable. Only the order changes, never which strings exist.
    """
    return sorted(strings, key=lambda text: (_string_class(text), text))


def _string_class(text):
    letters = sum(1 for char in text if char.isalpha())
    if _VERSION_RE.match(text.encode("ascii", "replace")) or text.startswith("\\x1b["):
        return 0
    if len(set(text)) <= 3 or letters * 2 < len(text):
        return 2
    if " " in text.strip() and letters * 10 >= len(text) * 6:
        return 0
    return 1


def _excerpt_pairs(added, removed):
    """Shorten long near-copies to the parts that differ.

    Rust and Go store string literals back to back with no NUL between them, so
    one printable run can be kilobytes of help text, and a version bump inside it
    listed the whole run twice (added and removed). A long added string is paired
    with the long removed string most like it; when they are mostly the same, each
    is replaced by excerpts around the differences. The lists keep their length
    and order, so the totals stay true; unpaired strings are left whole.
    """
    long_added = [i for i, text in enumerate(added) if len(text) > _EXCERPT_OVER]
    long_removed = [i for i, text in enumerate(removed) if len(text) > _EXCERPT_OVER]
    if not long_added or not long_removed:
        return added, removed
    candidates = []
    for i in long_added:
        for j in long_removed:
            matcher = difflib.SequenceMatcher(None, removed[j], added[i], autojunk=False)
            if matcher.real_quick_ratio() < _EXCERPT_RATIO \
                    or matcher.quick_ratio() < _EXCERPT_RATIO:
                continue
            ratio = matcher.ratio()
            if ratio >= _EXCERPT_RATIO:
                candidates.append((-ratio, i, j, matcher))
    candidates.sort(key=lambda c: c[:3])
    added, removed = list(added), list(removed)
    used_a, used_r = set(), set()
    for _, i, j, matcher in candidates:
        if i in used_a or j in used_r:
            continue
        used_a.add(i)
        used_r.add(j)
        old_parts, new_parts = [], []
        for tag, a1, a2, b1, b2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            old_parts.append(_excerpt(removed[j], a1, a2))
            new_parts.append(_excerpt(added[i], b1, b2))
        removed[j] = _join_excerpts(old_parts)
        added[i] = _join_excerpts(new_parts)
    return added, removed


def _excerpt(text, start, stop):
    return (max(0, start - _EXCERPT_CONTEXT), min(len(text), stop + _EXCERPT_CONTEXT), text)


def _join_excerpts(parts):
    """Merge overlapping windows; "…" marks text left out at either end or between."""
    merged = []
    for start, stop, text in parts:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(stop, merged[-1][1]), text)
        else:
            merged.append((start, stop, text))
    pieces = []
    for start, stop, text in merged:
        piece = text[start:stop]
        if len(piece) > _EXCERPT_PIECE:      # a long insertion: its two ends only
            half = _EXCERPT_PIECE // 2
            piece = piece[:half] + "…" + piece[-half:]
        pieces.append(piece)
    first, last = merged[0], merged[-1]
    return (("…" if first[0] > 0 else "") + " … ".join(pieces)
            + ("…" if last[1] < len(last[2]) else ""))


# Build hashes embedded in Rust paths: the cargo registry directory
# ("index.crates.io-<16 hex>") and the rustc commit ("/rustc/<40 hex>/"). A rebuild
# of the same version changes them, so they are masked before strings are compared.
_BUILD_HASH_RE = re.compile(rb"(index\.crates\.io-)[0-9a-f]{16}|(/rustc/)[0-9a-f]{40}(?![0-9a-f])")


def _mask_build_hashes(data):
    return _BUILD_HASH_RE.sub(
        lambda m: (m.group(1) + b"<hash>") if m.group(1) else (m.group(2) + b"<hash>"), data)


# Source paths that Rust packs between literals. A path inside the compiler library or
# a cargo registry is toolchain churn: a rebuild with another rustc or registry moves
# or renames them (the version of a dependency is reported on its own line). The
# project's own paths (crates/, src/) stay as pieces. Lazy up to the first ".rs",
# because the next literal is glued right after it.
# A bare "std/src/" (no "library/" before it) is only toolchain when what follows is a
# real top-level std/alloc module or file; otherwise "nostd/src/main.rs" would be cut.
_STD_MODULES = (
    "alloc|ascii|backtrace|borrow|boxed|bstr|collections|env|error|f32|f64|ffi|fmt|fs|hash|"
    "io|keyword_docs|lib|macros|net|num|os|panic|panicking|path|personality|pipe|prelude|"
    "process|random|raw_vec|rc|rt|slice|str|string|sync|sys|sys_common|task|thread|time|vec")
_TOOLCHAIN_PATH_RE = (
    r"(?:(?:/[\w<>.+-]+)*?/registry/src/[\w<>.+-]+/|/rustc/<hash>/|/rust/deps/|"
    r"/usr/src/rustc-[\d.]+/|(?:/[\w.+-]+)*?/rustlib/(?:[\w.+-]+/)*?library/|"
    r"library/(?=(?:core|std|alloc)/src/)|(?=(?:std|alloc)/src/(?:%s)(?:/|\.rs)))"
    r"[\w<>.+/-]*?\.rs") % _STD_MODULES
_OWN_PATH_RE = r"(?:crates/|(?<![\w/.+-])src/)[\w.+-]+(?:/[\w.+-]+)*?\.rs"
_PATH_SPLIT_RE = re.compile("(%s)|(%s)" % (_TOOLCHAIN_PATH_RE, _OWN_PATH_RE))
_PIECE_MIN = 8


def _pieces(text):
    """Split a packed literal at its source paths: drop toolchain paths, keep the rest.

    Literals are glued together, and a rebuild lays them out in another order, so a
    whole string differs while every piece is the same. Text without a path is
    returned as is.
    """
    parts = _PATH_SPLIT_RE.split(text)
    if len(parts) == 1:
        return [text]
    pieces = []
    for at in range(0, len(parts), 3):
        pieces.append(parts[at])
        if at + 2 < len(parts) and parts[at + 2]:
            pieces.append(parts[at + 2])
    return [p.strip() for p in pieces if p and len(p.strip()) >= _PIECE_MIN]


def _strings(data):
    if data is None:
        return set()
    data = _mask_build_hashes(data)
    found = set()
    for match in _STRING_RE.findall(data):
        found.update(_pieces(match.decode("ascii")))
    for match in _SHORT_RE.finditer(data):
        text = match.group(1)
        if _ESCAPE_RE.match(text) or _VERSION_RE.match(text) or _LABEL_RE.match(text):
            found.add(text.decode("ascii").replace("\x1b", "\\x1b"))
    return found


# A rebuild lays the packed literals out again, so a string can differ only because of
# its neighbours, and compiled code that happens to be ASCII differs on every rebuild.
# A string is layout noise if each of its letters and digits is covered by an 8-character
# window that exists on the other side; only punctuation at a join may be unseen. A
# replaced or added word or number is not dropped by this (a pure deletion is). Skipped for very large
# string tables, where the window sets would cost too much memory.
_WINDOW = 8
_WINDOW_LIMIT = 6_000_000
_WORD_RE = re.compile(r"[a-z]{3}|[A-Z][a-z]{2}")


# An upper-case name ("NO_COLOR", "RG_DEBUG_LOG") or label ("WARNING:") has no
# lower-case word but is text a release adds; instruction bytes ("AWAVAUAT", "H;l$`u!H")
# have neither an underscore between upper-case runs nor a trailing colon.
_UPPER_NAME_RE = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\Z|[A-Z]{3,}:[ ]*\Z")


def _is_code_like(text):
    """Short, no 3-letter word, no format mark and not a version: instruction bytes."""
    return (len(text) <= 12 and "%" not in text and not _WORD_RE.search(text)
            and not _UPPER_NAME_RE.match(text)
            and not text.startswith("\\x1b[") and not _VERSION_RE.match(text.encode("ascii", "replace")))


def _windows(strings):
    found = set()
    for text in strings:
        for at in range(len(text) - _WINDOW + 1):
            found.add(text[at:at + _WINDOW])
    return found


_EDGE_WORD = 7          # a short literal glued to one end of a seen string, letters only
_EDGE_REST = 16         # ... and at least this much of the string is seen elsewhere


def _is_relaid(text, other_windows, other_text):
    """True if nothing but a join is unseen: every letter or digit is covered by a window
    that exists on the other side, except at most a short letters-only literal (or
    punctuation) glued to one end of a long string that is otherwise seen.

    That edge literal must itself exist on the other side (a glued literal moves, it
    does not vanish) and hold at least three letters, so "do not follow", "Enable" for
    "Disable" and a plural "s" stay changes. Interior changes, digits (a version bump)
    and longer runs are new text. Limit: a changed edge word of 3 to 7 letters that
    happens to appear elsewhere on the other side still looks like a join.
    """
    covered = [False] * len(text)
    for at in range(len(text) - _WINDOW + 1):
        if text[at:at + _WINDOW] in other_windows:
            for i in range(at, at + _WINDOW):
                covered[i] = True
    if all(covered):
        return True
    seen = [i for i, done in enumerate(covered) if done]
    if len(seen) < _EDGE_REST:
        return False
    first, last = seen[0], seen[-1]
    for i, (char, done) in enumerate(zip(text, covered)):
        if done or not char.isalnum():
            continue
        edge = i < first or i > last
        if not edge or char.isdigit():
            return False
    if first > _EDGE_WORD or len(text) - 1 - last > _EDGE_WORD:
        return False
    for fragment, body, tail in ((text[:first], text[first:first + _WINDOW], False),
                                 (text[last + 1:], text[last + 1 - _WINDOW:last + 1], True)):
        letters = sum(1 for char in fragment if char.isalnum())
        if not letters:
            continue
        if letters < 3 or fragment.strip() not in other_text:
            return False
        if not _at_literal_edge(body, other_text, tail):
            return False
    return True


def _at_literal_edge(window, other_text, tail):
    """True if `window` sits at the end (tail) or start of a literal on the other side.

    A glued literal is a join: the seen text ends (or begins) where the other build's
    literal ends (or begins). If the seen text continues there with other characters,
    the literal was one string and its end word changed.
    """
    probe = window.rstrip() if tail else window.lstrip()
    if not probe:
        return True
    at = other_text.find(probe)
    while at >= 0:
        if tail:
            i = at + len(probe)
            while i < len(other_text) and other_text[i] == " ":
                i += 1
            if i >= len(other_text) or other_text[i] == "\x00":
                return True
        else:
            i = at - 1
            while i >= 0 and other_text[i] == " ":
                i -= 1
            if i < 0 or other_text[i] == "\x00":
                return True
        at = other_text.find(probe, at + 1)
    return False


_DELETION_TRIES = 20
_MAX_LITERAL = 4096          # a longer literal is not scanned for a removed piece
_MAX_CHUNK = 200             # a longer removed piece is shown cut to this
_MAX_DELETION_CHECKS = 2000  # strings examined per build; the rest count as layout


def _removed_chunk(text, other_text, own_text):
    """The piece `other_text` has and `text` lacks, if `text` is one of its literals with
    a single contiguous piece cut out and that piece exists nowhere on text's own side.

    Such a string is covered by the other side's windows just as a re-glued literal is,
    but nothing moved: text was removed (or, seen from the other side, inserted). A
    moved chunk occurs as often on the string's own side as on the other side; a
    deleted one occurs less often. Counting, not mere presence, because a short word
    ("not") almost always exists somewhere else in the build.
    """
    at = other_text.find(text[:_WINDOW])
    for _ in range(_DELETION_TRIES):
        if at < 0:
            return None
        start = other_text.rfind("\x00", 0, at) + 1
        stop = other_text.find("\x00", at)
        literal = other_text[start:len(other_text) if stop < 0 else stop]
        if len(literal) > _MAX_LITERAL:
            return None
        if len(literal) > len(text):
            k = 0
            while k < len(text) and text[k] == literal[k]:
                k += 1
            if literal.endswith(text[k:]):
                chunk = literal[k:len(literal) - (len(text) - k)].strip()
                if len(chunk) > _MAX_CHUNK:
                    chunk = chunk[:_MAX_CHUNK]
                    shown = chunk + "…"
                else:
                    shown = chunk
                if (_WORD_RE.search(chunk)   # a removed word, not stray code bytes
                        and own_text.count(chunk) < other_text.count(chunk)):
                    return shown
                return None
        at = other_text.find(text[:_WINDOW], at + 1)
    return None


def _drop_layout_noise(new_b, new_a, all_a, all_b):
    """(added, removed, how many were dropped, deletions) from the strings only one side has.

    `deletions` are strings that differ from a literal on the other side only by a
    removed or inserted piece; they are reported, labelled, not dropped.
    """
    dropped = 0
    deletions = []
    code_b = {t for t in new_b if _is_code_like(t)}
    code_a = {t for t in new_a if _is_code_like(t)}
    new_b, new_a, dropped = new_b - code_b, new_a - code_a, len(code_b) + len(code_a)
    if sum(map(len, all_a)) + sum(map(len, all_b)) <= _WINDOW_LIMIT:
        win_a, win_b = _windows(all_a), _windows(all_b)
        text_a, text_b = "\x00".join(all_a), "\x00".join(all_b)

        checks = [0]

        def keep(strings, windows, other_text, own_text, verb):
            kept = set()
            for t in sorted(strings):   # sorted: the budget cuts the same strings every run
                if len(t) < _WINDOW or not _is_relaid(t, windows, other_text):
                    kept.add(t)
                    continue
                checks[0] += 1
                chunk = (_removed_chunk(t, other_text, own_text)
                         if checks[0] <= _MAX_DELETION_CHECKS else None)
                if chunk is not None:
                    deletions.append("%s (%s: %s)" % (t, verb, chunk))
            return kept

        keep_b = keep(new_b, win_a, text_a, text_b, "removed")
        keep_a = keep(new_a, win_b, text_b, text_a, "inserted")
        dropped += (len(new_b) - len(keep_b)) + (len(new_a) - len(keep_a)) - len(deletions)
        new_b, new_a = keep_b, keep_a
    return new_b, new_a, dropped, deletions

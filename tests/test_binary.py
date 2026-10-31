"""Unit tests for binary comparison — no git, no IDA, no Ghidra, no network.

Kept apart from test_manishtana.py so the binary feature can be carried to another
copy of the repository as new files, with few edits to shared ones.

PE and ELF inputs are synthetic, built byte by byte with struct: the smallest file
that has each noise field the normalizer must erase, at a known offset, so a test
can say exactly which bytes changed and which must not have.
"""

import os
import shutil
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manishtana.binnoise import normalize, summarize
from manishtana.compare import Comparison, binary_pairs
from manishtana.output import build_html, build_json, build_report
from manishtana.rules import BINARY_STEP, prepare_copy


# --- synthetic binaries ------------------------------------------------------

PE_OFF = 0x80            # e_lfanew
SECTION_RAW = 0x200      # file offset of the one section
SECTION_VA = 0x1000      # its virtual address


def make_pe(stamp=0x11111111, checksum=0x22222222, guid=b"G" * 16, age=7,
            repro=b"R" * 32, export_stamp=0x33333333, code=b"\x90" * 16):
    """A PE32+ with a debug directory (CodeView + REPRO) and an export directory.

    Returns (bytes, offsets) where offsets names where each noise field lives.
    """
    buf = bytearray(0x400)
    buf[0:2] = b"MZ"
    struct.pack_into("<I", buf, 0x3C, PE_OFF)
    buf[PE_OFF:PE_OFF + 4] = b"PE\x00\x00"
    coff = PE_OFF + 4
    opt_size = 112 + 16 * 8
    struct.pack_into("<HHIIIHH", buf, coff, 0x8664, 1, stamp, 0, 0, opt_size, 0x22)
    opt = coff + 20
    struct.pack_into("<H", buf, opt, 0x20B)
    struct.pack_into("<I", buf, opt + 64, checksum)
    struct.pack_into("<I", buf, opt + 108, 16)

    # Section layout (file offsets from SECTION_RAW, RVAs from SECTION_VA):
    export_at, debug_at, cv_at, repro_at, code_at = 0x00, 0x40, 0x80, 0xC0, 0x100
    rva = lambda off: SECTION_VA + off
    struct.pack_into("<II", buf, opt + 112, rva(export_at), 40)          # export dir
    struct.pack_into("<II", buf, opt + 112 + 6 * 8, rva(debug_at), 56)   # 2 debug entries

    table = opt + opt_size
    buf[table:table + 8] = b".text\x00\x00\x00"
    struct.pack_into("<IIII", buf, table + 8, 0x200, SECTION_VA, 0x200, SECTION_RAW)

    base = SECTION_RAW
    struct.pack_into("<II", buf, base + export_at, 0, export_stamp)
    struct.pack_into("<IIHHIIII", buf, base + debug_at, 0, stamp, 0, 0, 2, 24 + 4,
                     rva(cv_at), base + cv_at)
    struct.pack_into("<IIHHIIII", buf, base + debug_at + 28, 0, stamp, 0, 0, 16, len(repro),
                     rva(repro_at), base + repro_at)
    buf[base + cv_at:base + cv_at + 4] = b"RSDS"
    buf[base + cv_at + 4:base + cv_at + 20] = guid
    struct.pack_into("<I", buf, base + cv_at + 20, age)
    buf[base + cv_at + 24:base + cv_at + 28] = b"x.pd"
    buf[base + repro_at:base + repro_at + len(repro)] = repro
    buf[base + code_at:base + code_at + len(code)] = code
    offsets = {"code": base + code_at, "cv_path": base + cv_at + 24}
    return bytes(buf), offsets


def make_elf(build_id=b"B" * 20, crc=0x44444444, code=b"\x90" * 16, wide=True, little=True):
    """An ELF with a GNU build-id note (as section and PT_NOTE) and a .gnu_debuglink."""
    end = "<" if little else ">"
    ehsize = 64 if wide else 52
    phentsize = 56 if wide else 32
    shentsize = 64 if wide else 40

    note = struct.pack(end + "III", 4, len(build_id), 3) + b"GNU\x00" + build_id
    debuglink = b"x.debug\x00" + struct.pack(end + "I", crc)
    shstrtab = b"\x00.note.gnu.build-id\x00.gnu_debuglink\x00.shstrtab\x00.text\x00"

    phoff = ehsize
    note_off = phoff + phentsize
    link_off = note_off + len(note)
    code_off = link_off + len(debuglink)
    strtab_off = code_off + len(code)
    shoff = (strtab_off + len(shstrtab) + 7) & ~7

    buf = bytearray(shoff + 5 * shentsize)
    buf[0:4] = b"\x7fELF"
    buf[4] = 2 if wide else 1
    buf[5] = 1 if little else 2
    buf[6] = 1
    if wide:
        struct.pack_into(end + "HHIQQQIHHHHHH", buf, 16, 2, 62, 1, 0, phoff, shoff, 0, ehsize,
                         phentsize, 1, shentsize, 5, 4)
        struct.pack_into(end + "IIQQQQQQ", buf, phoff, 4, 4, note_off, 0, 0, len(note),
                         len(note), 4)
    else:
        struct.pack_into(end + "HHIIIIIHHHHHH", buf, 16, 2, 3, 1, 0, phoff, shoff, 0, ehsize,
                         phentsize, 1, shentsize, 5, 4)
        struct.pack_into(end + "IIIIIIII", buf, phoff, 4, note_off, 0, 0, len(note),
                         len(note), 4, 4)
    buf[note_off:note_off + len(note)] = note
    buf[link_off:link_off + len(debuglink)] = debuglink
    buf[code_off:code_off + len(code)] = code
    buf[strtab_off:strtab_off + len(shstrtab)] = shstrtab

    def section(index, name, kind, offset, size):
        at = shoff + index * shentsize
        if wide:
            struct.pack_into(end + "IIQQQQIIQQ", buf, at, name, kind, 0, 0, offset, size,
                             0, 0, 4, 0)
        else:
            struct.pack_into(end + "IIIIIIIIII", buf, at, name, kind, 0, 0, offset, size,
                             0, 0, 4, 0)

    section(1, shstrtab.index(b".note.gnu.build-id"), 7, note_off, len(note))
    section(2, shstrtab.index(b".gnu_debuglink"), 1, link_off, len(debuglink))
    section(3, shstrtab.index(b".text"), 1, code_off, len(code))
    section(4, shstrtab.index(b".shstrtab"), 3, strtab_off, len(shstrtab))
    return bytes(buf), {"code": code_off}


# --- layer 1: normalize ------------------------------------------------------


class NormalizePe(unittest.TestCase):
    def test_two_links_of_the_same_code_become_identical(self):
        first, _ = make_pe()
        second, _ = make_pe(stamp=0x55555555, checksum=0x66666666, guid=b"H" * 16, age=9,
                            repro=b"S" * 32, export_stamp=0x77777777)
        self.assertNotEqual(first, second)
        a, fmt_a = normalize(first)
        b, fmt_b = normalize(second)
        self.assertEqual((fmt_a, fmt_b), ("pe", "pe"))
        self.assertEqual(a, b)

    def test_a_code_change_survives(self):
        first, _ = make_pe()
        second, _ = make_pe(stamp=0x55555555, code=b"\xcc" * 16)
        self.assertNotEqual(normalize(first)[0], normalize(second)[0])

    def test_only_noise_fields_are_touched(self):
        data, offsets = make_pe()
        cleaned, _ = normalize(data)
        start = offsets["code"]
        self.assertEqual(cleaned[start:start + 16], data[start:start + 16])
        path = offsets["cv_path"]
        self.assertEqual(cleaned[path:path + 4], b"x.pd")
        self.assertEqual(cleaned[:2], b"MZ")

    def test_truncated_pe_is_returned_unchanged(self):
        data, _ = make_pe()
        cut = data[:PE_OFF + 30]
        self.assertEqual(normalize(cut), (cut, None))

    def test_not_a_pe_signature(self):
        data = bytearray(make_pe()[0])
        data[PE_OFF:PE_OFF + 4] = b"NE\x00\x00"
        self.assertEqual(normalize(bytes(data)), (bytes(data), None))


class NormalizeElf(unittest.TestCase):
    def test_two_builds_differing_in_build_id_and_crc_become_identical(self):
        for wide, little in ((True, True), (False, False)):
            first, _ = make_elf(wide=wide, little=little)
            second, _ = make_elf(build_id=b"C" * 20, crc=0x99999999, wide=wide, little=little)
            a, fmt = normalize(first)
            b, _ = normalize(second)
            self.assertEqual(fmt, "elf")
            self.assertEqual(a, b, "wide=%s little=%s" % (wide, little))

    def test_a_code_change_survives(self):
        first, _ = make_elf()
        second, _ = make_elf(build_id=b"C" * 20, code=b"\xcc" * 16)
        self.assertNotEqual(normalize(first)[0], normalize(second)[0])

    def test_stripped_of_section_headers_still_loses_the_build_id(self):
        # Program headers alone must be enough: zero e_shoff and e_shnum.
        first = bytearray(make_elf()[0])
        second = bytearray(make_elf(build_id=b"C" * 20)[0])
        for buf in (first, second):
            struct.pack_into("<Q", buf, 0x28, 0)
            struct.pack_into("<H", buf, 0x3C, 0)
        self.assertEqual(normalize(bytes(first))[0], normalize(bytes(second))[0])

    def test_unknown_format_is_untouched(self):
        data = b"\x00\x01\x02 just bytes \x00" * 10
        self.assertEqual(normalize(data), (data, None))


# --- layer 1: summarize ------------------------------------------------------


class Summarize(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.work, True)

    def _file(self, name, data):
        path = os.path.join(self.work, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def test_sizes_hashes_regions_and_strings(self):
        a = self._file("a", b"\x00" * 5000 + b"old_function_name\x00" + b"\x00" * 100)
        b = self._file("b", b"\x00" * 5000 + b"new_function_name\x00" + b"\x00" * 100
                       + b"tail")
        summary = summarize(a, b)
        self.assertEqual(summary.size_a, 5118)
        self.assertEqual(summary.size_b, 5122)
        self.assertNotEqual(summary.sha_a, summary.sha_b)
        self.assertEqual(summary.regions[0], (5000, 5003))   # "old" vs "new", exact bytes
        self.assertEqual(summary.regions_total, 2)            # plus the appended tail
        self.assertEqual(summary.strings_added, ["new_function_name"])
        self.assertEqual(summary.strings_removed, ["old_function_name"])

    def test_added_binary_has_no_old_side(self):
        b = self._file("b", b"\x00added_payload\x00")
        summary = summarize(None, b)
        self.assertIsNone(summary.size_a)
        self.assertEqual(summary.regions_total, 0)
        self.assertEqual(summary.strings_added, ["added_payload"])

    def test_string_lists_are_capped_but_counted(self):
        a = self._file("a", b"\x00")
        b = self._file("b", b"\x00".join(b"string_number_%03d" % i for i in range(50)))
        summary = summarize(a, b)
        self.assertEqual(summary.strings_added_total, 50)
        self.assertEqual(len(summary.strings_added), 20)

    def test_regions_ignore_build_stamps(self):
        first, offsets = make_pe()
        second, _ = make_pe(stamp=0x55555555, checksum=0x66666666, code=b"\xcc" * 16)
        summary = summarize(self._file("a.exe", first), self._file("b.exe", second))
        start = offsets["code"]
        self.assertEqual(summary.regions, [(start, start + 16)])
        self.assertNotEqual(summary.sha_a, summary.sha_b)

    def test_format_is_reported(self):
        a = self._file("a.exe", make_pe()[0])
        b = self._file("b.exe", make_pe(code=b"\xcc" * 16)[0])
        self.assertEqual(summarize(a, b).format, "pe")


# --- the normalize step in prepare_copy ---------------------------------------


class BinaryStep(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.src = os.path.join(self.work, "src")
        os.makedirs(self.src)

    def _copy(self, rules):
        dst = os.path.join(self.work, "dst%d" % len(os.listdir(self.work)))
        prepare_copy(self.src, dst, rules)
        return dst

    def _put(self, name, data):
        with open(os.path.join(self.src, name), "wb") as handle:
            handle.write(data)

    def _get(self, root, name):
        with open(os.path.join(root, name), "rb") as handle:
            return handle.read()

    def test_binary_is_normalized_when_the_rule_asks(self):
        data, _ = make_pe()
        self._put("app.exe", data)
        dst = self._copy([{"match": ["*.exe"], "normalize": [BINARY_STEP]}])
        self.assertEqual(self._get(dst, "app.exe"), normalize(data)[0])
        self.assertNotEqual(self._get(dst, "app.exe"), data)

    def test_binary_is_untouched_without_the_step(self):
        data, _ = make_pe()
        self._put("app.exe", data)
        dst = self._copy([{"match": ["*.exe"], "ignore_lines": ["x"]}])
        self.assertEqual(self._get(dst, "app.exe"), data)

    def test_the_step_on_a_text_file_is_a_silent_no_op(self):
        # One rule may cover both, e.g. "*" — the text file must not try to run a
        # program called binary-stamps and warn that it is missing.
        self._put("notes.txt", b"hello\n")
        dst = self._copy([{"match": ["*"], "normalize": [BINARY_STEP]}])
        self.assertEqual(self._get(dst, "notes.txt"), b"hello\n")


# --- pairing and rendering -----------------------------------------------------


def binary_comparison(real):
    return Comparison(real=real, noise=set(), skipped=set(), sections={},
                      binaries=set(real))


class BinaryPairs(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.a = os.path.join(self.work, "a")
        self.b = os.path.join(self.work, "b")
        for root, names in ((self.a, ["same.bin", "old.bin", "gone.bin"]),
                            (self.b, ["same.bin", "new.bin", "fresh.bin"])):
            os.makedirs(root)
            for name in names:
                open(os.path.join(root, name), "wb").close()

    def test_each_status_pairs_the_right_sides(self):
        pairs = binary_pairs(binary_comparison({
            "same.bin": ("M", ["same.bin"]),
            "new.bin": ("R", ["old.bin", "new.bin"]),
            "gone.bin": ("D", ["gone.bin"]),
            "fresh.bin": ("A", ["fresh.bin"]),
        }), self.a, self.b)
        self.assertEqual(pairs["same.bin"], (os.path.join(self.a, "same.bin"),
                                             os.path.join(self.b, "same.bin")))
        self.assertEqual(pairs["new.bin"], (os.path.join(self.a, "old.bin"),
                                            os.path.join(self.b, "new.bin")))
        self.assertEqual(pairs["gone.bin"], (os.path.join(self.a, "gone.bin"), None))
        self.assertEqual(pairs["fresh.bin"], (None, os.path.join(self.b, "fresh.bin")))


class BinaryRendering(unittest.TestCase):
    def setUp(self):
        work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work, True)
        a = os.path.join(work, "a.exe")
        b = os.path.join(work, "b.exe")
        with open(a, "wb") as handle:
            handle.write(make_pe()[0] + b"\x00old_marker_text\x00")
        with open(b, "wb") as handle:
            handle.write(make_pe(code=b"\xcc" * 16)[0] + b"\x00new_marker_text\x00")
        self.summary = summarize(a, b)
        self.comparison = binary_comparison({"app.exe": ("M", ["app.exe"])})

    def test_markdown_shows_the_facts(self):
        md = build_report("v1", "v2", self.comparison, {}, None,
                          binary={"app.exe": self.summary})
        self.assertIn("PE (Windows)", md)
        self.assertIn("`new_marker_text`", md)
        self.assertIn("Differing regions: 1 (0x300", md)   # both changes share one 4 KiB block
        self.assertNotIn("not analyzed", md)

    def test_html_shows_the_facts_escaped(self):
        html = build_html("v1", "v2", self.comparison, {}, None,
                          binary={"app.exe": self.summary})
        self.assertIn('<ul class="facts">', html)
        self.assertIn("<code>old_marker_text</code>", html)

    def test_json_carries_the_detail(self):
        payload = build_json("v1", "v2", self.comparison, {}, None,
                             binary={"app.exe": self.summary})
        detail = payload["real"][0]["binary_detail"]
        self.assertEqual(detail["format"], "pe")
        self.assertEqual(detail["strings_added"], ["new_marker_text"])
        self.assertEqual(payload["schema_version"], 3)

    def test_without_detail_the_old_note_remains(self):
        md = build_report("v1", "v2", self.comparison, {}, None)
        self.assertIn("Binary file — not analyzed", md)
        payload = build_json("v1", "v2", self.comparison, {}, None)
        self.assertIsNone(payload["real"][0]["binary_detail"])


if __name__ == "__main__":
    unittest.main()

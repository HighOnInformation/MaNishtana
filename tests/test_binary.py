"""Unit tests for binary comparison — no git, no IDA, no Ghidra, no network.

Kept apart from test_manishtana.py so the binary feature can be carried to another
copy of the repository as new files, with few edits to shared ones.

PE and ELF inputs are synthetic, built byte by byte with struct: the smallest file
that has each noise field the normalizer must erase, at a known offset, so a test
can say exactly which bytes changed and which must not have.
"""

import contextlib
import io
import json
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manishtana.binengines import (
    ENGINES,
    _binexport_functions,
    _merge,
    FunctionChange,
    FunctionDiff,
    decompiled_diff,
    describe_tools,
    diff_functions,
    discover,
    runnable,
)
from manishtana.binnoise import BinarySummary, normalize, summarize
from manishtana.cli import main
from manishtana.compare import Comparison, binary_pairs
from manishtana.output import build_html, build_json, build_report
from manishtana.rules import BINARY_STEP, prepare_copy
from manishtana.settings import load as load_settings


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


# --- layer 2: engines --------------------------------------------------------


def write_db(path, statements):
    con = sqlite3.connect(path)
    try:
        for sql, rows in statements:
            if rows is None:
                con.execute(sql)
            else:
                con.executemany(sql, rows)
        con.commit()
    finally:
        con.close()


OLD_CODE = {
    "parse": "int parse(char *s)\n{\n  return atoi(s);\n}\n",
    "legacy": "void legacy(void)\n{\n}\n",
    "same": "int same(void)\n{\n  return 1;\n}\n",
}
NEW_CODE = {
    "parse": "int parse(char *s)\n{\n  if (!s)\n    return -1;\n  return atoi(s);\n}\n",
    "fresh": "int fresh(int x)\n{\n  return x * 2;\n}\n",
    "same": OLD_CODE["same"],
}


class FakeEngines:
    """Stands in for the run seam. Writes what IDA+Diaphora would, records argv."""

    def __init__(self, export_fails=False, with_pseudocode=True):
        self.calls = []
        self.export_fails = export_fails
        self.with_pseudocode = with_pseudocode

    def __call__(self, argv, cwd, env, timeout):
        self.calls.append((list(argv), env))
        if argv[0].endswith(("idat64", "idat64.exe")):
            if self.export_fails:
                return 4, "IDA: license problem\n"
            side = os.path.basename(os.path.dirname(argv[-1]))
            code = OLD_CODE if side == "old" else NEW_CODE
            rows = [(name, text if self.with_pseudocode else None)
                    for name, text in sorted(code.items())]
            write_db(env["DIAPHORA_EXPORT_FILE"], [
                ("create table functions (id integer primary key, name varchar(255), "
                 "pseudocode text)", None),
                ("insert into functions (name, pseudocode) values (?, ?)", rows),
            ])
            return 0, "exported\n"
        out = argv[argv.index("-o") + 1]
        write_db(out, [
            ("create table results (type, line, address, name, address2, name2, ratio, "
             "nodes1, nodes2, description)", None),
            ("create table unmatched (type, line, address, name)", None),
            ("insert into results values (?,?,?,?,?,?,?,?,?,?)", [
                ("best", "00000", "00401000", "same", "00401000", "same", "1.000", 1, 1,
                 "Same name"),
                ("partial", "00001", "00401100", "parse", "00401100", "parse", "0.750", 3, 4,
                 "Pseudo-code fuzzy hash"),
            ]),
            ("insert into unmatched values (?,?,?,?)", [
                ("primary", "00000", "00402000", "legacy"),
                ("secondary", "00000", "00403000", "fresh"),
            ]),
        ])
        return 0, "diffed\n"


class EngineSetup(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.ida_dir = os.path.join(self.work, "IDA Pro 9")
        os.makedirs(os.path.join(self.ida_dir, "plugins"))
        self.idat = os.path.join(self.ida_dir, "idat64.exe")
        open(self.idat, "wb").close()
        open(os.path.join(self.ida_dir, "plugins", "hexx64.dll"), "wb").close()
        self.diaphora = os.path.join(self.work, "diaphora", "diaphora.py")
        os.makedirs(os.path.dirname(self.diaphora))
        open(self.diaphora, "wb").close()
        self.old = os.path.join(self.work, "old.exe")
        self.new = os.path.join(self.work, "new.exe")
        for path in (self.old, self.new):
            with open(path, "wb") as handle:
                handle.write(b"MZ")
        self.binary = load_settings({"binary": {
            "ida": self.ida_dir, "diaphora": os.path.dirname(self.diaphora),
            "python": "py3"}}, ".").binary
        self.tools = discover(self.binary)


class Discover(EngineSetup):
    def test_config_directories_are_searched(self):
        self.assertEqual(self.tools["idat"], os.path.abspath(self.idat))
        self.assertTrue(self.tools["hexrays"].endswith("hexx64.dll"))
        self.assertEqual(self.tools["diaphora"], os.path.abspath(self.diaphora))
        self.assertEqual(self.tools["python"], "py3")
        self.assertEqual(runnable(self.tools), ["diaphora"])

    def test_nothing_configured_or_installed_means_nothing_runnable(self):
        tools = {"idat": None, "hexrays": None, "diaphora": None, "python": "x"}
        self.assertEqual(runnable(tools), [])
        report = describe_tools(tools)
        self.assertIn("not found", report)
        self.assertIn("Runnable: none", report)

    def test_report_names_paths_and_ready_engines(self):
        report = describe_tools(self.tools)
        self.assertIn(os.path.abspath(self.idat), report)
        self.assertIn("engine diaphora   ready", report)
        self.assertIn("Runnable: diaphora", report)


class DiaphoraAdapter(EngineSetup):
    def test_functions_are_classified_and_identical_ones_dropped(self):
        found = diff_functions(self.old, self.new, ["diaphora"], self.tools, self.binary,
                               run=FakeEngines())
        self.assertEqual(found.engines, ["diaphora"])
        self.assertEqual(found.notes, [])
        by_status = dict((c.status, c) for c in found.changes)
        self.assertEqual(sorted(by_status), ["added", "modified", "removed"])
        self.assertEqual(by_status["modified"].name_a, "parse")
        self.assertAlmostEqual(by_status["modified"].similarity, 0.75)
        self.assertEqual(by_status["removed"].name_a, "legacy")
        self.assertEqual(by_status["added"].name_b, "fresh")
        self.assertNotIn("same", [c.name_a for c in found.changes])
        self.assertEqual(by_status["modified"].views, {"diaphora": "modified"})

    def test_pseudo_code_diff_comes_from_the_exports(self):
        found = diff_functions(self.old, self.new, ["diaphora"], self.tools, self.binary,
                               run=FakeEngines())
        modified = [c for c in found.changes if c.status == "modified"][0]
        self.assertIn("+  if (!s)", modified.pseudo_diff)
        self.assertIn("--- a/parse", modified.pseudo_diff)

    def test_command_lines_and_environment(self):
        fake = FakeEngines()
        diff_functions(self.old, self.new, ["diaphora"], self.tools, self.binary, run=fake)
        export_argv, export_env = fake.calls[0]
        self.assertEqual(export_argv[:3], [self.tools["idat"], "-A", "-B"])
        self.assertTrue(export_argv[3].startswith("-S"))
        self.assertEqual(export_env["DIAPHORA_AUTO"], "1")
        self.assertEqual(export_env["DIAPHORA_USE_DECOMPILER"], "1")   # hexx64 was found
        # IDA writes its database next to the input: it must be a private copy.
        self.assertNotEqual(os.path.dirname(export_argv[-1]), self.work)
        diff_argv, _ = fake.calls[2]
        self.assertEqual(diff_argv[:2], ["py3", self.tools["diaphora"]])
        self.assertFalse(os.path.exists(self.old + ".i64"))

    def test_a_path_with_spaces_is_quoted_for_ida(self):
        tools = dict(self.tools, diaphora=os.path.join(self.work, "my tools", "diaphora.py"))
        fake = FakeEngines()
        diff_functions(self.old, self.new, ["diaphora"], tools, self.binary, run=fake)
        self.assertEqual(fake.calls[0][0][3], '-S"%s"' % tools["diaphora"])

    def test_no_decompiler_means_no_pseudo_code_but_still_a_match(self):
        tools = dict(self.tools, hexrays=None)
        fake = FakeEngines(with_pseudocode=False)
        found = diff_functions(self.old, self.new, ["diaphora"], tools, self.binary, run=fake)
        self.assertNotIn("DIAPHORA_USE_DECOMPILER", fake.calls[0][1])
        self.assertEqual(len(found.changes), 3)
        self.assertTrue(all(c.pseudo_diff is None for c in found.changes))

    def test_a_failed_export_is_a_note_not_a_crash(self):
        found = diff_functions(self.old, self.new, ["diaphora"], self.tools, self.binary,
                               run=FakeEngines(export_fails=True))
        self.assertEqual(found.engines, [])
        self.assertEqual(found.changes, [])
        self.assertIn("export of the old side failed", found.notes[0])
        self.assertIn("license problem", found.notes[0])


class DecompiledDiff(unittest.TestCase):
    def _change(self, name, similarity, diff):
        return FunctionChange(name, name, "modified", similarity, {"diaphora": "modified"}, diff)

    def test_most_changed_first_and_capped(self):
        changes = [self._change("a", 0.9, "--- a/a\n"), self._change("b", 0.2, "--- a/b\n"),
                   self._change("c", 0.5, "--- a/c\n"), self._change("d", 0.1, None)]
        text = decompiled_diff(changes, 2)
        self.assertEqual(text.splitlines()[:2], ["--- a/b", "--- a/c"])
        self.assertIn("1 more changed functions not shown", text)

    def test_nothing_to_show(self):
        self.assertIsNone(decompiled_diff([self._change("a", 0.5, None)], 5))


class FunctionRendering(unittest.TestCase):
    def setUp(self):
        self.comparison = binary_comparison({"app.exe": ("M", ["app.exe"])})
        self.summary = BinarySummary("pe", 10, 12, "a" * 64, "b" * 64, [(0, 2)], 1, [], [], 0, 0)
        self.found = FunctionDiff(["diaphora"], [
            FunctionChange("parse", "parse", "modified", 0.75, {"diaphora": "modified"},
                           "--- a/parse\n+++ b/parse\n+  if (!s)\n"),
            FunctionChange(None, "fresh", "added", None, {"diaphora": "added"}, None),
        ], [])
        self.kwargs = {"binary": {"app.exe": self.summary}, "functions": {"app.exe": self.found},
                       "decompiled": {"app.exe": "--- a/parse\n+++ b/parse\n+  if (!s)\n"}}

    def test_markdown_table_and_decompiled_diff(self):
        md = build_report("v1", "v2", self.comparison, {"app.exe": "model says"}, None,
                          **self.kwargs)
        self.assertIn("**Function-level diff** (diaphora): 1 modified, 1 added.", md)
        self.assertIn("| `parse` | modified | 0.75 | diaphora |", md)
        self.assertIn("Show decompiled diff", md)
        self.assertIn("model says", md)

    def test_html_table(self):
        html = build_html("v1", "v2", self.comparison, {}, None, **self.kwargs)
        self.assertIn('<table class="functions">', html)
        self.assertIn("<code>fresh</code>", html)
        self.assertIn("Decompiled diff", html)

    def test_json_functions(self):
        payload = build_json("v1", "v2", self.comparison, {}, None, **self.kwargs)
        functions = payload["real"][0]["binary_functions"]
        self.assertEqual(functions["engines"], ["diaphora"])
        self.assertEqual(functions["changes"][0]["status"], "modified")
        self.assertEqual(functions["changes"][0]["similarity"], 0.75)

    def test_engine_ran_and_found_no_function_change(self):
        empty = FunctionDiff(["diaphora"], [], [])
        md = build_report("v1", "v2", self.comparison, {}, None,
                          binary={"app.exe": self.summary}, functions={"app.exe": empty})
        self.assertIn("no function changed", md)
        self.assertIn("data, resources or layout", md)

    def test_long_lists_are_capped(self):
        many = FunctionDiff(["diaphora"], [
            FunctionChange("f%d" % i, "f%d" % i, "modified", 0.5, {"diaphora": "modified"}, None)
            for i in range(5)], [])
        md = build_report("v1", "v2", self.comparison, {}, None,
                          binary={"app.exe": self.summary}, functions={"app.exe": many},
                          max_functions=3)
        self.assertIn("2 more not listed", md)


class EngineFlags(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.config = os.path.join(self.work, "config.json")
        with open(self.config, "w") as handle:
            handle.write('{"rules": []}')

    def _main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_unknown_engine_is_a_usage_error(self):
        code, _out, err = self._main([self.work, self.work, "-c", self.config,
                                      "--binary-engines", "nope"])
        self.assertEqual(code, 2)
        self.assertIn("unknown binary engine", err)

    def test_check_binary_tools_exits_zero(self):
        code, out, _err = self._main(["--check-binary-tools", "-c", self.config])
        self.assertEqual(code, 0)
        self.assertIn("engine diaphora", out)


# --- BinDiff ------------------------------------------------------------------


def pb_varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def pb_field(number, value):
    """Encode one protobuf field: int as varint, bytes/str as length-delimited."""
    if isinstance(value, int):
        return pb_varint(number << 3) + pb_varint(value)
    if isinstance(value, str):
        value = value.encode("utf-8")
    return pb_varint(number << 3 | 2) + pb_varint(len(value)) + value


def binexport(functions):
    """A BinExport2 message holding only a call graph: [(address, name, type)]."""
    vertices = b""
    for address, name, kind in functions:
        vertex = pb_field(1, address)
        if kind:
            vertex += pb_field(2, kind)
        if name:
            vertex += pb_field(3, name)
        vertices += pb_field(1, vertex)
    return pb_field(1, b"meta") + pb_field(8, vertices)


OLD_FUNCS = [(0x1000, "parse", 0), (0x1100, "legacy", 0), (0x1200, "same", 0),
             (0x9000, "memcpy", 1), (0x1300, None, 0)]
NEW_FUNCS = [(0x1000, "parse", 0), (0x1180, "fresh", 0), (0x1200, "same", 0),
             (0x9000, "memcpy", 1), (0x1300, None, 0)]


class FakeBinDiff:
    """Stands in for the run seam: BinExport (IDA or Ghidra) then bindiff."""

    def __init__(self, fail_matching=False):
        self.calls = []
        self.fail_matching = fail_matching

    def __call__(self, argv, cwd, env, timeout):
        self.calls.append((list(argv), env))
        if os.path.basename(argv[0]).startswith("bindiff"):
            if self.fail_matching:
                return 1, "bindiff: bad file\n"
            out_dir = [a for a in argv if a.startswith("--output_dir=")][0].split("=", 1)[1]
            write_db(os.path.join(out_dir, "old_vs_new.BinDiff"), [
                ("create table function (id integer primary key, address1 bigint, name1 text, "
                 "address2 bigint, name2 text, similarity double precision, confidence double "
                 "precision, flags integer, algorithm smallint, evaluate boolean, "
                 "commentsported boolean, basicblocks integer, edges integer, "
                 "instructions integer)", None),
                ("insert into function (address1, name1, address2, name2, similarity, confidence)"
                 " values (?,?,?,?,?,?)", [
                     (0x1000, "parse", 0x1000, "parse", 0.62, 0.9),
                     (0x1200, "same", 0x1200, "same", 1.0, 1.0),
                     (0x9000, "memcpy", 0x9000, "memcpy", 1.0, 1.0),
                     (0x1300, "sub_1300", 0x1300, "sub_1300", 1.0, 1.0),
                 ]),
            ])
            return 0, "done\n"
        if any(a.startswith("-OBinExportModule:") for a in argv):
            export = [a for a in argv if a.startswith("-OBinExportModule:")][0].split(":", 1)[1]
        else:
            export = argv[argv.index("ManishtanaBinExport.java") + 1]
        side = os.path.basename(os.path.dirname(argv[-1] if "-OBinExportModule:" in " ".join(argv)
                                                else argv[argv.index("-import") + 1]))
        with open(export, "wb") as handle:
            handle.write(binexport(OLD_FUNCS if side == "old" else NEW_FUNCS))
        return 0, "exported\n"


class BinDiffSetup(EngineSetup):
    def setUp(self):
        super(BinDiffSetup, self).setUp()
        self.ghidra = os.path.join(self.work, "ghidra_12.2_PUBLIC")
        os.makedirs(os.path.join(self.ghidra, "support"))
        headless = "analyzeHeadless.bat" if os.name == "nt" else "analyzeHeadless"
        open(os.path.join(self.ghidra, "support", headless), "wb").close()
        lib = os.path.join(self.ghidra, "Ghidra", "Extensions", "BinExport", "lib")
        os.makedirs(lib)
        open(os.path.join(lib, "BinExport.jar"), "wb").close()
        self.bindiff_dir = os.path.join(self.work, "BinDiff")
        os.makedirs(os.path.join(self.bindiff_dir, "bin"))
        open(os.path.join(self.bindiff_dir, "bin", "bindiff.exe"), "wb").close()
        open(os.path.join(self.ida_dir, "plugins", "binexport12_ida64.dll"), "wb").close()
        self.binary = load_settings({"binary": {
            "ida": self.ida_dir, "diaphora": os.path.dirname(self.diaphora), "python": "py3",
            "bindiff": self.bindiff_dir, "ghidra": self.ghidra, "java": "/jdk25"}}, ".").binary
        self.tools = discover(self.binary)
        self.ghidra_only = dict(self.tools, idat=None, binexport_ida=None)


class BinDiffDiscovery(BinDiffSetup):
    def test_tools_are_found(self):
        self.assertTrue(self.tools["bindiff"].endswith("bindiff.exe"))
        self.assertTrue(self.tools["binexport_ida"].endswith("binexport12_ida64.dll"))
        self.assertEqual(self.tools["ghidra"], os.path.abspath(self.ghidra))
        self.assertTrue(self.tools["binexport_ghidra"].endswith("BinExport"))
        self.assertEqual(runnable(self.tools), ["diaphora", "bindiff"])

    def test_either_exporter_is_enough(self):
        self.assertIn("bindiff", runnable(self.ghidra_only))
        no_ghidra = dict(self.tools, ghidra=None, binexport_ghidra=None)
        self.assertIn("bindiff", runnable(no_ghidra))
        neither = dict(self.ghidra_only, ghidra=None)
        self.assertNotIn("bindiff", runnable(neither))
        self.assertIn("engine bindiff    missing: ", describe_tools(neither))


class BinDiffAdapter(BinDiffSetup):
    def test_matched_unmatched_and_library_functions(self):
        found = diff_functions(self.old, self.new, ["bindiff"], self.tools, self.binary,
                               run=FakeBinDiff())
        self.assertEqual(found.engines, ["bindiff"])
        summary = sorted((c.status, c.name_a, c.name_b) for c in found.changes)
        self.assertEqual(summary, [("added", None, "fresh"), ("modified", "parse", "parse"),
                                   ("removed", "legacy", None)])
        modified = [c for c in found.changes if c.status == "modified"][0]
        self.assertAlmostEqual(modified.similarity, 0.62)
        self.assertIsNone(modified.pseudo_diff)

    def test_ida_plugin_is_preferred(self):
        fake = FakeBinDiff()
        diff_functions(self.old, self.new, ["bindiff"], self.tools, self.binary, run=fake)
        argv = fake.calls[0][0]
        self.assertEqual(argv[0], self.tools["idat"])
        self.assertIn("-OBinExportAutoAction:BinExportBinary", argv)
        self.assertNotEqual(fake.calls[0][1].get("JAVA_HOME"), "/jdk25")   # IDA run: untouched

    def test_ghidra_path_writes_the_post_script_and_sets_java(self):
        fake = FakeBinDiff()
        found = diff_functions(self.old, self.new, ["bindiff"], self.ghidra_only, self.binary,
                               run=fake)
        argv, env = fake.calls[0]
        self.assertTrue(argv[0].endswith(("analyzeHeadless", "analyzeHeadless.bat")))
        self.assertIn("-postScript", argv)
        self.assertIn("-deleteProject", argv)
        script_dir = argv[argv.index("-scriptPath") + 1]
        self.assertTrue(script_dir.endswith("ghidra_scripts"))
        self.assertEqual(env["JAVA_HOME"], "/jdk25")
        self.assertEqual(len(found.changes), 3)
        bindiff_argv = fake.calls[2][0]
        self.assertTrue(bindiff_argv[1].startswith("--primary="))

    def test_failed_matching_is_a_note(self):
        found = diff_functions(self.old, self.new, ["bindiff"], self.tools, self.binary,
                               run=FakeBinDiff(fail_matching=True))
        self.assertEqual(found.engines, [])
        self.assertIn("bindiff: matching failed", found.notes[0])


class BinExportReader(unittest.TestCase):
    def test_names_types_and_unnamed(self):
        work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work, True)
        path = os.path.join(work, "x.BinExport")
        with open(path, "wb") as handle:
            handle.write(binexport([(0x401000, "main", 0), (0x402000, "printf", 2),
                                    (0x403000, None, 0), (0x404000, "thunk", 3)]))
        self.assertEqual(_binexport_functions(path),
                         {0x401000: "main", 0x403000: "sub_403000"})

    def test_large_addresses_and_unknown_fields_are_skipped_cleanly(self):
        data = pb_field(5, 123) + pb_field(8, pb_field(1, pb_field(1, 0xFFFFFFFF00001000)
                                                       + pb_field(9, "extra")
                                                       + pb_field(4, "demangled()")))
        work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work, True)
        path = os.path.join(work, "y.BinExport")
        with open(path, "wb") as handle:
            handle.write(data)
        self.assertEqual(_binexport_functions(path), {0xFFFFFFFF00001000: "demangled()"})


class TwoEngines(BinDiffSetup):
    def _both(self):
        diaphora_fake, bindiff_fake = FakeEngines(), FakeBinDiff()

        def run(argv, cwd, env, timeout):
            if "DIAPHORA_AUTO" in env or argv[0] == "py3":
                return diaphora_fake(argv, cwd, env, timeout)
            return bindiff_fake(argv, cwd, env, timeout)
        return diff_functions(self.old, self.new, ["diaphora", "bindiff"], self.tools,
                              self.binary, run=run)

    def test_views_from_both_and_agreement(self):
        found = self._both()
        self.assertEqual(found.engines, ["diaphora", "bindiff"])
        parse = [c for c in found.changes if c.name_a == "parse"][0]
        self.assertEqual(parse.views, {"diaphora": "modified", "bindiff": "modified"})
        self.assertAlmostEqual(parse.similarity, 0.62)   # the lower of 0.75 and 0.62
        self.assertIn("+  if (!s)", parse.pseudo_diff)    # pseudo-code from Diaphora

    def test_an_engine_that_did_not_list_a_function_saw_it_unchanged(self):
        diaphora_only = FunctionChange("x", "x", "modified", 0.5, {"diaphora": "modified"}, None)
        merged = _merge({"diaphora": [diaphora_only], "bindiff": []})
        self.assertEqual(merged[0].views, {"diaphora": "modified", "bindiff": "unchanged"})

    def test_disagreement_is_flagged_in_the_report(self):
        found = FunctionDiff(["diaphora", "bindiff"], _merge({
            "diaphora": [FunctionChange("x", "x", "modified", 0.5, {"diaphora": "modified"}, None)],
            "bindiff": []}), [])
        md = build_report("v1", "v2", binary_comparison({"a.exe": ("M", ["a.exe"])}), {}, None,
                          binary={"a.exe": BinarySummary("pe", 1, 1, "a", "b", [], 0, [], [],
                                                         0, 0)},
                          functions={"a.exe": found})
        self.assertIn("modified ⚠ engines disagree", md)
        self.assertIn("bindiff: unchanged, diaphora: modified", md)


# --- ghidriff -------------------------------------------------------------------


def esym(name, code, external=False):
    """The shape ghidriff gives one function (its esym record), trimmed to what is read."""
    return {"name": name, "fullname": name, "code": code, "external": external,
            "address": "140001000"}


PDIFF = {
    "functions": {
        "added": [esym("fresh", "int fresh(int x)\n{\n  return x * 2;\n}\n"),
                  esym("ImportedThing", "", external=True)],
        "deleted": [esym("legacy", "void legacy(void)\n{\n}\n")],
        "modified": [
            {"old": esym("parse", OLD_CODE["parse"]), "new": esym("parse", NEW_CODE["parse"]),
             "diff": "(whole-function diff)", "diff_type": ["code", "length"], "ratio": 0.71},
            {"old": esym("moved", "x"), "new": esym("moved", "x"),
             "diff": "", "diff_type": ["address", "refcount"], "ratio": 1.0},
        ],
    },
    "stats": {"added_funcs_len": 2, "deleted_funcs_len": 1, "modified_funcs_len": 2},
}


class FakeGhidriff:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, argv, cwd, env, timeout):
        self.calls.append((list(argv), env))
        if self.fail:
            return 1, "Exception: JVM not found\n"
        out = os.path.join(argv[argv.index("-o") + 1], "json")
        os.makedirs(out)
        with open(os.path.join(out, "old_x-new_x.ghidriff.json"), "w") as handle:
            json.dump(PDIFF, handle)
        with open(os.path.join(out, "old_x-new_x.ghidriff.matches.json"), "w") as handle:
            json.dump({"address_matches": []}, handle)
        return 0, "done\n"


class GhidriffSetup(BinDiffSetup):
    def setUp(self):
        super(GhidriffSetup, self).setUp()
        venv = os.path.join(self.work, "ghidriff-venv", "Scripts")
        os.makedirs(venv)
        open(os.path.join(venv, "ghidriff.exe"), "wb").close()
        self.binary = load_settings({"binary": {
            "ida": self.ida_dir, "diaphora": os.path.dirname(self.diaphora), "python": "py3",
            "bindiff": self.bindiff_dir, "ghidra": self.ghidra, "java": "/jdk25",
            "ghidriff": os.path.dirname(venv)}}, ".").binary
        self.tools = discover(self.binary)


class GhidriffAdapter(GhidriffSetup):
    def test_found_in_a_virtualenv_directory(self):
        self.assertTrue(self.tools["ghidriff"].endswith("ghidriff.exe"))
        self.assertEqual(runnable(self.tools), ["diaphora", "bindiff", "ghidriff"])
        self.assertNotIn("ghidriff", runnable(dict(self.tools, ghidra=None)))

    def test_code_changes_only_and_no_imports(self):
        found = diff_functions(self.old, self.new, ["ghidriff"], self.tools, self.binary,
                               run=FakeGhidriff())
        self.assertEqual(found.engines, ["ghidriff"])
        summary = sorted((c.status, c.name_a, c.name_b) for c in found.changes)
        self.assertEqual(summary, [("added", None, "fresh"), ("modified", "parse", "parse"),
                                   ("removed", "legacy", None)])
        parse = [c for c in found.changes if c.status == "modified"][0]
        self.assertAlmostEqual(parse.similarity, 0.71)
        self.assertIn("+  if (!s)", parse.pseudo_diff)
        self.assertNotIn("whole-function", parse.pseudo_diff)   # rebuilt with 3 lines context

    def test_command_line_and_environment(self):
        fake = FakeGhidriff()
        diff_functions(self.old, self.new, ["ghidriff"], self.tools, self.binary, run=fake)
        argv, env = fake.calls[0]
        self.assertIn("--no-symbols", argv)   # never reach for a symbol server
        self.assertTrue(os.path.basename(argv[-2]).startswith("old_"))
        self.assertTrue(os.path.basename(argv[-1]).startswith("new_"))
        self.assertEqual(env["GHIDRA_INSTALL_DIR"], self.tools["ghidra"])
        self.assertEqual(env["JAVA_HOME"], "/jdk25")

    def test_failure_is_a_note(self):
        found = diff_functions(self.old, self.new, ["ghidriff"], self.tools, self.binary,
                               run=FakeGhidriff(fail=True))
        self.assertEqual(found.engines, [])
        self.assertIn("JVM not found", found.notes[0])


class ThreeEngines(GhidriffSetup):
    def test_all_three_merge(self):
        fakes = {"diaphora": FakeEngines(), "bindiff": FakeBinDiff(), "ghidriff": FakeGhidriff()}

        def run(argv, cwd, env, timeout):
            if "DIAPHORA_AUTO" in env or argv[0] == "py3":
                return fakes["diaphora"](argv, cwd, env, timeout)
            if "--no-symbols" in argv:
                return fakes["ghidriff"](argv, cwd, env, timeout)
            return fakes["bindiff"](argv, cwd, env, timeout)

        found = diff_functions(self.old, self.new, list(ENGINES), self.tools, self.binary,
                               run=run)
        self.assertEqual(found.engines, ["diaphora", "bindiff", "ghidriff"])
        parse = [c for c in found.changes if c.name_a == "parse"][0]
        self.assertEqual(parse.views, {"diaphora": "modified", "bindiff": "modified",
                                       "ghidriff": "modified"})
        self.assertAlmostEqual(parse.similarity, 0.62)
        fresh = [c for c in found.changes if c.name_b == "fresh"][0]
        self.assertEqual(set(fresh.views.values()), {"added"})


if __name__ == "__main__":
    unittest.main()

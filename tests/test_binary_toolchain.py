"""Label toolchain churn so it is not read as a source change (round 1 finding 2).

zstd 1.5.6 -> 1.5.7 on Windows: GCC 13.2.0 -> 14.2.0 and a different MinGW runtime
swapped dozens of startup and CRT strings and functions in with the real changes.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manishtana.binengines import FunctionChange, FunctionDiff
from manishtana.binnoise import BinarySummary, summarize
from manishtana.compare import Comparison
from manishtana.output import _is_toolchain_name, build_json, build_report


class ToolchainStrings(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.work, True)

    def _sum(self, a, b):
        paths = []
        for name, data in (("a", a), ("b", b)):
            path = os.path.join(self.work, name)
            with open(path, "wb") as handle:
                handle.write(data)
            paths.append(path)
        return summarize(*paths)

    def test_compiler_change_is_labelled_and_not_a_plain_string(self):
        old = b"\x00'GNU C99 13.2.0 -m64 -O2\x00some real string here\x00"
        new = b"\x00'GNU C99 14.2.0 -m64 -O2\x00some other string here\x00"
        s = self._sum(old, new)
        self.assertEqual(s.toolchain, ["GNU C99: 13.2.0 -> 14.2.0"])
        self.assertEqual(s.strings_added, ["some other string here"])
        self.assertEqual(s.strings_removed, ["some real string here"])
        self.assertEqual(s.strings_added_total, 1)

    def test_rustc_and_unpaired(self):
        s = self._sum(b"\x00", b"\x00rustc 1.82.0 (f6e511eec 2024-10-15)\x00")
        self.assertEqual(s.toolchain, ["rustc: added 1.82.0"])

    def test_rustc_commit_in_embedded_paths_is_a_toolchain_change(self):
        # alpine 3.21 -> 3.22 rebuild of ripgrep 14.1.1: the rustc hash sits inside
        # packed literals, so it never shows as a string of its own.
        old = b"\x00xx/rustc/" + b"f6e511eec7342f59a25f7c0534f1dbea00d01b14" + b"/library/core\x00"
        new = b"\x00xx/rustc/" + b"05f9846f893b09a1be1fc8560e33fc3c815cfecb" + b"/library/core\x00"
        self.assertEqual(self._sum(old, new).toolchain,
                         ["rustc commit: f6e511eec734 -> 05f9846f893b"])
        self.assertEqual(self._sum(old, old + b"y" * 9).toolchain, [])

    def test_same_compiler_is_not_reported(self):
        both = b"\x00GCC: (GNU) 13.2.0\x00"
        self.assertEqual(self._sum(both + b"x" * 9, both + b"y" * 9).toolchain, [])

    def test_prose_that_mentions_a_compiler_stays_a_plain_string(self):
        # a user-facing message is a real change, not toolchain
        old = b"\x00requires rustc 1.70 or newer\x00clang version 12 is too old\x00"
        new = b"\x00requires rustc 1.75 or newer\x00clang version 14 is too old\x00"
        s = self._sum(old, new)
        self.assertEqual(s.toolchain, [])
        self.assertEqual(sorted(s.strings_added),
                         ["clang version 14 is too old", "requires rustc 1.75 or newer"])

    def test_clang_ident_is_still_toolchain(self):
        s = self._sum(b"\x00clang version 17.0.6 (https://x)\x00",
                      b"\x00Ubuntu clang version 18.1.3 (1ubuntu1)\x00")
        self.assertEqual(s.toolchain, ["clang: 17.0.6 -> 18.1.3"])

    def test_same_version_with_other_flags_is_reported(self):
        s = self._sum(b"\x00GNU C99 13.2.0 -m64 -O2\x00", b"\x00GNU C99 13.2.0 -m64 -O0\x00")
        self.assertEqual(s.toolchain, ["GNU C99 flags: -m64 -O2 -> -m64 -O0"])

    def test_version_only_change_has_no_flags_line(self):
        s = self._sum(b"\x00GNU C99 13.2.0 -m64 -O2\x00", b"\x00GNU C99 14.2.0 -m64 -O2\x00")
        self.assertEqual(s.toolchain, ["GNU C99: 13.2.0 -> 14.2.0"])


class ToolchainFunctions(unittest.TestCase):
    def setUp(self):
        self.comparison = Comparison(real={"app.exe": ("M", ["app.exe"])}, noise=set(),
                                     skipped=set(), sections={}, binaries={"app.exe"})
        self.summary = BinarySummary("pe", 10, 12, "a" * 64, "b" * 64, [(0, 2)], 1, [], [],
                                     0, 0, 0, ["GNU C99: 13.2.0 -> 14.2.0"])
        self.found = FunctionDiff(["ghidriff"], [
            FunctionChange("FUN_1", "FUN_1", "modified", 0.5, {"ghidriff": "modified"}, None),
            FunctionChange("_initterm", None, "removed", None, {"ghidriff": "removed"}, None),
        ], [])
        self.kw = {"binary": {"app.exe": self.summary}, "functions": {"app.exe": self.found}}

    def test_known_runtime_names_are_labelled(self):
        for name in ("__mingw_setusermatherr", "_pei386_runtime_relocator", "_initterm",
                     "QueryPerformanceCounter", "__p___initenv", "_fpreset"):
            self.assertTrue(_is_toolchain_name(name), name)
        for name in ("ZSTD_compress", "FUN_140001000", "parse", "main"):
            self.assertFalse(_is_toolchain_name(name), name)

    def test_markdown_labels_row_and_toolchain_fact(self):
        md = build_report("v1", "v2", self.comparison, {}, None, **self.kw)
        self.assertIn("| `_initterm` | removed | — | ghidriff (likely toolchain) |", md)
        self.assertIn("| `FUN_1` | modified | 0.50 | ghidriff |", md)
        self.assertIn("Toolchain changed: GNU C99: 13.2.0 -> 14.2.0", md)

    def test_json_flag(self):
        payload = build_json("v1", "v2", self.comparison, {}, None, **self.kw)
        changes = payload["real"][0]["binary_functions"]["changes"]
        self.assertEqual([c["toolchain"] for c in changes], [False, True])
        self.assertEqual(payload["real"][0]["binary_detail"]["toolchain"],
                         ["GNU C99: 13.2.0 -> 14.2.0"])

    def test_renamed_function_is_labelled_by_either_name(self):
        from manishtana.output import _function_rows
        found = FunctionDiff(["ghidriff"], [FunctionChange(
            "FUN_9", "__mingw_foo", "modified", 0.9, {"ghidriff": "modified"}, None)], [])
        rows = _function_rows(found, 10)
        self.assertIn("(likely toolchain)", rows.agreed[0][4])


if __name__ == "__main__":
    unittest.main()


class RebuildHashNoise(unittest.TestCase):
    """Same upstream version rebuilt (alpine 3.21 -> 3.22, ripgrep 14.1.1): the cargo
    registry directory and the rustc commit are build hashes, so a packed literal that
    differs only in them is noise, not a changed string."""

    setUp = ToolchainStrings.setUp
    _sum = ToolchainStrings._sum

    def test_registry_and_rustc_hashes_alone_do_not_change_a_string(self):
        old = (b"\x00/home/b/.cargo/registry/src/index.crates.io-6f17d22bba15001f/bstr-1.10.0/src/io.rs"
               b"and /rustc/f6e511eec7342f59a25f7c0534f1dbea00d01b14/library/std/src/thread/mod.rs\x00")
        new = old.replace(b"6f17d22bba15001f", b"1949cf8c6b5b557f").replace(
            b"f6e511eec7342f59a25f7c0534f1dbea00d01b14",
            b"05f9846f893b09a1be1fc8560e33fc3c815cfecb")
        s = self._sum(old, new)
        self.assertEqual((s.strings_added, s.strings_removed), ([], []))
        self.assertEqual(s.strings_added_total + s.strings_removed_total, 0)

    def test_a_real_change_next_to_the_hashes_is_still_reported(self):
        old = b"\x00index.crates.io-6f17d22bba15001f/bstr-1.10.0/src/io.rs failed\x00"
        new = b"\x00index.crates.io-1949cf8c6b5b557f/bstr-1.11.0/src/io.rs failed\x00"
        s = self._sum(old, new)
        self.assertEqual(s.strings_added_total, 1)
        self.assertIn("bstr-1.11.0", s.strings_added[0])

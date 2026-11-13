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

    def test_same_compiler_is_not_reported(self):
        both = b"\x00GCC: (GNU) 13.2.0\x00"
        self.assertEqual(self._sum(both + b"x" * 9, both + b"y" * 9).toolchain, [])


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


if __name__ == "__main__":
    unittest.main()

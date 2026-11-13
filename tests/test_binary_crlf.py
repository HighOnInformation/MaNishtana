"""Decompiled code from ghidriff on Windows carries CRLF line endings.

Written to a report file opened in text mode, "\r\n" becomes "\r\r\n" and every
line is followed by a blank one (round 1 finding 4).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manishtana.binengines import _read_ghidriff, _unified


class LineEndings(unittest.TestCase):
    def test_unified_has_no_carriage_returns(self):
        diff = _unified("f", "f", "int a;\r\nint b;\r\nreturn 0;\r\n",
                        "int a;\r\nint c;\r\nreturn 0;\r\n")
        self.assertNotIn("\r", diff)
        self.assertIn("-int b;\n", diff)
        self.assertIn("+int c;\n", diff)

    def test_mixed_endings_do_not_show_as_changes(self):
        # One side CRLF, the other LF: not a code change.
        self.assertIsNone(_unified("f", "f", "x;\r\ny;\r\n", "x;\ny;\n"))

    def test_lone_cr_is_kept_out_too(self):
        self.assertNotIn("\r", _unified("f", "f", "a\rb\n", "a\rc\n"))

    def test_ghidriff_reader_applies_it(self):
        pdiff = {"functions": {"modified": [{
            "old": {"name": "f", "address": "1000", "code": "a;\r\nb;\r\n"},
            "new": {"name": "f", "address": "1000", "code": "a;\r\nc;\r\n"},
            "ratio": 0.5, "diff_type": ["code"]}]}}
        change = _read_ghidriff(pdiff)[0]
        self.assertNotIn("\r", change.pseudo_diff)

    def test_added_function_code(self):
        pdiff = {"functions": {"added": [{"name": "g", "address": "2000",
                                          "code": "x;\r\ny;\r\n"}]}}
        self.assertNotIn("\r", _read_ghidriff(pdiff)[0].pseudo_diff)


if __name__ == "__main__":
    unittest.main()

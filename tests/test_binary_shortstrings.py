"""Short strings that matter: a version and an ANSI colour code (round 1 finding 1).

jq 1.7 -> 1.7.1 changed "1.7" to "1.7.1" and the colour "0;37" to "0;39"; the
eight-character floor hid both.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manishtana.binnoise import summarize


class ShortStrings(unittest.TestCase):
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

    def test_version_is_listed(self):
        s = self._sum(b"\x00" * 9 + b"1.7\x00" + b"x" * 9, b"\x00" * 9 + b"1.7.1\x00" + b"x" * 9)
        self.assertEqual(s.strings_added, ["1.7.1"])
        self.assertEqual(s.strings_removed, ["1.7"])

    def test_ansi_colour_is_listed(self):
        s = self._sum(b"\x00\x1b[0;37m\x00", b"\x00\x1b[0;39m\x00")
        self.assertEqual(s.strings_added, ["\e[0;39m"])
        self.assertEqual(s.strings_removed, ["\e[0;37m"])

    def test_plain_short_text_is_still_hidden(self):
        s = self._sum(b"\x00abcd\x00efgh\x00", b"\x00abce\x00efgi\x00")
        self.assertEqual(s.strings_added, [])
        self.assertEqual(s.strings_added_total, 0)

    def test_short_version_must_stand_alone(self):
        # inside instruction bytes: no NUL on both sides
        s = self._sum(b"\x00\x90" + b"1.2.3" + b"\xc3\x00", b"\x00\x90" + b"1.2.4" + b"\xc3\x00")
        self.assertEqual(s.strings_added, [])

    def test_special_strings_come_first_when_capped(self):
        b = b"\x00".join(b"aaa_string_number_%03d" % i for i in range(40)) + b"\x001.7.1\x00"
        s = self._sum(b"\x00", b)
        self.assertEqual(s.strings_added[0], "1.7.1")
        self.assertEqual(s.strings_added_total, 41)

    def test_long_strings_are_shortened_for_display(self):
        s = self._sum(b"\x00", b"\x00" + b"z" * 5000 + b"\x00")
        self.assertLess(len(s.strings_added[0]), 300)
        self.assertTrue(s.strings_added[0].endswith("..."))


if __name__ == "__main__":
    unittest.main()

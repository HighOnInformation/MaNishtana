"""Versions inside packed Rust strings link to the commits that bumped them.

fd 10.4.1 -> 10.4.2: the version sits inside a packed literal
('...IONSgen-completions10.4.2A program to find entrie...'), which no commit
contains as a whole. The version itself is what the bump commit changed.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manishtana import binlink
from manishtana.gitrepo import Commit

PACKED = "…IONSgen-completions10.4.2A program to find entrie…"
BUMP = (
    "diff --git a/Cargo.toml b/Cargo.toml\n--- a/Cargo.toml\n+++ b/Cargo.toml\n"
    '@@ -1 +1 @@\n-version = "10.4.1"\n+version = "10.4.2"\n'
)


class VersionsInPackedStrings(unittest.TestCase):
    def test_a_version_inside_a_packed_string_is_matchable(self):
        self.assertIn("10.4.2", binlink.pieces(PACKED))

    def test_short_standalone_version_is_matchable(self):
        self.assertEqual(binlink.pieces("1.7.1"), ["1.7.1"])

    def test_version_digits_inside_a_longer_number_are_not_a_version(self):
        self.assertNotIn("10.4.2", binlink.pieces("…x110.4.21 y…"))

    def test_existing_behaviour_kept(self):
        self.assertEqual(binlink.pieces("first piece here … second piece there"),
                         ["first piece here", "second piece there"])
        self.assertEqual(binlink.pieces("…short…"), [])

    def test_packed_version_links_to_the_bump_commit(self):
        commits = [Commit("c" * 40, "x", "2026-01-03", "Bump", "")]
        links = binlink.link(commits, {"c" * 40: BUMP}, [PACKED], [])
        self.assertEqual(links.strings, {PACKED: ["c" * 40]})


if __name__ == "__main__":
    unittest.main()

"""Dependency and build-revision changes inside a binary (round 5 and the s13 drafts).

A Rust binary carries its crates' source paths (the panic locations), a Go binary
its module list and the git revision it was built from. A changed version there is
a dependency update, not a source change of the project, so it is reported on its
own line. ripgrep 14.1.1-1 -> 14.1.1-1+b4 (a Debian rebuild of one source version)
showed 546 added strings, almost all of them crate paths with a new version.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manishtana.binnoise import BinarySummary, summarize
from manishtana.output import _binary_facts

CRATES = b"/cargo/registry/src/index.crates.io-6f17d22bba15001f/"


class Dependencies(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.work, True)

    def _pair(self, old, new):
        paths = []
        for name, body in (("a", old), ("b", new)):
            path = os.path.join(self.work, name)
            with open(path, "wb") as handle:
                handle.write(b"\x00" + body + b"\x00")
            paths.append(path)
        return summarize(*paths)

    def test_rust_crate_versions(self):
        old = (CRATES + b"lazy_static-1.4.0/src/x.rs" + CRATES + b"clap-4.5.1/src/a.rs"
               + CRATES + b"gone-0.1.0/src/a.rs")
        new = (CRATES + b"lazy_static-1.5.0/src/x.rs" + CRATES + b"clap-4.5.1/src/a.rs"
               + CRATES + b"fresh-2.0.0-rc.1/src/a.rs")
        self.assertEqual(self._pair(old, new).dependencies, [
            "fresh: added 2.0.0-rc.1",
            "gone: removed 0.1.0",
            "lazy_static: 1.4.0 -> 1.5.0",
        ])

    def test_old_cargo_registry_directory_names(self):
        old = b"/cargo/registry/src/github.com-1ecc6299db9ec823/aho-corasick-0.7.18/src/a.rs"
        new = b"/cargo/registry/src/github.com-1ecc6299db9ec823/aho-corasick-0.7.19/src/a.rs"
        self.assertEqual(self._pair(old, new).dependencies, ["aho-corasick: 0.7.18 -> 0.7.19"])

    def test_debian_vendored_registry_in_packed_literals(self):
        # no NULs between the literals, and a Debian path without an index directory
        old = b"x/usr/share/cargo/registry/regex-syntax-0.8.2/src/error.rs: ?/usr/src/rustc-1.80.1/library/x.rs"
        new = b"x/usr/share/cargo/registry/regex-syntax-0.8.5/src/error.rs: ?/usr/src/rustc-1.85.0/library/x.rs"
        summary = self._pair(old, new)
        self.assertEqual(summary.dependencies, ["regex-syntax: 0.8.2 -> 0.8.5"])
        self.assertEqual(summary.toolchain, ["rustc: 1.80.1 -> 1.85.0"])

    def test_same_dependencies_is_empty(self):
        body = CRATES + b"clap-4.5.1/src/a.rs"
        self.assertEqual(self._pair(body + b" one", body + b" two").dependencies, [])

    def test_go_module_versions(self):
        old = b"dep\tgithub.com/x/y\tv1.2.3\th1:abc=\ndep\tgolang.org/z\tv0.1.0\th1:def="
        new = b"dep\tgithub.com/x/y\tv1.2.4\th1:abd=\ndep\tgolang.org/z\tv0.1.0\th1:def="
        self.assertEqual(self._pair(old, new).dependencies, ["github.com/x/y: v1.2.3 -> v1.2.4"])

    def test_go_build_revision(self):
        old = b"build\tvcs.revision=" + b"a" * 40 + b"\nbuild\tvcs.time=2024-11-01T00:00:00Z"
        new = b"build\tvcs.revision=" + b"b" * 40 + b"\nbuild\tvcs.time=2024-11-15T00:00:00Z"
        self.assertEqual(self._pair(old, new).revisions, ("a" * 40, "b" * 40))

    def test_same_revision_gives_none(self):
        body = b"build\tvcs.revision=" + b"a" * 40
        self.assertIsNone(self._pair(body + b" x", body + b" y").revisions)

    def test_report_names_dependencies_and_revisions(self):
        summary = BinarySummary("elf", 1, 2, "a", "b", [(0, 1)], 1, ["new text"], [], 1, 0,
                                dependencies=["clap: 4.5.1 -> 4.5.2"],
                                revisions=("a" * 40, "b" * 40))
        text = "\n".join("".join(run for run, _ in facts) for facts in _binary_facts(summary))
        self.assertIn("Dependencies changed: clap: 4.5.1 -> 4.5.2", text)
        self.assertIn("git log " + "a" * 12 + ".." + "b" * 12, text)

    def test_report_without_the_new_fields_is_unchanged(self):
        summary = BinarySummary("pe", 1, 1, "a", "b", [(0, 1)], 1, [], [], 0, 0)
        text = "\n".join("".join(run for run, _ in facts) for facts in _binary_facts(summary))
        self.assertNotIn("Dependencies", text)
        self.assertNotIn("git log", text)


if __name__ == "__main__":
    unittest.main()

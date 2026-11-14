"""Link by source file name (round 5, finding 3, second approach).

A change with no literal of its own (fd's one-syscall fix in src/walk.rs) cannot be
linked by string. But a Rust panic location, a C assert or a log line quotes the
source file name, and a commit's patch names the files it changed. A function or
string that quotes "src/walk.rs" is therefore tied to the commits that changed
that file. Weaker evidence than a literal on a changed line: it says "this code
lives in a file the commit touched", not "the commit touched these lines".
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manishtana import binlink
from manishtana.binengines import FunctionChange
from manishtana.gitrepo import Commit

WALK = ("diff --git a/src/walk.rs b/src/walk.rs\n--- a/src/walk.rs\n+++ b/src/walk.rs\n"
        "@@ -1 +1 @@\n-    let a = 1;\n+    let a = 2;\n")
OTHER = ("diff --git a/src/other.rs b/src/other.rs\n--- a/src/other.rs\n+++ b/src/other.rs\n"
         "@@ -1 +1 @@\n-    let b = 1;\n+    let b = 2;\n")
COMMITS = [Commit("a" * 40, "x", "d", "Skip syscall", ""), Commit("b" * 40, "x", "d", "Other", "")]
PATCHES = {"a" * 40: WALK, "b" * 40: OTHER}


def change(name, pseudo):
    return FunctionChange(name, name, "modified", 0.9, {}, pseudo)


class SourcePaths(unittest.TestCase):
    def test_paths_are_found_in_text(self):
        self.assertEqual(binlink.paths('panic at "src/walk.rs:120:9" and lib.c'),
                         ["src/walk.rs", "lib.c"])

    def test_no_extension_no_path(self):
        self.assertEqual(binlink.paths("version 1.7.1 of the tool"), [])

    def test_changed_files_of_a_patch(self):
        self.assertEqual(binlink.changed_files(WALK), ["src/walk.rs"])


class FileLinks(unittest.TestCase):
    def test_function_quoting_a_source_path_links_to_the_commit(self):
        found = change("FUN_00401000", '+  core::panicking::panic("src/walk.rs", 120);\n')
        links = binlink.link(COMMITS, PATCHES, [], [found])
        self.assertEqual(links.functions, {"FUN_00401000": ["a" * 40]})

    def test_registry_prefix_in_the_binary_still_matches_by_suffix(self):
        text = "/rustc/abc/library/src/walk.rs"
        links = binlink.link(COMMITS, PATCHES, [text], [])
        self.assertEqual(links.strings, {text: ["a" * 40]})

    def test_basename_alone_is_not_enough_when_the_directory_differs(self):
        links = binlink.link(COMMITS, PATCHES, ["vendor/dep/walk.rs"], [])
        self.assertEqual(links.strings, {})

    def test_bare_file_name_matches_a_nested_path(self):
        # a C binary's __FILE__ is often just the base name
        patch = ("diff --git a/lib/zstd_compress.c b/lib/zstd_compress.c\n--- a\n+++ b\n"
                 "@@ -1 +1 @@\n-x\n+y\n")
        commits = [Commit("c" * 40, "x", "d", "s", "")]
        links = binlink.link(commits, {"c" * 40: patch}, ["zstd_compress.c"], [])
        self.assertEqual(links.strings, {"zstd_compress.c": ["c" * 40]})

    def test_literal_evidence_comes_first_and_is_not_duplicated(self):
        both = WALK + '+    println!("ignore-contain check skipped");\n'
        found = change("FUN_1", '+  puts("ignore-contain check skipped");\n+  at("src/walk.rs");\n')
        links = binlink.link(COMMITS, {"a" * 40: both, "b" * 40: OTHER}, [], [found])
        self.assertEqual(links.functions, {"FUN_1": ["a" * 40]})

    def test_no_patches_no_file_links(self):
        self.assertEqual(binlink.link(COMMITS, {}, ["src/walk.rs"], []).strings, {})


if __name__ == "__main__":
    unittest.main()

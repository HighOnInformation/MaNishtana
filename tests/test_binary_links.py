"""Link binary findings to commits of a revision range (round 1 finding 3).

A stripped binary has only FUN_ names, so a changed function cannot be matched to
the source by name. What survives stripping is the string literals: the decompiled
code of a changed function quotes them, and so do the commits that changed them.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manishtana import binlink, cli, gitrepo
from manishtana.binengines import FunctionChange, FunctionDiff
from manishtana.binnoise import BinarySummary
from manishtana.compare import Comparison
from manishtana.gitrepo import Commit
from manishtana.output import build_json, build_report

PATCH_WALK = (
    "diff --git a/src/walk.rs b/src/walk.rs\n"
    "--- a/src/walk.rs\n+++ b/src/walk.rs\n"
    "@@ -10,3 +10,3 @@ fn spawn_senders(config: &Config)\n"
    '-    println!("skipping the syscall");\n'
    '+    println!("ignore-contain check skipped");\n'
)
PATCH_DOC = (
    "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
    "@@ -1 +1 @@\n-old text\n+new text about ignore-contain check skipped\n"
)
COMMITS = [Commit("a" * 40, "x", "2026-01-01", "Skip syscall", ""),
           Commit("b" * 40, "x", "2026-01-02", "Docs", "")]
PATCHES = {"a" * 40: PATCH_WALK, "b" * 40: PATCH_DOC}


class Literals(unittest.TestCase):
    def test_literals_come_from_changed_lines_only(self):
        diff = ('--- a\n+++ b\n@@ -1 +1 @@\n'
                ' puts("unchanged context line");\n'
                '-  puts("old message here\\n");\n'
                '+  puts("new message here\\n");\n')
        self.assertEqual(binlink.literals(diff), ["old message here\\n", "new message here\\n"])

    def test_short_literals_are_ignored(self):
        self.assertEqual(binlink.literals('+  f("ab", "%d", "enough text");'), ["enough text"])

    def test_excerpt_pieces_are_matched_not_the_ellipsis(self):
        self.assertEqual(binlink.pieces("first piece here … second piece there"),
                         ["first piece here", "second piece there"])
        self.assertEqual(binlink.pieces("…short…"), [])


class Matching(unittest.TestCase):
    def test_string_links_to_the_commits_that_touch_it(self):
        links = binlink.link(COMMITS, PATCHES, ["ignore-contain check skipped"], [])
        self.assertEqual(links.strings, {"ignore-contain check skipped": ["a" * 40, "b" * 40]})

    def test_unrelated_string_has_no_link(self):
        links = binlink.link(COMMITS, PATCHES, ["nothing like it anywhere"], [])
        self.assertEqual(links.strings, {})

    def test_context_lines_do_not_count(self):
        patches = {"a" * 40: "@@ -1 +1 @@\n context only mentioned here\n+x\n"}
        self.assertEqual(binlink.link(COMMITS[:1], patches, ["context only mentioned here"],
                                      []).strings, {})

    def test_stripped_function_is_linked_by_its_literals(self):
        change = FunctionChange("FUN_00101000", "FUN_00101000", "modified", 0.9,
                                {"ghidriff": "modified"},
                                '--- a\n+++ b\n-  puts("skipping the syscall");\n'
                                '+  puts("ignore-contain check skipped");\n')
        links = binlink.link(COMMITS, PATCHES, [], [change])
        self.assertEqual(links.functions, {"FUN_00101000": ["a" * 40, "b" * 40]})

    def test_named_function_is_linked_by_hunk_context(self):
        change = FunctionChange("spawn_senders", "spawn_senders", "modified", 0.9,
                                {"ghidriff": "modified"}, None)
        links = binlink.link(COMMITS, PATCHES, [], [change])
        self.assertEqual(links.functions, {"spawn_senders": ["a" * 40]})

    def test_function_name_must_be_a_whole_word(self):
        change = FunctionChange("senders", "senders", "modified", 0.9, {}, None)
        self.assertEqual(binlink.link(COMMITS, PATCHES, [], [change]).functions, {})

    def test_fun_names_are_never_searched_as_names(self):
        patches = {"a" * 40: "@@ -1 +1 @@\n+// FUN_00101000 is mentioned\n"}
        change = FunctionChange("FUN_00101000", None, "removed", None, {}, None)
        self.assertEqual(binlink.link(COMMITS[:1], patches, [], [change]).functions, {})

    def test_many_commits_are_capped(self):
        commits = [Commit("%040x" % i, "x", "d", "s", "") for i in range(9)]
        patches = {c.sha: "+  puts(\"shared literal text\");\n" for c in commits}
        links = binlink.link(commits, patches, ["shared literal text"], [])
        self.assertEqual(len(links.strings["shared literal text"]), binlink.MAX_LINKS)

    def test_escaped_form_of_a_real_newline_matches(self):
        patches = {"a" * 40: '+  puts("two\\nlines here");\n'}
        links = binlink.link(COMMITS[:1], patches, ["two\nlines here"], [])
        self.assertEqual(list(links.strings), ["two\nlines here"])


class Rendering(unittest.TestCase):
    def setUp(self):
        self.comparison = Comparison(real={"fd": ("M", ["fd"])}, noise=set(), skipped=set(),
                                     sections={}, binaries={"fd"})
        self.summary = BinarySummary("elf", 10, 12, "a" * 64, "b" * 64, [(0, 2)], 1,
                                     ["ignore-contain check skipped"], [], 1, 0, 0, [])
        self.found = FunctionDiff(["ghidriff"], [
            FunctionChange("FUN_00101000", "FUN_00101000", "modified", 0.5,
                           {"ghidriff": "modified"}, None)], [])
        self.links = binlink.Links({"ignore-contain check skipped": ["a" * 40]},
                                   {"FUN_00101000": ["a" * 40]})
        self.kw = {"binary": {"fd": self.summary}, "functions": {"fd": self.found},
                   "commits": COMMITS, "links": {"fd": self.links}}

    def test_markdown_names_the_commit_next_to_the_function_and_string(self):
        md = build_report("v1", "v2", self.comparison, {}, None, **self.kw)
        self.assertIn("| `FUN_00101000` | modified | 0.50 | ghidriff; commit aaaaaaaaa |", md)
        self.assertIn("Linked to commits: `ignore-contain check skipped` → aaaaaaaaa "
                      "(Skip syscall)", md)

    def test_json_carries_the_links(self):
        payload = build_json("v1", "v2", self.comparison, {}, None, **self.kw)
        entry = payload["real"][0]
        self.assertEqual(entry["binary_functions"]["changes"][0]["commits"], ["a" * 40])
        self.assertEqual(entry["binary_links"],
                         {"ignore-contain check skipped": ["a" * 40]})

    def test_without_links_nothing_changes(self):
        del self.kw["links"]
        md = build_report("v1", "v2", self.comparison, {}, None, **self.kw)
        self.assertIn("| `FUN_00101000` | modified | 0.50 | ghidriff |", md)
        self.assertNotIn("Linked to commits", md)


def _git(repo, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c",
                    "commit.gpgsign=false"] + list(args), cwd=repo, check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class RepoPatches(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q")
        self._commit("a.c", 'puts("hello old world");\n', "first")
        self.base = gitrepo.resolve(self.repo, "HEAD")
        self._commit("a.c", 'puts("hello brand new world");\n', "reword greeting")
        self.head = gitrepo.resolve(self.repo, "HEAD")

    def _commit(self, name, text, message):
        with open(os.path.join(self.repo, name), "w", newline="\n") as handle:
            handle.write(text)
        _git(self.repo, "add", name)
        _git(self.repo, "commit", "-q", "-m", message)

    def test_patches_are_keyed_by_sha_for_the_range(self):
        patches = gitrepo.patches(self.repo, self.base, self.head)
        self.assertEqual(list(patches), [self.head])
        self.assertIn('+puts("hello brand new world");', patches[self.head])
        self.assertNotIn("first", patches[self.head])

    def test_bad_range_gives_empty_not_a_crash(self):
        self.assertEqual(gitrepo.patches(self.repo, "nope", "nada"), {})

    def test_cli_link_repo_with_two_directories(self):
        a, b = os.path.join(self.tmp, "a"), os.path.join(self.tmp, "b")
        for path, text in ((a, b"hello old world"), (b, b"hello brand new world")):
            os.makedirs(path)
            with open(os.path.join(path, "tool.bin"), "wb") as handle:
                handle.write(b"\x00\x01\x02" + b"\x00" + text + b"\x00" + b"\x03" * 20)
        config = os.path.join(self.tmp, "c.json")
        with open(config, "w") as handle:
            json.dump({"binary": {"engines": []}}, handle)
        out, js = os.path.join(self.tmp, "r.md"), os.path.join(self.tmp, "r.json")
        code = cli.main([a, b, "-c", config, "-o", out, "-j", js, "--no-llm",
                         "--link-repo", self.repo, "--from", self.base, "--to", self.head])
        self.assertEqual(code, 0)
        with open(js, encoding="utf-8") as handle:
            data = json.load(handle)
        links = data["real"][0]["binary_links"]
        self.assertEqual(links, {"hello brand new world": [self.head],
                                 "hello old world": [self.head]})
        with open(out, encoding="utf-8") as handle:
            self.assertIn("Linked to commits", handle.read())

    def test_link_repo_without_from_is_an_error(self):
        a = os.path.join(self.tmp, "a")
        os.makedirs(a)
        code = cli.main([a, a, "--link-repo", self.repo, "--no-llm", "-o",
                         os.path.join(self.tmp, "x.md")])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()

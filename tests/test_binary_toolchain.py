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


class RebuildLayoutNoise(unittest.TestCase):
    """ripgrep 14.1.1 alpine 3.21 -> 3.22: the same literals, glued to compiler-library
    and registry source paths, are laid out in a different order. Paths inside the
    compiler library or a cargo registry are toolchain churn; the text around them is
    compared piece by piece."""

    setUp = ToolchainStrings.setUp
    _sum = ToolchainStrings._sum

    REG = b"/home/buildozer/.cargo/registry/src/index.crates.io-6f17d22bba15001f/"
    STD = b"/rustc/f6e511eec7342f59a25f7c0534f1dbea00d01b14/library/"

    def test_same_literals_laid_out_differently_are_not_a_change(self):
        old = (b"\x00" + self.REG + b"regex-syntax-0.8.4/src/hir/visitor.rsUnicode not allowed here"
               + self.STD + b"std/src/thread/mod.rsfailed to spawn thread"
               + b"crates/searcher/src/lines.rsassertion failed: m.0 <= m.1: \x00")
        new = (b"\x00" + b"crates/searcher/src/lines.rsassertion failed: m.0 <= m.1: "
               + self.STD + b"core/src/cell/once.rsUnicode not allowed here"
               + self.REG + b"bstr-1.10.0/src/impls.rsfailed to spawn thread\x00")
        s = self._sum(old, new)
        self.assertEqual((s.strings_added_total, s.strings_removed_total), (0, 0))

    def test_a_compiler_library_path_alone_is_not_a_changed_string(self):
        old = b"\x00" + self.STD + b"core/src/iter/traits/iterator.rsmid > len\x00"
        new = b"\x00" + self.STD + b"core/src/slice/mod.rsmid > len\x00"
        s = self._sum(old, new)
        self.assertEqual((s.strings_added_total, s.strings_removed_total), (0, 0))

    def test_a_new_message_next_to_library_paths_is_still_reported(self):
        old = b"\x00" + self.STD + b"std/src/thread/mod.rsfailed to spawn thread\x00"
        new = (b"\x00" + self.STD + b"std/src/thread/mod.rsfailed to spawn thread"
               + self.REG + b"bstr-1.10.0/src/impls.rsa brand new error message\x00")
        s = self._sum(old, new)
        self.assertEqual(s.strings_added_total, 1)
        self.assertIn("a brand new error message", s.strings_added[0])

    def test_the_projects_own_source_path_is_still_a_string(self):
        old = b"\x00crates/core/flags/doc/help.rsone message\x00"
        new = b"\x00crates/core/flags/doc/help.rsone message crates/core/flags/new_file.rs\x00"
        s = self._sum(old, new)
        self.assertEqual(s.strings_added_total, 1)
        self.assertIn("crates/core/flags/new_file.rs", s.strings_added[0])


    def test_a_core_dir_outside_crates_is_the_projects_own_path(self):
        old = b"\x00core/src/engine.rsone message here\x00"
        new = b"\x00core/src/engine.rsone message here core/src/extra.rs\x00"
        s = self._sum(old, new)
        self.assertEqual(s.strings_added_total, 1)
        self.assertIn("core/src/extra.rs", s.strings_added[0])

    def test_rustlib_library_paths_are_toolchain(self):
        old = b"\x00/usr/lib/rustlib/src/rust/library/core/src/cell.rsmid > len\x00"
        new = b"\x00/usr/lib/rustlib/src/rust/library/core/src/slice.rsmid > len\x00"
        s = self._sum(old, new)
        self.assertEqual((s.strings_added_total, s.strings_removed_total), (0, 0))


class RebuildLiteralRelayout(unittest.TestCase):
    """The same literals re-glued next to different neighbours (real strings from the
    ripgrep 14.1.1 alpine 3.21 / 3.22 rebuild), compiled-code bytes that look like text,
    and the library path with and without its "library/" prefix."""

    setUp = ToolchainStrings.setUp
    _sum = ToolchainStrings._sum

    def test_regluing_and_library_prefix_change_nothing(self):
        old = (b"\x00mid > lenfile system loop foundIO error for operation on : File system loop found:  points to an ancestor"
               b"std/src/path.rs.extension cannot contain path separators: stdinstd/src/sync/once.rslock count overflow in reentrant mutex\x00")
        new = (b"\x00.extension cannot contain path separators: stdinlibrary/std/src/sync/poison/once.rslock count overflow in reentrant mutex"
               b"library/std/src/thread/current.rsmid > len\x00"
               b"\x00file system loop foundIO error for operation on : File system loop found:  points to an ancestor\x00")
        s = self._sum(old, new)
        self.assertEqual((s.strings_added, s.strings_removed), ([], []))
        self.assertGreater(s.layout_noise, 0)

    def test_one_new_word_in_a_reglued_literal_is_reported(self):
        old = b"\x00failed to spawn thread, a message that stays\x00"
        new = b"\x00a message that staysfailed to spawn thread, and ripgrep now prints a brand new warning\x00"
        s = self._sum(old, new)
        self.assertEqual(s.strings_added_total, 1)
        self.assertIn("brand new warning", s.strings_added[0])

    def test_compiled_code_bytes_are_counted_not_listed(self):
        old = b"\x00H9D$ u{M\x00L9t$Hv0I\x00"
        new = b"\x00tHL;t$Hu\x00H;l$`u!H\x00"
        s = self._sum(old, new)
        self.assertEqual((s.strings_added, s.strings_removed), ([], []))
        self.assertEqual(s.layout_noise, 4)

    def test_a_short_format_string_is_not_taken_for_code(self):
        old = b"\x00nothing here at all\x00"
        new = b"\x00nothing here at all\x00%d.%d.%d-%s\x00"
        s = self._sum(old, new)
        self.assertEqual(s.strings_added_total, 1)

    def test_report_mentions_the_count(self):
        summary = BinarySummary("elf", 1, 2, "a", "b", [(0, 1)], 1, [], [], 0, 0, layout_noise=7)
        comparison = Comparison(real={"rg": ("M", ["rg"])}, noise=set(), skipped=set(),
                               sections={}, binaries={"rg"})
        text = build_report("A", "B", comparison, {}, None, binary={"rg": summary})
        self.assertIn("7 strings differ only in literal layout or look like code bytes", text)


class RelayoutFalseNegatives(unittest.TestCase):
    """Real edits that must survive the layout-noise filter (review of round 7)."""

    setUp = ToolchainStrings.setUp
    _sum = ToolchainStrings._sum
    F = b"\x00filler string number one\x00"

    def _changed(self, old, new):
        s = self._sum(self.F + old, self.F + new)
        return s.strings_added, s.strings_removed

    def test_a_word_inserted_near_the_start_is_reported(self):
        added, _ = self._changed(b"\x00do follow symbolic links\x00",
                                 b"\x00do not follow symbolic links\x00")
        self.assertEqual(added, ["do not follow symbolic links"])

    def test_a_changed_first_word_is_reported(self):
        added, removed = self._changed(b"\x00Disable the frobnicator option\x00",
                                       b"\x00Enable the frobnicator option\x00")
        self.assertEqual((added, removed), (["Enable the frobnicator option"],
                                            ["Disable the frobnicator option"]))

    def test_a_changed_last_word_is_reported(self):
        added, _ = self._changed(b"\x00search hidden files and dirs\x00",
                                 b"\x00search hidden files and links\x00")
        self.assertEqual(added, ["search hidden files and links"])

    def test_a_plural_at_the_end_is_reported(self):
        added, _ = self._changed(b"\x00failed to spawn worker thread\x00",
                                 b"\x00failed to spawn worker threads\x00")
        self.assertEqual(added, ["failed to spawn worker threads"])

    def test_a_renamed_project_source_file_is_reported(self):
        added, _ = self._changed(b"\x00crates/core/src/main.rsok\x00",
                                 b"\x00crates/core/src/mainx.rsok\x00")
        self.assertEqual(added, ["crates/core/src/mainx.rs"])

    def test_a_short_literal_glued_at_an_end_still_counts_as_layout(self):
        # "stdin" is glued after a long seen literal in one build, alone in the other
        old = b"\x00.extension cannot contain path separators: \x00stdin and more\x00"
        new = b"\x00.extension cannot contain path separators: stdin\x00"
        s = self._sum(old, new)
        self.assertEqual(s.strings_added, [])

    LONG = b"the quick brown fox jumps over the lazy sleeping dog today"
    SHORT = b"the quick brown fox jumps over the lazy dog today"

    def test_a_pure_deletion_is_reported_and_labelled(self):
        s = self._sum(b"\x00" + self.F + self.LONG + b"\x00", b"\x00" + self.F + self.SHORT + b"\x00")
        self.assertEqual(s.strings_added, [])
        self.assertEqual(len(s.deletions), 1)
        self.assertIn("the quick brown fox jumps over the lazy dog today", s.deletions[0])
        self.assertIn("removed: sleeping", s.deletions[0])

    def test_a_pure_insertion_is_reported_and_labelled(self):
        s = self._sum(b"\x00" + self.F + self.SHORT + b"\x00", b"\x00" + self.F + self.LONG + b"\x00")
        self.assertEqual(len(s.deletions), 1)
        self.assertIn("inserted: sleeping", s.deletions[0])

    def test_a_deleted_common_word_is_reported(self):
        # "not" still occurs in another literal of the new build ("file not found"), but
        # the old build had one more "not": it was deleted, not moved
        old = b"\x00--no-ignore: do not respect ignore files\x00file not found\x00"
        new = b"\x00--no-ignore: do respect ignore files\x00file not found\x00"
        s = self._sum(self.F + old, self.F + new)
        self.assertEqual(len(s.deletions), 1)
        self.assertIn("removed: not", s.deletions[0])

    def test_a_moved_literal_is_layout_not_a_deletion(self):
        # "sleeping dog today" moved to its own literal: nothing was deleted
        old = b"\x00" + self.F + self.LONG + b"\x00"
        new = b"\x00" + self.F + self.SHORT + b"\x00sleeping\x00"
        s = self._sum(old, new)
        self.assertEqual(s.deletions, [])

    def test_deletions_are_shown_in_the_report(self):
        from manishtana.output import _binary_facts
        s = self._sum(b"\x00" + self.F + self.LONG + b"\x00", b"\x00" + self.F + self.SHORT + b"\x00")
        text = " ".join(t for fact in _binary_facts(s) for t, _ in fact)
        self.assertIn("pure deletion", text)
        self.assertIn("removed: sleeping", text)

    def test_an_environment_variable_name_is_not_code(self):
        added, _ = self._changed(b"", b"\x00RG_DEBUG_LOG\x00NO_COLOR\x00")
        self.assertEqual(sorted(added), ["NO_COLOR", "RG_DEBUG_LOG"])

    def test_error_to_fatal_label_change_is_reported(self):
        added, removed = self._changed(b"\x00ERROR: \x00", b"\x00FATAL: \x00")
        self.assertEqual((added, removed), (["FATAL: "], ["ERROR: "]))

    def test_a_padded_label_is_not_code(self):
        added, _ = self._changed(b"", b"\x00WARNING:   \x00")
        self.assertEqual(added, ["WARNING:   "])

    def test_a_changed_end_word_that_exists_elsewhere_is_reported(self):
        # "links" appears in another literal on the other side, but "...files and dirs" was one
        # literal there, so "...files and links" is an edit, not a re-gluing
        old = b"\x00search hidden files and dirs\x00show all links\x00"
        new = b"\x00search hidden files and links\x00show all links\x00"
        added, removed = self._changed(old, new)
        self.assertEqual(added, ["search hidden files and links"])
        self.assertEqual(removed, ["search hidden files and dirs"])

    def test_a_changed_start_word_that_exists_elsewhere_is_reported(self):
        old = b"\x00Disable the frobnicator option\x00please Enable it now\x00"
        new = b"\x00Enable the frobnicator option\x00please Enable it now\x00"
        added, _ = self._changed(old, new)
        self.assertEqual(added, ["Enable the frobnicator option"])

    def test_an_upper_case_label_is_not_code(self):
        added, _ = self._changed(b"", b"\x00WARNING:\x00")
        self.assertEqual(added, ["WARNING:"])

    def test_a_project_path_ending_in_std_src_is_not_compiler_noise(self):
        # "nostd/src/" must not be cut at its "std/src/" tail
        added, _ = self._changed(b"\x00nostd/src/engine.rsone message here\x00",
                                 b"\x00nostd/src/engine2.rsone message here\x00")
        self.assertEqual(added, ["nostd/src/engine2.rsone message here"])

    def test_a_project_path_ending_in_alloc_src_is_not_compiler_noise(self):
        added, _ = self._changed(b"\x00myalloc/src/pool.rsone message here\x00",
                                 b"\x00myalloc/src/pool2.rsone message here\x00")
        self.assertEqual(added, ["myalloc/src/pool2.rsone message here"])

    def test_a_real_std_path_glued_to_a_literal_is_still_noise(self):
        old = b"\x00stdinstd/src/sync/once.rslock count overflow in reentrant mutex\x00"
        new = b"\x00stdinstd/src/thread/mod.rslock count overflow in reentrant mutex\x00"
        added, _ = self._changed(old, new)
        self.assertEqual(added, [])

    def test_json_deletions_are_capped_with_a_total(self):
        from manishtana.output import _DELETIONS_JSON_MAX, _binary_json
        items = ["literal number %d (removed: piece)" % n for n in range(_DELETIONS_JSON_MAX + 50)]
        summary = BinarySummary(*[None] * len(BinarySummary._fields))._replace(deletions=items)
        detail = _binary_json(summary)
        self.assertEqual(len(detail["deletions"]), _DELETIONS_JSON_MAX)
        self.assertEqual(detail["deletions_total"], _DELETIONS_JSON_MAX + 50)
        self.assertEqual(detail["deletions"][0], items[0])

    def test_json_without_deletions_has_a_zero_total(self):
        from manishtana.output import _binary_json
        summary = BinarySummary(*[None] * len(BinarySummary._fields))._replace(deletions=[])
        detail = _binary_json(summary)
        self.assertEqual((detail["deletions"], detail["deletions_total"]), ([], 0))

    def test_a_huge_literal_is_not_scanned_for_a_removed_chunk(self):
        from manishtana.binnoise import _MAX_LITERAL, _removed_chunk
        base = "word " * (_MAX_LITERAL // 5 + 100)
        other = "start of the literal " + base + "and the end"
        text = "start of the literal " + base[:-10] + "and the end"
        self.assertIsNone(_removed_chunk(text, other, text))

    def test_a_huge_removed_chunk_is_reported_truncated(self):
        from manishtana.binnoise import _MAX_CHUNK, _removed_chunk
        chunk = "unique words in this long deleted piece " * 40
        other = "the head of the literal " + chunk + " the tail of the literal"
        text = "the head of the literal  the tail of the literal"
        got = _removed_chunk(text, other, text)
        self.assertIsNotNone(got)
        self.assertLessEqual(len(got), _MAX_CHUNK + 1)

    def test_deletion_checks_are_budgeted_and_the_rest_counted_as_layout(self):
        from manishtana import binnoise
        old_max = binnoise._MAX_DELETION_CHECKS
        binnoise._MAX_DELETION_CHECKS = 2
        self.addCleanup(setattr, binnoise, "_MAX_DELETION_CHECKS", old_max)
        a = [("the quick brown fox %d jumps over the lazy sleeping dog today" % n) for n in range(6)]
        b = [x.replace("sleeping ", "") for x in a]
        _, _, dropped, deletions = binnoise._drop_layout_noise(set(b), set(a), a, b)
        self.assertEqual(len(deletions), 2)
        self.assertGreaterEqual(dropped, 4)

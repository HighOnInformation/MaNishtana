"""Unit tests for manishtana — no git, no normalizers, no network.

Run from the repository root:

    python3 -m unittest discover -s tests

Most tests exercise the public interface of a module. A few reach for underscore
names: those are internal seams, private to their module but used by its own tests
because git's path formatting and the risk-extraction regex have enough edge cases
that testing them only from the outside would make failures hard to localize.
"""

import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manishtana.cli import main
from manishtana.compare import Comparison, _noise
from manishtana.gitdiff import _parse_name_status, _relativize, _split_sections, _unquote
from manishtana.gitrepo import Commit, _is_within, _parse_log, intent_text
from manishtana.llm import _manifest, analyze_changes
from manishtana.output import (
    _extract_risk,
    _mark_pair,
    _side_by_side,
    build_html,
    build_json,
    build_report,
)
from manishtana.settings import load as load_settings
from manishtana.symbols import CrossReference as CrossRef
from manishtana.symbols import _definitions, cross_reference
from manishtana.rules import (
    IGNORED_TOKEN,
    _apply_ignore_lines,
    _command_for,
    _program_of,
    _quote_name,
    prepare_copy,
    skipped_files,
)


def comparison(real=None, noise=None, skipped=None, sections=None, binaries=None):
    """Build a Comparison for tests, defaulting to one modified file."""
    if real is None:
        real = {"sensor.c": ("M", ["sensor.c"])}
    if sections is None:
        sections = {
            rel: "diff --git a/%s b/%s\n@@ -1 +1 @@\n-x\n+y\n" % (rel, rel) for rel in real
        }
    return Comparison(
        real=real,
        noise=set() if noise is None else noise,
        skipped=set() if skipped is None else skipped,
        sections=sections,
        binaries=set() if binaries is None else binaries,
    )


class FakeChat:
    """Stands in for the chat seam: records calls, returns a canned reply."""

    def __init__(self, reply="**Risk** — Low, fine."):
        self.calls = []
        self._reply = reply

    def __call__(self, system, user, label):
        self.calls.append({"system": system, "user": user, "label": label})
        return self._reply(len(self.calls)) if callable(self._reply) else self._reply

    @property
    def labels(self):
        return [call["label"] for call in self.calls]


# --- rules -------------------------------------------------------------------


class SkippedFiles(unittest.TestCase):
    def setUp(self):
        self.rules = [
            {"match": ["generated_*.c"], "skip": True},
            {"match": ["*.c", "*.h"], "normalize": ["clang-format"]},
            {"match": ["*.log"], "skip": True},
        ]

    def test_first_match_wins(self):
        # generated_*.c is listed before *.c, so it skips rather than normalizes.
        self.assertEqual(
            skipped_files(self.rules, ["src/generated_tables.c", "src/sensor.c"]),
            {"src/generated_tables.c"},
        )

    def test_matches_basename_not_only_path(self):
        self.assertEqual(skipped_files(self.rules, ["deep/nested/build.log"]), {"deep/nested/build.log"})

    def test_unmatched_paths_are_not_skipped(self):
        self.assertEqual(skipped_files(self.rules, ["notes.txt", "src/sensor.c"]), set())


class ApplyIgnoreLines(unittest.TestCase):
    def test_replaces_and_preserves_line_count(self):
        text = "keep\n#define BUILD_NUMBER 7\nkeep too"
        result = _apply_ignore_lines(text, [r"^\s*#define\s+BUILD_NUMBER\b"])
        self.assertEqual(result.split("\n"), ["keep", IGNORED_TOKEN, "keep too"])

    def test_no_patterns_is_a_passthrough(self):
        self.assertEqual(_apply_ignore_lines("a\nb", []), "a\nb")

    def test_non_matching_text_unchanged(self):
        self.assertEqual(_apply_ignore_lines("a\nb", ["^zzz"]), "a\nb")


class QuoteName(unittest.TestCase):
    """Filenames come from the compared tree — untrusted — into a shell=True line."""

    def test_metacharacters_are_quoted(self):
        quoted = _quote_name("x&mkdir OWNED&.c")
        self.assertTrue(quoted[0] in "'\"" and quoted[-1] in "'\"")
        self.assertIn("x&mkdir OWNED&.c", quoted)

    def test_command_substitutes_the_quoted_name(self):
        command = _command_for("clang-format", "my file.c")
        self.assertIn(_quote_name("my file.c"), command)
        self.assertNotIn("=my file.c", command)

    def test_plain_name_still_substituted(self):
        self.assertIn("sensor.c", _command_for("clang-format", "sensor.c"))

    def test_unknown_step_is_still_run_as_written(self):
        self.assertTrue(_command_for("tr -d x", "a.c").startswith("tr -d x"))


class PrepareCopy(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.src = os.path.join(self.work, "src")
        self.dst = os.path.join(self.work, "dst")
        os.makedirs(self.src)

    def _write(self, name, data):
        path = os.path.join(self.src, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def test_undecodable_bytes_are_left_byte_identical(self):
        # errors="replace" would fold every bad byte to U+FFFD, making two
        # different cp1252 files identical after normalization — i.e. "noise".
        original = b'const char *mode = "caf\xe9";\n'
        self._write("enc.c", original)
        prepare_copy(self.src, self.dst, [{"match": ["*.c"], "ignore_lines": ["^// NOTE"]}])
        with open(os.path.join(self.dst, "enc.c"), "rb") as handle:
            self.assertEqual(handle.read(), original)

    def test_read_only_file_does_not_abort_the_run(self):
        path = self._write("ro.c", b"int a;\n")
        os.chmod(path, stat.S_IREAD)
        self.addCleanup(os.chmod, path, stat.S_IWRITE)
        prepare_copy(self.src, self.dst, [{"match": ["*.c"], "ignore_lines": ["^int"]}])
        copied = os.path.join(self.dst, "ro.c")
        os.chmod(copied, stat.S_IWRITE)
        self.assertTrue(os.path.isfile(copied))

    def test_a_configured_normalizer_alias_is_used(self):
        settings = load_settings({"normalizers": {"clang-format": "no-such-tool-xyz {name}"}}, ".")
        self._write("a.c", b"int a;\n")
        # The tool does not exist, so the step is skipped with a warning naming it.
        prepare_copy(self.src, self.dst, [{"match": ["*.c"], "normalize": ["clang-format"]}],
                     settings=settings)
        self.assertTrue(os.path.isfile(os.path.join(self.dst, "a.c")))

    def test_a_configured_ignored_token_is_used(self):
        settings = load_settings({"tuning": {"ignored_token": "<<GONE>>"}}, ".")
        self._write("v.h", b"#define BUILD_NUMBER 7\nint a;\n")
        prepare_copy(self.src, self.dst, [{"match": ["*.h"], "ignore_lines": [r"^#define BUILD"]}],
                     settings=settings)
        with open(os.path.join(self.dst, "v.h"), "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read().split("\n")[0], "<<GONE>>")

    def test_ignore_lines_still_applied_to_a_normal_file(self):
        self._write("v.h", b"#define BUILD_NUMBER 7\nint a;\n")
        prepare_copy(self.src, self.dst, [{"match": ["*.h"], "ignore_lines": [r"^#define BUILD"]}])
        with open(os.path.join(self.dst, "v.h"), "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read().split("\n")[0], IGNORED_TOKEN)


class ProgramOf(unittest.TestCase):
    def test_plain_command(self):
        self.assertEqual(_program_of("clang-format -style=LLVM"), "clang-format")

    def test_quoted_path_with_spaces(self):
        self.assertEqual(_program_of('"C:/Program Files/py.exe" -c "x"'), "C:/Program Files/py.exe")


# --- settings ----------------------------------------------------------------


class LoadSettings(unittest.TestCase):
    """Every word sent to the model must be declarable in the config."""

    def setUp(self):
        self.work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.work, True)

    def test_an_empty_config_yields_the_built_in_defaults(self):
        settings = load_settings({}, self.work)
        self.assertIn("senior software engineer", settings.prompts["file_system"])
        self.assertEqual(settings.risk["file_label"], "Risk")
        self.assertIn("clang-format", settings.normalizers)
        self.assertEqual(settings.tuning["normalize_timeout_sec"], 60)

    def test_a_prompt_can_be_overridden_inline(self):
        settings = load_settings({"prompts": {"file_system": "be brief"}}, self.work)
        self.assertEqual(settings.prompts["file_system"], "be brief")
        # untouched keys keep their defaults
        self.assertIn("{diff}", settings.prompts["file_user"])

    def test_an_at_prefix_reads_the_prompt_from_a_file(self):
        path = os.path.join(self.work, "review.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("prompt from a file\n")
        settings = load_settings({"prompts": {"file_system": "@review.md"}}, self.work)
        self.assertEqual(settings.prompts["file_system"], "prompt from a file")

    def test_a_missing_prompt_file_is_a_loud_error(self):
        with self.assertRaises(ValueError) as caught:
            load_settings({"prompts": {"file_system": "@nope.md"}}, self.work)
        self.assertIn("nope.md", str(caught.exception))

    def test_an_unknown_placeholder_is_rejected(self):
        with self.assertRaises(ValueError) as caught:
            load_settings({"prompts": {"file_user": "{path} {banana}"}}, self.work)
        self.assertIn("banana", str(caught.exception))

    def test_a_missing_required_placeholder_is_rejected(self):
        with self.assertRaises(ValueError) as caught:
            load_settings({"prompts": {"file_user": "no diff here"}}, self.work)
        self.assertIn("{diff}", str(caught.exception))

    def test_an_unknown_prompt_key_is_rejected(self):
        # A typo'd key would otherwise be a silent no-op.
        with self.assertRaises(ValueError) as caught:
            load_settings({"prompts": {"file_systm": "oops"}}, self.work)
        self.assertIn("file_systm", str(caught.exception))

    def test_the_risk_label_can_move_with_the_prompt(self):
        settings = load_settings({"risk": {"file_label": "Severity"}}, self.work)
        self.assertEqual(settings.risk["file_label"], "Severity")
        self.assertEqual(settings.risk["summary_label"], "Overall risk")

    def test_a_normalizer_alias_can_be_replaced(self):
        settings = load_settings({"normalizers": {"clang-format": "my-fmt {name}"}}, self.work)
        self.assertEqual(settings.normalizers["clang-format"], "my-fmt {name}")
        self.assertIn("strip-comments", settings.normalizers)

    def test_prompts_have_a_digest_for_provenance(self):
        one = load_settings({}, self.work)
        two = load_settings({"prompts": {"file_system": "different"}}, self.work)
        self.assertEqual(len(one.digest), 64)
        self.assertNotEqual(one.digest, two.digest)


# --- symbols -----------------------------------------------------------------


class Definitions(unittest.TestCase):
    """Conservative on purpose: over-reporting is worse than under-reporting."""

    def test_function_definition(self):
        added, removed = _definitions("@@\n+int sensor_alarm(int reading){\n")
        self.assertEqual(added, {"sensor_alarm"})
        self.assertEqual(removed, set())

    def test_removed_function_definition(self):
        added, removed = _definitions("@@\n-int legacy_calibrate(int raw){\n")
        self.assertEqual(removed, {"legacy_calibrate"})

    def test_define(self):
        added, _ = _definitions("@@\n+#define ALARM_LOW 2\n")
        self.assertEqual(added, {"ALARM_LOW"})

    def test_a_call_is_not_a_definition(self):
        added, _ = _definitions("@@\n+    sensor_alarm(reading);\n")
        self.assertEqual(added, set())

    def test_a_declaration_without_a_body_is_not_a_definition(self):
        added, _ = _definitions("@@\n+int sensor_alarm(int reading);\n")
        self.assertEqual(added, set())

    def test_control_flow_is_not_a_definition(self):
        for line in ("+if(reading > TEMP_MAX){", "+for(i = 0;", "+while(x){", "+switch(k){"):
            added, _ = _definitions("@@\n%s\n" % line)
            self.assertEqual(added, set(), line)

    def test_context_lines_are_neither(self):
        added, removed = _definitions("@@\n int sensor_alarm(int reading){\n")
        self.assertEqual((added, removed), (set(), set()))


class CrossReference(unittest.TestCase):
    def setUp(self):
        self.tree = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tree, True)

    def _write(self, name, text):
        path = os.path.join(self.tree, name)
        os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(name) else None
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_removed_symbol_still_referenced_is_dangling(self):
        self._write("caller.c", "int go(void){ return legacy_calibrate(3); }\n")
        sections = {"legacy.c": "@@\n-int legacy_calibrate(int raw){\n"}
        result = cross_reference(sections, self.tree)
        self.assertEqual(result.removed, {"legacy.c": ["legacy_calibrate"]})
        self.assertEqual(result.dangling, {"legacy_calibrate": ["caller.c"]})

    def test_removed_symbol_nobody_uses_is_not_dangling(self):
        self._write("caller.c", "int go(void){ return 0; }\n")
        result = cross_reference({"legacy.c": "@@\n-int legacy_calibrate(int raw){\n"}, self.tree)
        self.assertEqual(result.dangling, {})
        self.assertEqual(result.removed, {"legacy.c": ["legacy_calibrate"]})

    def test_a_move_is_not_a_removal(self):
        # Removed from one file and defined in another: still defined, not dangling.
        self._write("new.c", "int calib_apply(int r){ return r; }\n")
        sections = {
            "old.c": "@@\n-int calib_apply(int r){\n",
            "new.c": "@@\n+int calib_apply(int r){\n",
        }
        result = cross_reference(sections, self.tree)
        self.assertEqual(result.dangling, {})
        self.assertEqual(result.removed, {})

    def test_substring_is_not_a_reference(self):
        self._write("caller.c", "int x = legacy_calibrate_v2(3);\n")
        result = cross_reference({"legacy.c": "@@\n-int legacy_calibrate(int raw){\n"}, self.tree)
        self.assertEqual(result.dangling, {})

    def test_binary_files_are_not_searched(self):
        with open(os.path.join(self.tree, "blob.bin"), "wb") as handle:
            handle.write(b"\x00legacy_calibrate\x00")
        result = cross_reference({"legacy.c": "@@\n-int legacy_calibrate(int raw){\n"}, self.tree)
        self.assertEqual(result.dangling, {})

    def test_no_removals_means_no_work(self):
        result = cross_reference({"a.c": "@@\n+int added(void){\n"}, self.tree)
        self.assertEqual((result.removed, result.dangling), ({}, {}))


class CrossReferenceRendering(unittest.TestCase):
    """A dangling reference is a defect signal, so it must be impossible to miss."""

    def setUp(self):
        self.xref = CrossRef(
            removed={"legacy.c": ["legacy_calibrate"]},
            dangling={"legacy_calibrate": ["adc.c"]},
        )
        self.empty = CrossRef(removed={}, dangling={})

    def test_markdown_reports_a_dangling_symbol(self):
        md = build_report("v1", "v2", comparison(), {}, None, xref=self.xref)
        self.assertIn("legacy_calibrate", md)
        self.assertIn("adc.c", md)

    def test_markdown_says_nothing_when_there_is_nothing_to_say(self):
        md = build_report("v1", "v2", comparison(), {}, None, xref=self.empty)
        self.assertNotIn("still referenced", md.lower())

    def test_json_records_the_prompt_digest(self):
        settings = load_settings({}, ".")
        payload = build_json("v1", "v2", comparison(), {}, None, settings=settings)
        self.assertEqual(payload["inputs"]["prompts_sha256"], settings.digest)

    def test_html_reports_a_dangling_symbol(self):
        html = build_html("v1", "v2", comparison(), {}, None, xref=self.xref)
        self.assertIn("<code>legacy_calibrate</code>", html)
        self.assertIn('id="xref"', html)

    def test_html_omits_the_panel_when_empty(self):
        self.assertNotIn('id="xref"', build_html("v1", "v2", comparison(), {}, None, xref=self.empty))

    def test_json_carries_both_halves(self):
        payload = build_json("v1", "v2", comparison(), {}, None, xref=self.xref)
        self.assertEqual(payload["cross_reference"]["dangling"], {"legacy_calibrate": ["adc.c"]})
        self.assertEqual(payload["cross_reference"]["removed"], {"legacy.c": ["legacy_calibrate"]})

    def test_json_without_a_cross_reference_is_explicit(self):
        payload = build_json("v1", "v2", comparison(), {}, None)
        self.assertEqual(payload["cross_reference"], {"removed": {}, "dangling": {}})


# --- gitdiff -----------------------------------------------------------------


class PathHandling(unittest.TestCase):
    def test_unquote_c_style(self):
        self.assertEqual(_unquote(r'"C:\\Users\\x/f.c"'), r"C:\Users\x/f.c")

    def test_unquote_passthrough(self):
        self.assertEqual(_unquote("plain/path.c"), "plain/path.c")

    def test_relativize_strips_root(self):
        self.assertEqual(_relativize("/tmp/v1/src/f.c", ["/tmp/v1", "/tmp/v2"]), "src/f.c")

    def test_relativize_temp_copy_names(self):
        self.assertEqual(_relativize("A/sensor.c", ["A", "B"]), "sensor.c")

    def test_relativize_unknown_root_passthrough(self):
        self.assertEqual(_relativize("other/f.c", ["A", "B"]), "other/f.c")


class ParseNameStatus(unittest.TestCase):
    def test_modified_and_added(self):
        out = "M\tA/one.c\nA\tB/two.c\n"
        parsed = _parse_name_status(out, ["A", "B"])
        self.assertEqual(parsed["one.c"][0], "M")
        self.assertEqual(parsed["two.c"][0], "A")

    def test_rename_is_keyed_by_new_path(self):
        parsed = _parse_name_status("R100\tA/old.c\tB/new.c\n", ["A", "B"])
        self.assertIn("new.c", parsed)
        self.assertEqual(parsed["new.c"][0], "R")

    def test_quoted_windows_path_is_unquoted(self):
        # This is the shape git emits on Windows: C-quoted with escaped backslashes.
        out = 'M\t"C:\\\\tmp\\\\v1/sensor.c"\n'
        self.assertEqual(list(_parse_name_status(out, ["C:/tmp/v1"])), ["sensor.c"])

    def test_quoted_path_with_a_space(self):
        self.assertEqual(list(_parse_name_status('M\t"A/my file.c"\n', ["A", "B"])), ["my file.c"])

    def test_backslash_in_a_filename_becomes_a_separator(self):
        # Accepted limitation: separators are normalized to '/' so paths compare
        # consistently across platforms, so a POSIX file literally named 'od\d.c'
        # is reported as 'od/d.c'. Windows correctness is worth more than this case.
        self.assertEqual(list(_parse_name_status('M\t"A/od\\\\d.c"\n', ["A", "B"])), ["od/d.c"])

    def test_empty_output(self):
        self.assertEqual(_parse_name_status("", ["A", "B"]), {})


class ClassifyNoise(unittest.TestCase):
    """'Noise only' must mean 'differs before normalization but not after'."""

    def test_rename_old_path_is_not_noise(self):
        # Normalization raises similarity, so the pass can detect a rename the raw
        # pass saw as add+delete. The old path no longer exists in either form.
        raw = {"new.c": ("A", ["new.c"]), "old.c": ("D", ["old.c"])}
        normalized = {"new.c": ("R", ["old.c", "new.c"])}
        self.assertEqual(_noise(raw, normalized, set()), set())

    def test_genuine_noise_is_still_detected(self):
        self.assertEqual(_noise({"util.h": ("M", ["util.h"])}, {}, set()), {"util.h"})

    def test_skipped_is_never_noise(self):
        raw = {"build.log": ("M", ["build.log"])}
        self.assertEqual(_noise(raw, {}, {"build.log"}), set())


class SplitSections(unittest.TestCase):
    def test_keys_by_file_and_strips_copy_prefix(self):
        diff = (
            "diff --git a/A/one.c b/B/one.c\n"
            "--- a/A/one.c\n+++ b/B/one.c\n@@ -1 +1 @@\n-a\n+b\n"
            "diff --git a/A/two.c b/B/two.c\n"
            "--- a/A/two.c\n+++ b/B/two.c\n@@ -1 +1 @@\n-c\n+d\n"
        )
        sections = _split_sections(diff, "A", "B")
        self.assertEqual(sorted(sections), ["one.c", "two.c"])
        self.assertIn("+++ b/one.c", sections["one.c"])
        self.assertNotIn("A/", sections["one.c"])

    def test_deleted_file_uses_the_minus_line(self):
        diff = "diff --git a/A/gone.c b/B/gone.c\n--- a/A/gone.c\n+++ /dev/null\n@@ -1 +0 @@\n-a\n"
        self.assertEqual(list(_split_sections(diff, "A", "B")), ["gone.c"])

    def test_added_file_header_keeps_no_copy_prefix(self):
        # git names both sides under one directory for an add, so stripping only
        # 'a/<dir_a>/' and 'b/<dir_b>/' leaves the temp copy name in the header.
        diff = "diff --git a/B/new.c b/B/new.c\n--- /dev/null\n+++ b/B/new.c\n@@ -0,0 +1 @@\n+a\n"
        sections = _split_sections(diff, "A", "B")
        self.assertEqual(list(sections), ["new.c"])
        self.assertNotIn("a/B/", sections["new.c"])

    def test_deleted_file_header_keeps_no_copy_prefix(self):
        diff = "diff --git a/A/gone.c b/A/gone.c\n--- a/A/gone.c\n+++ /dev/null\n@@ -1 +0 @@\n-a\n"
        sections = _split_sections(diff, "A", "B")
        self.assertEqual(list(sections), ["gone.c"])
        self.assertNotIn("b/A/", sections["gone.c"])

    def test_path_like_content_is_never_rewritten(self):
        # An unanchored replace of 'a/A/' also edits the diff body, showing the
        # reviewer a path that appears in no file.
        diff = (
            "diff --git a/A/paths.c b/B/paths.c\n"
            "--- a/A/paths.c\n+++ b/B/paths.c\n@@ -1 +1 @@\n"
            '-const char *p = "data/A/tables.bin";\n'
            '+const char *p = "data/A/tables_v2.bin";\n'
        )
        body = _split_sections(diff, "A", "B")["paths.c"]
        self.assertIn('"data/A/tables.bin"', body)
        self.assertIn("--- a/paths.c", body)
        self.assertIn("+++ b/paths.c", body)

    def test_binary_notice_loses_the_copy_prefix(self):
        diff = (
            "diff --git a/A/img.bin b/B/img.bin\n"
            "Binary files a/A/img.bin and b/B/img.bin differ\n"
        )
        body = _split_sections(diff, "A", "B")["img.bin"]
        self.assertNotIn("a/A/", body)
        self.assertNotIn("b/B/", body)

    def test_empty_diff(self):
        self.assertEqual(_split_sections("", "A", "B"), {})


# --- llm ---------------------------------------------------------------------


class Manifest(unittest.TestCase):
    """Pass 0 sees an inventory of the whole change, never the code."""

    def test_lists_every_analysed_file_with_its_status(self):
        real = {"a.c": ("M", ["a.c"]), "b.c": ("A", ["b.c"])}
        text = _manifest(comparison(real=real), None, ["a.c", "b.c"])
        self.assertIn("a.c", text)
        self.assertIn("b.c", text)
        self.assertIn("modified", text)
        self.assertIn("added", text)

    def test_carries_the_counts(self):
        text = _manifest(comparison(noise={"u.h"}, skipped={"b.log"}), None, ["sensor.c"])
        self.assertIn("1 real", text)
        self.assertIn("1 noise", text)
        self.assertIn("1 skipped", text)

    def test_carries_the_dangling_findings(self):
        xref = CrossRef(removed={"legacy.c": ["legacy_calibrate"]},
                        dangling={"legacy_calibrate": ["adc.c"]})
        text = _manifest(comparison(), xref, ["sensor.c"])
        self.assertIn("legacy_calibrate", text)
        self.assertIn("adc.c", text)

    def test_never_contains_the_diffs(self):
        # The whole point of pass 0 is that it is cheap.
        text = _manifest(comparison(), None, ["sensor.c"])
        self.assertNotIn("```diff", text)
        self.assertNotIn("+y", text)


class AnalyzeChanges(unittest.TestCase):
    """The chat seam makes the whole analysis stage testable without a server."""

    def test_orientation_then_one_call_per_file_then_a_summary(self):
        chat = FakeChat()
        real = {"a.c": ("M", ["a.c"]), "b.c": ("M", ["b.c"])}
        result = analyze_changes(comparison(real=real), chat)
        self.assertEqual(sorted(result.analyses), ["a.c", "b.c"])
        self.assertIsNotNone(result.summary)
        self.assertIsNotNone(result.brief)
        self.assertEqual(chat.labels, ["orientation", "a.c", "b.c", "executive summary"])

    def test_the_brief_is_given_to_every_file_call(self):
        chat = FakeChat(reply="THE-BRIEF")
        real = {"a.c": ("M", ["a.c"]), "b.c": ("M", ["b.c"])}
        analyze_changes(comparison(real=real), chat)
        for call in chat.calls[1:-1]:
            self.assertIn("THE-BRIEF", call["user"])

    def test_a_single_file_skips_orientation(self):
        # One file has no "whole" to orient against; the call would be waste.
        chat = FakeChat()
        analyze_changes(comparison(), chat)
        self.assertEqual(chat.labels, ["sensor.c", "executive summary"])

    def test_a_failed_orientation_does_not_stop_the_run(self):
        chat = FakeChat(reply=lambda n: None if n == 1 else "**Risk** — Low, ok.")
        real = {"a.c": ("M", ["a.c"]), "b.c": ("M", ["b.c"])}
        result = analyze_changes(comparison(real=real), chat)
        self.assertIsNone(result.brief)
        self.assertEqual(sorted(result.analyses), ["a.c", "b.c"])

    def test_the_summary_sees_the_manifest_as_well_as_the_analyses(self):
        chat = FakeChat()
        real = {"a.c": ("M", ["a.c"]), "b.c": ("M", ["b.c"])}
        analyze_changes(comparison(real=real), chat)
        self.assertIn("2 real", chat.calls[-1]["user"])

    def test_binaries_are_never_sent(self):
        chat = FakeChat()
        real = {"a.c": ("M", ["a.c"]), "logo.png": ("M", ["logo.png"])}
        result = analyze_changes(comparison(real=real, binaries={"logo.png"}), chat)
        self.assertEqual(list(result.analyses), ["a.c"])
        self.assertNotIn("logo.png", chat.labels)

    def test_max_files_caps_the_work(self):
        chat = FakeChat()
        real = {"%d.c" % i: ("M", []) for i in range(5)}
        result = analyze_changes(comparison(real=real), chat, max_files=2)
        self.assertEqual(len(result.analyses), 2)

    def test_failed_calls_produce_no_summary(self):
        chat = FakeChat(reply=None)
        result = analyze_changes(comparison(), chat)
        self.assertEqual(result.analyses, {})
        self.assertIsNone(result.summary)
        self.assertNotIn("executive summary", chat.labels)

    def test_long_diff_is_split_into_parts(self):
        big = "diff --git a/f.c b/f.c\n--- a/f.c\n+++ b/f.c\n" + "".join(
            "@@ -%d +%d @@\n-%s\n+%s\n" % (i, i, "x" * 40, "y" * 40) for i in range(6)
        )
        chat = FakeChat()
        result = analyze_changes(
            comparison(real={"f.c": ("M", [])}, sections={"f.c": big}), chat, max_chars=200
        )
        part_labels = [label for label in chat.labels if "part" in label]
        self.assertGreater(len(part_labels), 1)
        self.assertIn("#### Part 1 of", result.analyses["f.c"])

    def test_missing_section_is_skipped(self):
        chat = FakeChat()
        result = analyze_changes(comparison(real={"a.c": ("M", [])}, sections={}), chat)
        self.assertEqual(result.analyses, {})

    def test_prompts_are_taken_from_settings(self):
        chat = FakeChat()
        prompts = load_settings({"prompts": {"file_system": "MY-OWN-PROMPT"}}, ".").prompts
        analyze_changes(comparison(), chat, prompts=prompts)
        self.assertEqual(chat.calls[0]["system"], "MY-OWN-PROMPT")


# --- output ------------------------------------------------------------------


class ExtractRisk(unittest.TestCase):
    """The level lives in free-form prose, so extraction is the fragile part."""

    def test_plain_level(self):
        self.assertEqual(_extract_risk("**Risk** — Medium, fine.", "Risk"), "medium")

    def test_allows_does_not_leak_low(self):
        # "allows" contains "low"; without word boundaries this returns None.
        self.assertEqual(_extract_risk("**Risk** — Medium, this allows overflow.", "Risk"), "medium")

    def test_below_does_not_leak_low(self):
        self.assertEqual(_extract_risk("**Risk** — High, see below.", "Risk"), "high")

    def test_template_echo_rejected(self):
        self.assertIsNone(_extract_risk("**Risk** — Low / Medium / High", "Risk"))

    def test_first_level_wins(self):
        self.assertEqual(_extract_risk("**Risk** — High, not low as first assessed.", "Risk"), "high")

    def test_most_severe_across_parts(self):
        text = "**Risk** — Low, minor.\n**Risk** — High, serious."
        self.assertEqual(_extract_risk(text, "Risk"), "high")

    def test_case_insensitive(self):
        self.assertEqual(_extract_risk("**risk** — HIGH, bad.", "Risk"), "high")

    def test_prose_mentioning_risk_does_not_escalate(self):
        # Ordinary review prose names a level without declaring one. Only a
        # labelled field counts, or "**Risk** — Low" plus one such sentence reads High.
        text = "**Risk** — Low, a rounding fix.\nReduces the risk of a high reading being missed."
        self.assertEqual(_extract_risk(text, "Risk"), "low")

    def test_prose_alone_yields_no_level(self):
        self.assertIsNone(_extract_risk("This lowers the risk of a high-severity fault.", "Risk"))

    def test_colon_separator_is_a_label(self):
        self.assertEqual(_extract_risk("**Risk**: High, overflow.", "Risk"), "high")

    def test_missing_label(self):
        self.assertIsNone(_extract_risk("Nothing relevant here.", "Risk"))

    def test_empty_text(self):
        self.assertIsNone(_extract_risk(None, "Risk"))
        self.assertIsNone(_extract_risk("", "Risk"))

    def test_overall_label(self):
        self.assertEqual(_extract_risk("**Overall risk** — Medium, ok.", "Overall risk"), "medium")


class BuildReport(unittest.TestCase):
    def test_renders_every_category(self):
        md = build_report(
            "v1",
            "v2",
            comparison(noise={"util.h"}, skipped={"build.log"}),
            {"sensor.c": "the analysis"},
            "the summary",
        )
        self.assertIn("| Real changes | 1 |", md)
        self.assertIn("| `sensor.c` | modified |", md)
        self.assertIn("| `util.h` | noise only |", md)
        self.assertIn("| `build.log` | skipped |", md)
        self.assertIn("## Executive summary", md)
        self.assertIn("the analysis", md)
        self.assertIn("<details><summary>Show diff</summary>", md)

    def test_the_orientation_brief_is_rendered(self):
        md = build_report("v1", "v2", comparison(), {}, None, brief="what this change is about")
        self.assertIn("what this change is about", md)

    def test_a_configured_risk_label_is_extracted(self):
        risk = load_settings({"risk": {"file_label": "Severity"}}, ".").risk
        md = build_report("v1", "v2", comparison(), {"sensor.c": "**Severity** — High, bad."},
                          None, risk=risk)
        self.assertIn("High, bad.", md)

    def test_no_summary_section_without_llm(self):
        self.assertNotIn("## Executive summary", build_report("v1", "v2", comparison(), {}, None))

    def test_binary_gets_a_note_instead_of_a_diff(self):
        md = build_report("v1", "v2", comparison(binaries={"sensor.c"}), {}, None)
        self.assertIn("Binary file — not analyzed", md)
        self.assertNotIn("Show diff", md)

    def test_no_differences_at_all(self):
        md = build_report("v1", "v2", comparison(real={}, sections={}), {}, None)
        self.assertIn("no differences", md)
        self.assertIn("No substantive changes remain", md)


class BuildJson(unittest.TestCase):
    def test_shape_with_analysis(self):
        payload = build_json(
            "v1",
            "v2",
            comparison(noise={"util.h"}, skipped={"build.log"}),
            {"sensor.c": "**Risk** — High, serious."},
            "**Overall risk** — Medium, ok.",
        )
        self.assertEqual(payload["counts"], {"real": 1, "noise": 1, "skipped": 1})
        self.assertTrue(payload["llm_ran"])
        self.assertEqual(payload["real"][0]["risk"], "high")
        self.assertEqual(payload["summary"]["risk"], "medium")
        self.assertEqual(payload["noise"], ["util.h"])
        self.assertEqual(payload["skipped"], ["build.log"])

    def test_nulls_without_llm(self):
        payload = build_json("v1", "v2", comparison(), {}, None)
        self.assertFalse(payload["llm_ran"])
        self.assertIsNone(payload["real"][0]["analysis"])
        self.assertIsNone(payload["real"][0]["risk"])
        self.assertIsNone(payload["summary"]["risk"])
        self.assertIn("diff --git", payload["real"][0]["diff"])

    def test_binary_flag(self):
        payload = build_json("v1", "v2", comparison(binaries={"sensor.c"}), {}, None)
        self.assertTrue(payload["real"][0]["binary"])


class SideBySide(unittest.TestCase):
    """Aligning a unified diff into two panes, the way a compare tool shows it."""

    def test_context_line_appears_on_both_sides(self):
        rows = _side_by_side("@@ -10,3 +10,3 @@\n int a;\n-int b;\n+int c;\n")
        self.assertEqual(rows[0].kind, "ctx")
        self.assertEqual((rows[0].left_no, rows[0].right_no), (10, 10))
        self.assertEqual(rows[0].left, rows[0].right)

    def test_edited_line_pairs_old_and_new_on_one_row(self):
        rows = _side_by_side("@@ -1 +1 @@\n-int b;\n+int c;\n")
        self.assertEqual([row.kind for row in rows], ["chg"])
        self.assertEqual((rows[0].left, rows[0].right), ("int b;", "int c;"))

    def test_added_line_has_an_empty_left_side(self):
        rows = _side_by_side("@@ -1,0 +1,1 @@\n+int c;\n")
        self.assertEqual(rows[0].kind, "add")
        self.assertIsNone(rows[0].left_no)
        self.assertEqual(rows[0].right, "int c;")

    def test_deleted_line_has_an_empty_right_side(self):
        rows = _side_by_side("@@ -1,1 +1,0 @@\n-int b;\n")
        self.assertEqual(rows[0].kind, "del")
        self.assertIsNone(rows[0].right_no)
        self.assertEqual(rows[0].left, "int b;")

    def test_uneven_runs_pair_then_spill(self):
        rows = _side_by_side("@@ -1,1 +1,3 @@\n-a\n+x\n+y\n+z\n")
        self.assertEqual([row.kind for row in rows], ["chg", "add", "add"])

    def test_line_numbers_advance_from_the_hunk_header(self):
        rows = _side_by_side("@@ -5,3 +9,3 @@\n a\n b\n c\n")
        self.assertEqual([row.left_no for row in rows], [5, 6, 7])
        self.assertEqual([row.right_no for row in rows], [9, 10, 11])

    def test_file_header_lines_are_not_rows(self):
        section = (
            "diff --git a/x.c b/x.c\nindex 1..2 100644\n--- a/x.c\n+++ b/x.c\n"
            "@@ -1 +1 @@\n-a\n+b\n"
        )
        self.assertEqual([row.kind for row in _side_by_side(section)], ["chg"])

    def test_second_hunk_is_separated_by_a_gap(self):
        section = "@@ -1 +1 @@\n-a\n+b\n@@ -9 +9 @@\n-c\n+d\n"
        self.assertEqual([row.kind for row in _side_by_side(section)], ["chg", "gap", "chg"])

    def test_new_file_hunk_starting_at_zero_still_yields_rows(self):
        rows = _side_by_side("@@ -0,0 +1,2 @@\n+a\n+b\n")
        self.assertEqual([row.kind for row in rows], ["add", "add"])
        self.assertEqual([row.right_no for row in rows], [1, 2])


class MarkPair(unittest.TestCase):
    def test_similar_lines_mark_only_what_differs(self):
        left, right = _mark_pair("int a = 1;", "int a = 2;")
        self.assertIn(">1</mark>", left)
        self.assertIn(">2</mark>", right)
        self.assertTrue(left.startswith("int a = "))

    def test_dissimilar_lines_are_not_marked(self):
        # Marking every character of an unrelated pair is noise, not information.
        left, right = _mark_pair("if (a<b) {}", "<script>alert(1)</script>")
        self.assertNotIn("<mark", left + right)
        self.assertIn("&lt;script&gt;", right)
        self.assertIn("a&lt;b", left)


class BuildHtml(unittest.TestCase):
    """The HTML report must be a complete, self-contained, offline-safe document."""

    def test_is_a_complete_document(self):
        html = build_html("v1", "v2", comparison(), {}, None)
        self.assertTrue(html.lstrip().lower().startswith("<!doctype html>"))
        self.assertIn("</html>", html)

    def test_no_external_references(self):
        # The target is an air-gapped box: a report that fetches anything is broken.
        html = build_html("v1", "v2", comparison(), {"sensor.c": "text"}, "summary")
        for forbidden in ("http://", "https://", "<link", "<img", "src=", "@import"):
            self.assertNotIn(forbidden, html)

    def test_counts_are_tagged_for_each_category(self):
        html = build_html(
            "v1", "v2", comparison(noise={"util.h"}, skipped={"build.log"}), {}, None
        )
        self.assertIn('data-kind="real" data-count="1"', html)
        self.assertIn('data-kind="noise" data-count="1"', html)
        self.assertIn('data-kind="skipped" data-count="1"', html)

    def test_every_category_appears_in_the_file_table(self):
        html = build_html(
            "v1", "v2", comparison(noise={"util.h"}, skipped={"build.log"}), {}, None
        )
        self.assertIn('data-status="modified"', html)
        self.assertIn('data-status="noise only"', html)
        self.assertIn('data-status="skipped"', html)
        self.assertIn("<code>util.h</code>", html)

    def test_summary_section_only_when_there_is_a_summary(self):
        self.assertNotIn('id="summary"', build_html("v1", "v2", comparison(), {}, None))
        self.assertIn('id="summary"', build_html("v1", "v2", comparison(), {}, "the summary"))

    def test_risk_badge_comes_from_the_analysis_text(self):
        html = build_html("v1", "v2", comparison(), {"sensor.c": "**Risk** — High, serious."}, None)
        self.assertIn('class="badge risk-high"', html)

    def test_no_risk_badge_without_an_extractable_level(self):
        html = build_html("v1", "v2", comparison(), {"sensor.c": "no level stated"}, None)
        self.assertNotIn("badge risk-", html)

    def test_diff_is_rendered_as_two_aligned_panes(self):
        html = build_html("v1", "v2", comparison(), {}, None)
        self.assertIn('class="sbs"', html)
        self.assertIn('class="s-chg"', html)

    def test_hovering_a_row_highlights_both_sides(self):
        # Corresponding lines share a <tr>, so the pairing is structural and one
        # CSS rule lights both panes — no script, which the no-JS report needs.
        html = build_html("v1", "v2", comparison(), {}, None)
        self.assertIn("tbody tr:not(.s-gap):hover td", html)
        self.assertIn("--hover:", html)

    def test_panes_are_labelled_with_the_directories(self):
        html = build_html("src/v1", "src/v2", comparison(), {}, None)
        self.assertIn('<th colspan="2">src/v1</th>', html)
        self.assertIn('<th colspan="2">src/v2</th>', html)

    def test_rename_with_no_content_change_explains_itself(self):
        # git emits no @@ at all for a pure rename, so there are no rows to align.
        # An empty disclosure would drop the only fact that matters about it.
        section = (
            "diff --git a/old.c b/new.c\nsimilarity index 100%\n"
            "rename from old.c\nrename to new.c\n"
        )
        real = {"new.c": ("R", ["old.c", "new.c"])}
        html = build_html("v1", "v2", comparison(real=real, sections={"new.c": section}), {}, None)
        self.assertIn("<code>old.c</code>", html)
        self.assertIn("no content change", html)
        self.assertNotIn('<div class="scroll"></div>', html)

    def test_comparison_table_declares_its_column_widths(self):
        # table-layout:fixed takes widths from the first row, whose cells span two
        # columns each, so the gutter width has to come from a colgroup.
        self.assertIn("<colgroup>", build_html("v1", "v2", comparison(), {}, None))

    def test_prose_headings_are_rendered(self):
        # llm.py emits "#### Part 1 of 2" itself when it splits a long diff.
        html = build_html("v1", "v2", comparison(), {"sensor.c": "#### Part 1 of 2\n\nbody"}, None)
        self.assertIn("<h4>Part 1 of 2</h4>", html)
        self.assertNotIn("#### Part", html)

    def test_binary_gets_a_note_instead_of_a_diff(self):
        html = build_html("v1", "v2", comparison(binaries={"sensor.c"}), {}, None)
        self.assertIn("Binary file", html)
        self.assertNotIn('class="sbs"', html)

    def test_no_differences_at_all(self):
        html = build_html("v1", "v2", comparison(real={}, sections={}), {}, None)
        self.assertIn("No substantive changes remain", html)

    def test_sticky_headers_and_print_styles_are_declared(self):
        html = build_html("v1", "v2", comparison(), {}, None)
        self.assertIn("position:sticky;top:0", html)
        self.assertIn("@media print", html)

    def test_summary_file_rows_have_anchor_links_to_change_cards(self):
        html = build_html("v1", "v2", comparison(real={"sensor.c": ("M", ["sensor.c"])}), {}, None)
        self.assertIn('<a href="#file-sensor.c"><code>sensor.c</code></a>', html)
        self.assertIn('<article class="change" id="file-sensor.c">', html)

    def test_type_changed_status_chip_is_styled(self):
        html = build_html("v1", "v2", comparison(real={"device.so": ("T", ["device.so"])}), {}, None)
        self.assertIn('tr[data-status="type changed"] .chip', html)

    def test_markup_in_a_diff_is_escaped(self):
        # A diff legitimately contains C like `a <b`, and could contain HTML.
        evil = "diff --git a/x.c b/x.c\n@@ -1 +1 @@\n-if (a<b) {}\n+<script>alert(1)</script>\n"
        html = build_html("v1", "v2", comparison(sections={"sensor.c": evil}), {}, None)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("a&lt;b", html)

    def test_markup_in_a_filename_is_escaped(self):
        real = {"<img>.c": ("M", ["<img>.c"])}
        html = build_html("v1", "v2", comparison(real=real, sections={}), {}, None)
        self.assertIn("&lt;img&gt;.c", html)

    def test_analysis_prose_is_rendered(self):
        analysis = "**Summary** — it changed.\n\n**Changes**\n- first thing\n- second thing"
        html = build_html("v1", "v2", comparison(), {"sensor.c": analysis}, None)
        self.assertIn("<strong>Summary</strong>", html)
        self.assertIn("<li>first thing</li>", html)
        self.assertIn("<li>second thing</li>", html)

    def test_identifiers_with_underscores_are_not_italicised(self):
        # `SAMPLE_COUNT ... SAMPLE_COUNT` must not be read as an italic span.
        analysis = "Rounds SAMPLE_COUNT against SAMPLE_COUNT now."
        html = build_html("v1", "v2", comparison(), {"sensor.c": analysis}, None)
        self.assertIn("SAMPLE_COUNT against SAMPLE_COUNT", html)
        self.assertNotIn("<em>", html)

    def test_directories_are_reported(self):
        html = build_html("src/v1", "src/v2", comparison(), {}, None)
        self.assertIn("src/v1", html)
        self.assertIn("src/v2", html)


# --- gitrepo -----------------------------------------------------------------
# No git is run here: the parsing of what git prints is the part with edge cases,
# and it is a pure function of a string.


def _record(sha, author, date, subject, body):
    return "\x1f".join([sha, author, date, subject, body]) + "\x1e"


class ParseLog(unittest.TestCase):
    def test_single_commit(self):
        text = _record("abc123", "Ada", "2026-01-02T03:04:05+00:00", "Fix the thing", "Why.\n")
        items = _parse_log(text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].sha, "abc123")
        self.assertEqual(items[0].author, "Ada")
        self.assertEqual(items[0].subject, "Fix the thing")
        self.assertEqual(items[0].body, "Why.")

    def test_body_spanning_blank_lines_stays_one_commit(self):
        # A line-oriented parse would split this into several commits.
        body = "First paragraph.\n\nSecond paragraph.\n\nSigned-off-by: Ada <a@b>\n"
        items = _parse_log(_record("abc", "Ada", "d", "Subject", body))
        self.assertEqual(len(items), 1)
        self.assertIn("Second paragraph.", items[0].body)
        self.assertIn("Signed-off-by", items[0].body)

    def test_several_commits_keep_their_order(self):
        text = (
            _record("aaa", "Ada", "d", "First", "")
            + _record("bbb", "Bo", "d", "Second", "body")
        )
        self.assertEqual([item.sha for item in _parse_log(text)], ["aaa", "bbb"])

    def test_empty_and_malformed_records_are_dropped(self):
        text = "\x1e\x1e" + "not\x1fenough\x1ffields\x1e" + _record("ccc", "C", "d", "S", "")
        self.assertEqual([item.sha for item in _parse_log(text)], ["ccc"])

    def test_subject_with_a_colon_and_unicode(self):
        items = _parse_log(_record("d1", "Ægir", "d", "fs/ext2: hä", "ünicode body"))
        self.assertEqual(items[0].subject, "fs/ext2: hä")
        self.assertEqual(items[0].author, "Ægir")


class IntentText(unittest.TestCase):
    def test_no_commits_is_empty(self):
        self.assertEqual(intent_text([]), "")

    def test_subject_and_body_both_appear(self):
        item = Commit("abcdef1234", "Ada", "d", "Bound the parser", "It could overflow.")
        text = intent_text([item])
        self.assertIn("abcdef123", text)
        self.assertIn("Bound the parser", text)
        self.assertIn("It could overflow.", text)
        self.assertIn("Ada", text)


class IsWithin(unittest.TestCase):
    """The export unpacks an archive built from a repository the reviewer did not write."""

    def test_child_is_within(self):
        self.assertTrue(_is_within(os.sep + "base", os.path.join(os.sep + "base", "a", "b.c")))

    def test_escape_is_rejected(self):
        self.assertFalse(_is_within(os.sep + "base", os.path.join(os.sep + "base", "..", "x")))

    def test_sibling_prefix_is_rejected(self):
        # "/base-other" starts with "/base" as a string but is not inside it.
        self.assertFalse(_is_within(os.sep + "base", os.sep + "base-other"))


class IntentInAnalysis(unittest.TestCase):
    """Independence is the default; the author's account travels only when asked."""

    def test_intent_is_absent_unless_supplied(self):
        chat = FakeChat()
        analyze_changes(comparison(), chat, xref=None)
        self.assertNotIn("claim to verify", chat.calls[0]["user"])

    def test_intent_reaches_the_per_file_call(self):
        chat = FakeChat()
        analyze_changes(comparison(), chat, xref=None, intent="Bound the parser.")
        self.assertIn("Bound the parser.", chat.calls[0]["user"])
        self.assertIn("claim to verify", chat.calls[0]["user"])


class CommitsInOutput(unittest.TestCase):
    def setUp(self):
        self.commits = [Commit("abcdef1234", "Ada", "d", "Bound the parser", "It could overflow.")]

    def test_report_records_the_stated_intent(self):
        text = build_report("v1", "v2", comparison(), {}, None, commits=self.commits)
        self.assertIn("Stated intent", text)
        self.assertIn("Bound the parser", text)
        self.assertIn("It could overflow.", text)

    def test_report_says_whether_the_model_saw_it(self):
        withheld = build_report("v1", "v2", comparison(), {}, None, commits=self.commits)
        self.assertIn("*not* shown to the model", withheld)
        shown = build_report(
            "v1", "v2", comparison(), {}, None, commits=self.commits, intent_shown=True
        )
        self.assertIn("**was** supplied to the model", shown)
        self.assertNotIn("*not* shown to the model", shown)

    def test_no_intent_section_without_commits(self):
        self.assertNotIn("Stated intent", build_report("v1", "v2", comparison(), {}, None))

    def test_html_carries_the_same_distinction(self):
        html = build_html("v1", "v2", comparison(), {}, None, commits=self.commits)
        self.assertIn("Stated intent", html)
        self.assertIn("Bound the parser", html)

    def test_json_records_commits_and_whether_they_were_shown(self):
        payload = build_json(
            "v1", "v2", comparison(), {}, None, commits=self.commits, intent_shown=True
        )
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["commits"][0]["sha"], "abcdef1234")
        self.assertTrue(payload["inputs"]["intent_shown"])

    def test_json_defaults_to_independent(self):
        payload = build_json("v1", "v2", comparison(), {}, None)
        self.assertEqual(payload["commits"], [])
        self.assertFalse(payload["inputs"]["intent_shown"])


# --- cli ---------------------------------------------------------------------
# The only module the suite reaches through its real entry point. main(argv) was
# always written to take argv for this; nothing here starts a subprocess, so the
# no-git, no-network rule still holds.


class CliArgs(unittest.TestCase):
    """Every way the command line can be wrong, and the exit code it earns."""

    def setUp(self):
        self.work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.config = os.path.join(self.work, "config.json")
        self._config({"llm": {}, "rules": []})

    def _config(self, data):
        with open(self.config, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def _main(self, *argv):
        """main() with BOTH streams captured: _configure_streams touches both."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue() + err.getvalue()

    def test_an_unreadable_config_names_the_file(self):
        code, text = self._main("a", "b", "-c", os.path.join(self.work, "nope.json"))
        self.assertEqual(code, 2)
        self.assertIn("nope.json", text)

    def test_a_config_that_is_not_json_is_an_error_not_a_traceback(self):
        with open(self.config, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        code, text = self._main("a", "b", "-c", self.config)
        self.assertEqual(code, 2)
        self.assertIn("cannot read config file", text)

    def test_a_bad_prompt_placeholder_is_reported_before_anything_runs(self):
        self._config({"prompts": {"file_user": "{diff} {nonsense}"}})
        code, text = self._main("a", "b", "-c", self.config)
        self.assertEqual(code, 2)
        self.assertIn("{nonsense}", text)

    def test_no_input_at_all_says_what_the_two_shapes_are(self):
        code, text = self._main("-c", self.config)
        self.assertEqual(code, 2)
        self.assertIn("two directories", text)
        self.assertIn("--git", text)

    def test_a_missing_directory_is_named(self):
        code, text = self._main(self.work, os.path.join(self.work, "gone"), "-c", self.config)
        self.assertEqual(code, 2)
        self.assertIn("gone", text)

    def test_git_needs_an_existing_repository_directory(self):
        code, text = self._main("--git", os.path.join(self.work, "gone"), "--commit", "HEAD",
                                "-c", self.config)
        self.assertEqual(code, 2)
        self.assertIn("not an existing directory", text)

    def test_commit_cannot_be_combined_with_from(self):
        code, text = self._main("--git", self.work, "--commit", "HEAD", "--from", "v1",
                                "-c", self.config)
        self.assertEqual(code, 2)
        self.assertIn("--commit cannot be combined", text)

    def test_git_without_a_revision_says_which_flags_to_use(self):
        code, text = self._main("--git", self.work, "-c", self.config)
        self.assertEqual(code, 2)
        self.assertIn("--commit", text)
        self.assertIn("--from", text)


if __name__ == "__main__":
    unittest.main()

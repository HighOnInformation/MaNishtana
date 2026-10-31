"""The filter stage: decide what to ignore, and rewrite files so it disappears.

Public interface:
    IGNORED_TOKEN                       the marker left behind by ignore_lines
    BINARY_STEP                         the built-in normalize step for binaries
    binary_ignore_strings(rules, rel)   the ignore_strings a binary's rule declares
    is_binary(path) -> bool
    skipped_files(rules, paths) -> set  paths matched by a skip rule
    prepare_copy(src, dst, rules)       copy a tree and apply the rules to the copy

A rule is a plain dict from the config file:

    {"match": [...], "skip": bool, "normalize": [...], "ignore_lines": [...],
     "ignore_strings": [...]}

ignore_strings is for binaries and takes effect with the BINARY_STEP step.

How normalizers are spawned, how missing binaries are tolerated, and how lines are
neutralised are all internal. Callers only ever ask for a prepared copy.
"""

import fnmatch
import os
import re
import shlex
import shutil
import subprocess
import sys

from .binnoise import normalize as _normalize_binary
from .common import warn

IGNORED_TOKEN = "<<ignored-by-rule>>"

# Not a command line: it runs in-process (binnoise), so it is not one of the
# configurable normalizer aliases and cannot be overridden by one.
BINARY_STEP = "binary-stamps"

_JSON_SORT_CODE = (
    "import json,sys;"
    "sys.stdout.write(json.dumps(json.load(sys.stdin),sort_keys=True,"
    "indent=2,ensure_ascii=False))"
)

_ALIASES = {
    "clang-format": "clang-format -style=LLVM -assume-filename={name}",
    "strip-comments": "gcc -fpreprocessed -dD -E -P -x c -",
    "json-sort": '"%s" -c "%s"' % (sys.executable, _JSON_SORT_CODE),
}

_missing_tools = set()


def is_binary(path):
    """Treat a NUL byte in the first 8KB as the binary marker, as git does."""
    try:
        with open(path, "rb") as handle:
            return b"\x00" in handle.read(8192)
    except OSError:
        return False


def binary_ignore_strings(rules, rel):
    """The ignore_strings that apply to a binary at rel ([] when none do).

    Only a rule that lists BINARY_STEP rewrites binaries, so only its
    ignore_strings count; that is the one place they are documented to work.
    """
    rule = _matching_rule(rules, rel)
    if not rule or BINARY_STEP not in (rule.get("normalize") or []):
        return []
    return list(rule.get("ignore_strings") or [])


def skipped_files(rules, paths):
    """Return the subset of paths whose matching rule says skip."""
    result = set()
    for rel in paths:
        rule = _matching_rule(rules, rel)
        if rule and rule.get("skip"):
            result.add(rel)
    return result


def prepare_copy(src, dst, rules, settings=None):
    """Copy a directory to dst and apply the rules to the copy.

    Skipped files are deleted, and everything else is normalized then
    line-filtered. Binaries are left untouched unless their rule lists
    BINARY_STEP, in which case their build-noise fields are zeroed so that a
    rebuild of unchanged code drops out of the second diff as noise. Failures
    degrade: a missing normalizer or a failing command warns and leaves the text
    as it was.

    `settings` supplies the normalizer command lines and the tuning values; the
    built-in defaults are used when it is absent.
    """
    aliases, timeout, token = _from_settings(settings)
    shutil.copytree(src, dst)
    for root, _dirs, files in os.walk(dst):
        for name in files:
            path = os.path.join(root, name)
            rel = os.path.relpath(path, dst).replace("\\", "/")
            rule = _matching_rule(rules, rel)
            if rule is None:
                continue
            if rule.get("skip"):
                os.remove(path)
                continue
            steps = rule.get("normalize") or []
            if is_binary(path):
                if BINARY_STEP in steps:
                    _rewrite_binary(path, rel, rule.get("ignore_strings") or [])
                continue
            steps = [step for step in steps if step != BINARY_STEP]
            patterns = rule.get("ignore_lines") or []
            if not steps and not patterns:
                continue
            try:
                with open(path, "r", encoding="utf-8", newline="") as handle:
                    text = handle.read()
            except UnicodeDecodeError as exc:
                # Folding undecodable bytes to U+FFFD would make two files that
                # differ only in those bytes identical after normalization — the
                # exact definition of "noise". Leave the copy alone instead, so a
                # real change stays a real change.
                warn("%s is not valid UTF-8 (%s) — comparing it un-normalized." % (rel, exc))
                continue
            except OSError as exc:
                warn("cannot read %s: %s" % (rel, exc))
                continue
            for step in steps:
                text = _normalize_text(step, text, os.path.basename(rel), rel, aliases, timeout)
            text = _apply_ignore_lines(text, patterns, token)
            try:
                with open(path, "w", encoding="utf-8", newline="") as handle:
                    handle.write(text)
            except OSError as exc:
                # copytree preserves the read-only bit, so a vendor drop off an
                # ISO lands read-only. That must not take the whole run down.
                warn("cannot rewrite %s: %s — comparing it un-normalized." % (rel, exc))


# --- internals ---------------------------------------------------------------


def _rewrite_binary(path, rel, ignore_strings=()):
    """Zero a binary copy's build-noise fields and any ignore_strings matches.

    An unknown format gets no field zeroing, but the user's ignore_strings still
    apply to it: they were declared for exactly these files.
    """
    try:
        with open(path, "rb") as handle:
            data = handle.read()
        cleaned, _fmt = _normalize_binary(data, ignore_strings)
        if cleaned == data:
            return
        with open(path, "wb") as handle:
            handle.write(cleaned)
    except OSError as exc:
        warn("cannot rewrite binary %s: %s — comparing it un-normalized." % (rel, exc))


def _from_settings(settings):
    """(aliases, timeout, token), falling back to the built-in defaults."""
    if settings is None:
        return _ALIASES, 60, IGNORED_TOKEN
    return (
        settings.normalizers,
        settings.tuning["normalize_timeout_sec"],
        settings.tuning["ignored_token"],
    )


def _matching_rule(rules, rel):
    """First rule with a pattern matching the relative path or the basename wins.

    There is no negation: order the list from specific to general.
    """
    base = os.path.basename(rel)
    for rule in rules:
        for pattern in rule.get("match", []):
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(base, pattern):
                return rule
    return None


def _program_of(command):
    """Extract the executable name from a shell command string."""
    command = command.strip()
    if command.startswith('"'):
        end = command.find('"', 1)
        if end > 0:
            return command[1:end]
    parts = command.split()
    return parts[0] if parts else ""


def _quote_name(name):
    """Quote a filename before it is spliced into a shell command line.

    The name comes from the tree being compared, which for this tool is source
    from somewhere else — so it is untrusted input reaching a shell. cmd.exe does
    not honour metacharacters inside double quotes; POSIX shells need shlex.
    Without this, a file called 'x&mkdir OWNED&.c' runs mkdir during a comparison,
    and the benign 'my file.c' arrives at the normalizer as two arguments.
    """
    if os.name == "nt":
        return '"%s"' % name.replace('"', "")
    return shlex.quote(name)


def _command_for(step, name, aliases=None):
    """Resolve an alias and substitute {name}, quoted. The rest is run as written."""
    table = _ALIASES if aliases is None else aliases
    return table.get(step, step).replace("{name}", _quote_name(name))


def _normalize_text(step, text, name, rel, aliases=None, timeout=60):
    """Run one normalization step (stdin to stdout). On any failure, return the input.

    A missing binary is warned about once per tool, then skipped for the rest of
    the run — an air-gapped box without clang-format should still get a report.
    """
    command = _command_for(step, name, aliases)
    program = _program_of(command)
    if program in _missing_tools:
        return text
    if shutil.which(program) is None:
        _missing_tools.add(program)
        warn(
            "tool '%s' is not installed — normalize step '%s' skipped, "
            "comparison continues without it." % (program, step)
        )
        return text
    try:
        proc = subprocess.run(
            command,
            shell=True,
            input=text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        warn(
            "normalize step '%s' exceeded %ds for %s — keeping the original text."
            % (step, timeout, rel)
        )
        return text
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().split("\n")[0]
        warn(
            "normalize step '%s' failed for %s (exit %d) — keeping the original text. %s"
            % (step, rel, proc.returncode, detail)
        )
        return text
    return proc.stdout


def _apply_ignore_lines(text, patterns, token=IGNORED_TOKEN):
    """Replace every matching line with a fixed token, preserving the line count.

    Keeping the count stable means diff line numbers do not shift, and applying
    the same token on both sides is what makes per-file-type ignoring work inside
    a single git diff call — something git's global -I flag cannot do.
    """
    if not patterns:
        return text
    regexes = [re.compile(p) for p in patterns]
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if any(regex.search(line) for regex in regexes):
            lines[index] = token
    return "\n".join(lines)

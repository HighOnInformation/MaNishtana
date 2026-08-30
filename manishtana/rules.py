"""The filter stage: decide what to ignore, and rewrite files so it disappears.

Public interface:
    IGNORED_TOKEN                       the marker left behind by ignore_lines
    is_binary(path) -> bool
    skipped_files(rules, paths) -> set  paths matched by a skip rule
    prepare_copy(src, dst, rules)       copy a tree and apply the rules to the copy

A rule is a plain dict from the config file:

    {"match": [...], "skip": bool, "normalize": [...], "ignore_lines": [...]}

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

from .common import warn

IGNORED_TOKEN = "<<ignored-by-rule>>"

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

    Skipped files are deleted, binaries are left untouched, and everything else is
    normalized then line-filtered. Failures degrade: a missing normalizer or a
    failing command warns and leaves the text as it was.

    `settings` supplies the normalizer command lines and the tuning values; the
    built-in defaults are used when it is absent.
    """
    aliases, timeout, token = _from_settings(settings)
    # symlinks=True copies links as links. The default dereferences them, pulling
    # whatever they point at — outside the tree included — into the temporary
    # copy, which then gets compared as though it belonged here.
    shutil.copytree(src, dst, symlinks=True)
    for root, _dirs, files in os.walk(dst):
        for name in files:
            path = os.path.join(root, name)
            rel = os.path.relpath(path, dst).replace("\\", "/")
            # Reading or rewriting a link acts on its target, which the copy
            # above deliberately did not bring across.
            if os.path.islink(path):
                continue
            rule = _matching_rule(rules, rel)
            if rule is None:
                continue
            if rule.get("skip"):
                os.remove(path)
                continue
            if is_binary(path):
                continue
            steps = rule.get("normalize") or []
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


def _split_command(command):
    """Split a configured command line into argv, for running without a shell.

    shlex gets Windows wrong in both directions: posix=True strips the
    backslashes out of C:\Python\python.exe, and posix=False leaves the quotes
    inside the token so the executable name becomes literally '"C:/py.exe"'.
    Split without posix rules to keep the backslashes, then drop the quote pair
    that leaves behind. The default json-sort normalizer is exactly this shape —
    sys.executable, quoted, containing backslashes — so neither mode works alone.
    """
    if os.name == "nt":
        parts = shlex.split(command, posix=False)
        return [
            part[1:-1] if len(part) >= 2 and part[0] == '"' and part[-1] == '"' else part
            for part in parts
        ]
    return shlex.split(command)


def _program_of(argv):
    """The executable a resolved command will run."""
    return argv[0] if argv else ""


def _command_for(step, name, aliases=None):
    """Resolve an alias to argv, substituting {name} into the argument itself.

    The filename reaches the normalizer as its own argv element and never passes
    through a shell. It comes from the tree being compared — source from
    somewhere else, so it is untrusted — and spliced into a shell string, a file
    called 'x&mkdir OWNED&.c' runs mkdir during a comparison. Quoting that safely
    on cmd.exe is not actually possible: %VAR% still expands inside double
    quotes. Removing the shell removes the question.
    """
    table = _ALIASES if aliases is None else aliases
    template = table.get(step, step)
    return [part.replace("{name}", name) for part in _split_command(template)]


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
        # Splitting on \n leaves the CR of a CRLF file attached to the line.
        # Replacing the whole line with a bare token converts just those lines to
        # LF, and the mixed endings then surface as changes in the very diff this
        # exists to quieten. Match without it, put it back after.
        ending = "\r" if line.endswith("\r") else ""
        if any(regex.search(line[: len(line) - len(ending)]) for regex in regexes):
            lines[index] = token + ending
    return "\n".join(lines)

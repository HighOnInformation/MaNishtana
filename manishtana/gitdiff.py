"""Answers one question: what changed between two directories?

Public interface:
    changed_files(dir_a, dir_b, cwd=None) -> {relative path: (status letter, [paths])}
    diff_sections(dir_a, dir_b, cwd=None) -> {relative path: unified diff text}

Both accept the two directories and return plain relative paths. Everything about
how git invokes, quotes, prefixes and formats paths is hidden here — callers never
see a `a/`, a `"C:\\\\quoted\\\\path"`, or a temp-directory prefix.

Note: git exits 1 to mean "differences found", which is the normal case. Only other
non-zero codes are reported as problems.
"""

import os
import re
import subprocess

from .common import warn

_GIT = ["git", "-c", "core.quotepath=false", "-c", "core.autocrlf=false"]


def changed_files(dir_a, dir_b, cwd=None):
    """Return {relative path: (status letter, [paths])} for every differing file."""
    out = _git(["--name-status", _posix(dir_a), _posix(dir_b)], cwd, "git diff --name-status")
    return _parse_name_status(out, [dir_a, dir_b])


def diff_sections(dir_a, dir_b, cwd=None):
    """Return {relative path: unified diff text}, one entry per differing file."""
    text = _git(["--unified=3", _posix(dir_a), _posix(dir_b)], cwd, "git diff")
    return _split_sections(text, dir_a, dir_b)


# --- internals ---------------------------------------------------------------
# Private, but exercised directly by the tests as internal seams: git's path
# formatting has enough edge cases that testing it only through the two public
# functions would make failures hard to localize.


def _git(args, cwd, label, timeout=300):
    """Run a git diff --no-index invocation and return stdout.

    The timeout matters more here than it looks: git is being pointed at two
    directories this tool did not create, over storage that may be a network
    mount. Without one, a wedged git leaves the whole comparison hanging with no
    output and no indication of why.
    """
    cmd = _GIT + ["diff", "--no-index", "-M"] + args
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        warn("%s exceeded %ds — no diff could be produced." % (label, timeout))
        return ""
    if proc.returncode not in (0, 1):
        warn("%s exited %d: %s" % (label, proc.returncode, proc.stderr.decode("utf-8", "replace").strip()))
    return proc.stdout.decode("utf-8", "replace")


def _parse_name_status(out, roots):
    """Parse --name-status output into {relative path: (status letter, [paths])}."""
    changed = {}
    for line in out.split("\n"):
        line = line.rstrip("\r")
        if not line.strip():
            continue
        fields = line.split("\t")
        status = fields[0].strip()
        paths = [_relativize(p, roots) for p in fields[1:] if p.strip()]
        if not status or not paths:
            continue
        changed[paths[-1]] = (status[0], paths)
    return changed


def _split_sections(text, dir_a, dir_b):
    """Split a unified diff into one section per file, without the directory prefixes."""
    roots = [dir_a, dir_b]
    sections = {}
    for chunk in re.split(r"(?m)^(?=diff --git )", _strip_prefixes(text, roots)):
        if not chunk.startswith("diff --git "):
            continue
        rel = _section_path(chunk, roots)
        if rel:
            sections[rel] = chunk.rstrip("\n") + "\n"
    return sections


def _strip_prefixes(text, roots):
    """Drop the copy-directory name from the path-bearing lines, and only those.

    Rewriting the whole text would edit diff content too: a source line holding
    the literal "data/A/tables.bin" would come out as "data/tables.bin" and show
    the reviewer a path that exists in no file. Every extracted path goes through
    _relativize, which strips whichever root is present — necessary because git
    names both sides of an added or deleted file after the one directory that
    has it, so 'a/' can carry dir_b's name.
    """

    def header(match):
        return "diff --git a/%s b/%s" % (
            _relativize(match.group(1), roots),
            _relativize(match.group(2), roots),
        )

    def side(match):
        return "%s%s/%s" % (match.group(1), match.group(2), _relativize(match.group(3), roots))

    def binary(match):
        return "Binary files a/%s and b/%s differ" % (
            _relativize(match.group(1), roots),
            _relativize(match.group(2), roots),
        )

    def moved(match):
        return "%s %s %s" % (match.group(1), match.group(2), _relativize(match.group(3), roots))

    text = re.sub(r"(?m)^diff --git a/(.*) b/(.*)$", header, text)
    text = re.sub(r"(?m)^(--- |\+\+\+ )([ab])/(.*)$", side, text)
    text = re.sub(r"(?m)^Binary files a/(.*) and b/(.*) differ$", binary, text)
    text = re.sub(r"(?m)^(rename|copy) (from|to) (.*)$", moved, text)
    return text


def _posix(path):
    """Git quotes paths containing backslashes, so always hand it forward slashes."""
    return path.replace("\\", "/")


def _unquote(value):
    """Undo the C-style quoting git applies to unusual paths."""
    value = value.strip()
    if len(value) < 2 or not (value.startswith('"') and value.endswith('"')):
        return value
    inner = value[1:-1]
    escapes = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"'}
    out = []
    index = 0
    while index < len(inner):
        char = inner[index]
        if char == "\\" and index + 1 < len(inner) and inner[index + 1] in escapes:
            out.append(escapes[inner[index + 1]])
            index += 2
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _fold(value):
    """Case-insensitive path comparison, on Windows only.

    Deliberately not os.path.normcase: on Windows that also turns '/' into '\\',
    which breaks comparison against git-style paths.
    """
    return value.lower() if os.name == "nt" else value


def _relativize(path, roots):
    """Strip the base-directory prefix from a path git reported."""
    clean = _unquote(path).replace("\\", "/").strip()
    lowered = _fold(clean)
    best = None
    for root in roots:
        for candidate in (root, os.path.abspath(root)):
            base = candidate.replace("\\", "/").rstrip("/")
            if not base:
                continue
            if lowered.startswith(_fold(base) + "/"):
                rel = clean[len(base) + 1:]
                if best is None or len(rel) < len(best):
                    best = rel
    return best if best is not None else clean


def _section_path(chunk, roots):
    """Work out which file a diff section belongs to.

    The '+++ b/' and '--- a/' lines carry a single path each, so they parse
    unambiguously even when a filename contains spaces. The 'diff --git' header
    holds two paths on one line and is only a fallback (binary files, which have
    no +++/--- lines).
    """
    for pattern in (r"(?m)^\+\+\+ b/(.+)$", r"(?m)^--- a/(.+)$"):
        match = re.search(pattern, chunk)
        if match:
            value = match.group(1).rstrip("\r").strip()
            if value and value != "/dev/null":
                return _relativize(value, roots)
    match = re.match(r"diff --git a/(.*) b/(.*)$", chunk.split("\n")[0])
    if match:
        return _relativize(match.group(2).rstrip("\r"), roots)
    return None

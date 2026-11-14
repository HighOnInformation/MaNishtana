"""Link binary findings to the commits of a revision range.

Public interface:
    Links                          namedtuple(strings, functions)
                                   {string: [sha]} and {function name: [sha]}
    link(commits, patches, strings, changes) -> Links
    literals(pseudo_diff) -> [str]   string literals on the changed lines of a diff
    pieces(text) -> [str]            the matchable parts of an excerpted string
    paths(text) -> [str]             the project's source file names quoted in a text
    changed_files(patch) -> [str]    files one commit's patch touches
    MAX_LINKS                      most commits named per finding

Why strings: a stripped binary only has FUN_ names, so a changed function cannot be
matched to the source by name. The string literals survive stripping. The decompiled
code of a changed function quotes them, and the commit that changed the source quotes
them too, so a literal on a changed line of the pseudo-code that also sits on a changed
line of a commit's patch is evidence of a link. It is evidence, not proof: a change
that adds no literal (the fd round's one-syscall fix) stays unlinked, and a literal
several commits touch names all of them (capped at MAX_LINKS). Functions that still
have names are also matched by name, but only against hunk headers and changed lines,
as a whole word; a FUN_ name means nothing in source and is never searched.

Second, weaker evidence: a Rust panic location, a C assert or a log line quotes the
source file name ("src/walk.rs"), and a patch names the files it touches. Code that
quotes a file name is tied to the commits that changed that file, after the commits
whose changed lines matched a literal. It says "lives in a file this commit touched",
not "this commit touched these lines", and a path that ends the same way in another
project (a std-library file of the same name) can match falsely. A change in a file
the binary never quotes stays unlinked.

Imports nothing from the package.
"""

import collections
import re

Links = collections.namedtuple("Links", "strings functions")

MAX_LINKS = 5
_MIN_LITERAL = 6
_MIN_NAME = 5

_LITERAL_RE = re.compile(r'"((?:[^"\\\n]|\\.)*)"')
_VERSION_RE = re.compile(r"(?<![\d.])\d+\.\d+\.\d+(?!\d|\.\d)")
_PATH_RE = re.compile(
    r"[A-Za-z0-9_./\-]+\.(?:rs|c|h|cc|cpp|cxx|hpp|go|py|java|cs|zig)(?![A-Za-z0-9_])")
_FILE_HEADER_RE = re.compile(r"^diff --git a/(.+) b/(.+)$")
# Paths into the compiler's own library or a dependency's sources: never the project's.
_EXTERNAL_RE = re.compile(
    r"(?:^|/)(?:rustc[-/]|rust/deps/|\.cargo/|registry/|vendor/|library/(?:core|std|alloc)/)"
    r"|^/usr/(?:src|share|lib)/")
_FUN_RE = re.compile(r"^(FUN|SUB|LAB|DAT|sub|loc)_[0-9A-Fa-f]+$")


def literals(pseudo_diff):
    """String literals quoted on the added and removed lines, in order, raw (escaped)."""
    found = []
    for line in (pseudo_diff or "").split("\n"):
        if line[:1] not in ("+", "-") or line.startswith(("+++", "---")):
            continue
        for text in _LITERAL_RE.findall(line):
            if len(text) >= _MIN_LITERAL and text not in found:
                found.append(text)
    return found


def pieces(text):
    """Split an excerpted string at its ellipses; drop pieces too short to match on."""
    parts = re.split(r"\s*…\s*", text)
    found = [part for part in parts if len(part) >= _MIN_LITERAL]
    # Rust and Go pack their literals without NULs, so a version sits inside a long
    # run no commit contains whole; the version itself is what a bump commit changed.
    for version in _VERSION_RE.findall(text):
        if version not in found:
            found.append(version)
    return found


def paths(text):
    """Source file names quoted in a text, in order, normalised (no leading ./)."""
    found = []
    for path in _PATH_RE.findall(text or ""):
        path = path.replace("\\", "/")
        if _EXTERNAL_RE.search(path):
            continue
        while path.startswith("./"):
            path = path[2:]
        if path and path not in found:
            found.append(path)
    return found


def changed_files(patch):
    """The files one commit's patch touches (new names), in order."""
    files = []
    for line in (patch or "").split("\n"):
        found = _FILE_HEADER_RE.match(line)
        if found and found.group(2) not in files:
            files.append(found.group(2))
    return files


def link(commits, patches, strings, changes):
    """Match strings and changed functions against each commit's changed lines."""
    index = [(commit.sha, _changed_lines(patches.get(commit.sha, ""))) for commit in commits]
    files = [(commit.sha, changed_files(patches.get(commit.sha, ""))) for commit in commits]
    out_strings = {}
    for text in strings:
        shas = _commits_with(index, _forms(text))
        shas += [sha for sha in _commits_with_files(files, paths(text)) if sha not in shas]
        shas = shas[:MAX_LINKS]
        if shas:
            out_strings[text] = shas
    out_functions = {}
    for change in changes:
        name = change.name_a or change.name_b
        if not name or name in out_functions:
            continue
        shas = []
        for literal in literals(change.pseudo_diff):
            for sha in _commits_with(index, [literal]):
                if sha not in shas:
                    shas.append(sha)
        if len(name) >= _MIN_NAME and not _FUN_RE.match(name):
            word = re.compile(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(name))
            for sha, (changed, headers) in index:
                if sha not in shas and any(word.search(line) for line in changed + headers):
                    shas.append(sha)
        quoted = []
        for line in (change.pseudo_diff or "").split("\n"):
            if line[:1] in ("+", "-") and not line.startswith(("+++", "---")):
                quoted += [path for path in paths(line) if path not in quoted]
        shas += [sha for sha in _commits_with_files(files, quoted) if sha not in shas]
        if shas:
            out_functions[name] = shas[:MAX_LINKS]
    return Links(out_strings, out_functions)


# --- internals ---------------------------------------------------------------


def _changed_lines(patch):
    """(added and removed lines, hunk headers) of one commit's patch."""
    changed, headers = [], []
    for line in patch.split("\n"):
        if line.startswith("@@"):
            headers.append(line)
        elif line[:1] in ("+", "-") and not line.startswith(("+++", "---")):
            changed.append(line[1:])
    return changed, headers


def _forms(text):
    """The ways a string can be spelled in source: as is, and with escapes written out."""
    forms = []
    for part in pieces(text):
        escaped = part.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t") \
                      .replace('"', '\\"')
        for form in (part, escaped):
            if form not in forms:
                forms.append(form)
    return forms


def _commits_with(index, forms):
    shas = []
    for sha, (changed, _) in index:
        if any(form in line for line in changed for form in forms):
            shas.append(sha)
    return shas[:MAX_LINKS]


def _commits_with_files(files, quoted):
    """Commits that changed a file the code quotes: same path, or one is the other's tail."""
    shas = []
    for sha, changed in files:
        for path in quoted:
            if any(path == f or path.endswith("/" + f) or f.endswith("/" + path)
                   for f in changed):
                shas.append(sha)
                break
    return shas

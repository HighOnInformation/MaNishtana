"""The core algorithm: compare twice, and let the difference define the noise.

Public interface:
    compare(dir_a, dir_b, rules) -> Comparison
    Comparison                      namedtuple(real, noise, skipped, sections, binaries)

git diff runs once on the untouched originals and once on rule-normalized copies.
A file that differs in the first pass but not the second changed only in ways the
rules were told to ignore — that is what "noise only" means here.
"""

import collections
import os
import shutil
import stat
import tempfile

from .gitdiff import changed_files, diff_sections
from .rules import is_binary, prepare_copy, skipped_files

Comparison = collections.namedtuple("Comparison", "real noise skipped sections binaries")


def compare(dir_a, dir_b, rules, settings=None):
    """Run both diffs and classify every changed file.

    Returns a Comparison where:
      real     {path: (status letter, [paths])} — survived normalization
      noise    set of paths that differ only before normalization
      skipped  set of paths deleted by a skip rule, never compared
      sections {path: unified diff text} for the real changes
      binaries subset of real that is binary

    The three categories are disjoint by construction: skipped files are removed
    from both copies, so they cannot reappear in the normalized set.
    """
    raw = changed_files(dir_a, dir_b)

    tmp = tempfile.mkdtemp()
    try:
        prepare_copy(dir_a, os.path.join(tmp, "A"), rules, settings)
        prepare_copy(dir_b, os.path.join(tmp, "B"), rules, settings)
        normalized = changed_files("A", "B", cwd=tmp)
        sections = diff_sections("A", "B", cwd=tmp)
    finally:
        _remove_tree(tmp)

    skipped = skipped_files(rules, raw)

    return Comparison(
        real=normalized,
        noise=_noise(raw, normalized, skipped),
        skipped=skipped,
        sections=sections,
        binaries=_find_binaries(normalized, dir_a, dir_b),
    )


def _noise(raw, normalized, skipped):
    """Paths that differed before normalization but not after.

    Subtract every path the normalized pass touched, not just its keys: a rename
    is keyed by its new path while covering both. Normalization raises similarity,
    so it can pair files the raw pass saw as an add plus a delete — and keying
    alone would then report the old path as "noise only", which reads as
    "unchanged apart from formatting" for a file that no longer exists.
    """
    covered = set()
    for _status, paths in normalized.values():
        covered.update(paths)
    return set(raw) - covered - skipped


def _find_binaries(real, dir_a, dir_b):
    """Detect binaries on the originals, since the copies may have been rewritten."""
    binaries = set()
    for rel in real:
        for root in (dir_b, dir_a):
            candidate = _inside(root, rel)
            if candidate is None:
                continue
            if os.path.isfile(candidate):
                if is_binary(candidate):
                    binaries.add(rel)
                break
    return binaries


def _inside(root, rel):
    """Join rel onto root, or None when the result would escape root.

    os.path.join discards root entirely the moment rel is absolute, so a path
    arriving as '/etc/passwd' or 'C:\Windows\...' would otherwise be opened and
    read from outside the tree being compared. The paths come from git's output
    on directories this tool did not create.
    """
    base = os.path.abspath(root)
    candidate = os.path.abspath(os.path.join(base, rel.replace("/", os.sep)))
    try:
        if os.path.commonpath([base, candidate]) != base:
            return None
    except ValueError:
        # Raised for paths on different drives, which is itself an escape.
        return None
    return candidate


def _remove_tree(path):
    """Delete the temporary tree, including files copytree left read-only.

    copytree preserves mode bits, so a read-only source file lands read-only in
    the copy. Windows then refuses to delete it and the run dies during cleanup —
    after the comparison already succeeded and the report was already written.
    """
    try:
        shutil.rmtree(path)
        return
    except OSError:
        pass
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                os.chmod(os.path.join(root, name), stat.S_IWRITE)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)

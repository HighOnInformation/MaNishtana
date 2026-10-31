"""The core algorithm: compare twice, and let the difference define the noise.

Public interface:
    compare(dir_a, dir_b, rules) -> Comparison
    Comparison                      namedtuple(real, noise, skipped, sections, binaries)
    binary_pairs(comparison, dir_a, dir_b) -> {path: (old file | None, new file | None)}

git diff runs once on the untouched originals and once on rule-normalized copies.
A file that differs in the first pass but not the second changed only in ways the
rules were told to ignore — that is what "noise only" means here.
"""

import collections
import os
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

    with tempfile.TemporaryDirectory() as tmp:
        prepare_copy(dir_a, os.path.join(tmp, "A"), rules, settings)
        prepare_copy(dir_b, os.path.join(tmp, "B"), rules, settings)
        normalized = changed_files("A", "B", cwd=tmp)
        sections = diff_sections("A", "B", cwd=tmp)

    skipped = skipped_files(rules, raw)

    return Comparison(
        real=normalized,
        noise=_noise(raw, normalized, skipped),
        skipped=skipped,
        sections=sections,
        binaries=_find_binaries(normalized, dir_a, dir_b),
    )


def binary_pairs(comparison, dir_a, dir_b):
    """Where each real binary lives on disk, old side and new side.

    Added files have no old side and deleted files no new side (None). A rename
    or copy pairs its old path in dir_a with its new path in dir_b, so the two
    sides are the same file before and after, not two unrelated paths.
    """
    pairs = {}
    for rel in sorted(comparison.binaries):
        status, paths = comparison.real[rel]
        old_rel = paths[0] if status in ("R", "C") else rel
        old = None if status == "A" else _existing(dir_a, old_rel)
        new = None if status == "D" else _existing(dir_b, rel)
        pairs[rel] = (old, new)
    return pairs


def _existing(root, rel):
    path = os.path.join(root, rel.replace("/", os.sep))
    return path if os.path.isfile(path) else None


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
            candidate = os.path.join(root, rel.replace("/", os.sep))
            if os.path.isfile(candidate):
                if is_binary(candidate):
                    binaries.add(rel)
                break
    return binaries

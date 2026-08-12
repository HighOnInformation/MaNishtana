"""Two revisions of a git repository, materialized as two directories.

Public interface:
    Commit                                   namedtuple(sha, author, date, subject, body)
    resolve(repo, rev) -> sha                 full sha, or None when rev does not exist
    export(repo, rev, dest)                   write that revision's tree into dest
    commits(repo, rev_a, rev_b) -> [Commit]   oldest first, exclusive of rev_a
    intent_text(commits) -> str               the stated intent, as prompt-ready prose

The rest of the tool compares directories and knows nothing about revisions. This
module is the adapter: it turns "the last commit of this repo" into the pair of
directories `compare` already accepts, and it reads the messages that pair came
with. Nothing downstream changes.

Why `git archive` into a tar and not `git worktree` or a plain checkout: a
worktree leaves a `.git` file inside the tree, which would then be compared as
content, and a checkout mutates the user's repository. An archive is a read-only
export of exactly one revision, and `tarfile` unpacks it without assuming a `tar`
binary exists on the box.

The commit messages are read here but deliberately not handed to the model unless
the caller asks. A summary written by a model that was shown the author's own
description of the change will agree with it, which destroys the one comparison
worth making: what the diff shows against what the commit claims. Independence is
the default; `cli` passes the text in only when told to.

Depends on `common` only, so the one-way import chain still holds.
"""

import collections
import os
import subprocess
import tarfile
import tempfile

from .common import warn

Commit = collections.namedtuple("Commit", "sha author date subject body")

# Field and record separators that cannot occur in a commit message: git's own
# --format placeholders emit them literally, and no editor writes them.
_FIELD = "\x1f"
_RECORD = "\x1e"

_LOG_FORMAT = _FIELD.join(["%H", "%an", "%aI", "%s", "%b"]) + _RECORD

_GIT = ["git", "-c", "core.quotepath=false"]


class GitError(Exception):
    """A git invocation failed in a way the caller cannot work around."""


def resolve(repo, rev):
    """Return the full sha for rev, or None when it does not name anything.

    Used to fail before any tree is exported: a typo'd revision should be a
    startup error, not a comparison against an empty directory.
    """
    try:
        out = _git(repo, ["rev-parse", "--verify", "--quiet", rev + "^{commit}"])
    except GitError:
        return None
    text = out.decode("utf-8", "replace").strip()
    return text or None


def export(repo, rev, dest):
    """Write the tree of rev into dest, which must not already exist.

    The archive goes through a temporary file rather than a pipe: git writes a
    tar to stdout, and reading it as a stream while git is still writing invites
    a deadlock on the platforms this has to run on.
    """
    os.makedirs(dest)
    handle, archive = tempfile.mkstemp(suffix=".tar")
    os.close(handle)
    try:
        _git(repo, ["archive", "--format=tar", "-o", archive, rev])
        with tarfile.open(archive, "r") as tar:
            _extract_all(tar, dest)
    finally:
        try:
            os.remove(archive)
        except OSError:
            pass


def commits(repo, rev_a, rev_b):
    """Every commit reachable from rev_b but not rev_a, oldest first.

    Exclusive of rev_a, matching the range the directory comparison covers: the
    tree at rev_a is the "before", so the commit that produced it is not part of
    the change being reviewed.
    """
    try:
        out = _git(
            repo,
            ["log", "--reverse", "--no-merges", "--format=" + _LOG_FORMAT, "%s..%s" % (rev_a, rev_b)],
        )
    except GitError as exc:
        warn("cannot read commit messages: %s — continuing without them." % exc)
        return []
    return _parse_log(out.decode("utf-8", "replace"))


def intent_text(items):
    """The commit messages as one block of prose, or "" when there are none.

    Subject and body are kept apart because they are different claims: the
    subject says what the change is, the body says why it was made, and a
    reviewer comparing the diff against the message needs to see which of the
    two the code actually supports.
    """
    if not items:
        return ""
    blocks = []
    for item in items:
        lines = ["%s — %s" % (item.sha[:9], item.subject)]
        if item.author:
            lines.append("Author: %s" % item.author)
        if item.body:
            lines.append("")
            lines.append(item.body)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# --- internals ---------------------------------------------------------------


def _git(repo, args):
    """Run one git command in repo and return raw stdout, or raise GitError."""
    proc = subprocess.run(
        _GIT + args, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().split("\n")[0]
        raise GitError(detail or "git exited %d" % proc.returncode)
    return proc.stdout


def _parse_log(text):
    """Parse the --format record stream into Commit records.

    Split on the record separator rather than on newlines: a commit body spans
    lines and contains blank ones, so any line-oriented parse would split a
    single message into several commits.
    """
    items = []
    for record in text.split(_RECORD):
        record = record.strip("\n")
        if not record.strip():
            continue
        fields = record.split(_FIELD)
        if len(fields) < 5:
            continue
        sha, author, date, subject = (field.strip() for field in fields[:4])
        body = fields[4].strip()
        if not sha:
            continue
        items.append(Commit(sha=sha, author=author, date=date, subject=subject, body=body))
    return items


def _is_within(base, target):
    """True when target stays inside base once both are made absolute."""
    base = os.path.abspath(base)
    target = os.path.abspath(target)
    return target == base or target.startswith(base + os.sep)


def _extract_all(tar, dest):
    """Extract every member, refusing any that would land outside dest.

    git archive does not produce '../' entries, but this reads a tar file whose
    contents come from a repository the reviewer did not write. Python's own
    extractall grew a filter parameter for exactly this reason, and it is not
    available on the oldest interpreter this has to run on, so the check is here.
    """
    members = []
    for member in tar.getmembers():
        target = os.path.join(dest, member.name.replace("/", os.sep))
        if not _is_within(dest, target):
            warn("archive entry '%s' escapes the export directory — skipped." % member.name)
            continue
        if member.issym() or member.islnk():
            if not _is_within(dest, os.path.join(os.path.dirname(target), member.linkname)):
                warn("archive link '%s' points outside the export directory — skipped." % member.name)
                continue
        members.append(member)
    tar.extractall(dest, members=members)

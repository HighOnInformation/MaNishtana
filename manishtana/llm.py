"""Model analysis of the real changes, in three passes.

Public interface:
    http_chat(llm_config) -> chat
    analyze_changes(comparison, chat, ...) -> Analysis
    Analysis                                  namedtuple(brief, analyses, summary)

The seam is `chat`, a callable:

    chat(system, user, label) -> str | None

`analyze_changes` never builds a transport of its own — it is handed one. That is
what lets the whole analysis stage (orientation, file capping, hunk splitting,
summary folding) be tested in-process with a fake, without an HTTP server.
`http_chat` is the real adapter; a test fake is the second one, which is what
makes this a seam worth having.

A chat callable returns None on failure rather than raising, so a dead endpoint
degrades the report instead of killing the run.

The three passes exist because a file read in isolation cannot be understood:

  0. Orientation — one cheap call over an inventory of the whole change (files,
     churn, symbols added and removed, definitions deleted while still
     referenced) and NOT the diffs. Skipped when there is only one file, which
     has no whole to orient against.
  1. Per file — the same call as before, now carrying the orientation brief, so
     each file is read in context rather than in a vacuum. No extra calls.
  2. Reconciliation — the summary, now given the inventory as well as the
     per-file analyses, and asked which earlier conclusions the whole change
     revises.

Total: N + 2 calls, one more than before regardless of how large the drop is.
"""

import collections
import json
import urllib.request

from .common import warn
from .settings import load as _load_settings

Analysis = collections.namedtuple("Analysis", "brief analyses summary")

_STATUS_WORDS = {
    "A": "added", "D": "deleted", "M": "modified",
    "R": "renamed", "C": "copied", "T": "type changed",
}


def http_chat(llm):
    """Build a chat callable that talks to an OpenAI-compatible endpoint."""

    def chat(system, user, label):
        try:
            return _post(llm, system, user)
        except Exception as exc:  # a model failure must never bring down the report
            warn("LLM analysis failed for %s: %s" % (label, exc))
            return None

    return chat


def analyze_changes(comparison, chat, prompts=None, xref=None, max_files=50, max_chars=24000,
                    intent=None):
    """Orient, analyze every real change, then reconcile the results.

    `intent` is the author's own description of the change (commit messages, when
    the input was a git range). It is off by default and travels no further than
    the per-file context: a model given the author's account of what a change does
    will reproduce it, and the report's value is that it was written from the diff
    alone. When it is supplied, the prompt asks for it to be checked, not assumed.
    """
    if prompts is None:
        prompts = _load_settings({}).prompts

    targets = [rel for rel in sorted(comparison.real) if rel not in comparison.binaries]
    if len(targets) > max_files:
        warn(
            "%d files to analyze but the limit is %d — the last %d will not be analyzed."
            % (len(targets), max_files, len(targets) - max_files)
        )
        targets = targets[:max_files]

    manifest = _manifest(comparison, xref, targets)
    brief = None
    if len(targets) > 1:
        brief = chat(
            prompts["orient_system"],
            prompts["orient_user"].format(manifest=manifest),
            "orientation",
        )
    context = prompts["context_block"].format(brief=brief) if brief else ""
    if intent:
        context = prompts["intent_block"].format(intent=intent) + context

    analyses = {}
    for rel in targets:
        section = comparison.sections.get(rel)
        if not section:
            continue
        answer = _analyze_file(chat, rel, section, max_chars, prompts, context)
        if answer:
            analyses[rel] = answer

    summary = None
    if analyses:
        body = "\n\n".join(
            prompts["summary_item"].format(path=rel, analysis=text)
            for rel, text in sorted(analyses.items())
        )
        if len(body) > max_chars:
            body = body[:max_chars] + "\n\n[analyses truncated due to length limit]"
        summary = chat(
            prompts["summary_system"],
            prompts["summary_user"].format(manifest=manifest, analyses=body),
            "executive summary",
        )

    return Analysis(brief=brief, analyses=analyses, summary=summary)


# --- internals ---------------------------------------------------------------


def _manifest(comparison, xref, targets):
    """An inventory of the whole change: everything except the code.

    Deliberately cheap — a few hundred tokens for a whole drop — because its job
    is to let one call establish what the change is before any file is read.
    """
    real, noise, skipped = comparison.real, comparison.noise, comparison.skipped
    lines = [
        "Change inventory",
        "%d real, %d noise only, %d skipped." % (len(real), len(noise), len(skipped)),
        "",
        "Files carrying substantive change:",
    ]
    removed = dict(xref.removed) if xref else {}
    for rel in targets:
        status = _STATUS_WORDS.get(real[rel][0], real[rel][0]) if rel in real else "changed"
        detail = "- %s (%s)" % (rel, status)
        section = comparison.sections.get(rel, "")
        added_lines = sum(
            1 for line in section.split("\n") if line.startswith("+") and not line.startswith("+++")
        )
        removed_lines = sum(
            1 for line in section.split("\n") if line.startswith("-") and not line.startswith("---")
        )
        detail += ", +%d/-%d lines" % (added_lines, removed_lines)
        if rel in removed:
            detail += ", removes: %s" % ", ".join(removed[rel])
        lines.append(detail)

    if comparison.binaries:
        lines.append("")
        lines.append("Binary, not analysed: %s" % ", ".join(sorted(comparison.binaries)))

    dangling = dict(xref.dangling) if xref else {}
    if dangling:
        lines.append("")
        lines.append("Deleted but still named in the new tree (found by search, verify each):")
        for symbol in sorted(dangling):
            lines.append("- %s, still named in %s" % (symbol, ", ".join(dangling[symbol])))

    return "\n".join(lines)


def _post(llm, system, user):
    """Send one chat completion request and return the assistant's text."""
    url = llm.get("base_url", "").rstrip("/") + "/chat/completions"
    payload = {
        "model": llm.get("model", ""),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": llm.get("temperature", 0.2),
        "max_tokens": llm.get("max_tokens", 1200),
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    if llm.get("api_key"):
        request.add_header("Authorization", "Bearer " + llm["api_key"])
    with urllib.request.urlopen(request, timeout=llm.get("timeout_sec", 180)) as response:
        data = json.loads(response.read().decode("utf-8", "replace"))
    return data["choices"][0]["message"]["content"].strip()


def _analyze_file(chat, rel, section, max_chars, prompts, context):
    """Analyze one file's diff, splitting it across calls when it is too long."""
    if len(section) <= max_chars:
        return chat(
            prompts["file_system"],
            prompts["file_user"].format(context=context, path=rel, diff=section),
            rel,
        )

    parts = _split_by_hunks(section, max_chars)
    pieces = []
    for number, part in enumerate(parts, 1):
        answer = chat(
            prompts["file_system"],
            prompts["part_user"].format(
                context=context, path=rel, diff=part, part=number, parts=len(parts)
            ),
            "%s (part %d/%d)" % (rel, number, len(parts)),
        )
        if answer:
            pieces.append("#### Part %d of %d\n\n%s" % (number, len(parts), answer))
    return "\n\n".join(pieces) if pieces else None


def _split_by_hunks(section, limit):
    """Split a large diff section on @@ boundaries, repeating the file header."""
    lines = section.split("\n")
    index = 0
    while index < len(lines) and not lines[index].startswith("@@"):
        index += 1
    header = "\n".join(lines[:index])
    hunks = []
    current = []
    for line in lines[index:]:
        if line.startswith("@@") and current:
            hunks.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        hunks.append("\n".join(current))
    if not hunks:
        return [section]

    parts = []
    batch = []
    size = len(header)
    for hunk in hunks:
        if batch and size + len(hunk) > limit:
            parts.append(header + "\n" + "\n".join(batch))
            batch = []
            size = len(header)
        batch.append(hunk)
        size += len(hunk)
    if batch:
        parts.append(header + "\n" + "\n".join(batch))
    return parts

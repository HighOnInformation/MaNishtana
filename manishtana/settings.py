"""Every input the run depends on, declared in one place.

Public interface:
    load(config, base_dir) -> Settings
    Settings   namedtuple(prompts, risk, normalizers, tuning, digest)

The prompts, the risk labels the extraction looks for, and the normalizer command
lines are inputs, not code. On a closed network a reviewer has to be able to read
and approve the exact text that leaves the box without reading Python, so all of
it is declarable in the config file and defaults to the values below when absent.

Two rules make this safe rather than merely flexible:

  * A prompt may be written inline or as "@relative/path.md", resolved against the
    config file's directory, so prose stays reviewable Markdown instead of JSON
    escapes.
  * Placeholders are validated at load. An unknown or missing one is a startup
    error, because the alternative is discovering a mangled prompt three files
    into a review.

The risk labels live here beside the prompts on purpose. `_extract_risk` looks for
the literal label the prompt asks the model to write; translating one without the
other makes every badge silently disappear. They are one coupled input.
"""

import collections
import hashlib
import json
import os

Settings = collections.namedtuple("Settings", "prompts risk normalizers tuning digest")

_ORIENT_SYSTEM = """You are a senior engineer opening a source drop for review.

Below is an inventory of one change: the files that differ, how much each moved,
the definitions added and removed, and any definition that was deleted while the
new tree still names it. You are NOT being shown the code yet.

In no more than four sentences, say what this change appears to be, and name the
files that look coupled to each other. If the inventory suggests something was
left half-done, say so. Do not speculate beyond what the inventory supports."""

_FILE_SYSTEM = """You are a senior software engineer reviewing a code change.

The diff below has already been filtered automatically: comments, formatting,
whitespace and generated lines (timestamps, build numbers) were removed from it.
Every change that remains is therefore substantive — do not describe it as
stylistic.

Answer in English, using exactly this structure:

**Summary** — one sentence.

**Changes**
- one bullet per substantive change.

**Risk** — Low, Medium or High, followed by a short justification."""

_SUMMARY_SYSTEM = """You are the lead code reviewer. Below is an inventory of the
whole change, followed by analyses of the individual files, already filtered of
formatting noise.

Write an executive summary: what actually changed, the practical implications,
and what deserves attention during review.

Then, only where the whole change makes it necessary, add these two sections:

**Connections** — findings in one file that bear on another. Say which files.

**Reconsidered** — per-file conclusions that should be revised now that the whole
change is visible. Name the file and what changes about the reading. Omit this
section entirely if nothing needs revising; do not pad it.

End with a separate line in this exact format:

**Overall risk** — Low, Medium or High, followed by a short justification.

Be concise and do not repeat detail that already appears in the analyses."""

# Each prompt's allowed placeholders, and which of them it must contain.
_PLACEHOLDERS = {
    "orient_system": (set(), set()),
    "orient_user": ({"manifest"}, {"manifest"}),
    "file_system": (set(), set()),
    "file_user": ({"path", "diff", "context"}, {"diff"}),
    "part_user": ({"path", "diff", "context", "part", "parts"}, {"diff"}),
    "context_block": ({"brief"}, {"brief"}),
    "intent_block": ({"intent"}, {"intent"}),
    "summary_system": (set(), set()),
    "summary_user": ({"manifest", "analyses"}, {"analyses"}),
    "summary_item": ({"path", "analysis"}, {"analysis"}),
}

_DEFAULT_PROMPTS = {
    "orient_system": _ORIENT_SYSTEM,
    "orient_user": "{manifest}",
    "file_system": _FILE_SYSTEM,
    "file_user": "{context}File: {path}\n\n```diff\n{diff}```",
    "part_user": "{context}File: {path}\nThis is part {part} of {parts} of the diff.\n\n```diff\n{diff}```",
    "context_block": "The change as a whole:\n{brief}\n\nYou are reviewing one file of it.\n\n",
    # Phrased as a claim to check rather than as background. A model shown the
    # author's description tends to restate it; the wording is what keeps the
    # analysis answerable against the diff instead of against the message.
    "intent_block": (
        "The author describes the change as follows. Treat this as a claim to verify "
        "against the code, not as established fact — say so if the diff does not "
        "support it.\n\n{intent}\n\n"
    ),
    "summary_system": _SUMMARY_SYSTEM,
    "summary_user": "{manifest}\n\n{analyses}",
    "summary_item": "## {path}\n{analysis}",
}

_DEFAULT_RISK = {
    "file_label": "Risk",
    "summary_label": "Overall risk",
    "levels": ["low", "medium", "high"],
}

_JSON_SORT_CODE = (
    "import json,sys;"
    "sys.stdout.write(json.dumps(json.load(sys.stdin),sort_keys=True,"
    "indent=2,ensure_ascii=False))"
)

_DEFAULT_NORMALIZERS = {
    "clang-format": "clang-format -style=LLVM -assume-filename={name}",
    "strip-comments": "gcc -fpreprocessed -dD -E -P -x c -",
    "json-sort": None,  # built at load: depends on the running interpreter
}

_DEFAULT_TUNING = {
    "normalize_timeout_sec": 60,
    "ignored_token": "<<ignored-by-rule>>",
    "mark_similarity_floor": 0.5,
}


def load(config, base_dir="."):
    """Resolve every declarable input, falling back to the built-in defaults."""
    prompts = _merge("prompts", _DEFAULT_PROMPTS, config.get("prompts") or {})
    prompts = {key: _resolve(key, value, base_dir) for key, value in prompts.items()}
    for key, text in prompts.items():
        _check_placeholders(key, text)

    return Settings(
        prompts=prompts,
        risk=_merge("risk", _DEFAULT_RISK, config.get("risk") or {}),
        normalizers=_normalizers(config.get("normalizers") or {}),
        tuning=_merge("tuning", _DEFAULT_TUNING, config.get("tuning") or {}),
        digest=_digest(prompts),
    )


# --- internals ---------------------------------------------------------------


def _merge(section, defaults, given):
    """Defaults overlaid with what the config declared. Unknown keys are errors.

    A typo'd key would otherwise be a silent no-op, and the user would conclude
    the setting does not work.
    """
    unknown = sorted(set(given) - set(defaults))
    if unknown:
        raise ValueError(
            "unknown key(s) in config '%s': %s — expected one of: %s"
            % (section, ", ".join(unknown), ", ".join(sorted(defaults)))
        )
    merged = dict(defaults)
    merged.update(given)
    return merged


def _resolve(key, value, base_dir):
    """'@path' means read the prompt from that file, relative to the config."""
    if not isinstance(value, str) or not value.startswith("@"):
        return value
    path = os.path.join(base_dir, value[1:])
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return handle.read().strip()
    except OSError as exc:
        raise ValueError("cannot read prompt file for '%s': %s (%s)" % (key, value[1:], exc))


def _check_placeholders(key, text):
    allowed, required = _PLACEHOLDERS[key]
    used = set()
    for _literal, field, _spec, _conv in __import__("string").Formatter().parse(text):
        if field is not None:
            used.add(field)
    unknown = sorted(used - allowed)
    if unknown:
        raise ValueError(
            "prompt '%s' uses unknown placeholder(s): %s — allowed: %s"
            % (key, ", ".join("{%s}" % u for u in unknown),
               ", ".join("{%s}" % a for a in sorted(allowed)) or "none")
        )
    missing = sorted(required - used)
    if missing:
        raise ValueError(
            "prompt '%s' must contain %s"
            % (key, ", ".join("{%s}" % m for m in missing))
        )


def _normalizers(given):
    import sys

    defaults = dict(_DEFAULT_NORMALIZERS)
    defaults["json-sort"] = '"%s" -c "%s"' % (sys.executable, _JSON_SORT_CODE)
    merged = dict(defaults)
    merged.update(given)  # extra aliases are allowed here: they are new commands
    return merged


def _digest(prompts):
    """A stable fingerprint of the exact text this run will send to the model."""
    canonical = json.dumps(prompts, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

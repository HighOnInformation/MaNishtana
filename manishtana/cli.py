"""Command-line entry point.

Public interface:
    main(argv=None) -> exit code

This module is wiring only: read arguments and config, choose the chat adapter,
run the pipeline, write the files. All the behaviour lives behind the interfaces
it calls.

Two input shapes reach the same pipeline. Two directories is the original one. A
git repository and a revision range is the second: `gitrepo` exports both
revisions into a temporary directory and the comparison proceeds unchanged, so
nothing downstream knows which shape it was given.

`--check` is answered here too. No probing logic lives in this module: git is
asked of gitdiff, the normalizers of rules, the prompts of settings, the endpoint
of llm, and nothing here learns how to look. What this module does now own is the
policy on top of those answers -- which of them are required and which are
forgiven, and therefore the exit code. That is a judgement about deployment, not
wiring, and it sits beside the same kind of judgement in _run_git (a commit means
REV^..REV; --to defaults to HEAD). Two such rules is the limit: a third means
both belong in a module of their own and the first line above stops being true.
"""

import argparse
import json
import os
import sys
import tempfile

from .common import warn
from .compare import compare
from .llm import Analysis, analyze_changes, http_chat, probe
from .output import build_html, build_json, build_report
from .settings import load as load_settings
from .symbols import cross_reference
from . import gitdiff, gitrepo, rules

# The value config.json ships with. A config still carrying it is half-configured,
# which is worse than unconfigured: every call is rejected for an unknown model,
# each rejection is only a warning, and the run still ends with a success line.
_PLACEHOLDER_MODEL = "PLACEHOLDER-MODEL-NAME"


def main(argv=None):
    _configure_streams()
    args = _parse_args(argv)

    try:
        with open(args.config, "r", encoding="utf-8-sig") as handle:
            config = json.load(handle)
    except (OSError, ValueError) as exc:
        sys.stderr.write("Error: cannot read config file '%s': %s\n" % (args.config, exc))
        return 2

    # Prompts and normalizer commands are inputs too. They resolve against the
    # config file's own directory, so "@prompts/review.md" means what it looks like.
    try:
        settings = load_settings(config, os.path.dirname(os.path.abspath(args.config)))
    except ValueError as exc:
        sys.stderr.write("Error: %s\n" % exc)
        return 2

    if args.check:
        return _check(args, config, settings)

    llm = config.get("llm") or {}
    if not args.no_llm and llm.get("model") == _PLACEHOLDER_MODEL:
        sys.stderr.write(
            "Error: llm.model in %s is still %s. Every call would be rejected, each "
            "rejection is only a warning, and the run would still print a success "
            "line and exit 0. Set a real model name, or pass --no-llm for a "
            "diff-only report.\n" % (args.config, _PLACEHOLDER_MODEL)
        )
        return 2

    if args.git:
        return _run_git(args, config, settings)

    if not args.dir_a or not args.dir_b:
        sys.stderr.write("Error: give two directories, or --git REPO with --commit/--from/--to.\n")
        return 2
    dir_a = args.dir_a.rstrip("/\\")
    dir_b = args.dir_b.rstrip("/\\")
    for path in (dir_a, dir_b):
        if not os.path.isdir(path):
            sys.stderr.write("Error: '%s' is not an existing directory.\n" % path)
            return 2
    if not _require_git():
        return 2
    return _run(args, config, settings, dir_a, dir_b, [])


# --- internals ---------------------------------------------------------------


def _run_git(args, config, settings):
    """Export two revisions to a temporary pair of trees, then run the pipeline."""
    if not os.path.isdir(args.git):
        sys.stderr.write("Error: '%s' is not an existing directory.\n" % args.git)
        return 2

    if args.commit:
        if args.rev_a or args.rev_b:
            sys.stderr.write("Error: --commit cannot be combined with --from/--to.\n")
            return 2
        rev_a, rev_b = args.commit + "^", args.commit
    else:
        rev_a, rev_b = args.rev_a, args.rev_b or "HEAD"
        if not rev_a:
            sys.stderr.write("Error: --git needs --commit REV, or --from REV (--to defaults "
                             "to HEAD).\n")
            return 2

    if not _require_git():
        return 2

    resolved = {}
    for name, rev in (("from", rev_a), ("to", rev_b)):
        sha = gitrepo.resolve(args.git, rev)
        if sha is None:
            sys.stderr.write("Error: '%s' does not name a commit in %s.\n" % (rev, args.git))
            return 2
        resolved[name] = sha

    commits = gitrepo.commits(args.git, resolved["from"], resolved["to"])
    if not commits:
        warn("no commits between the two revisions — the trees may be identical.")

    with tempfile.TemporaryDirectory() as tmp:
        dir_a = os.path.join(tmp, "before")
        dir_b = os.path.join(tmp, "after")
        try:
            gitrepo.export(args.git, resolved["from"], dir_a)
            gitrepo.export(args.git, resolved["to"], dir_b)
        except (gitrepo.GitError, OSError) as exc:
            sys.stderr.write("Error: cannot export revisions from %s: %s\n" % (args.git, exc))
            return 2
        print("Comparing %s..%s (%d commit%s) from %s"
              % (resolved["from"][:9], resolved["to"][:9], len(commits),
                 "" if len(commits) == 1 else "s", args.git))
        return _run(args, config, settings, dir_a, dir_b, commits)


def _require_git():
    """False, having said why, when this box has no usable git.

    Checked after the arguments are validated, so a typo on the command line is
    still reported as a typo. The README said a missing git yields "a Python
    traceback rather than a diagnostic" -- the diagnostic was always known here,
    it was simply never printed.
    """
    if gitdiff.version():
        return True
    sys.stderr.write(
        "Error: git was not found on PATH. 'git diff --no-index' is the comparison "
        "engine, so there is nothing to compare without it. Install git 2.30+, then "
        "run 'python -m manishtana --check'.\n"
    )
    return False


def _check(args, config, settings):
    """Report what this box can do with these inputs, then exit on the answer.

    Formatting only. Each row is (label, detail, ok, required): the required rows
    decide the exit code and the rest are reported and forgiven, exactly as a run
    forgives a missing optional normalizer. One command replaces the seven of
    docs/UIUX-GUIDE.md Flow A, and it is the same command natively, inside the
    container and on Windows — which is the only form a Windows box has ever had.
    """
    git = gitdiff.version()
    rows = [
        ("python", "%d.%d.%d" % sys.version_info[:3], sys.version_info >= (3, 8), True),
        ("git", git or "not found — 'git diff --no-index' is the comparison engine",
         git is not None, True),
        ("config", os.path.abspath(args.config), True, True),
        ("prompts", "%d resolved, digest %s"
         % (len(settings.prompts), settings.digest[:12]), True, True),
    ]
    for tool in rules.tool_status(config.get("rules") or [], settings):
        rows.append((
            "normalize:" + tool.step,
            tool.command if tool.available else
            "%s not found — step skipped, so formatting counts as real change"
            % tool.program,
            tool.available,
            False,
        ))
    rows.extend(_llm_rows(args, config.get("llm") or {}))

    for label, detail, ok, required in rows:
        print("%-4s %-24s %s"
              % ("ok" if ok else ("FAIL" if required else "warn"), label, detail))
    missing = [label for label, _detail, ok, required in rows if required and not ok]
    if missing:
        # stdout is block-buffered when redirected to a file and stderr is not, so
        # without this the verdict lands above the table it is a verdict on.
        sys.stdout.flush()
        sys.stderr.write(
            "Error: this box cannot run MaNishtana as configured: %s.\n"
            % ", ".join(missing)
        )
        return 2
    return 0


def _llm_rows(args, llm):
    """The endpoint's row: not asked for, still a placeholder, unset, or answering.

    The placeholder is tested before base_url so --check and a real run agree: a
    config still carrying it fails both, whatever else is set.
    """
    if args.no_llm:
        return [("llm", "--no-llm — the endpoint is not contacted", True, False)]
    if llm.get("model") == _PLACEHOLDER_MODEL:
        return [("llm", "llm.model is still %s — set a real model name, or pass "
                 "--no-llm" % _PLACEHOLDER_MODEL, False, True)]
    if not llm.get("base_url"):
        return [("llm", "llm.base_url is not set — the report will carry no analysis",
                 False, False)]
    reply = probe(http_chat(llm))
    return [("llm",
             "%s answered for model %s" % (llm["base_url"], llm.get("model", "")) if reply
             else "%s did not answer — see the warning above" % llm["base_url"],
             bool(reply), True)]


def _run(args, config, settings, dir_a, dir_b, commits):
    """The pipeline itself, identical whichever input shape produced the trees."""
    llm = config.get("llm") or {}
    comparison = compare(dir_a, dir_b, config.get("rules") or [], settings)
    xref = cross_reference(comparison.sections, dir_b)

    # The author's own account of the change is withheld unless asked for: a model
    # shown it restates it, and the report is worth more when it can be checked
    # against the message rather than derived from it.
    intent = gitrepo.intent_text(commits) if (commits and args.intent_context) else None

    result = Analysis(brief=None, analyses={}, summary=None)
    if args.no_llm:
        pass
    elif not llm.get("base_url"):
        warn("llm.base_url is not set in the config — producing the report without LLM analysis.")
    else:
        result = analyze_changes(
            comparison,
            http_chat(llm),
            prompts=settings.prompts,
            xref=xref,
            max_files=llm.get("max_files", 50),
            max_chars=llm.get("max_chars_per_call", 24000),
            intent=intent,
        )
    analyses, summary, brief = result.analyses, result.summary, result.brief

    shared = {
        "xref": xref,
        "brief": brief,
        "risk": settings.risk,
        "commits": commits,
        "intent_shown": bool(intent),
    }
    report = build_report(dir_a, dir_b, comparison, analyses, summary, **shared)
    if not _write(args.output, report, "report"):
        return 2

    if args.html:
        page = build_html(dir_a, dir_b, comparison, analyses, summary, **shared)
        if not _write(args.html, page, "HTML report"):
            return 2

    if args.json:
        payload = build_json(
            dir_a, dir_b, comparison, analyses, summary, settings=settings, **shared
        )
        if not _write(args.json, json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "JSON file"):
            return 2

    print(
        "Report written to %s — %d real changes, %d noise only, %d skipped."
        % (args.output, len(comparison.real), len(comparison.noise), len(comparison.skipped))
    )
    return 0


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="manishtana",
        description="MaNishtana — rule-based directory comparison, with LLM analysis of "
        "the real changes only. Compares two directories, or two revisions of a git "
        "repository.",
    )
    parser.add_argument(
        "dir_a", metavar="DIR_A", nargs="?", help="first directory (the old version)"
    )
    parser.add_argument(
        "dir_b", metavar="DIR_B", nargs="?", help="second directory (the new version)"
    )
    parser.add_argument(
        "-g", "--git", metavar="REPO", help="compare two revisions of this git repository "
        "instead of two directories"
    )
    parser.add_argument(
        "--commit", metavar="REV", help="with --git: review this single commit (REV^ to REV)"
    )
    parser.add_argument(
        "--from", dest="rev_a", metavar="REV", help="with --git: the old revision"
    )
    parser.add_argument(
        "--to", dest="rev_b", metavar="REV", help="with --git: the new revision (default: HEAD)"
    )
    parser.add_argument(
        "--intent-context",
        action="store_true",
        help="with --git: also give the commit messages to the model. Off by default — "
        "the analysis is then an independent reading of the diff, which is what makes "
        "comparing it against the message meaningful.",
    )
    parser.add_argument(
        "-c", "--config", default="config.json", help="JSON config file (default: config.json)"
    )
    parser.add_argument(
        "-o", "--output", default="report.md", help="report file to write (default: report.md)"
    )
    parser.add_argument(
        "-H",
        "--html",
        metavar="PATH",
        help="also write a self-contained HTML report; off unless given",
    )
    parser.add_argument(
        "-j",
        "--json",
        metavar="PATH",
        help="also write JSON output for automated consumers (agent, CI); off unless given",
    )
    parser.add_argument(
        "--no-llm", action="store_true", help="skip LLM analysis and produce a diff-only report"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="check that this box can run the tool — interpreter, git, the normalizers "
        "these rules need, the config, the prompts and the LLM endpoint — then exit "
        "without comparing anything",
    )
    return parser.parse_args(argv)


def _configure_streams():
    """Force UTF-8 on stdout/stderr so output is identical across platforms."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _write(path, text, what):
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return True
    except OSError as exc:
        sys.stderr.write("Error: cannot write %s '%s': %s\n" % (what, path, exc))
        return False

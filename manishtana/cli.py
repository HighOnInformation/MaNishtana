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
"""

import argparse
import json
import os
import sys
import tempfile

from .binnoise import summarize as summarize_binary
from .common import warn
from .compare import binary_pairs, compare
from .llm import Analysis, analyze_changes, http_chat
from .output import build_html, build_json, build_report
from .settings import load as load_settings
from .rules import binary_ignore_strings
from .symbols import cross_reference
from . import binengines, gitrepo


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

    if args.check_binary_tools:
        sys.stdout.write(binengines.describe_tools(binengines.discover(settings.binary)))
        return 0

    engines = _selected_engines(args, settings)
    if engines is None:
        return 2
    args.engines = engines

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


def _run(args, config, settings, dir_a, dir_b, commits):
    """The pipeline itself, identical whichever input shape produced the trees."""
    llm = config.get("llm") or {}
    comparison = compare(dir_a, dir_b, config.get("rules") or [], settings)
    xref = cross_reference(comparison.sections, dir_b)
    pairs = binary_pairs(comparison, dir_a, dir_b)
    rules = config.get("rules") or []
    binary = {rel: summarize_binary(old, new, binary_ignore_strings(rules, rel))
              for rel, (old, new) in pairs.items()}
    functions = _binary_functions(args.engines, settings, pairs)

    # Decompiled diffs reach the model through the ordinary per-file pass: the
    # binary gets a text section and stops counting as "not analysed". llm.py
    # needs no knowledge of binaries for this.
    decompiled = {}
    for rel, found in functions.items():
        text = binengines.decompiled_diff(found.changes, settings.binary["max_decompiled"])
        if text:
            decompiled[rel] = text
    for_model = comparison
    if decompiled:
        sections = dict(comparison.sections)
        header = settings.prompts["decompiled_header"]
        sections.update((rel, header + text) for rel, text in decompiled.items())
        for_model = comparison._replace(sections=sections,
                                        binaries=comparison.binaries - set(decompiled))

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
            for_model,
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
        "binary": binary,
        "functions": functions,
        "decompiled": decompiled,
        "max_functions": settings.binary["max_functions"],
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


def _selected_engines(args, settings):
    """The engines to try: --binary-engines if given, else the config's list."""
    if args.binary_engines is None:
        names = list(settings.binary["engines"])
    elif args.binary_engines.strip().lower() == "none":
        return []
    else:
        names = [name.strip().lower() for name in args.binary_engines.split(",") if name.strip()]
    unknown = [name for name in names if name not in binengines.ENGINES]
    if unknown:
        sys.stderr.write("Error: unknown binary engine(s): %s — known: %s, or none.\n"
                         % (", ".join(unknown), ", ".join(binengines.ENGINES)))
        return None
    return names


def _binary_functions(engines, settings, pairs):
    """Run the available engines on every binary that exists on both sides.

    An added or deleted binary has nothing to diff against, so it gets layer 1
    only. Tools are discovered once per run, and a wanted engine that cannot run
    is warned about once, not once per file.
    """
    both = dict((rel, pair) for rel, pair in pairs.items() if pair[0] and pair[1])
    if not engines or not both:
        return {}
    tools = binengines.discover(settings.binary)
    ready = binengines.runnable(tools)
    usable = [name for name in engines if name in ready]
    missing = [name for name in engines if name not in ready]
    if missing:
        warn("binary engine(s) not available: %s — run with --check-binary-tools to see "
             "what is missing." % ", ".join(missing))
    if not usable:
        return {}
    functions = {}
    for rel in sorted(both):
        old, new = both[rel]
        print("Diffing functions in %s with %s ..." % (rel, ", ".join(usable)))
        found = binengines.diff_functions(old, new, usable, tools, settings.binary)
        for note in found.notes:
            warn("%s: %s" % (rel, note))
        if found.engines:
            functions[rel] = found
    return functions


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
        "--binary-engines",
        metavar="LIST",
        help="function-level engines to run on changed binaries, comma-separated (%s), "
        "or 'none'. Default: the config's binary.engines; engines whose tools are "
        "missing are skipped with a warning." % ", ".join(binengines.ENGINES),
    )
    parser.add_argument(
        "--check-binary-tools",
        action="store_true",
        help="report which binary comparison tools (IDA, Hex-Rays, Diaphora, ...) were "
        "found and which engines can run, then exit",
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

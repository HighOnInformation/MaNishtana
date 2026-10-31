"""Rendering the results.

Public interface:
    build_report(dir_a, dir_b, comparison, analyses, summary) -> str   (Markdown)
    build_html(dir_a, dir_b, comparison, analyses, summary) -> str     (HTML)
    build_json(dir_a, dir_b, comparison, analyses, summary) -> dict

All three take an optional `binary` mapping, {path: binnoise.BinarySummary}, for
the real binaries; without it a binary is reported as not analyzed, as before.
`functions` ({path: binengines.FunctionDiff}) adds the function-level table and
`decompiled` ({path: unified diff text}) the pseudo-code diff, when layer 2 ran.
These are read by attribute only: output imports nothing from the package.

All three are pure: same inputs, same output, no I/O. Writing files is the caller's
job, which is what makes them straightforward to test.

The HTML is one self-contained file — styles inlined, nothing fetched. On the
air-gapped box it is meant for, a report that reaches for a CDN renders unstyled.
"""

import collections
import datetime
import difflib
import re
from html import escape

_STATUS_LABELS = {
    "A": "added",
    "D": "deleted",
    "M": "modified",
    "R": "renamed",
    "C": "copied",
    "T": "type changed",
}

_RISK_ORDER = ["low", "medium", "high"]

# Word boundaries matter: "allows" contains "low" and "below" contains "low".
_RISK_RE = re.compile(r"\b(low|medium|high)\b")

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

# Which of these two the report prints is the difference between an independent
# reading and a restatement, so it is never inferred — the caller says which.
_INTENT_NOTE_WITHHELD = (
    "What the author said this change does. Recorded from the commit messages and "
    "*not* shown to the model — the analysis below was written from the diff alone, "
    "so the two can be compared.\n"
)
_INTENT_NOTE_SHOWN = (
    "What the author said this change does. This text **was** supplied to the model "
    "as context, so the analysis below is not an independent reading of the diff and "
    "agreement with it is not evidence.\n"
)

# One aligned line of a two-pane comparison. A None number means that side has
# no line here — the blank half of an insertion or a deletion.
Row = collections.namedtuple("Row", "kind left_no left right_no right")

# Below this similarity, two paired lines are unrelated and marking every
# differing character lights up the whole row instead of informing.
_MARK_FLOOR = 0.5

# The palette is defined once per theme as tokens. Every rule below reads tokens
# only, so the page cannot end up with one theme's text on the other's ground.
_LIGHT = """--ground:#f4f6f7;--surface:#ffffff;--ink:#16191f;--muted:#616b76;
--line:#dde3e6;--accent:#0f6f6c;--recessed:#6b747c;
--add-bg:#e7f1e9;--add-ink:#1f5c33;--del-bg:#f8e7e7;--del-ink:#8a2b2b;
--filler:#eef1f2;--mark-add:#a8dcbb;--mark-del:#f0bcbc;--hover:rgba(15,111,108,.10);
--risk-low:#2f6b3f;--risk-med:#8a5a12;--risk-high:#9b2c2c;
--risk-low-bg:#e7f1e9;--risk-med-bg:#f7efdb;--risk-high-bg:#f8e5e5;"""

_DARK = """--ground:#0f1316;--surface:#171c20;--ink:#e2e8ea;--muted:#94a1aa;
--line:#28323a;--accent:#5fbfba;--recessed:#7d8a93;
--add-bg:#16301f;--add-ink:#84d29e;--del-bg:#331a1c;--del-ink:#e59795;
--filler:#12171a;--mark-add:#2c6b45;--mark-del:#6b2c2f;--hover:rgba(95,191,186,.16);
--risk-low:#84d29e;--risk-med:#e0b45f;--risk-high:#ef918e;
--risk-low-bg:#16301f;--risk-med-bg:#332708;--risk-high-bg:#331a1a;"""

_CSS = """
*{box-sizing:border-box}
:root{color-scheme:light dark;__LIGHT__
--sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
--mono:ui-monospace,SFMono-Regular,"Cascadia Mono",Consolas,"Liberation Mono",monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){__DARK__}}
:root[data-theme="dark"]{__DARK__}
body{margin:0;background:var(--ground);color:var(--ink);font:16px/1.6 var(--sans)}
.wrap{max-width:64rem;margin:0 auto;padding:3rem 1.5rem 5rem;
display:flex;flex-direction:column;gap:2.5rem}
h1{margin:0;font:600 1.6rem/1.2 var(--mono);letter-spacing:-.02em;text-wrap:balance}
.eyebrow{margin:0 0 .7rem;font:500 .72rem/1 var(--mono);letter-spacing:.2em;
text-transform:uppercase;color:var(--accent)}
.meta{display:grid;grid-template-columns:auto 1fr;gap:.35rem 1.4rem;margin:1.2rem 0 0;
font:.85rem/1.5 var(--mono);color:var(--ink)}
.meta dt{color:var(--recessed);letter-spacing:.06em}
.meta dd{margin:0;overflow-wrap:anywhere}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(9rem,1fr));
gap:1px;background:var(--line);border:1px solid var(--line)}
.tile{background:var(--surface);padding:1.1rem 1.25rem;display:flex;flex-direction:column;gap:.4rem}
.tile-n{font:600 2.1rem/1 var(--mono);letter-spacing:-.03em;font-variant-numeric:tabular-nums}
.tile-l{font:.72rem/1 var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
.tile[data-kind="real"] .tile-n{color:var(--accent)}
.tile[data-kind="noise"] .tile-n,.tile[data-kind="skipped"] .tile-n{color:var(--recessed)}
h2{margin:0 0 1.1rem;padding-bottom:.6rem;border-bottom:1px solid var(--line);
font:600 .78rem/1 var(--mono);letter-spacing:.16em;text-transform:uppercase;color:var(--muted)}
.panel{background:var(--surface);border:1px solid var(--line);
border-left:3px solid var(--accent);padding:1.3rem 1.5rem}
.panel.alarm{border-left-color:var(--risk-high)}
.panel.alarm code{background:var(--risk-high-bg);color:var(--risk-high)}
.prose p{margin:0 0 .85rem;max-width:68ch}
.prose p:last-child,.prose ul:last-child{margin-bottom:0}
.prose ul{margin:0 0 .85rem;padding-left:1.2rem}
.prose li{margin:.25rem 0;max-width:68ch;overflow-wrap:anywhere}
.prose code{font:.85em var(--mono);background:var(--ground);padding:.1em .35em}
table{width:100%;border-collapse:collapse;font-size:.9rem}
th{text-align:left;padding:0 .8rem .6rem 0;border-bottom:1px solid var(--line);
font:500 .7rem/1 var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
td{padding:.6rem .8rem .6rem 0;border-bottom:1px solid var(--line);vertical-align:middle}
td code{font:.88rem var(--mono);overflow-wrap:anywhere}
td a{color:inherit;text-decoration:none}
td a:hover{text-decoration:underline;color:var(--accent)}
tr[data-status="noise only"] td,tr[data-status="skipped"] td{color:var(--recessed)}
.chip{display:inline-block;padding:.28em .55em;border:1px solid var(--line);
font:.68rem/1 var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
tr[data-status="added"] .chip{color:var(--add-ink);border-color:var(--add-ink)}
tr[data-status="deleted"] .chip{color:var(--del-ink);border-color:var(--del-ink)}
tr[data-status="modified"] .chip,tr[data-status="renamed"] .chip,
tr[data-status="copied"] .chip,tr[data-status="type changed"] .chip{color:var(--accent);border-color:var(--accent)}
.badge{display:inline-block;padding:.32em .6em;font:.68rem/1 var(--mono);
letter-spacing:.1em;text-transform:uppercase}
.risk-low{color:var(--risk-low);background:var(--risk-low-bg)}
.risk-medium{color:var(--risk-med);background:var(--risk-med-bg)}
.risk-high{color:var(--risk-high);background:var(--risk-high-bg)}
.changes{display:flex;flex-direction:column;gap:1.25rem}
.change{background:var(--surface);border:1px solid var(--line);padding:1.3rem 1.5rem;
display:flex;flex-direction:column;gap:1rem}
.change h3{margin:0;font:600 1rem/1.4 var(--mono);display:flex;flex-wrap:wrap;
align-items:center;gap:.65rem;overflow-wrap:anywhere}
details summary{cursor:pointer;padding:.35rem 0;color:var(--muted);
font:.72rem/1 var(--mono);letter-spacing:.14em;text-transform:uppercase}
summary:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
.scroll{overflow-x:auto;border:1px solid var(--line);background:var(--ground);margin-top:.6rem}
table.sbs{width:100%;min-width:34rem;table-layout:fixed;border-collapse:collapse;
font:.8rem/1.6 var(--mono)}
table.sbs th{padding:.45rem .6rem;background:var(--surface);border-bottom:1px solid var(--line);
font:500 .68rem/1 var(--mono);letter-spacing:.12em;text-transform:uppercase;color:var(--muted);
position:sticky;top:0;z-index:2}
table.sbs th:first-child{border-right:1px solid var(--line)}
table.sbs td{padding:.05rem .6rem;vertical-align:top;background:var(--surface);border:0}
table.sbs col.cn{width:3.4rem}
table.sbs td.n{text-align:right;color:var(--recessed);
font-variant-numeric:tabular-nums;-webkit-user-select:none;user-select:none}
table.sbs td.t{white-space:pre-wrap;overflow-wrap:anywhere}
table.sbs td:nth-child(3){border-left:1px solid var(--line)}
.s-chg td:nth-child(2){background:var(--del-bg);color:var(--del-ink)}
.s-chg td:nth-child(4){background:var(--add-bg);color:var(--add-ink)}
.s-del td:nth-child(2){background:var(--del-bg);color:var(--del-ink);border-left:3px solid var(--del-ink)}
.s-add td:nth-child(4){background:var(--add-bg);color:var(--add-ink);border-left:3px solid var(--add-ink)}
.s-del td:nth-child(3),.s-del td:nth-child(4){background:var(--filler)}
.s-add td:nth-child(1),.s-add td:nth-child(2){background:var(--filler)}
.s-gap td{background:var(--ground);color:var(--recessed);text-align:center;
padding:.25rem;letter-spacing:.4em}
mark.w{color:inherit;padding:.05em 0}
.s-chg td:nth-child(2) mark.w{background:var(--mark-del)}
.s-chg td:nth-child(4) mark.w{background:var(--mark-add)}
/* Corresponding lines share a row, so hovering either side lights both. The
   tint is a background-image, which composites over whatever colour the row
   already has instead of replacing it. */
table.sbs tbody tr:not(.s-gap):hover td{
background-image:linear-gradient(var(--hover),var(--hover));
box-shadow:inset 0 1px 0 var(--accent),inset 0 -1px 0 var(--accent)}
table.sbs tbody tr:not(.s-gap):hover td.n{color:var(--ink)}
.empty{margin:0;color:var(--muted)}
.facts{margin:0;padding-left:1.2em}.facts li{margin:.15em 0}.facts code{word-break:break-all}
.fn-head{margin:.6em 0 .3em}pre.code{margin:0;padding:.6em;white-space:pre;font-size:.85em}
@media print{
body{background:#fff;color:#000}
.wrap{max-width:none;padding:0}
.scroll{overflow:visible}
table.sbs{width:100%}
tr{break-inside:avoid}
details{open:true}
}
"""

_STYLE = _CSS.replace("__LIGHT__", _LIGHT).replace("__DARK__", _DARK)


def _xref_parts(xref):
    """Normalize an optional CrossReference into two plain dicts."""
    if xref is None:
        return {}, {}
    return dict(xref.removed), dict(xref.dangling)


def _labels(risk):
    """The labels extraction looks for — configurable, because the prompt is."""
    if not risk:
        return "Risk", "Overall risk"
    return risk.get("file_label", "Risk"), risk.get("summary_label", "Overall risk")


def build_json(dir_a, dir_b, comparison, analyses, summary, xref=None, brief=None,
               risk=None, settings=None, commits=None, intent_shown=False, binary=None,
               functions=None, decompiled=None, max_functions=200):
    """Machine-readable output for the rest of the pipeline (agent, CI, review bot)."""
    file_label, summary_label = _labels(risk)
    files = []
    for rel in sorted(comparison.real):
        analysis = analyses.get(rel)
        files.append(
            {
                "file": rel,
                "status": comparison.real[rel][0],
                "binary": rel in comparison.binaries,
                "binary_detail": _binary_json((binary or {}).get(rel)),
                "binary_functions": _functions_json((functions or {}).get(rel)),
                "risk": _extract_risk(analysis, file_label),
                "analysis": analysis,
                "diff": comparison.sections.get(rel),
            }
        )
    return {
        # 2: added "commits" — the stated intent that came with a git range — and
        # "inputs.intent_shown", which says whether the model was allowed to see it.
        # 3: added "binary_detail" on each real file — sizes, hashes, differing
        # regions and strings for a binary, null otherwise — and "binary_functions",
        # the function-level diff when an engine ran on it.
        "schema_version": 3,
        "generated_at": datetime.datetime.now().replace(microsecond=0).isoformat(),
        "dir_a": dir_a,
        "dir_b": dir_b,
        "llm_ran": bool(analyses or summary),
        "counts": {
            "real": len(comparison.real),
            "noise": len(comparison.noise),
            "skipped": len(comparison.skipped),
        },
        "brief": brief,
        "summary": {"text": summary, "risk": _extract_risk(summary, summary_label)},
        "cross_reference": dict(zip(("removed", "dangling"), _xref_parts(xref))),
        "inputs": {
            "prompts_sha256": settings.digest if settings else None,
            "intent_shown": bool(intent_shown),
        },
        "commits": [dict(item._asdict()) for item in (commits or [])],
        "real": files,
        "noise": sorted(comparison.noise),
        "skipped": sorted(comparison.skipped),
    }


def build_report(dir_a, dir_b, comparison, analyses, summary, xref=None, brief=None, risk=None,
                 commits=None, intent_shown=False, binary=None, functions=None,
                 decompiled=None, max_functions=200):
    """Render the Markdown report."""
    real, noise, skipped = comparison.real, comparison.noise, comparison.skipped
    _, dangling = _xref_parts(xref)
    file_label, summary_label = _labels(risk)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    out = []
    out.append("# Directory comparison report\n")
    out.append("| | |")
    out.append("|---|---|")
    out.append("| Directory A | `%s` |" % dir_a)
    out.append("| Directory B | `%s` |" % dir_b)
    out.append("| Date | %s |" % now)
    out.append("| Real changes | %d |" % len(real))
    out.append("| Noise only | %d |" % len(noise))
    out.append("| Skipped | %d |" % len(skipped))
    if commits:
        out.append("| Commits | %d |" % len(commits))
    out.append("")

    if commits:
        out.append("## Stated intent\n")
        out.append(_INTENT_NOTE_SHOWN if intent_shown else _INTENT_NOTE_WITHHELD)
        out.append("| Commit | Subject | Author |")
        out.append("|---|---|---|")
        for item in commits:
            out.append(
                "| `%s` | %s | %s |" % (item.sha[:9], item.subject or "—", item.author or "—")
            )
        out.append("")
        for item in commits:
            if not item.body:
                continue
            out.append("<details><summary>%s — full message</summary>\n" % item.sha[:9])
            out.append("```")
            out.append(item.body.rstrip("\n"))
            out.append("```\n")
            out.append("</details>\n")

    if brief:
        out.append("## The change as a whole\n")
        out.append(brief)
        out.append("")

    if summary:
        out.append("## Executive summary\n")
        out.append(summary)
        out.append("")

    if dangling:
        out.append("## Removed but still referenced\n")
        out.append(
            "These definitions were deleted, yet the new tree still names them. "
            "Found by search, not by a model — verify each one.\n"
        )
        out.append("| Symbol | Still named in |")
        out.append("|---|---|")
        for symbol in sorted(dangling):
            files = ", ".join("`%s`" % path for path in dangling[symbol])
            out.append("| `%s` | %s |" % (symbol, files))
        out.append("")

    out.append("## Files\n")
    out.append("| File | Status |")
    out.append("|---|---|")
    for rel in sorted(real):
        out.append("| `%s` | %s |" % (rel, _STATUS_LABELS.get(real[rel][0], real[rel][0])))
    for rel in sorted(noise):
        out.append("| `%s` | noise only |" % rel)
    for rel in sorted(skipped):
        out.append("| `%s` | skipped |" % rel)
    if not (real or noise or skipped):
        out.append("| — | no differences |")
    out.append("")

    out.append("## Changes\n")
    if not real:
        out.append("No substantive changes remain after noise filtering.\n")
        return "\n".join(out)

    for rel in sorted(real):
        out.append("### `%s` — %s\n" % (rel, _STATUS_LABELS.get(real[rel][0], real[rel][0])))
        if rel in comparison.binaries:
            detail = (binary or {}).get(rel)
            if detail is None:
                out.append("Binary file — not analyzed, no diff shown.\n")
            else:
                out.extend("- %s" % _md_inline(item) for item in _binary_facts(detail))
                out.append("")
            found = (functions or {}).get(rel)
            if found is not None:
                out.extend(_md_functions(found, max_functions))
            if rel in analyses:
                out.append(analyses[rel])
                out.append("")
            code = (decompiled or {}).get(rel)
            if code:
                out.append("<details><summary>Show decompiled diff</summary>\n")
                out.append("```diff")
                out.append(code.rstrip("\n"))
                out.append("```\n")
                out.append("</details>\n")
            continue
        if rel in analyses:
            out.append(analyses[rel])
            out.append("")
        section = comparison.sections.get(rel)
        if section:
            out.append("<details><summary>Show diff</summary>\n")
            out.append("```diff")
            out.append(section.rstrip("\n"))
            out.append("```\n")
            out.append("</details>\n")
    return "\n".join(out)


def build_html(dir_a, dir_b, comparison, analyses, summary, xref=None, brief=None, risk=None,
               commits=None, intent_shown=False, binary=None, functions=None,
               decompiled=None, max_functions=200):
    """Render the report as one self-contained HTML file."""
    real, noise, skipped = comparison.real, comparison.noise, comparison.skipped
    _, dangling = _xref_parts(xref)
    file_label, summary_label = _labels(risk)
    out = ["<!doctype html>", '<html lang="en">', "<head>", '<meta charset="utf-8">']
    out.append('<meta name="viewport" content="width=device-width,initial-scale=1">')
    out.append("<title>Directory comparison — %s / %s</title>" % (escape(dir_a), escape(dir_b)))
    out.append("<style>%s</style>" % _STYLE)
    out.append("</head>")
    out.append("<body>")
    out.append('<div class="wrap">')

    out.append("<header>")
    out.append('<p class="eyebrow">MaNishtana report</p>')
    out.append("<h1>Directory comparison</h1>")
    out.append('<dl class="meta">')
    out.append("<dt>Directory A</dt><dd>%s</dd>" % escape(dir_a))
    out.append("<dt>Directory B</dt><dd>%s</dd>" % escape(dir_b))
    out.append(
        "<dt>Generated</dt><dd>%s</dd>" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    )
    out.append("</dl></header>")

    out.append('<div class="tiles">')
    for kind, label, count in (
        ("real", "Real changes", len(real)),
        ("noise", "Noise only", len(noise)),
        ("skipped", "Skipped", len(skipped)),
    ):
        out.append(
            '<div class="tile" data-kind="%s" data-count="%d">'
            '<span class="tile-n">%d</span><span class="tile-l">%s</span></div>'
            % (kind, count, count, label)
        )
    out.append("</div>")

    if commits:
        out.append('<section id="intent"><h2>Stated intent</h2>')
        out.append('<div class="panel prose">')
        out.append("<p>%s</p>" % _inline(
            _INTENT_NOTE_SHOWN if intent_shown else _INTENT_NOTE_WITHHELD
        ).strip())
        out.append("<table><thead><tr><th>Commit</th><th>Subject</th><th>Author</th>"
                   "</tr></thead><tbody>")
        for item in commits:
            out.append(
                "<tr><td><code>%s</code></td><td>%s</td><td>%s</td></tr>"
                % (escape(item.sha[:9]), escape(item.subject or "—"), escape(item.author or "—"))
            )
        out.append("</tbody></table>")
        for item in commits:
            if not item.body:
                continue
            out.append(
                "<details><summary>%s — full message</summary><pre>%s</pre></details>"
                % (escape(item.sha[:9]), escape(item.body.rstrip("\n")))
            )
        out.append("</div></section>")

    if brief:
        out.append('<section id="brief"><h2>The change as a whole</h2>')
        out.append('<div class="panel prose">%s</div></section>' % _html_prose(brief))

    if summary:
        out.append('<section id="summary"><h2>Executive summary</h2>')
        out.append('<div class="panel prose">')
        badge = _risk_badge(summary, summary_label)
        if badge:
            out.append("<p>%s</p>" % badge)
        out.append(_html_prose(summary))
        out.append("</div></section>")

    if dangling:
        out.append('<section id="xref"><h2>Removed but still referenced</h2>')
        out.append('<div class="panel alarm">')
        out.append(
            "<p>These definitions were deleted, yet the new tree still names them. "
            "Found by search, not by a model — verify each one.</p>"
        )
        out.append("<table><thead><tr><th>Symbol</th><th>Still named in</th>"
                   "</tr></thead><tbody>")
        for symbol in sorted(dangling):
            files = " ".join("<code>%s</code>" % escape(p) for p in dangling[symbol])
            out.append("<tr><td><code>%s</code></td><td>%s</td></tr>" % (escape(symbol), files))
        out.append("</tbody></table></div></section>")

    out.append("<section><h2>Files</h2>")
    out.append("<table><thead><tr><th>File</th><th>Status</th></tr></thead><tbody>")
    for rel in sorted(real):
        out.append(_file_row(rel, _STATUS_LABELS.get(real[rel][0], real[rel][0]), is_real=True))
    for rel in sorted(noise):
        out.append(_file_row(rel, "noise only"))
    for rel in sorted(skipped):
        out.append(_file_row(rel, "skipped"))
    if not (real or noise or skipped):
        out.append('<tr><td colspan="2" class="empty">no differences</td></tr>')
    out.append("</tbody></table></section>")

    out.append("<section><h2>Changes</h2>")
    if not real:
        out.append('<p class="empty">No substantive changes remain after noise filtering.</p>')
    else:
        out.append('<div class="changes">')
        for rel in sorted(real):
            out.append(_change_block(rel, comparison, analyses, real, dir_a, dir_b, file_label,
                                     (binary or {}).get(rel), (functions or {}).get(rel),
                                     (decompiled or {}).get(rel), max_functions))
        out.append("</div>")
    out.append("</section>")

    out.append("</div></body></html>")
    return "\n".join(out) + "\n"


# --- internals ---------------------------------------------------------------


def _file_row(rel, status, is_real=False):
    code_text = "<code>%s</code>" % escape(rel)
    if is_real:
        code_text = '<a href="#file-%s">%s</a>' % (escape(rel), code_text)
    return '<tr data-status="%s"><td>%s</td><td><span class="chip">%s</span></td></tr>' % (
        escape(status),
        code_text,
        escape(status),
    )


def _change_block(rel, comparison, analyses, real, dir_a, dir_b, file_label="Risk",
                  binary=None, functions=None, decompiled=None, max_functions=200):
    status = _STATUS_LABELS.get(real[rel][0], real[rel][0])
    parts = ['<article class="change" id="file-%s">' % escape(rel)]
    heading = ["<h3><code>%s</code>" % escape(rel), '<span class="chip">%s</span>' % escape(status)]
    badge = _risk_badge(analyses.get(rel), file_label)
    if badge:
        heading.append(badge)
    parts.append("".join(heading) + "</h3>")
    if rel in comparison.binaries:
        if binary is None:
            parts.append('<p class="empty">Binary file — not analyzed, no diff shown.</p>')
        else:
            parts.append('<ul class="facts">%s</ul>' % "".join(
                "<li>%s</li>" % _html_inline(item) for item in _binary_facts(binary)))
            if functions is not None:
                parts.append(_html_functions(functions, max_functions))
            if rel in analyses:
                parts.append('<div class="prose">%s</div>' % _html_prose(analyses[rel]))
            if decompiled:
                parts.append("<details><summary>Decompiled diff</summary>")
                parts.append('<div class="scroll"><pre class="code">%s</pre></div>'
                             % escape(decompiled))
                parts.append("</details>")
        return "\n".join(parts) + "\n</article>"
    if rel in analyses:
        parts.append('<div class="prose">%s</div>' % _html_prose(analyses[rel]))
    section = comparison.sections.get(rel)
    table = _html_side_by_side(section, dir_a, dir_b) if section else ""
    if table:
        parts.append("<details open><summary>Side-by-side comparison</summary>")
        parts.append('<div class="scroll">%s</div>' % table)
        parts.append("</details>")
    elif section:
        # git emits no @@ for a pure rename, so there is nothing to align. An
        # empty disclosure would drop the only fact the reviewer needs.
        parts.append('<p class="empty">%s</p>' % _metadata_only_note(rel, real))
    return "\n".join(parts) + "\n</article>"


# A fact is a list of (text, is_code) runs, so Markdown and HTML each render the
# code spans their own way from one source of wording.
def _binary_facts(summary):
    """What layer 1 found about a binary that still differs, as facts."""
    kind = {"pe": "PE (Windows)", "elf": "ELF (Linux/Unix)"}.get(summary.format,
                                                                 "unrecognized format")
    facts = [
        [("Binary file, %s." % kind, False)],
        [("Size: %s" % _size_change(summary.size_a, summary.size_b), False)],
        [("SHA-256: ", False), (_short(summary.sha_a), True), (" → ", False),
         (_short(summary.sha_b), True)],
    ]
    if summary.regions_total:
        shown = ", ".join("0x%X–0x%X" % (start, stop) for start, stop in summary.regions)
        more = summary.regions_total > len(summary.regions)
        facts.append([("Differing regions: %d (%s%s)"
                       % (summary.regions_total, shown, ", …" if more else ""), False)])
    for label, items, total in (
        ("Strings added", summary.strings_added, summary.strings_added_total),
        ("Strings removed", summary.strings_removed, summary.strings_removed_total),
    ):
        if not total:
            continue
        runs = [("%s (%d): " % (label, total), False)]
        for index, item in enumerate(items):
            if index:
                runs.append((", ", False))
            runs.append((item, True))
        if total > len(items):
            runs.append((", …", False))
        facts.append(runs)
    return facts


def _size_change(size_a, size_b):
    if size_a is None:
        return "added, %s bytes" % format(size_b or 0, ",")
    if size_b is None:
        return "deleted, was %s bytes" % format(size_a, ",")
    delta = size_b - size_a
    return "%s → %s bytes (%s%s)" % (format(size_a, ","), format(size_b, ","),
                                     "+" if delta >= 0 else "−", format(abs(delta), ","))


def _short(sha):
    return sha[:16] if sha else "—"


def _md_inline(runs):
    """Runs to Markdown. A string holding a backtick gets a double-backtick span."""
    out = []
    for text, is_code in runs:
        if not is_code:
            out.append(text)
        elif "`" in text:
            out.append("`` %s ``" % text)
        else:
            out.append("`%s`" % text)
    return "".join(out)


def _html_inline(runs):
    return "".join("<code>%s</code>" % escape(text) if is_code else escape(text)
                   for text, is_code in runs)


def _function_rows(found, limit):
    """(rows, hidden count). Each row: name, status, similarity text, engines text, disputed."""
    rows = []
    for change in found.changes[:limit]:
        if change.name_a and change.name_b and change.name_a != change.name_b:
            name = "%s → %s" % (change.name_a, change.name_b)
        else:
            name = change.name_a or change.name_b or "?"
        similarity = "—" if change.similarity is None else "%.2f" % change.similarity
        disputed = len(set(change.views.values())) > 1
        if disputed:
            engines = ", ".join("%s: %s" % item for item in sorted(change.views.items()))
        else:
            engines = ", ".join(sorted(change.views))
        rows.append((name, change.status, similarity, engines, disputed))
    return rows, max(0, len(found.changes) - limit)


def _function_counts(found):
    counts = collections.Counter(change.status for change in found.changes)
    return ", ".join("%d %s" % (counts[key], key) for key in ("modified", "added", "removed")
                     if counts[key]) or "no function changed"


def _md_functions(found, limit):
    out = ["**Function-level diff** (%s): %s." % (", ".join(found.engines),
                                                   _function_counts(found)), ""]
    if not found.changes:
        out.append("The difference is outside the functions the engines matched — data, "
                   "resources or layout.\n")
        return out
    rows, hidden = _function_rows(found, limit)
    out.append("| Function | Status | Similarity | Engines |")
    out.append("|---|---|---|---|")
    for name, status, similarity, engines, disputed in rows:
        out.append("| `%s` | %s%s | %s | %s |" % (name.replace("|", "\\|"), status,
                                               " ⚠ engines disagree" if disputed else "",
                                               similarity, engines))
    if hidden:
        out.append("| … | %d more not listed | | |" % hidden)
    out.append("")
    return out


def _html_functions(found, limit):
    parts = ['<p class="fn-head"><strong>Function-level diff</strong> (%s): %s.</p>'
             % (escape(", ".join(found.engines)), escape(_function_counts(found)))]
    if not found.changes:
        parts.append('<p class="empty">The difference is outside the functions the engines '
                     'matched — data, resources or layout.</p>')
        return "\n".join(parts)
    rows, hidden = _function_rows(found, limit)
    parts.append("<table class=\"functions\"><thead><tr><th>Function</th><th>Status</th>"
                 "<th>Similarity</th><th>Engines</th></tr></thead><tbody>")
    for name, status, similarity, engines, disputed in rows:
        flag = ' <span class="chip">engines disagree</span>' if disputed else ""
        parts.append('<tr data-status="%s"><td><code>%s</code></td><td>%s%s</td><td>%s</td>'
                     "<td>%s</td></tr>" % (escape(status), escape(name), escape(status), flag,
                                           escape(similarity), escape(engines)))
    if hidden:
        parts.append('<tr><td colspan="4" class="empty">%d more not listed</td></tr>' % hidden)
    parts.append("</tbody></table>")
    return "\n".join(parts)


def _functions_json(found):
    if found is None:
        return None
    return {
        "engines": list(found.engines),
        "changes": [
            {
                "name_a": change.name_a,
                "name_b": change.name_b,
                "status": change.status,
                "similarity": change.similarity,
                "views": dict(change.views),
                "pseudo_diff": change.pseudo_diff,
            }
            for change in found.changes
        ],
    }


def _binary_json(summary):
    return dict(summary._asdict()) if summary is not None else None


def _metadata_only_note(rel, real):
    """Explain a change that produced no comparable lines."""
    status, paths = real[rel]
    if status in ("R", "C") and len(paths) > 1:
        verb = "Renamed" if status == "R" else "Copied"
        return "%s from <code>%s</code> — no content change after normalization." % (
            verb,
            escape(paths[0]),
        )
    return "No line changes after normalization — the difference is in file metadata only."


def _side_by_side(section):
    """Align a unified diff into left/right rows, the way a two-pane tool shows it.

    A run of deleted lines meeting a run of added lines is paired positionally, so
    an edited line carries its old and new text on one row; whatever is left over
    gets a blank cell opposite it. Line numbers come from the hunk header, which
    is also where a gap row goes -- the reader needs to see that lines were skipped.
    """
    rows, dels, adds = [], [], []
    left_no = right_no = 0
    in_hunk = False

    def flush():
        for index in range(max(len(dels), len(adds))):
            old = dels[index] if index < len(dels) else None
            new = adds[index] if index < len(adds) else None
            if old and new:
                rows.append(Row("chg", old[0], old[1], new[0], new[1]))
            elif old:
                rows.append(Row("del", old[0], old[1], None, None))
            else:
                rows.append(Row("add", None, None, new[0], new[1]))
        del dels[:]
        del adds[:]

    for line in section.rstrip("\n").split("\n"):
        match = _HUNK_RE.match(line)
        if match:
            flush()
            if rows:
                rows.append(Row("gap", None, None, None, None))
            left_no, right_no = int(match.group(1)), int(match.group(2))
            in_hunk = True
            continue
        if not in_hunk or line.startswith("\\"):
            continue
        if line.startswith("-"):
            dels.append((left_no, line[1:]))
            left_no += 1
        elif line.startswith("+"):
            adds.append((right_no, line[1:]))
            right_no += 1
        else:
            flush()
            text = line[1:] if line.startswith(" ") else line
            rows.append(Row("ctx", left_no, text, right_no, text))
            left_no += 1
            right_no += 1
    flush()
    return rows


def _mark_pair(left, right):
    """Escape both sides, marking the characters that differ when they are related.

    Segments are escaped individually so a mark can never be inserted inside an
    HTML entity.
    """
    matcher = difflib.SequenceMatcher(None, left, right)
    if matcher.ratio() < _MARK_FLOOR:
        return escape(left), escape(right)
    old, new = [], []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            old.append(escape(left[i1:i2]))
            new.append(escape(right[j1:j2]))
            continue
        if i1 != i2:
            old.append('<mark class="w">%s</mark>' % escape(left[i1:i2]))
        if j1 != j2:
            new.append('<mark class="w">%s</mark>' % escape(right[j1:j2]))
    return "".join(old), "".join(new)


def _html_side_by_side(section, dir_a, dir_b):
    """Render the aligned rows as one table, so both panes cannot drift apart."""
    rows = _side_by_side(section)
    if not rows:
        return ""
    out = ['<table class="sbs">']
    # table-layout:fixed takes its widths from the first row, and those cells each
    # span two columns, so the gutter width has to be declared here instead.
    out.append('<colgroup><col class="cn"><col><col class="cn"><col></colgroup>')
    out.append(
        '<thead><tr><th colspan="2">%s</th><th colspan="2">%s</th></tr></thead><tbody>'
        % (escape(dir_a), escape(dir_b))
    )
    for row in rows:
        if row.kind == "gap":
            out.append('<tr class="s-gap"><td colspan="4">&#8943;</td></tr>')
            continue
        if row.kind == "chg":
            left, right = _mark_pair(row.left, row.right)
        else:
            left = escape(row.left) if row.left is not None else ""
            right = escape(row.right) if row.right is not None else ""
        out.append(
            '<tr class="s-%s"><td class="n">%s</td><td class="t">%s</td>'
            '<td class="n">%s</td><td class="t">%s</td></tr>'
            % (
                row.kind,
                "" if row.left_no is None else row.left_no,
                left,
                "" if row.right_no is None else row.right_no,
                right,
            )
        )
    out.append("</tbody></table>")
    return "\n".join(out)


def _html_prose(text):
    """Render the model's answer: paragraphs, '- ' bullets, and inline marks."""
    blocks, items = [], []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("- "):
            items.append("<li>%s</li>" % _inline(stripped[2:]))
            continue
        if items:
            blocks.append("<ul>%s</ul>" % "".join(items))
            items = []
        if not stripped:
            continue
        # llm.py writes "#### Part 1 of 2" itself when it splits a long diff.
        heading = re.match(r"#{1,6}\s+(.*)$", stripped)
        if heading:
            blocks.append("<h4>%s</h4>" % _inline(heading.group(1)))
            continue
        blocks.append("<p>%s</p>" % _inline(stripped))
    if items:
        blocks.append("<ul>%s</ul>" % "".join(items))
    return "\n".join(blocks)


def _inline(text):
    """Escape first, then apply the few inline marks the model actually emits.

    The italic rule demands a non-word character on both sides, so identifiers
    like SAMPLE_COUNT ... SAMPLE_COUNT are never read as an italic span.
    """
    out = escape(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"(?<![\w`])_([^_\n]+)_(?!\w)", r"<em>\1</em>", out)
    return out


def _risk_badge(text, label):
    """A badge only when a level could actually be extracted — never a guess."""
    risk = _extract_risk(text, label)
    if not risk:
        return ""
    return '<span class="badge risk-%s">%s</span>' % (risk, risk.capitalize())


def _extract_risk(text, label):
    """Pull a risk level out of the model's free-form answer, or return None.

    Best effort by design. The label must introduce a field — "**Risk** — Low",
    "Risk: High" — because review prose names levels without declaring one:
    "reduces the risk of a high reading" would otherwise report High. A line
    echoing the whole template (all three levels) is rejected; otherwise the
    first level named after the label wins. When a diff was split into parts,
    the most severe level across parts wins.
    """
    if not text:
        return None
    field = re.compile(re.escape(label.lower()) + r"[*_`\s]*[:\-–—]")
    best = None
    for line in text.split("\n"):
        lowered = line.lower()
        match = field.search(lowered)
        if match is None:
            continue
        found = _RISK_RE.findall(lowered[match.end():])
        if not found or len(set(found)) == len(_RISK_ORDER):
            continue
        level = found[0]
        if best is None or _RISK_ORDER.index(level) > _RISK_ORDER.index(best):
            best = level
    return best

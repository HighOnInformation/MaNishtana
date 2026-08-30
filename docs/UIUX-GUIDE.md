# MaNishtana — UI/UX guide of use

How to use this tool, how to read what it produces, and how to extend it without
breaking the one idea it is built around.

`README.md` is the reference: every flag, every config field, every JSON key. This
guide is the *interpretation* — what each thing means and what to do about it.

Citations name files and symbols rather than line numbers, because line numbers rot
on the next edit. `rules.py::_matching_rule` means that function in that module.

---

## 1. Who this is for, and the three flows

You receive a source drop on a closed network and must answer one question: **the
vendor sent us v2 — what actually changed versus v1, and what should a reviewer look
at?** No internet, no pip; the tool arrives as a copied directory.

Everything except the prose is deterministic. The comparison, the classification, the
diffs, the pane alignment, the character marking and the hover pairing all work with
`--no-llm`. The model adds per-file analysis, the executive summary, and the risk
badges — which are regex-extracted from that prose, so they vanish without it.

### Flow A — first run on a new box

Prove the box before you need it. Each step catches a different failure.

```bash
python3 --version                        # 3.8+
git --version                            # 2.30+, needs diff --no-index
python3 -m manishtana --help                # proves the package copied whole
clang-format --version                   # optional normalizer
gcc --version                            # optional, powers strip-comments
python3 -m unittest discover -s tests    # 179 tests, no git or network needed
python3 -m manishtana v1 v2 -c config.json -o /tmp/smoke.md --no-llm 2> /tmp/smoke.warn
```

Read `/tmp/smoke.warn` first. If it names a missing tool, every later report will
count formatting as substantive change — decide that knowingly, not by accident.

`git` is the one dependency with no graceful failure. `gitdiff.py::_git` does not
catch `OSError`, so a missing git gives a traceback and exit 1, not a diagnostic.
That is why `git --version` is step two.

### Flow B — routine review of a drop

Run from wherever the drop lives, with the package on `PYTHONPATH`. Pass **short**
directory arguments: those strings become the side-by-side pane headers verbatim, and
long absolute paths bloat every pane.

```bash
cd /srv/drops/acme
PYTHONPATH=/opt/manishtana python3 -m manishtana v1 v2 \
    -c acme.json -o v2-review.md -H v2-review.html -j v2-review.json \
    2> v2-review.warnings
```

Reading order, every time:

1. `v2-review.warnings` — decide whether the classification can be trusted at all (§5).
2. The one stdout line — the three counts are a rule-health check.
3. `v2-review.html` — tiles, then the summary badge, then per-file badges, then panes.
4. `v2-review.json` only if a bot or script consumes it. Never feed the HTML to a model.

### Flow C — triage of a large drop

Two passes. The first is free — no model calls — and exists to tune the rules until
the noise column absorbs the churn.

```bash
python3 -m manishtana v1 v2 -c acme.json -o pass1.md --no-llm 2> pass1.warnings
# edit acme.json, repeat, until "real changes" is a number a human can read
python3 -m manishtana v1 v2 -c acme.json -o review.md -H review.html -j review.json \
    2> review.warnings
```

Watch the file cap. `llm.max_files` defaults to 50; past it, files are dropped from
analysis after one stderr warning. Their diffs still appear, without prose. Raise the
cap or split the run by subtree — do not accept a partially analysed report silently.

There is no risk column in the files table, so sort the JSON to decide where to start:

```bash
python3 -c "import json;d=json.load(open('review.json'));\
print('\n'.join('%-6s %s'%(f['risk'],f['file']) for f in \
sorted(d['real'],key=lambda f:{'high':0,'medium':1,'low':2}.get(f['risk'],3))))"
```

---

## 2. The command line as an interface

```
usage: manishtana [-h] [-c CONFIG] [-o OUTPUT] [-H PATH] [-j PATH] [--no-llm]
               DIR_A DIR_B
```

| Flag | Default | Why that default |
|---|---|---|
| `DIR_A` | — | The old version. Order is meaning: A is the left pane and the `-` side of every diff. Trailing slashes are stripped, so tab-completion is safe |
| `DIR_B` | — | The new version |
| `-c`, `--config` | `config.json` | The rules are the real interface and ship beside the package, so the common invocation stays short. Read as `utf-8-sig`, so a BOM from a Windows editor is tolerated |
| `-o`, `--output` | `report.md` | Markdown is the only always-produced artifact — there is no "no report" mode. It is also what survives being pasted into a ticket |
| `-H`, `--html` | off | Needs an explicit path; nothing derives a filename from `-o`. The HTML is a second artifact for a human, so it is opt-in |
| `-j`, `--json` | off | Same shape. JSON is for machines; producing it unasked would litter the drop |
| `--no-llm` | off | The default assumes an endpoint. With no `base_url` you get the same result plus a warning, so `--no-llm` means "do not try, and do not warn me" |

Two things you cannot configure: diff context is fixed at three lines
(`--unified=3`) and rename detection is always on (`-M`).

### stdout versus stderr

Exactly one line reaches stdout, only on success:

```
Report written to v2-review.md — 3 real changes, 0 noise only, 1 skipped.
```

Everything else goes to stderr with a `Warning: ` or `Error: ` prefix. Both streams
are forced to UTF-8, which is what keeps that em dash from breaking a Windows console.

That line is written **last**, after every requested file, so its presence is a real
"all outputs exist" signal. The three counts are the same numbers as the HTML tiles
and the JSON `counts`, which makes them a cheap check on your rules:

- `real` unexpectedly high on a quiet drop → your rules are not matching, or a
  normalizer is missing. A file matched by **no** rule is compared raw.
- `noise only` at 0 when you configured normalizers → same suspicion; confirm in the
  warnings.
- `skipped` counts only skipped files **that differ**. A skipped file identical on
  both sides is invisible, so "skipped: 1" does not mean one file matched your skip
  rules.

There is no pluralisation, so a single change reads `1 real changes`.

### Exit codes

| Code | When |
|---|---|
| `0` | The report and every requested extra were written. **Returned even when warnings fired** — a degraded run and a clean run are indistinguishable by exit code |
| `2` | A directory argument is not a directory; the config is missing or invalid JSON; any output write failed; argparse usage error |
| `1` | Only from an uncaught exception — notably `git` absent, or `copytree` failing. You get a traceback, not a message |

The comparison and every model call run *before* the first write, so a bad `-o` path
wastes the whole run. Check output directories first.

---

## 3. Reading the report

The visual language has one organising idea: **signal is coloured, noise is recessed,
and nothing is ever guessed.** Learn three registers and the page reads itself.

| Register | Token | Where |
|---|---|---|
| Signal | `--accent` teal | The real-change count, the summary panel's left border, chips for real statuses, hover, focus rings |
| Recessed | `--recessed` grey | Noise and skipped tile numbers and rows, line-number gutters, the gap glyph |
| Change | `--add-*` / `--del-*` / `--mark-*` | Diff cells only, never chrome |

**Header.** An accent eyebrow, the title, then Directory A, Directory B and the
timestamp. *Do:* check the two directory strings first — a swapped A/B inverts the
meaning of every colour on the page.

### Stat tiles

Three tiles reading as one bordered block, stacking on a narrow window. **Real** is
accent-coloured; **noise only** and **skipped** are recessed — the count you must act
on is the only one with colour. Each carries `data-kind` and `data-count`, guaranteed
by test, but the JSON is the supported machine surface.

Precisely: `real` = differs after normalization, including every file no rule matched.
`noise` = differed before normalization, identical after. `skipped` = matched a `skip`
rule *and* differed.

*Do:* read the pair (real, noise) as a verdict on your **config**, not only on the
drop. Forty real and zero noise, on a codebase you know reformats every release, means
your rules are not being applied.

### Files table

File and Status, grouped real → noise → skipped, each group sorted independently.
Noise and skipped rows are recessed on every cell — deliberately the quietest text on
the page, though still above the 4.5:1 contrast floor.

*Do:* use it as the inventory, not as navigation — rows are not links. Scan the noise
group for anything that surprises you. **A file you expected to change sitting in
"noise only" is the highest-value finding on the page**, because it means either the
vendor changed nothing substantive or one of your `ignore_lines` patterns ate the
change (§4).

### Status chips

| Chip | Colour | git status |
|---|---|---|
| `added` | green | `A` |
| `deleted` | red | `D` |
| `modified` / `renamed` / `copied` | accent | `M` / `R` / `C` |
| `type changed` | grey | `T` — labelled but given no colour |
| `noise only` / `skipped` | grey | not git statuses |

*Do:* read `added` and `deleted` as a pair when both appear — normalization raises
similarity, so the tool may pair files into a `renamed` that the raw pass saw as an
add plus a delete. A grey `type changed` chip looks ignorable and is not; treat it as
`modified`.

### Risk badges, and their absence

A solid pill: green `Low`, amber `Medium`, red `High`. Two placements only — the top
of the executive summary panel (from the `**Overall risk**` line) and inline in each
change block's heading (from that file's `**Risk**` line).

A badge exists only when a level could be extracted from prose. Extraction requires
the label to **introduce a field** (`**Risk** — High`, `Risk: High`); a line naming all
three levels is rejected as a template echo; across split parts the most severe wins.
Ordinary prose such as "reduces the risk of a high reading" declares nothing.

*Do:* treat a badge as evidence and its absence as **no information**. Absence means
one of: `--no-llm`, no endpoint, a failed call, a binary, a file past the cap, or
different phrasing. **It never means Low.** If a file matters and has no badge, read
its diff yourself.

### Change block and disclosure

A bordered card per real change: filename, status chip, risk badge if any, then the
prose, then the comparison. The HTML disclosure is `<details open>` — **expanded by
default**, keyboard-focusable. The Markdown disclosure starts closed.

*Do:* expect the whole drop laid open in one HTML scroll; there is no collapse-all. If
that is unusable on a large drop, review from the Markdown.

Two short-circuits replace the comparison with one line:

- **Binary** — `Binary file — not analyzed, no diff shown.` Detected by a NUL byte in
  the first 8KB of the *original*. Never sent to the model. *Do:* compare binaries
  out of band; the report will never say what changed inside one.
- **Metadata only** — `Renamed from old.c — no content change after normalization.`
  git emits no hunks for a pure rename, so there is nothing to align.

### The side-by-side panes

One table inside a horizontally scrolling box with a `min-width` floor — below that
width two panes are unreadable, so it scrolls rather than crushing. The header names
Directory A and Directory B, split by a spine that continues down the middle. Column
widths come from a `<colgroup>`, because `table-layout:fixed` takes its widths from
the first row and those cells each span two columns.

Four cells per row: left number, left text, right number, right text. Number cells are
`user-select:none`, so copying does not drag numbers along. Text cells wrap, and a long
line **grows the shared row** — which is the whole reason both panes live in one table.
They cannot drift out of alignment, ever.

Line numbers per side are independent. After an insertion the columns diverge, and
that divergence is information: it tells you how far the files have moved apart.

**The numbers and the text are from the normalized copies, not the files on disk.**
The pane headers say `v1` and `v2` and nothing on the page corrects them. Use the panes
to understand the change; find it in the original **by symbol name, never by line
number**.

#### The four row kinds, plus the gap

| Kind | Looks like | Means | Do |
|---|---|---|---|
| Context | Both halves plain, both numbers, identical text | Unchanged — three lines each side of every change | Orient yourself. A context line reading `<<ignored-by-rule>>` is your own `ignore_lines` token, not corruption |
| Changed | Left red, right green, both numbers, gutters neutral | One edited line, old left and new right, paired positionally | Read the character marks. A coloured row with **no** marks scored below the similarity floor — the pairing is arbitrary and you must verify against the file |
| Added | Right green; the whole left half drops to filler; left number blank | An insertion; the blank half holds alignment | Read it as new code with no predecessor |
| Deleted | Left red; the whole right half drops to filler; right number blank | A deletion | Ask what depended on the removed line — the report will not |
| Gap | One cell across all four columns, a centred `⋯`, excluded from hover | Lines between hunks were not shown | Remember unshown code exists. If a change looks incomplete, open the file |

Filler is a shade off the surface in both themes, so a blank half reads as
*structurally absent*, not as empty content.

#### Character marking and its threshold

Inside a changed row, only the characters that differ are marked — background only, so
the row's red or green survives. Marking is applied **only when the two lines score at
least 0.5 similarity** (`difflib.SequenceMatcher`). Below that they are unrelated, and
marking every difference would light the whole row instead of informing. Each segment
is escaped separately, so a mark can never land inside an HTML entity.

*Do:* trust marks as a precision tool — `CAL_OFFSET 12` against `18` marks just the
digits. Treat an unmarked coloured pair as a signal to slow down.

#### Hover pairing

Hover any non-gap row: a tint composited **over** the row's existing colour (so a
hovered insertion still reads green), accent hairlines above and below across both
panes, and the line numbers brightening. Counterparts share a `<tr>`, so one CSS rule
lights both panes and there is no JavaScript anywhere in the report.

Hovering an insertion or deletion lights the filler half opposite — the view saying
*this line has no counterpart*.

*Do:* use it to confirm which right-hand line belongs to which left-hand line on a
wrapped row. Do not depend on it: it is pointer-only, so it does not exist on touch,
keyboard, or paper. The colouring and the two number columns carry the same
information.

### Empty states

`no differences` in the files table when all three sets are empty; `No substantive
changes remain after noise filtering.` in the Changes section when nothing is real. A
blank region is never allowed to stand in for a state.

### Which artifact for whom

| Artifact | Audience | Rule |
|---|---|---|
| `-o` Markdown | You, a ticket, a terminal | Unified diff, disclosures collapsed |
| `-H` HTML | A human reviewer, offline, possibly on another machine | Self-contained: one inlined `<style>`, no URL, `<link>`, `<img>`, `src=` or `@import` — enforced by test. Opens from `file://`. **Never feed it to a model** |
| `-j` JSON | Agents, CI, review bots | `schema_version: 2`. `risk` and `analysis` may be `null`; `llm_ran` says whether prose was produced at all |

---

## 4. Writing the config

Rule authoring is where a silently wrong report comes from. Internalise one sentence:
**each file gets exactly one rule — the first whose pattern matches — and there is no
negation and no merging.**

Patterns are `fnmatch`, tested against the forward-slash relative path **and** the
basename. Three consequences:

- `*` crosses `/`. `*.c` matches `src/deep/a.c` (intended); `src/*` matches
  `src/a/b/c.c` (often not).
- Matching is **case-insensitive on Windows and case-sensitive on Linux**, because
  `fnmatch` normalises case. The same config behaves differently on the two platforms
  the tool supports. Write patterns in the real case, and list both forms if a drop is
  inconsistent.
- Write patterns with forward slashes only.

### What a rule can do

| Field | Effect | Cost |
|---|---|---|
| `skip: true` | The file is deleted from both copies before the second diff | Total blindness — the report can never say what changed inside it |
| `normalize: [...]` | Ordered commands, stdin to stdout, 60s each | Depends on external tools; degrades to raw text on any failure |
| `ignore_lines: [...]` | Every matching line becomes `<<ignored-by-rule>>` on both sides, line count preserved | The whole line is destroyed, on both sides |

`json-sort` runs the **current interpreter**, so it never depends on a `python3` on
`PATH`. Any other string runs as a shell command as written, and `{name}` is
substituted with the **shell-quoted** basename — do not quote it yourself. That
quoting exists because filenames arrive from someone else's source tree into a
`shell=True` command line.

Use `skip` for files with no reviewable content (`*.o`, `*.log`, maps, blobs). Use
`ignore_lines` when the file matters but carries specific generated lines. Prefer a
`normalize` step over `ignore_lines` where a tool can do the job: a normalizer rewrites
form, while `ignore_lines` destroys content.

### The mistakes that produce a silently wrong result

Ranked by damage.

1. **An unanchored `ignore_lines` regex hides a real change.** Patterns are applied
   with `search`, not a full match. With `"ignore_lines": ["BUILD_NUMBER"]`, a change
   from `if (BUILD_NUMBER > 3) { arm_actuator(); }` to `> 9` collapses to the token on
   both sides and the file is reported as **noise only** — it never appears in the
   Changes section. Always anchor, always add `\b`, never write a pattern that could
   match executable code: `"^\\s*#define\\s+BUILD_NUMBER\\b"`.
2. **A general rule before a specific one turns normalization off.** First match wins,
   so `[{"match":["*"]}, {"match":["*.c"], …}]` means the second rule never runs. Order
   skips first, then per-language rules. Never write a `{"match": ["*"]}` rule.
3. **A rule with `match` but no action is a hole.** With no `skip`, `normalize` or
   `ignore_lines`, the file matches, nothing happens, and every later rule is blocked.
   `{"match": ["*.c"]}` is not a no-op.
4. **An extension covered by no rule is compared raw**, so every reformatted brace is
   a real change. Enumerate the drop's extensions first; `*.cc`, `*.inc`, `*.S` and
   `*.mk` are the usual omissions.
5. **Backslashes must be doubled for JSON.** `"^\s*"` is invalid JSON and aborts with
   exit 2. This is the one mistake that fails loudly.
6. **`strip-comments` is a C preprocessor invocation** (`gcc -x c`) while the shipped
   rule also matches `*.cpp` and `*.hpp`. Check the result on real C++ before trusting
   the classification.
7. **Order within `normalize` matters.** `["strip-comments", "clang-format"]` strips
   first, so the formatter never reflows text about to be deleted.

### Verification loop

After every config edit, run `--no-llm` — free, deterministic, seconds — and check:

1. The counts moved the way you intended.
2. A file you *expect* to be noise is listed as `noise only`.
3. A file you *know* has a substantive change is still real, **and its change is still
   visible in the diff**. This is the only check that catches mistake 1.

---

## 5. Warnings and failure states

Every non-fatal problem goes to stderr prefixed `Warning: `. The run continues and
**the exit code stays 0**. Nothing about a degraded run appears in the Markdown, HTML
or JSON — so capture stderr next to the report, always.

| Warning | What it means for trust |
|---|---|
| `tool 'gcc' is not installed — normalize step 'strip-comments' skipped, comparison continues without it.` (once per tool) | **Classification is inflated.** Comment and formatting churn now counts as real change |
| `normalize step '<step>' failed for <file> (exit <n>) — keeping the original text. <detail>` | That file compared raw. A flood means the normalizer is wrong for this drop |
| `normalize step '<step>' exceeded 60s for <file> — keeping the original text.` | Same. Usually a huge generated file that should carry `skip` |
| `<file> is not valid UTF-8 (<detail>) — comparing it un-normalized.` | Deliberate: decoding lossily could make two genuinely different files look identical and hide a real change as noise. Its formatting differences now count as real |
| `cannot read <file>: <detail>` | That copy left as-is; same inflation |
| `cannot rewrite <file>: <detail> — comparing it un-normalized.` | `copytree` preserves the read-only bit, so a drop off an ISO lands read-only. Clear the attribute and rerun |
| `llm.base_url is not set in the config — producing the report without LLM analysis.` | Prose and badges absent. Comparison, diffs, panes, marks and hover unaffected |
| `LLM analysis failed for <file>: <exception>` (one per file) | Dead or wrong endpoint. When every call fails there is no summary call at all, and JSON reports `llm_ran: false` |
| `<n> files to analyze but the limit is <m> — the last <k> will not be analyzed.` | Diffs present, prose absent for the tail |
| `git diff --name-status exited <n>: <stderr>` | The most serious on this list. Anything other than 0 or 1 means the comparison itself may be partial. **Discard the report and fix the environment** |

Fatal, all to stderr, all exit 2: `Error: '<path>' is not an existing directory.`,
`Error: cannot read config file '<path>': <detail>`, `Error: cannot write report
'<path>': <detail>`.

### The trust rule

- **Classification warnings** (missing, failed or timed-out normalizer; non-UTF-8;
  unreadable; unrewritable): the report can *over*-report — noise appears as real
  change. It cannot under-report. Safe but noisy; fix the tooling before concluding
  the drop is large.
- **Prose warnings** (`base_url` unset, LLM failure, file cap): classification, diffs
  and panes are exactly right; only the guidance is missing. Trustworthy but unguided —
  and an absent badge is not a Low badge.
- **git warnings**: nothing in the report is trustworthy.

---

## 6. Design principles for extending the UI

Derived from the code, not invented.

1. **Signal/noise contrast is the organising idea.** One accent colour, spent only on
   things demanding action. Everything ignorable is recessed. Diff colours are reserved
   for diff bodies. Before colouring a new element, decide which of the three registers
   it belongs to; if none, it does not need colour.
2. **Tokens only; never a literal colour in a rule.** The light and dark blocks define
   the same token names so the page cannot end up with one theme's text on the other's
   ground. Adding a colour means adding a token to **both**.
3. **The theme structure is fixed.** Light on bare `:root`; dark applied twice — under
   `@media (prefers-color-scheme:dark)` guarded by `:root:not([data-theme="light"])`,
   and under `:root[data-theme="dark"]` — from one substituted string, so the copies
   cannot drift. `color-scheme:light dark` is declared so UA chrome follows.
4. **Structure carries meaning; CSS only reveals it.** Counterpart lines share a `<tr>`,
   which is why one hover rule lights both panes with no script and why the panes cannot
   desynchronise. Classification is a class or an attribute, never re-derived in CSS. If
   a new element would need JavaScript to stay consistent, change the HTML instead.
5. **Never render a guess.** The risk badge returns nothing when nothing extracts, and
   extraction rejects a template echo rather than picking a level. A new element must be
   derivable from the existing `Comparison` plus analyses, and must be **absent** — not
   defaulted, not greyed, not "unknown" — when its input is missing.
6. **Every state has explicit copy.** "no differences", "No substantive changes
   remain", the binary note, the metadata-only note. A blank region never stands in for
   a state.
7. **Escape at the boundary, per segment.** Both inputs — vendor source and model prose
   — are untrusted. Marking escapes each segment separately; the italic rule requires
   non-word characters on both sides so `SAMPLE_COUNT … SAMPLE_COUNT` is not emphasis.
   Tests enforce this.
8. **Self-contained or it does not ship.** One inlined `<style>`; no URL, `<link>`,
   `<img>`, `src=` or `@import`, asserted by test. New visuals must be CSS and Unicode —
   the gap marker is a character, not an icon. No animation, so nothing for
   reduced-motion to fight.
9. **Two type families do semantic work.** Sans for narrative prose, mono for
   everything identity-bearing: headings, tile numbers, chips, badges, table headers,
   code. Uppercase plus wide tracking marks a label rather than content.
10. **When does a new element earn its place?** All five must hold: (a) it answers a
    question the reviewer must answer to act; (b) it renders from the existing
    `Comparison` and analyses with no new inputs; (c) it degrades to absence, not to a
    placeholder; (d) it needs no script and no network; (e) it does not repeat what a
    neighbour already carries at the same altitude — which is exactly why the files
    table has no diff column and the Changes section does not restate the counts.

Two measured constraints: recessed text sits at 4.76:1 (light) and 4.85:1 (dark)
against the surface — keep 4.5:1 as the floor. And recessing is done with a colour
token, never with `opacity`, so text stays crisp.

---

## 7. Known UX rough edges

Ranked by impact. Honest list; none of these are hidden in the body above.

1. **A degraded run is indistinguishable from a clean one inside the report.** Warnings
   live only on stderr, and the exit code is 0 either way. The HTML you hand a reviewer
   looks identical whether `clang-format` ran or not. *Workaround:* always
   `2> report.warnings` and hand that over too. A provenance block is the fix.
2. **"Noise only" is asserted with no evidence.** The report shows the count zero for a
   file it filtered, and never the filtered diff. A reviewer who wants to verify the
   verdict must open both files by hand — the task the tool claims to have done. This is
   the single most valuable outstanding change.
3. **The panes claim the original directories while showing normalized content.** Line
   numbers and text are from the copies. Nothing on the page warns the reader.
   *Workaround:* navigate by symbol; tell recipients once, in writing.
4. **Exit code 0 means "a file was written", nothing more.** CI cannot gate on run
   quality without scraping stderr or re-deriving it from the JSON.
5. **`<<ignored-by-rule>>` appears in the panes with no legend.** First-time readers
   think the source was mangled.
6. **No navigation.** Everything is expanded, there is no collapse-all, and the files
   table emits no anchors. A sixty-file drop is one enormous scroll. *Workaround:*
   review from the Markdown.
7. **You cannot copy one side out of the report.** Both panes share a row, so a
   selection down the left pane copies both sides interleaved.
8. **Pairing inside a changed run is positional, not semantic.** A line inserted at the
   top of a changed run mis-pairs every row below it; those pairs then fall under the
   similarity floor, so the marks disappear. Read an unmarked coloured row as a warning.
9. **Hover is pointer-only, and there is no print stylesheet.** Tablet and paper readers
   lose the alignment aid, and rows can split across printed pages.
10. **Risk is not usable as a sort key.** No risk column, no severity ordering, and
    every badge disappears under `--no-llm`. Sort the JSON instead.
11. **The dark theme cannot be chosen.** The CSS honours `data-theme`, but nothing emits
    it, so the page follows the OS only.
12. **Smaller, all real:** `type changed` has a label but no chip colour; binary status
    is invisible in the files table; there is no separator between the real, noise and
    skipped groups; the success line does not pluralise; and a bad `-o` path fails only
    after the comparison and every model call have finished.

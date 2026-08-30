# Show the evidence behind "noise only"

**Status:** proposal, with a spike that ran. **Verdict: survives.**
**Spike:** [`spike-noise-size.py`](spike-noise-size.py) — 50 lines, stdlib only, run on this
repository's own history.

## Problem

The tool's central claim is *this file changed only in ways you told me to ignore*, and it is
the one claim the report cannot support.

The verdict is produced by subtraction. `compare.py:35` runs the raw pass:

```python
raw = changed_files(dir_a, dir_b)
```

`changed_files` is `git diff --name-status` (`gitdiff.py:26`) — it returns a status letter and
a path and nothing else. The only unified diff the run produces comes from the *normalized
copies*, inside the temp directory (`compare.py:41`):

```python
sections = diff_sections("A", "B", cwd=tmp)
```

A noise-only file is by definition absent from that second pass, so **the original diff for a
noise-only file is never produced anywhere in the program.** `Comparison.noise`
(`compare.py:19`, `compare.py:66`) is a bare `set` of paths. It is rendered as one table row in
Markdown (`output.py:307-308`), one table row in HTML (`output.py:430-431`), and one sorted
list of strings in JSON (`output.py:234`). Three renderings of a name, zero of a reason.

The consequence is asymmetric with the tool's other blind spot. `README.md:441` discloses that
skipped files are never compared, so the reader knows not to trust the report about them. The
noise verdict is *not* disclosed the same way — `README.md:423` names it as a limitation but the
report itself asserts "noise only" flatly. On a drop where accepting that verdict means **not
reviewing a file at all**, a reviewer who wants to check it has to open both files by hand: the
task the tool claims to have done. `docs/UIUX-GUIDE.md:484-487` ranks this second among the
known rough edges and calls it "the single most valuable outstanding change".

## Approach

Accepted as offered, with two decisions the spike forced (rendering shape and a total-size
budget — see *The size decision*).

**`compare.py`** — run `diff_sections` on the originals as well, and keep the entries whose path
the run classified as noise:

```python
raw = changed_files(dir_a, dir_b)
originals = diff_sections(dir_a, dir_b)
...
noise = _noise(raw, normalized, skipped)

return Comparison(
    ...,
    noise_sections=dict((rel, originals[rel]) for rel in noise if rel in originals),
)
```

`diff_sections(dir_a, dir_b)` returns a dict keyed by plain relative path — the same key space
`_noise` produces — so the intersection is a dict comprehension and no path handling leaks out
of `gitdiff.py`. The spike confirmed the keys match exactly, on a real tree, not by inspection:
`set(noise_sections) == comparison.noise` held for all 16 noise files.

**`Comparison`** gains a sixth field, `noise_sections`. This is a documented public interface,
so it does not move alone — see *Cost* for the exact list of documents.

**`output.py`** — one new section in each renderer, after *Files* and before *Changes*:

- Markdown: `## Filtered out as noise`, one `<details><summary>` per file wrapping a ```` ```diff ````
  fence. It goes last-but-one so the readable part of the document is unaffected by its bulk.
- HTML: `<section id="noise">`, one **default-closed** `<details>` per file wrapping a
  `<pre class="diff">` — *not* `_html_side_by_side`. See below.
- JSON: a new `"noise_diffs": {path: text}` key beside the existing `"noise"` list, which keeps
  its current shape so existing consumers do not break, and `schema_version` 2 → 3
  (`output.py:212`, per the convention in `CLAUDE.md`).

Label it as *filtered-out change, shown so the verdict can be checked* — never as a change to
review. Two properties are worth stating on the page: this text is the **original** diff, so
unlike the side-by-side panes its line numbers refer to files the reviewer actually has
(`docs/UIUX-GUIDE.md:488-490`, rough edge 3); and **skipped** files are still not shown, because
a `skip` rule means "never compared" and there is nothing to show.

**It must not reach the model, and a test must pin that.** Verified by reading every read of the
namedtuple: `llm.py` touches `comparison.real` and `comparison.binaries` (`llm.py:77`),
`comparison.sections` for those targets only (`llm.py:99`, `llm.py:143`), and the *lengths* of
`noise` and `skipped` for the manifest (`llm.py:132`). No noise text is reachable today, and a
sixth field changes nothing — but "unreachable by construction" is a property that decays
silently, so the shipped change adds a test that puts a magic string in `noise_sections`, runs
`analyze_changes` against `FakeChat`, and asserts the string appears in no `system` or `user`
text of any recorded call.

### Two shapes rejected

**Fold the noise diffs into the existing `sections` dict.** No: `sections` is documented as
"for the real changes", `llm.py` reads it, and the two dicts hold text of different provenance —
normalized copies versus untouched originals — with nothing to tell them apart. Merging them
would put raw original text one `.get()` away from the model path. A separate field is what
makes the "never sent" property structural rather than incidental.

**Compute the raw sections in `cli.py` and pass them to the renderers as another keyword**, like
`xref` and `commits`. This avoids changing a public interface and its three documents, which is
tempting. But `cli.py:6` documents itself as "wiring only", and the noise verdict is `compare`'s
verdict — splitting the evidence for it into a second module means the definition of "noise
only" no longer lives in one place. `compare` already makes the raw pass; the evidence belongs
beside it.

## What the spike measured

`docs/proposals/spike-noise-size.py` exports two revisions of this repository with
`gitrepo.export`, reformats every text file on the newer side (re-indent plus trailing pad —
whitespace only), and normalizes with a whitespace-flattening command declared through
`settings.normalizers`. That makes every otherwise-unchanged file noise-only, which is the
"drop that reformatted everything" case. Run on `HEAD~1..HEAD` of a **236 KB** tree:

```
   60884  docs/UIUX-GUIDE.md
   25839  airgap.sh
   22124  AUDIT_REPORT.md
   18440  manishtana/rules.py
   15348  manishtana/gitdiff.py
   13328  CLAUDE.md
   10406  manishtana/symbols.py
    8463  docs/specs/2026-08-11-change-context.md
    6721  manishtana/compare.py
    5045  Dockerfile
    1997  config.json
    1332  .dockerignore
    1081  manishtana/common.py
     922  manishtana/__init__.py
     566  manishtana/__main__.py
     371  .gitignore
real 7 noise 16 | noise 192867 chars | html 180422 -> 387479 (x2.15)
```

| | |
|---|---|
| Noise files | **16** (7 real alongside them) |
| Noise sections, total | **192,867 characters** |
| Largest single section | **60,884 characters** (`docs/UIUX-GUIDE.md`) |
| Top five files | 142,635 chars — **74% of the total** |
| Section size vs. the file's own size | **exactly 2.00×** (96,303 bytes of noise files → 192,867 chars) |
| HTML | 180,422 → **387,479 chars, ×2.15** |
| Markdown | 30,770 → **225,229 chars, ×7.32** |

Three further measurements, from throwaway variants of the same script:

1. **Rendering shape matters more than expected.** Putting the same sections through
   `output._html_side_by_side` instead of `<pre>` gives 661,163 chars — **×3.66 instead of
   ×2.15**. The two-pane renderer costs 70% more markup and buys column alignment for change the
   reader has been told to ignore.
2. **Noise shrinks as real change grows.** Over `HEAD~5..HEAD` with the same reformat, 26 files
   have real changes and only **1** is noise-only: 1,997 chars against an HTML report that is
   already 904,188 chars from the real changes. **×1.00.** Noise is bounded by the files that
   did *not* really change, and those are the cheap ones — the report is already far more
   expensive per real file (~30 KB of HTML each, through the side-by-side renderer) than per
   noise file (~13 KB as `<pre>`).
3. **The worst case is genuinely bad.** Replicating the tree 40× to **9.9 MB** and making the
   whole thing noise (943 files): **19.8 MB of noise sections**, and the HTML goes from
   **0.13 MB to 22.2 MB** — a 170× blow-up of a report whose entire message is "0 real changes,
   nothing here needs review". The extra `diff_sections` call cost 1.2 s against 2.7 s for the
   whole of `compare`, so the *time* is negligible; the bytes are not.

The 2.00× ratio is the rule to extrapolate with: a fully-reformatted file costs twice its own
size, because every line appears once as `-` and once as `+`. HTML escaping and the disclosure
markup add about 7% on top.

## The size decision

**Default-closed.** ×2.15 on the realistic case is affordable, but `docs/UIUX-GUIDE.md:495`
already calls the HTML "one enormous scroll" with no navigation, and the Markdown grows ×7.32 —
86% of the default report would become filtered-out change. Default-open would make the largest
thing on the page the thing the reviewer was told not to read. Note honestly what closing does
*not* buy: `<details>` still ships every byte. Closed solves attention, not size.

**No per-file cap.** Measured, on the realistic case: a 2,000-char/file cap keeps 14% of the
text, 4,000 keeps 24%, 8,000 keeps 43%. That is not a trim, it is a mutilation — and a truncated
noise diff cannot support the claim it exists to support. A reviewer shown the first 2 KB of a
61 KB diff has learned nothing about the other 59 KB, and the substantive change this section
exists to rule out could be in the part that was cut. Half the evidence is worse than none,
because it looks like evidence.

**A total budget instead.** While the sum of `noise_sections` is under a budget, render every
section in full. Above it, render the count and the total size and say plainly that the bodies
were withheld for size — the same disclosure discipline `README.md:441` already applies to
skipped files. That keeps the 9.9 MB drop from producing a 22 MB page, and it never shows anyone
a half-diff. It also matches how the evidence is actually used: it is worth auditing when a
handful of files were filtered and you want to check them; nobody audits 943 files of
"you told me to ignore this" one at a time.

A budget of **1 MB of section text** admits the measured realistic case with 5× headroom and
refuses the pathological one by 20×. That specific number is a judgement call from two data
points, not a measured optimum — see *Open questions*.

Ordering within the section: by size descending, largest first. The 74%-in-five-files
concentration means a reader who checks two or three files has checked most of the filtered
change.

## Cost

**M.** About 150 changed lines, no new module, no new dependency, no new pipeline stage, and no
new input to the run — the data already existed and was thrown away.

| Where | What |
|---|---|
| `compare.py` | one `diff_sections` call, one dict comprehension, sixth namedtuple field — ~8 lines |
| `output.py` | Markdown section, HTML section, one CSS rule, JSON key, `schema_version` 2 → 3, budget check — ~45 lines |
| `tests/test_manishtana.py` | ~6 tests, ~50 lines: keying, budget guard, each of the three renderers, and the pinning test that no noise text reaches the chat seam |
| `compare.py` docstrings | the module interface line (`compare.py:5`) and the field list in `compare()`'s docstring |
| `README.md` | classification table (`README.md:48`), the JSON schema section, and **delete** the limitation at `README.md:423` — it stops being true |
| `docs/UIUX-GUIDE.md` | rewrite §7 item 2 (`:484-487`), renumber the ranked list, document the new section in the report anatomy |

`Comparison` is a documented public interface, so those last four rows are not optional
follow-up — they move in the same commit as `compare.py:19`, or the docstring is lying the
moment the field lands.

One tension to settle out loud: `docs/UIUX-GUIDE.md` §6 rule 10(b) says a new element must
render "from the existing `Comparison` and analyses with no new inputs". This element changes
`Comparison`. It adds no new *input* — no new file read, no new tool, no new config — it
surfaces a value the pipeline already had to compute in order to reach the verdict. Either
rule 10(b) is amended to say "no new inputs to the run" (which is what it means), or this
element gets a named exemption. It should be the former.

Two stale counts will need updating anyway once tests are added: `README.md:413` and
`CLAUDE.md:8` both say "139 tests"; `main` is at 157.

## Risks

1. **Filtered-out content becomes visible in the deliverable.** This is the real one. A rule can
   be written to ignore anything — `ignore_lines` is documented for build stamps and timestamps,
   but nothing stops a rule ignoring a line that carries a credential or a customer name. Today
   that content is filtered from the comparison and never printed; afterwards it appears in a
   report that gets handed around a review box. Three things bound it: `skip` rules are
   untouched (a skipped file is still never shown — that boundary is the safety valve and must
   stay), the section is default-closed and labelled, and it never reaches the model. It should
   also be stated in `README.md` where the rules are documented: **what you `ignore_lines` still
   appears in the report; what you `skip` does not.**
2. **A reader mistakes filtered-out change for change to review.** Mitigated by placement (after
   *Files*, before *Changes*), by the label, and by the closed disclosure — but the wording has
   to be unambiguous, and the HTML needs a visual treatment distinct from a change card.
3. **Line-number convention differs from the panes.** The noise section shows *original* line
   numbers; the side-by-side panes show *normalized* ones. Two conventions on one page is a
   documented trap unless each is labelled. It is also an argument for fixing rough edge 3.
4. **JSON consumers.** `"noise"` keeps its shape, so nothing breaks, but a JSON file can now be
   20 MB where it was 20 KB. The budget applies to JSON too, or CI pipelines that archive the
   artifact get an unpleasant surprise.
5. **Second full raw diff held in memory.** `diff_sections(dir_a, dir_b)` builds the entire raw
   diff — including the real files, then discards them. On the 9.9 MB tree that is a 19.8 MB
   string, transient. `git diff --no-index` takes exactly two paths and no pathspec, so there is
   no way to ask only for the noise files without one invocation per file, which is worse. The
   measured cost was 1.2 s.
6. **Binaries.** A binary cannot be noise-only — `prepare_copy` skips binaries
   (`rules.py:85`), so a differing binary still differs after normalization and lands in `real`.
   If that reasoning is ever wrong, `git diff` emits a one-line "Binary files … differ" for it
   and the section is harmless. Reasoned, not measured.

## Interaction with the pending airgap merge

`origin/feature/airgap-deployment` touches `compare.py` in three places: the import block
(`+shutil, +stat`), the `tempfile.TemporaryDirectory()` context manager (replaced with
`mkdtemp()` plus a `try/finally` calling a new `_remove_tree`), and `_find_binaries` (paths now
go through a new `_inside`). It appends two private helpers at end of file.

This change touches `compare.py` at the module docstring, the namedtuple, and the `return
Comparison(...)` call — plus one line inserted after `raw = changed_files(...)`, which sits two
lines above airgap's first body hunk. It deliberately adds **no** function: the intersection is
a dict comprehension inline in the return. A new private helper appended at end of file would
land exactly where airgap appends `_inside` and `_remove_tree`, and that is the one edit here
that would conflict. If a helper is wanted later, add it after airgap has landed.

**Verified by running, not reasoned:** I committed the patch above onto a throwaway commit and
ran `git merge-tree --write-tree HEAD origin/feature/airgap-deployment`. It exited 0 with no
conflict, and the resulting `manishtana/compare.py` contains both changes composed correctly —
`mkdtemp`/`_remove_tree` around the copy pass, `originals = diff_sections(dir_a, dir_b)` above
it, and `noise_sections=` in the return. The probe commit was reset; nothing from it is on this
branch.

Airgap's test changes import `Comparison` but never construct one
(`from manishtana.compare import Comparison, _inside, _noise, _remove_tree`), so the sixth field
does not break them. The `comparison()` helper in `tests/test_manishtana.py:47` *does* construct
one, by keyword — it needs a `noise_sections` parameter defaulting to `{}`.

**Land airgap first.** Not because of a conflict — there is none — but because it is a
correctness fix that is already reviewed and ready, and this is an additive feature. If the
order is reversed, airgap's author has to re-verify a merge they had already verified. The one
thing to carry across: airgap's `_remove_tree` deletes the temp copies in a `finally`, so
`originals` must be computed **outside** that block, from the real directories. The patch above
already does — the call sits before `mkdtemp`, and the merge confirmed it stays there.

## Open questions

1. **The budget value.** 1 MB is chosen from two data points (193 KB realistic, 19.8 MB
   pathological). Nobody has measured a real vendor drop. It should be a `tuning` value in
   `settings.py` alongside `mark_similarity_floor`, so a site can raise it without editing code
   — that is where the repo already puts values of this kind.
2. **Above the budget, is a count enough?** The alternative is showing the *n* smallest sections
   in full and disclosing the rest, so a reviewer gets some evidence rather than none. It is
   more code and a fuzzier promise. I lean to the simple version until someone hits the budget.
3. **Should `--no-llm` runs default the disclosure open?** A `--no-llm` report is nothing but
   diffs; there is no analysis for the noise section to compete with. Cheap to do, but it makes
   the report's shape depend on a flag, which the guide's design rules discourage.
4. **The spike cannot become a test.** It needs git, subprocesses and a temp tree; the suite is
   deliberately free of all three (`CLAUDE.md` conventions, `tests/test_manishtana.py:1`). The
   shipped tests therefore verify the *rendering and the non-leaking* against synthetic
   `Comparison` values, and the *keying* claim — that `diff_sections` on the originals is keyed
   the way `_noise` keys its set — stays covered only by this spike and by
   `_split_sections`/`_parse_name_status`'s existing tests, which pin both key spaces
   independently. That is a seam worth naming rather than pretending is covered.

# Make `--git` a first-class mode, starting with the report that cannot name its own subject

Status: proposal, with the header half implemented and running on this branch.
Branch: `proposal/git-first-class`. Baseline: `main` @ `1cc3571`.

## Problem

`--git`/`--commit`/`--from`/`--to` landed in the newest commit on `main`
(`1cc3571`) and nothing around it moved. Reviewing a git repository is now the
common case, and in that case the product's own headline artifact — the report a
colleague opens — does not say what was compared.

### 1. The report identifies itself by two directories that no longer exist

`cli._run_git` exports both revisions into `tempfile.TemporaryDirectory()`
(`manishtana/cli.py:98` on `main`) and hands those paths to `output`, which puts
them in the header of all three formats:

| Where | `main` |
|---|---|
| Markdown header rows | `manishtana/output.py:250-251` |
| HTML `<title>` | `manishtana/output.py:346` |
| HTML header `<dl class="meta">` | `manishtana/output.py:356-357` |
| HTML side-by-side pane headers | `manishtana/output.py:589` |
| JSON `dir_a` / `dir_b` | `manishtana/output.py:217-218` |

`README.md:436-438` already concedes it:

> **In `--git` mode the report's "Directory A/B" rows name temporary paths** that
> no longer exist once the run finishes. The revisions are on stdout and in the
> *Stated intent* section; the header rows are not yet the place to look.

Two things that concession understates. First, the *Stated intent* section lists
commit subjects and the shas of the commits **in** the range — it never names the
repository, never names the endpoints, and is absent entirely when `git log`
fails (`gitrepo.commits` warns and returns `[]`) or when the range holds only
merges (`--no-merges`). Second, the pane headers of every side-by-side diff in
the HTML carry the same dead paths, which the limitation does not mention at all.

The temp directory is deleted by the `with` block before `main` returns. So the
header is not merely unhelpful: it names paths that are *provably* gone by the
time the artifact is read, and it names them in the browser tab too.

### 2. The shipped wrapper cannot reach the feature

`run.sh`, `QUICKSTART.md` and `INSTALL.md` live on the unmerged
`origin/feature/airgap-deployment` branch. Searched on that branch, the strings
`--git`, `--commit`, `--from`, `--to` and `--intent-context` appear **zero**
times in any of them — the only hits anywhere are `oc create configmap
--from-file` and `--from-literal` in `DEPLOYMENT.md:250-251`. `run.sh:19` is
`[ $# -ge 2 ] || die "usage: ./run.sh OLD_DIR NEW_DIR [extra args...]"`, and
`run.sh:48` hardcodes two positional bind mounts. After that merge the packaged
entry point — the only one the air-gapped target has — cannot invoke the
product's headline feature.

Worth stating because it is the good news: the *image* is already ready. `main`'s
`Dockerfile` installs `git` as "the one dependency the tool cannot degrade
without" and runs `git config --system --add safe.directory '*'`, which is
exactly what a repository bind-mounted under a foreign uid needs. Only the
wrapper and the guides are behind.

### 3. `gitrepo._git` is unbounded

`manishtana/gitrepo.py:134` calls `subprocess.run` with no `timeout=`. It is the
only subprocess in the package without one once the airgap branch lands:
`rules.py:192` already passes `timeout=timeout`, and the airgap branch adds
`timeout=300` to `gitdiff._git` with the reasoning that git is pointed at trees
the reviewer did not write, possibly over a network mount. `gitrepo` points git
at a *repository* the reviewer did not write — a wider surface, since
`git archive` walks the object database. **Not fixed here**: this is one line and
it belongs with whichever proposal already owns the `gitdiff` timeout, so folding
it in twice would guarantee a conflict. Recommendation to that proposal: give
`gitrepo._git` the same `timeout=300` and the same warn-and-continue shape.

### 4. `cli._run_git` uses `TemporaryDirectory()` — but the stated reason is wrong

The airgap branch replaced `tempfile.TemporaryDirectory()` in `compare.py:37`
with `mkdtemp()` + a `_remove_tree` helper, because `copytree` preserves mode
bits and Windows refuses to delete a read-only file, killing the run *after* the
report was written. `cli._run_git` uses the same construct, so the same defect
looks like it applies.

I checked, and it does not. `git archive` of this repository's `HEAD` produces 27
members, all mode `0o664` or `0o775`:

```
Counter({'0o664': 23, '0o775': 4})
any member without owner-write: []
```

That is structural, not luck — git tree entries are only ever `100644`, `100755`,
`120000` or `160000`, so a `git archive` tar can never carry a read-only regular
file, whatever the mode bits were in the working tree it was committed from. The
read-only-cleanup defect **cannot** fire on this path.

The remaining argument for changing it is consistency plus the *other* Windows
cleanup failures: `TemporaryDirectory` still raises from `__exit__` on a file
whose handle is held open — an antivirus scanner on a freshly written tree is the
realistic one — and it would again kill the run after the report was already
written. That is a real but lesser risk than the item claimed. Recommendation:
after the airgap merge, promote its `compare._remove_tree` to `common.remove_tree`
and have both callers use it. One helper, one behaviour, no duplicated fix.

## Approach

Make the report identify its own subject, and only then bring the wrapper up to
the feature.

**A `Source` record travels beside the trees.** `gitrepo` gains
`Source(repo, rev_a, sha_a, rev_b, sha_b, command)`. `cli._run_git` fills it;
directory mode passes `None`. `output` receives it as a `source=` keyword on all
three builders. No import direction changes — `output` already renders
`gitrepo.Commit` values without importing `gitrepo`, and this is the same shape.

**Both halves of each revision are printed.** The sha is authoritative — a branch
name resolves differently tomorrow. The symbolic name is what the reviewer typed,
and a header they cannot recognize is one they distrust. When the reviewer gave a
sha verbatim it is not printed twice.

**A `Reproduce` row carries the command.** Not `sys.argv`: that names this run's
output paths and whatever branch name was typed. The header carries a sha-pinned,
absolute-path invocation that regenerates the same report from anywhere next
month. That is the thing a reader actually needs and could not previously
reconstruct — `1cc3571^` is not recoverable from today's report at all.

**JSON gains `source`, `schema_version` goes 2 → 3.** `dir_a`/`dir_b` stay at the
top level for schema-2 consumers. Rejected alternative: dropping them at schema 3.
They are a true statement about the trees that were byte-compared, they are the
only sensible answer in directory mode, and removing them breaks every existing
consumer to fix a field that is useless rather than wrong.

**The side-by-side pane headers move too.** In git mode they carry the short sha
instead of the temp path. This was not in the original list of symptoms; the
first test written against the fix found it.

**Then the wrapper.** Sketch, for whoever lands it after the airgap merge:

```sh
if [ "${1:-}" = "--git" ] || [ "${1:-}" = "-g" ]; then
    REPO="$(cd "$2" && pwd)"; shift 2
    mounts=(-v "$(hostpath "$REPO"):/work/repo:ro" -v "$(hostpath "$OUT_ABS"):/work/out")
    positional=(--git /work/repo)
else
    ... existing two-directory path, unchanged ...
fi
```

`run.sh`'s existing `hostpath`/`MSYS_NO_PATHCONV` handling applies unchanged, and
the image's `safe.directory '*'` already covers the foreign-uid case.

## What the spike showed

`docs/proposals/spike-git-header.py` (50 lines, stdlib only) runs the tool in
`--git` mode over this repository, prints the header of all three artifacts, and
exits non-zero if any of them names the export directory the run just deleted. It
is a red/green witness, not a demo: **exit 1** against `main`'s `output.py`,
**exit 0** with this branch's. Both were run; the `main` run was done by stashing
this branch's changes, not by reasoning about them.

Underneath it is one command:

```
python -m manishtana --git . --commit 1cc3571 -c config.json --no-llm \
    -o report.md -j report.json -H report.html
```

**Before** — the real Markdown header, verbatim:

```markdown
# Directory comparison report

| | |
|---|---|
| Directory A | `C:\Users\meirh\AppData\Local\Temp\tmpzjt6cc7q\before` |
| Directory B | `C:\Users\meirh\AppData\Local\Temp\tmpzjt6cc7q\after` |
| Date | 2026-08-30 06:52 |
| Real changes | 7 |
```

and the HTML, which puts the same dead path in the browser tab:

```html
<title>Directory comparison — C:\Users\meirh\AppData\Local\Temp\tmpzjt6cc7q\before / ...\after</title>
<dt>Directory A</dt><dd>C:\Users\meirh\AppData\Local\Temp\tmpzjt6cc7q\before</dd>
```

`tmpzjt6cc7q` did not exist by the time that report was written to disk. The
spike's own verdict line on `main`:

```
artifacts naming the deleted export directory: ['md', 'html']
--- json source ---
"absent — this is schema 2"
```

**After**, same command:

```markdown
# Revision comparison report

| | |
|---|---|
| Repository | `C:\Users\meirh\Git\NafkaMina\...\agent-a24b06f173628c379` |
| From | `2476fba8592d739541b8973e80276acb59b93a99 (1cc3571^)` |
| To | `1cc3571a35ca6aebe369dbc77351bc804d8bbf2c (1cc3571)` |
| Reproduce | `python -m manishtana --git C:\...\agent-a24b06f173628c379 --from 2476fba8592d739541b8973e80276acb59b93a99 --to 1cc3571a35ca6aebe369dbc77351bc804d8bbf2c --no-llm` |
| Date | 2026-08-30 06:57 |
| Real changes | 7 |
```

```html
<title>Revision comparison — 2476fba85..1cc3571a3</title>
<dt>Repository</dt><dd>C:\Users\meirh\Git\NafkaMina\...</dd>
<th colspan="2">2476fba85</th><th colspan="2">1cc3571a3</th>
```

```json
"schema_version": 3,
"source": {
  "kind": "git",
  "repo": "C:\\Users\\meirh\\Git\\NafkaMina\\...",
  "from": {"rev": "1cc3571^", "sha": "2476fba8592d739541b8973e80276acb59b93a99"},
  "to":   {"rev": "1cc3571",  "sha": "1cc3571a35ca6aebe369dbc77351bc804d8bbf2c"},
  "command": "python -m manishtana --git ... --from ... --to ... --no-llm"
}
```

Directory mode was re-run (`python -m manishtana docs tests --no-llm`) and its
header is byte-identical to `main`'s, plus a `source` key of
`{"kind": "directories", "dir_a": "docs", "dir_b": "tests"}` in the JSON.

The fix was smaller than the symptom list suggested but not tiny: 76 lines of
code and 32 of comment across `cli.py`, `gitrepo.py` and `output.py`, plus 9
tests. It came in over the 50-line guide because the same lie is rendered in five
places, not one. `python -m unittest discover -s tests`: **166 passed** (157 on
`main`; one existing assertion changed from `schema_version == 2` to `3`).

## Should `--git` be the documented default path?

**Yes in QUICKSTART, as the first of two, and no in README.** Argued:

For. The author confirms repositories are now the common case. The image already
carries git and `safe.directory '*'`, so the packaged product is one wrapper
branch away. Git mode is one mount instead of two, and it removes the single most
likely user error in the current QUICKSTART — getting old and new the wrong way
round, which silently inverts every "added" and "deleted" in the report with no
symptom at all. And it is the only mode that produces *Stated intent*, which is
the tool's actual differentiator over `git diff`: a model reading the diff blind,
printed next to what the author claimed.

Against. QUICKSTART's demonstration — "the reformatted file reports as `noise
only`, the new file as a real change" — is directory-shaped and takes two
`mkdir`s to construct; the equivalent in git mode needs a scratch repository and
two commits, which is a worse first five minutes. And the vendor-drop case has
not gone away: a tarball from a supplier has no history, and that case is where
the noise filtering earns its keep most visibly.

So: QUICKSTART step 3 becomes "Review a commit" (`./run.sh --git repo --commit
HEAD`) and step 4 "Compare two directory drops", keeping the existing worked
example under step 4. README keeps both modes as peers — it is a reference, and
inverting the emphasis there would churn a document that is already correct.

## Cost

**M.** The header half is **S** and is done — the diff is on this branch, the
suite is green. The wrapper and guides are **S–M**, but they must wait for the
airgap merge and cannot be written blind: the `--git` container path needs one
real run before it is documented (see Risks).

## Risks

- **A read-only bind mount of a repository is unverified.** `run.sh` mounts
  everything `:ro`. `git archive` and `git rev-parse` should only read, but git
  writes opportunistically (`commit-graph`, index refresh). If `:ro` fails the
  mount has to be read-write and the guide has to say so. This must be tested in
  the container before the wrapper is documented; there is no Docker on this
  machine to test it with.
- **`schema_version` 3 is a breaking bump.** Mitigated by keeping `dir_a`/`dir_b`,
  so a schema-2 consumer that ignores unknown keys is unaffected. A consumer that
  pins `schema_version == 2` needs updating; that is what the field is for.
- **Absolute repository paths in the header can leak a directory layout.** The
  report already printed absolute temp paths, so this is not new exposure, but it
  is now a *meaningful* path. If that matters, `Reproduce` is the row to make
  relative — deliberately not done, because a relative path defeats its only
  purpose.
- **`Reproduce` is display text, not a shell-safe command.** `_shell_quote` wraps
  paths containing spaces in double quotes and does nothing else. It is never
  executed by this tool. A path containing a double quote renders wrongly.
  Escaping properly for two shells is not worth the code; the shas and the repo
  path beside it are the machine-readable form.

## Interaction with the pending airgap merge

The airgap branch forked at `2476fba`, before `--git` existed, so a naive diff of
it against `main` shows `gitrepo.py` being deleted and 126 lines of `cli.py`
changing. Merging it will need care regardless of this proposal.

- `output.py`: no overlap. The airgap branch's `output.py` changes are a large
  deletion in a region this proposal does not touch; this proposal's changes are
  in the `build_report`/`build_html`/`build_json` headers plus four new helpers.
- `cli.py`: the airgap branch's `cli.py` predates `_run_git` entirely. This is the
  real conflict, and it exists with or without this proposal. Resolve in favour of
  `main`'s `cli.py` plus the airgap branch's changes, then re-apply this branch's
  `source` wiring — six lines.
- `tests/`: this proposal appends one class and changes one assertion. The airgap
  branch rewrites large parts of the file. Append-only makes this the easy half.
- `run.sh`, `QUICKSTART.md`, `INSTALL.md`: **owned entirely by the airgap
  proposal**. Nothing is written to them here on purpose — the sketch above is the
  handoff.
- The `gitrepo._git` timeout is deliberately left to whichever proposal owns the
  `gitdiff._git` timeout, for the same reason.

Order: land airgap first, then this. Reversing it means resolving the same
`cli.py` conflict with more code in it.

## Open questions

1. Should `--git` mode still write `dir_a`/`dir_b` into JSON at all, or should a
   later schema drop them once consumers have moved? Kept for now; the decision
   does not have to be made today.
2. Should the `Reproduce` row appear in directory mode too? It is constructible
   there (`python -m manishtana A B`), but the directory paths are the reader's
   own and the row would add nothing. Left out.
3. `--commit REV` on a merge compares `REV^` — the first parent — to `REV`, while
   `gitrepo.commits` passes `--no-merges`, so *Stated intent* can be empty for a
   range whose trees differ substantially. The header now says what was compared
   regardless, which is an improvement, but should `--commit` refuse a merge
   outright, or say which parent it took?
4. Should stdout's one success line carry the `Reproduce` command too, so it lands
   in a CI log even when the report file is discarded?
5. `CLAUDE.md` still says "139 tests" and `README.md` said the same; `main` is at
   157. `README.md` is updated here to 166 because it is the user-facing spec and
   this change moved the number. `CLAUDE.md` is left alone deliberately — every
   sibling proposal that adds a test will want that same line, and a one-number
   conflict in five branches is worse than one stale number.

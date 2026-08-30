# Land `origin/feature/airgap-deployment`, and reconcile what the clean merge breaks

**Status:** proposed, with the merge and the reconciliation already performed on
`proposal/land-airgap` so the claims below can be checked rather than believed.
**Spike:** `docs/proposals/spike_land_airgap.py` — run it.

---

## Problem

`origin/feature/airgap-deployment` is 9 commits ahead of `main` and 1 behind, and
`git merge-tree --write-tree` returns a clean tree. It carries the fix for the
audit's only HIGH finding, three lesser security and portability fixes,
`DEPLOYMENT.md` / `INSTALL.md` / `QUICKSTART.md` / `run.sh`, a `.gitattributes`
LF pin, an OpenShift arbitrary-uid fix, and version 1.0.1. It should land.

A clean *textual* merge is not a correct merge here. Git resolved the text;
nothing checked whether the two halves still describe the same tool. Six things
break, none of them a conflict.

### (a) The merge silently breaks a documented public contract — **confirmed, worse than reported**

`README.md:369-372`, on the merged tree, documents:

> Any other string is run as a shell command as written. `{name}` is substituted
> with the basename **already quoted for the shell** — do not add quotes of your own.

The branch deletes `shell=True` from `rules._normalize_text` and has
`_command_for` return argv instead of a string. That is the right fix for the
HIGH finding: on `cmd.exe`, `%VAR%` expands inside double quotes, so quoting a
filename safely there is not actually possible, and removing the shell removes
the question. But a `normalizers` entry written as a shell pipeline — which the
README told the operator to write — now resolves to
`['sort', '|', 'uniq']`, and the pipe is passed to `sort` as an argument.

Two failure shapes, both measured (see *What the spike showed*):

- **Loud but misleading.** `sort | uniq` runs `sort` with the arguments `|` and
  `uniq`; on Windows it exits 1 with `Input file specified two times.` The
  warning names the step and blames the *file*, so it reads as a normalizer bug
  on one file rather than a step that will never work again.
- **Completely silent.** A pipeline whose first stage exits 0 — which is most of
  them: `cat`, `sed`, a `python -c` filter — returns its input unchanged, exits
  0, and produces **no warning at all**. The step degrades to a no-op that
  nothing downstream can detect.

The consequence is not cosmetic. Normalization is the whole product: `compare()`
defines "noise only" as *differs before normalization, identical after*. A
normalizer that stops normalizing does not fail — it reclassifies. Files that
were `noise only` become real changes, the LLM is handed diffs of pure
formatting, and the report says the drop changed far more than it did. That is
the tool inverting its own purpose, quietly, on a config the README endorsed.

The branch's own test suite documents the change without noticing the contract:
`test_unknown_step_is_still_run_as_written` was rewritten from
`_command_for("tr -d x", "a.c").startswith("tr -d x")` to
`== ["tr", "-d", "x"]`. Nothing covers a step containing shell syntax.

### (b) Two `SyntaxWarning`s, printed into the file the deployment guide says to read first — **confirmed**

`manishtana/rules.py:154` (`_split_command`) and `manishtana/compare.py:93`
(`_inside`) are non-raw docstrings containing Windows paths: `C:\Python\python.exe`
and `'C:\Windows\...'`. `\P` and `\W` are invalid escapes, which are a
`SyntaxWarning` from Python 3.12 on.

This is not a lint nit, because of where the warnings land. `run.sh:68`
redirects the run's stderr into `out/report.warnings`, then prints it under
*"There are warnings. They are never in the report itself, and some of them
change how much you should trust it. Read them:"*. `DEPLOYMENT.md` and
`INSTALL.md` both say to read that file first. And `Dockerfile:30` sets
`PYTHONDONTWRITEBYTECODE=1`, so nothing is cached and every module is recompiled
on **every** run inside the shipped image — not just the first.

The operator's first experience of 1.0.1 is therefore two warnings that mean
nothing, above the warnings that mean something. The signal this tool exists to
protect is the first casualty.

### (c) `AUDIT_REPORT.md` is stale on the merged tree — **confirmed**

The branch touches `AUDIT_REPORT.md` only to rename `NafkaMina` to `MaNishtana`.
It still lists as open the HIGH shell injection (`:34`), the path traversal
(`:35`) and the symlink dereference (`:36`) that this very branch fixed, and
still records `PASS | Temp Directory Security | tempfile.TemporaryDirectory()`
(`:37`) for a construct the branch replaced. Shipping a security report that
lists fixed findings as open is worse than shipping none: the next reader either
re-fixes them or stops trusting the document.

### (d) `schema_version` is published as 1; the tool emits 2 — **confirmed, plus a second error**

`output.py:215` emits `schema_version: 2`. Documented as `1` in `README.md:386`,
`docs/UIUX-GUIDE.md:312`, and — the one that matters — `DEPLOYMENT.md:38,42`,
where it is presented as the **service interface contract** for the downstream
patch-verification consumer.

While checking it, a second error in the same block: the sample names a `files`
key. There is no `files` key. Verified against a real run — the top-level keys
are `brief, commits, counts, cross_reference, dir_a, dir_b, generated_at,
inputs, llm_ran, noise, real, schema_version, skipped`, and the file list is
`real`. A consumer written from `DEPLOYMENT.md` reads the wrong key at the wrong
version.

The v2 additions are exactly the ones that consumer needs: `commits` (what the
author *claimed*) and `inputs.intent_shown` (whether the model was allowed to
read the claim before analysing the diff). A verdict stage that scores
diff-against-claim agreement is meaningless when `intent_shown` is true, and the
contract never told it to look.

### (e) The shipped wrapper cannot reach the common case — **confirmed**

`grep -n -- '--git\|--commit\|--from\|--to'` over `run.sh`, `QUICKSTART.md` and
`INSTALL.md` returns nothing. `run.sh` requires two directories
(`[ $# -ge 2 ]`, then `[ -d "$A" ]`), so `--git` cannot be passed through it at
all.

That is now backwards. Reviewing git repositories is the common case, and
`DEPLOYMENT.md`'s own diagram of where this fits draws it: *fetch repo at the
vulnerable revision* + *fetch repo at the claimed fix revision* → MaNishtana.
`--git REPO --from VULNERABLE --to FIXED` is that diagram in one invocation, and
the shipped wrapper has no way to say it.

### (f) The test count is wrong in four files — **confirmed**

`AUDIT_REPORT.md:44-45` says 90, `docs/UIUX-GUIDE.md:35` says 93,
`README.md:415` and `CLAUDE.md:8` say 139. The merged tree runs 168; with the
tests this proposal adds, 179. All four line numbers are as of the merge commit.

### Also folded in

- **`gitrepo._git` has no `timeout=`.** The branch gives `gitdiff._git` a bounded
  one, with a docstring explaining that git is being pointed at directories this
  tool did not create. `gitrepo._git` runs `rev-parse`, `log` and `archive`
  against a *repository* the reviewer did not write, which is strictly the
  stronger case, and it was written after the branch so the branch could not
  have covered it.
- **`cli._run_git` uses `tempfile.TemporaryDirectory()`** — flagged as the same
  construct the branch replaced in `compare.py`. **This one does not hold.** See
  below; it was measured and the premise is backwards.

---

## Approach

Land the merge as a merge commit on `proposal/land-airgap`, then reconcile in
one follow-up commit. Nothing on the branch is reverted — every fix it carries
is correct — and the reconciliation is deliberately small.

**`manishtana/rules.py` — make the lost feature loud instead of silent.**
`_shell_syntax_in(argv)` returns the shell operators appearing as whole
arguments; `_warn_once_about_shell_syntax(step, argv)` warns once per step,
naming the step, the operators, the program that will actually run, and the
consequence (*"Files this step was meant to flatten will be reported as real
changes"*). Called from `_normalize_text` before the missing-tool check.
Whole-argument matching only, because `{name}` is substituted *into* an argument
— a file called `x&mkdir OWNED&.c` yields `-assume-filename=x&mkdir OWNED&.c`
and cannot be mistaken for an operator. Warn-once per step, matching the
existing `_missing_tools` policy. Both new names are underscore-prefixed; the
module's documented public interface is unchanged.

**`README.md` — restate the contract.** Shell syntax is not interpreted; each
`normalize` entry is one argument vector; steps chain, each one's output feeding
the next, so a pipeline is written as several entries; anything genuinely
needing a shell goes in a script.

**`manishtana/compare.py`, `manishtana/rules.py` — two `r` characters.** Make
both docstrings raw. A test compiles every module in the package and asserts
zero warnings, so the next Windows path in a docstring fails the suite instead
of the operator's warnings file.

**`manishtana/gitrepo.py` — bound `_git`.** `timeout=300`, matching
`gitdiff._git`. A timeout raises `GitError`, which every caller already handles,
*and* warns directly — because `resolve()` turns any `GitError` into `None` and
`cli` then reports a revision that does not exist, which would make a hung git
look like a typo.

**`run.sh` — accept the second input shape.** `./run.sh --git REPO --commit REV`
and `--from/--to`. One read-only mount instead of two; the argument vector is
built into `input=(...)` rather than hardcoded `v1 v2`. `--git` without a
revision, and `--git` at a directory with no `.git`, fail with the usage line
rather than reaching docker.

**`QUICKSTART.md`, `INSTALL.md`, `DEPLOYMENT.md` — document that shape**,
including why the read-only mount suffices (`git archive` only reads), why the
image's `safe.directory '*'` is what makes a bind-mounted repository work under
`--user`, and that commit messages are withheld from the model by default.

**`AUDIT_REPORT.md` — a status column**, not a rewrite. Each finding keeps its
original text so the fix can be read against the report that asked for it, and
gains what actually happened — including where the shipped fix differs from the
recommended one.

**`DEPLOYMENT.md`, `README.md`, `docs/UIUX-GUIDE.md` — `schema_version: 2`**,
the corrected `real` key, and one paragraph on what v2 added and why
`intent_shown` gates the consumer's agreement score.

**Four test counts → 179**, with the Windows skip noted in `README.md` so the
number is not wrong again the moment someone runs the suite on Windows.

### Rejected

- **Restore `shell=True` behind a flag or a `shell:` prefix.** Keeps the
  documented feature working and puts the injection back for anyone who uses it,
  on a tool whose entire input is untrusted source from someone else's tree. A
  config file is data; a config file that can spawn a shell is code. The feature
  was worth less than the hole.
- **Emulate the pipeline ourselves** — split on `|`, chain the processes in
  Python. More code than the whole `rules` module, a new class of bug
  (quoting, redirects, exit-status semantics), and it would still not be a
  shell. Chained `normalize` steps already express a pipeline; they just need
  documenting.
- **Leave (a) alone and only fix the README.** Every existing config keeps
  producing wrong classifications until someone re-reads the documentation. The
  degradation is silent; that is precisely what has to change.
- **Fix `AUDIT_REPORT.md` by deleting the fixed findings.** The audit is the
  reason the branch exists. Deleting the findings deletes the argument for the
  code.
- **Merge with `--squash`, or rebase the branch onto `main`.** Nine commits with
  real messages, one per defect. A squash throws away the reasoning; a rebase
  rewrites published commits on a branch that exists on `origin`.
- **Change `cli._run_git` to use `compare._remove_tree`.** Measured, and the
  premise is wrong — see below. It would also have `cli` reach into another
  module's underscore internals, or force a helper up into `common`, for a
  defect that does not exist there.

---

## What the spike showed

`docs/proposals/spike_land_airgap.py`, Python 3.12.10 on win32. It loads
`main:manishtana/rules.py` out of git history into a throwaway package so both
normalizer implementations are live in one process, then swaps only
`rules._normalize_text` and runs the real `compare()` over one pair of
directories. The two runs differ in that function and nothing else.

**The merge itself.** Clean, exit 0, 14 files, +1021/−56, no conflicts. Suite on
`main`: **157 tests, OK**. Suite on the merged tree: **168 tests, OK
(skipped=1)** — the skip is the symlink test, which needs privilege on Windows.
So the branch is green as merged; the damage is not in the test suite.

**(b) SyntaxWarning — confirmed, at the exact lines claimed.**

```
$ python -c "import manishtana.cli"
manishtana/compare.py:93: SyntaxWarning: invalid escape sequence '\W'
manishtana/rules.py:154: SyntaxWarning: invalid escape sequence '\P'

$ python -W error::SyntaxWarning -c "import manishtana.cli"
SyntaxError: invalid escape sequence '\W'          # exit 1 — the package will not import
```

And into the operator's file, on a fresh checkout, via the documented command:

```
$ python -m manishtana --git . --commit HEAD --no-llm -o report.md 2> report.warnings
$ cat report.warnings
manishtana/compare.py:93: SyntaxWarning: invalid escape sequence '\W'
manishtana/rules.py:154: SyntaxWarning: invalid escape sequence '\P'
```

That is the *entire* contents of `report.warnings` for a successful run — two
lines of noise where the operator was told to look for the things that change
how much to trust the report. With `PYTHONDONTWRITEBYTECODE=1`, which
`Dockerfile:30` sets, it recurs on every run: three consecutive runs, 2
SyntaxWarnings each, 0 `__pycache__` directories. After the fix: `total: 0`.

**(a) The normalizer contract — confirmed, and the classification flips.**

Resolution, same input string to both versions:

```
step: sort | uniq
  merged -> argv ['sort', '|', 'uniq']        program 'sort'
  main   -> shell string 'sort | uniq'        program 'sort'
step: tr -d x && sort
  merged -> argv ['tr', '-d', 'x', '&&', 'sort']
```

Running it — a two-stage pipeline whose *second* stage does the work, so the
argv version's surviving first stage is a no-op:

```
main (shell=True)   input 'beta\nalpha\ngamma\n'  output 'alpha\nbeta\ngamma\n'  sorted? True   warnings (none)
merged (argv)       input 'beta\nalpha\ngamma\n'  output 'beta\nalpha\ngamma\n'  sorted? False  warnings (none)
```

**Same input, same config, opposite result, and not one word of warning.** The
README's own shape is the loud variant, and its warning is misleading:

```
merged (argv), 'sort | uniq': output unchanged? True
  Warning: normalize step 'sort | uniq' failed for src/sensor.c (exit 1)
           — keeping the original text. Input file specified two times.
```

End to end through `compare()`, two trees whose one file differs only in line
order — noise by construction under this rule:

```
main (shell=True)   real []              noise only ['sensor.c']
merged (argv)       real ['sensor.c']    noise only []
```

A file the tool used to correctly call *noise only* is now a *real change*, sent
to the model, counted in the summary. On a drop where a rule like this covers
every file, everything inflates at once — the failure mode the tool exists to
prevent.

After the fix, the classification still flips (it must — the semantics genuinely
changed), but the run now says so, once per step, naming the consequence:

```
Warning: normalize step 'sort | uniq' contains shell syntax (|), and normalizers
are not run through a shell — only 'sort' will run and the rest is passed to it
as arguments. Files this step was meant to flatten will be reported as real
changes. Split it into separate normalize steps, or wrap it in a script.
```

**(c), (d), (e), (f) — confirmed by inspection and by running the tool.**
`schema_version: 2` read out of a real `-j` run against `DEPLOYMENT.md`'s
published `1`; the `files`/`real` key mismatch found the same way; `grep` over
`run.sh`, `QUICKSTART.md`, `INSTALL.md` returning zero `--git` mentions.

**The `cli._run_git` fold-in — does NOT hold. The premise is backwards.**
Measured on Python 3.12.10 / Windows 11:

```
TemporaryDirectory cleanup with a read-only file: OK
shutil.rmtree          with a read-only file: FAILED PermissionError(13, 'Access is denied')
```

`TemporaryDirectory._rmtree` carries a `PermissionError` handler that resets the
mode and retries. It is `shutil.rmtree` — which `compare._remove_tree` tries
first — that fails. So `TemporaryDirectory()` is not "the construct with the
defect"; it is the one that already handled it, and `AUDIT_REPORT.md`'s
"CRITICAL WINDOWS FAILURE MODE" does not reproduce as described on this
interpreter.

Independently, no read-only file can reach `_run_git`'s temp directory at all:
it holds a `git archive` export, and git records only the executable bit.
Verified by exporting this repository's `HEAD` — **28 files, every one `0o666`**.

`cli._run_git` is therefore left alone, and `AUDIT_REPORT.md` now records why.
`compare._remove_tree` is kept: it is explicit, tested, and does not depend on
an interpreter-version detail — but the audit's stated mechanism for it is
wrong, and anyone reasoning from it will reach the wrong conclusion again.

**`run.sh`, exercised against a stubbed `docker`** that prints its argv
(`bash -n` clean; the two-directory path byte-for-byte unchanged):

```
./run.sh v1 v2 --no-llm
  -v .../out:/work/out -v .../v1:/work/v1:ro -v .../v2:/work/v2:ro  manishtana v1 v2 -c ... --no-llm
./run.sh --git repo --commit HEAD --no-llm
  -v .../out:/work/out -v .../repo:/work/repo:ro                    manishtana --git /work/repo -c ... --commit HEAD --no-llm
./run.sh --git repo --from v1.0 --to v2.0        -> --git /work/repo ... --from v1.0 --to v2.0
./run.sh -g repo --commit=abc123                 -> --git /work/repo ... --commit=abc123
./run.sh --git repo --no-llm                     -> error: --git needs --commit REV, or --from REV
./run.sh --git v1 --commit HEAD                  -> error: 'v1' is not a git repository (no .git)
```

That exact argument ordering was then run against the real CLI (`--git . -c ...
-o ... -H ... --commit HEAD --no-llm`) — exit 0, *"Comparing 1cc3571a3..3e1fe0938
(9 commits) from ."*, 27 real changes.

**Final state.** `python -m unittest discover -s tests`: **179 tests, OK
(skipped=1)**. `python -m manishtana --help`: exit 0. `bash -n run.sh` and
`bash -n airgap.sh`: clean. Import graph re-derived from the AST — no cycles,
`settings` still imports nothing from the package, no import added by any of
this. No third-party import, no f-string, no walrus anywhere in the change.

---

## Cost

**S–M.** The merge is free — git does it. The reconciliation is 375 added and 42
removed lines across 12 files, of which 128 added lines are tests and roughly
half the remainder is prose in five documents. The only new behaviour is one
warn-once function in `rules.py` (about 25 lines including its docstring) and a
`timeout=` in `gitrepo._git`. `run.sh` gains a branch and an argument array.

Reviewing it is the real cost, and it splits cleanly: two commits, the merge and
the reconciliation, with the code changes confined to `rules.py`, `gitrepo.py`,
two `r` prefixes, and `run.sh`.

---

## Risks

- **The (a) fix warns; it does not restore the feature.** Anyone with a
  shell-syntax normalizer still gets different classifications after this lands.
  That is unavoidable — the semantics genuinely changed and the alternative is
  the injection — but it is a behaviour change reaching an operator through a
  warning, and a warning can be scrolled past. Mitigated by naming the
  consequence, not just the cause, and by `README.md` now saying it.
- **False positives in the shell-syntax check.** A legitimate normalizer whose
  argument is literally `|`, `&&` or `>` warns wrongly. No such normalizer is
  plausible, whole-argument matching keeps filenames out of it, and the failure
  is a spurious warning rather than a wrong report — but it is a heuristic
  reading a config we do not control.
- **`run.sh --git` was verified against a stub, not against docker.** The
  argument vector and the mounts are right; whether git inside the container is
  happy with a `:ro` bind mount owned by another uid rests on the image's
  `safe.directory '*'` (`Dockerfile:25`) and on reasoning, not a run.
- **Everything was measured on Windows / Python 3.12.10.** The tool targets 3.8+
  on Linux too. The Linux paths are argued, not run.
- **`AUDIT_REPORT.md` is now a living document.** A status column has to be
  maintained or it goes stale again — which is the defect this proposal is
  fixing. Better than an audit that lies, and worse than an audit with a date on
  it and a separate changelog.
- **Test counts are still hand-maintained in four files.** Corrected, not
  automated; they will drift again on the next commit that adds a test.

---

## Open questions

1. **Should `settings.load` reject shell syntax at startup instead of warning at
   run time?** The repo already validates prompt placeholders at load, exiting 2
   on a typo, on the argument that a mistake found at startup beats one found
   three files into a review. A normalizer that will never work is the same
   class of mistake. The failure policy points the other way — *"a missing
   normalizer or a failing normalize step warns and continues"* — and `settings`
   may not import from `rules`, so the check would have to be duplicated. Warn
   was chosen; abort is defensible.
2. **Is `TemporaryDirectory`'s read-only handling present on 3.8 and 3.9?** Only
   3.12.10 was available to measure. If it is not, the audit's mechanism is right
   for the floor version and wrong for the current one, and
   `AUDIT_REPORT.md`'s note should say so more precisely.
3. **Should `--git` be reachable from `run.sh` at all, or should the wrapper
   split?** `run.sh` is now two tools in one script. A `review-repo.sh` would be
   clearer and one more file in the transfer set.
4. **`CLAUDE.md`'s dependency diagram omits `gitrepo`** (`common ← gitrepo ← cli`,
   verified from the AST). That drift came from `main`, not from this branch, so
   it was left alone — but it is one line and the diagram is the thing new
   contributors are told to preserve.
5. **`cli.py` remains entirely untested** — `AUDIT_REPORT.md` §2.2 flagged it, no
   test imports `manishtana.cli`, and `--git`'s argument validation and mutual
   exclusions now live there. Out of scope here; it is the largest remaining gap
   the audit named.

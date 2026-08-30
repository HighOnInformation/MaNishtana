# A preflight check, and the first tests for `cli.py`

Proposed 2026-08-30. Spiked, implemented and run on this branch
(`proposal/preflight-check`). Line references are to `main` at `1cc3571` unless
marked otherwise.

## The problem

Two problems that turn out to be one: the module that decides whether a run can
start has no tests, and the box a run starts on has no check.

### 1. `cli.py` is the largest untested surface, and it just grew

The audit (§2.2) named `cli.py` at "0 unit tests… command-line parsing, argument
validation, or exit codes". Since then `1cc3571` added **126 lines** to it —
`_run_git` at `cli.py:68`, revision resolution, and six new `return 2` paths at
`cli.py:72, 77, 84, 91, 106` on top of the five that were already there
(`40, 48, 55, 61`, plus the write failures at `150, 155, 162`). No test reaches
any of them: no test file imports `manishtana.cli` at all.

Nothing about the module resisted testing. `main(argv=None)` has always taken
`argv`, for exactly this purpose. It simply was never called.

### 2. First run on a new box is seven commands, and the only packaged check is bash

`docs/UIUX-GUIDE.md:29-37` lists seven commands to run before trusting a box.
`airgap.sh do_selftest` (`airgap.sh:205-238`) runs the suite and prints
`git --version` (`airgap.sh:218`) — and checks nothing else. Not the config, not
the prompts, not the normalizers these rules actually name, not the endpoint.

It is also `#!/usr/bin/env bash` (`airgap.sh:1`) and depends on `sha256sum` or
`shasum` (`airgap.sh:40-42`), `find` (`airgap.sh:77`) and `docker`
(`airgap.sh:36`). So `selftest` — the step the README calls "the step people skip
and should not" (`README.md:148`) — **has no Windows form at all**, on a tool
whose hard constraints list Windows as a deployment target. There is no `.bat`,
`.ps1` or `.cmd` anywhere in the repository, and there should not be: a third
copy of the check would be a third thing to drift.

Two consequences of the same gap:

**The tool knows the diagnostic and prints a traceback instead.** `README.md:113`
admits it, `docs/UIUX-GUIDE.md:42-44` explains it: `gitdiff._git` does not catch
`OSError`. Verified by running with git off `PATH` on this box:

```
File "…\subprocess.py", line 1538, in _execute_child
FileNotFoundError: [WinError 2] The system cannot find the file specified
```

**A wholly failed LLM run still reports success.** `llm.py:57` turns every
transport failure into a warning and a `None` — correct, a dead model must not
kill the report — but `cli.py:124-138` then leaves `Analysis(None, {}, None)`
untouched and `cli.py:164-168` prints the success line and returns 0. Verified by
running against a closed port with a valid-looking model name:

```
Warning: LLM analysis failed for x.c: <urlopen error timed out>
Report written to _t/r.md — 1 real changes, 0 noise only, 0 skipped.
exit=0
```

That is one warning on stderr and a report with no analysis in it, exit 0. The
shipped `config.json:5` reaches this state out of the box: `"model":
"PLACEHOLDER-MODEL-NAME"` against a live endpoint is rejected on every call. The
only pre-run guard in the whole tool is `cli.py:127`,
`elif not llm.get("base_url"):` — which a half-configured config passes.

## Approach, and why the tests come first

The fix is a `--check` flag. But adding startup branches to the one module with
no tests is how it stays untested forever, so the sequence is not negotiable:

1. **The harness, with no production change.** `CliArgs`, calling `main(argv)`
   directly with a temp config and temp directories, asserting exit codes and the
   text the user is shown. Cover the `return 2` branches that fire **before** any
   subprocess starts, so the suite's no-git, no-network rule survives intact.
2. **Then `--check`**, with every probe left in the module that owns the concern.

`--check` reports, in this order: the interpreter; git, with the real diagnostic;
every **resolved** normalizer program these rules name; the config path; the
prompt digest and the effective normalizer command lines; and, unless `--no-llm`,
one real completion through the existing `chat` seam. Exit 0 when every
**required** row passed, 2 when one did not. A missing `clang-format` is a `warn`,
not a `FAIL`, because on a bare target that is normal and `rules._normalize_text`
already forgives it — `--check` must not be stricter than the run it predicts.

Where each answer comes from, and why:

| Fact | Answered by | Why there |
|---|---|---|
| git | `gitdiff.version()` | That module owns every git invocation the comparison makes |
| normalizers | `rules.tool_status()` | Same alias table, same `{name}` substitution, same `shutil.which` that `_normalize_text` uses mid-run |
| prompts | `settings.digest` | Already the fingerprint of the exact text that leaves the box |
| endpoint | `llm.probe(chat)` | Takes a `chat`, so it is exercised by the same fake the analysis tests use and cannot grow a second transport |

`cli` formats those answers and turns them into an exit code. It learns how to
probe nothing.

Two behaviour changes come with it, both small and both argued:

- **A missing git is a diagnostic and exit 2, not a traceback.** Deliberately
  *not* an `OSError` catch inside `gitdiff._git`: swallowing it there turns a
  missing git into an empty diff and reports "no changes" for a drop that changed
  everything. Wrong is worse than absent. The check sits in `cli` *after*
  argument validation, so a typo on the command line is still reported as a typo.
- **`PLACEHOLDER-MODEL-NAME` refuses to start unless `--no-llm` is given.** That
  state is worse than unconfigured, for the reason demonstrated above.

## What the spike showed

**The harness is 20 lines plus four imports.** That is the whole finding of step
one, and it cuts against the excuse: `cli.py` was never hard to test.

```python
class CliArgs(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.config = os.path.join(self.work, "config.json")
        self._config({"llm": {}, "rules": []})

    def _config(self, data):
        with open(self.config, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def _main(self, *argv):
        """main() with BOTH streams captured: _configure_streams touches both."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue() + err.getvalue()
```

`_configure_streams()` runs first in `main()` and reconfigures **both** streams,
so both must be captured. It turned out to be harmless under capture, but by
accident rather than design: `io.StringIO` has no `reconfigure` attribute, so the
`hasattr` guard at `cli.py:230` makes the call a no-op. Verified by running on
Windows — `sys.stdout.encoding` was `cp1255` before a `main()` call under
`redirect_stdout` and `cp1255` after. Anything that captures with a real
`TextIOWrapper` instead would be silently reconfigured to UTF-8.

Eight tests dropped straight onto the existing branches with no production change
at all: unreadable config, malformed JSON, a bad prompt placeholder, no input at
all, a missing directory, a `--git` repository that is not a directory,
`--commit` combined with `--from`, and `--git` with no revision.

`--check` then works, and found real things about this box on the first run:

```
ok   python                   3.12.10
ok   git                      git version 2.55.0.windows.5
ok   config                   C:\…\config.json
ok   prompts                  10 resolved, digest 0ea2be6e4917
warn normalize:strip-comments gcc not found — step skipped, so formatting counts as real change
warn normalize:clang-format   clang-format not found — step skipped, so formatting counts as real change
ok   normalize:json-sort      "C:\…\python.exe" -c "import json,sys;…"
FAIL llm                      llm.model is still PLACEHOLDER-MODEL-NAME — set a real model name, or pass --no-llm
exit=2
```

Everything below the fold was verified by running, not reasoned about: the
placeholder `FAIL` against the shipped `config.json`; `FAIL llm … did not answer`
and exit 2 against a closed port; exit 0 with `--no-llm`; the git diagnostic and
exit 2 with git removed from `PATH`; and the interleaving bug that made the
verdict print *above* the table it judges, because stdout is block-buffered when
redirected and stderr is not — fixed with an explicit `sys.stdout.flush()`.

One thing the spike could not do cleanly, and it is worth stating plainly:
`CLAUDE.md` says the tests "deliberately touch no git, no subprocesses and no
network", and the `CliCheck` tests break the middle clause. `--check` reads the
box by design, so exercising it end-to-end spawns `git --version` and calls
`shutil.which` — five extra process spawns, which took the suite from 0.03s to
0.30s. Faking the probe would have meant testing a seam instead of the command,
which is the one thing a preflight must not do.

So the deviation is documented where the repository already documents its other
principled exception, in the test module's own docstring, and bounded: the tests
assert only what is true whether or not git is installed here — a suite that must
run on a bare target box may not assume it — and they need no repository and no
network. The aggregate exit code is asserted as the invariant it actually
promises (`2` iff some required row failed); exit 0 on a healthy box was
confirmed by hand rather than by the suite, for the same reason.

### Is `cli.py` still "wiring only"?

**No — and it already was not before this change.** `cli.py:6` claims it, but
`_run_git` (`cli.py:68-110`) holds real policy: a commit means `REV^..REV`,
`--to` defaults to `HEAD`, an empty range warns rather than fails, and the export
lands in a `before/`/`after/` pair under a temp directory. That is behaviour, and
it is exactly why 126 lines of it could arrive untested in one commit — a module
described as wiring invites nobody to look.

`--check` adds 123 lines to `cli.py` (242 → 365), of which roughly 80 are code
and the rest docstring and comment. None of it is a probe, but the policy — which rows
are *required* and which are forgiven — does live there. Rather than let the
docstring keep saying something false, this branch changes it to name what the
module owns and to set a tripwire: two such rules is the limit; a third means
both belong in a module of their own. That module already has an obvious shape —
`preflight.py`, sitting where `compare` sits, with `cli` importing it and nothing
else changing — and the tests written here are the precondition for moving to it
safely. It is deliberately not bundled into this proposal: a refactor and a
feature in one change is two things to review as one.

## Cost

**S.** Two commits, both landed and green.

| | |
|---|---|
| `tests/test_manishtana.py` | +184 −1, 19 new tests (8 then 11). 157 → 176, `python -m unittest discover -s tests` green |
| `manishtana/cli.py` | +125 −2 |
| `manishtana/rules.py` | +31 (`tool_status`, `ToolStatus`, `import collections`) |
| `manishtana/gitdiff.py` | +23 (`version`) |
| `manishtana/llm.py` | +17 (`probe`) |
| `airgap.sh`, `README.md`, `docs/UIUX-GUIDE.md` | +50 −23, plus the `--check` row in the flags table and the exit-code paragraph |
| `CLAUDE.md` | +1 — the dependency diagram now names the two new `cli` edges |

Standard library only; the one new import anywhere is `collections` in `rules`.
No walrus, no `match`, no positional-only parameters. Import direction unchanged
in kind: `cli` gains edges to `gitdiff` and `rules`, both downward, no cycle.
`settings` still imports nothing from the package.

Runtime cost of the new startup check: one `git --version` per run, a few
milliseconds.

## Risks

- **`cli` grows rather than shrinks.** Stated plainly above. The mitigation is
  the tripwire in the docstring and the named follow-up, not a promise.
- **`cli` now imports `gitdiff` and `rules` directly**, which it previously
  reached only through `compare`. Same direction, no cycle, but the module's
  surface is wider. A `preflight.py` would take both edges back.
- **The endpoint probe spends one completion.** It is the smallest possible
  exchange ("Reply with the single word: ok"), and `--no-llm` skips it entirely.
- **Refusing to start on the placeholder is a breaking change** for anyone whose
  workflow relies on the current silent degradation. The error names `--no-llm`
  as the one-word fix. `README.md` and `docs/UIUX-GUIDE.md` are updated on this
  branch; `QUICKSTART.md` and `INSTALL.md` on the pending airgap branch are not,
  and will need the same line (see below).
- **A `warn` row is easy to skim past.** It is the row that decides whether
  formatting counts as substantive change, which is the tool's entire premise.
  `--check --strict` is the obvious answer and is left as an open question rather
  than guessed at.

## Interaction with the pending airgap merge

`origin/feature/airgap-deployment` adds `run.sh`, `QUICKSTART.md`, `INSTALL.md`
and `DEPLOYMENT.md`. Its `run.sh:52-61` already greps `config.json` for
`PLACEHOLDER-MODEL-NAME` and warns when `--no-llm` was not passed — the same
problem, solved **outside the tool and for the Docker path only**. Its comment
even states the reasoning this proposal acts on: "the tool degrades quietly
there… so say it out loud instead of letting a thin report look like a full one."

The two do not conflict in intent; they conflict in placement. Once `--check` and
the startup guard land, `run.sh`'s grep is a fourth copy of a rule the tool now
enforces itself, on one of three platforms, in a language two of them do not
have. Recommended resolution, in order of preference:

1. **Delete the grep from `run.sh`.** The tool refuses to start on the placeholder
   now, with a better message, in the container and natively and on Windows.
   `run.sh` already forwards the exit code and prints the warnings file.
2. If it is kept as a fast pre-docker hint, it must stop being an independent
   rule: keep the `printf`, drop any implication that the run will proceed.

Either way `run.sh` should gain a `--check` path — `./run.sh --check` mapping to
`docker run … --check` — so the container's answer to "can this box run it" is
the same command as everywhere else.

Merge mechanics: that branch forked before `1cc3571`, so its diff against `main`
shows `gitrepo.py` deleted and `cli.py` −126. Landing it is a merge, not a
fast-forward, and it also touches `airgap.sh` (+8) and `README.md` (−37). This
branch touches `airgap.sh:210-233` (the `do_selftest` body) and four README
paragraphs. Expect small textual conflicts in both files; no logic conflicts.
Landing order does not matter, but whichever goes second should re-read
`README.md`'s dependency table and `QUICKSTART.md` for the now-wrong sentence
about a missing git giving a traceback.

`airgap.sh selftest` on this branch **calls** `--check --no-llm` instead of
re-probing python and git in shell, precisely so the two cannot drift. The
image's own `git --version && clang-format --version && gcc --version` line is
kept beside it on purpose: `--check` forgives a missing optional tool because on a
bare target that is normal, but the image exists to carry those tools, so there
their absence is a broken build rather than a fact about the box. That is a
different claim, not a duplicate one.

## Open questions

1. **Should a wholly failed LLM run still exit 0?** Demonstrated above; not
   changed here. `--check` catches the common causes *before* the run, which is
   why it was worth doing first, but a network that dies mid-run still produces a
   success line. A third exit code (say 3, "report written, analysis incomplete")
   would let CI distinguish the cases without breaking scripts that test for 0
   — but it is a wider behaviour change than a preflight, and belongs in its own
   proposal.
2. **`--check --strict`**, promoting `warn` to `FAIL`? It is what the image
   check wants, and it would let `airgap.sh` drop the last shell-side probe.
   Deferred because a flag added on speculation is a flag nobody uses.
3. **Should `--check` write machine-readable output?** `-j` exists for the report;
   a `--check --json` would let CI assert on rows rather than grep. Not proposed
   until something asks for it.
4. **The probe's warning text reads oddly.** A failed probe surfaces as
   `Warning: LLM analysis failed for endpoint check: …`, because `http_chat`
   phrases every failure that way. Fixing it means either a second message format
   in `llm.py` or a label that reads correctly in both sentences. Cosmetic, but
   it is the first line a new user sees when the endpoint is down.
5. **Does `_llm_rows` belong in `llm`?** It reads four `llm` config keys and
   decides what each state means. It is currently in `cli` because its output is
   display rows. If `preflight.py` happens, it moves.

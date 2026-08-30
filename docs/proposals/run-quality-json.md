# Make the run's own quality machine-readable, and settle the version rule

Status: proposal. Spike: `docs/proposals/spike_run_quality.py` (run, results below).

## Problem

The tool's central claim is a verdict about someone else's change: `counts.real == 0
&& counts.noise > 0` is the machine-readable form of *they changed the file but not
the code*. A downstream patch-verification service integrates against that. The
verdict is only as good as the normalizers that produced it — and **nothing in the
JSON says which normalizers ran.**

Where the facts are, and where they are not:

- `manishtana/rules.py:182-190` — when `shutil.which(program)` returns `None`, the
  step is skipped, `warn()` is called, and the comparison continues. Correct
  behaviour (`CLAUDE.md:40-41` requires it). The record of it goes nowhere.
- `manishtana/rules.py:41` — `_missing_tools` is a module global. It is written at
  `:185` and read at `:182`, both inside `_normalize_text`, purely to dedupe the
  warning. Nothing outside `rules.py` reads it, and it records only *missing*
  programs, never the ones that resolved.
- `manishtana/common.py:13-15` — `warn()` writes to `sys.stderr` and returns. In a CI
  job or a wrapper script that captures only stdout, the entire degradation record is
  discarded.
- `manishtana/output.py:195-237` — `build_json` has no field for any of this. The
  only provenance it carries is `inputs.prompts_sha256`, and
  `manishtana/settings.py:218-222` computes that digest over **prompts only**, so it
  is identical on a box with `gcc` and a box without.
- `manishtana/output.py:239` (`build_report`) and `:338` (`build_html`) do not take
  `settings` at all — `manishtana/cli.py:158-160` passes it to `build_json` alone. The
  two outputs a human actually reads cannot show even the one provenance field that
  exists.
- `README.md:107-110` explains the inflation ("Without the two optional tools … 
  formatting and comment changes count as real changes"). That is a sentence for a
  person reading the README. The machine interface says nothing.

The author already wrote this down twice. `docs/UIUX-GUIDE.md:480-483`: "A degraded
run is indistinguishable from a clean one inside the report… **A provenance block is
the fix.**" And `docs/UIUX-GUIDE.md:491-492`: "Exit code 0 means 'a file was written',
nothing more. CI cannot gate on run quality without scraping stderr or re-deriving it
from the JSON."

## The version rule, decided and argued

**Decision: `schema_version` is a compatibility number, not a changelog. It bumps if
and only if a correctly written existing consumer would break — a key removed,
renamed, retyped, or given a meaning that makes previously correct handling wrong.
Purely additive keys never bump it. Consumers feature-detect by testing for the key
they need.**

Consequences, stated plainly:

1. **This change does not bump.** It adds `run_quality` and `provenance` and removes
   nothing. It stays at `2`.
2. **The three proposals that each planned to bump to 3 are all wrong**, unless one of
   them is genuinely breaking — in which case that one takes `3` alone and the other
   two rebase onto it. This is not a coincidence to be arbitrated; it is the predicted
   output of a rule that routes every independent change through one global counter.
3. **Commit `1cc3571` bumped 1→2 for two additive keys** (`commits`,
   `inputs.intent_shown`; its own message says so). Under this rule that bump was
   unnecessary. It is not being reverted — `2` is what is emitted and what consumers
   have seen, so `2` is now simply the current compatibility level, reached by an
   over-cautious route.
4. **The documentation is a version behind and must be fixed with this change.**
   `README.md:384` and `docs/UIUX-GUIDE.md:312` both document `schema_version: 1`
   while `manishtana/output.py:215` emits `2`. A consumer that followed the README and
   gated on `== 1` is already broken today. This is the sharpest evidence for the
   rule: a version number nobody can keep in sync across three files is a version
   number that is not carrying compatibility information.
5. **`CLAUDE.md:104-106` is right as written and gets one clarifying sentence**, plus
   the consumer-facing half of the rule in `README.md`: additive keys do not bump;
   detect features by presence.

### Alternatives considered

**Bump on every change (current practice).** Rejected. The number then carries no
compatibility information: pinning `== N` breaks on every release, and pinning `>= N`
is presence-testing with extra steps and a false precision — it claims a total order
over changes that landed on independent branches. It also cannot express two additive
changes landing in parallel, which is exactly the collision that produced this
paragraph.

**Major.minor (`"2.1"`).** Rejected. It looks like it solves the problem and does not:
the minor number is still a single global counter, so three parallel proposals now
collide on `2.1` instead of on `3`. It also turns an integer comparison into string or
tuple parsing in every consumer, and it still does not tell a consumer *which*
additive key it received — presence-testing does, exactly and per-key.

**A `features: [...]` array alongside the version.** Rejected, though it is the most
tempting. It is a second source of truth for something the payload already states by
construction: the key is either there or it is not. A `features` list can drift from
the keys actually emitted, and then a consumer has to decide which to believe. The
payload is its own feature list.

## Approach

Five small changes, along the existing dependency arrows. Nothing new enters the
graph and `settings` still imports nothing from the package.

**1. `common.py` accumulates.** `warn()` keeps writing to stderr and additionally
appends to a module-private list; a new `warnings()` returns the deduplicated
messages in first-seen order, and `reset_warnings()` exists for tests.

This makes `common` a three-function module, against its own docstring: "Deliberately
one function." That docstring's stated reason is that "the 'Warning: ' convention and
the choice of stream live in one place rather than in every module that reports a
problem." The accumulator is the same concern — *what happens to a warning* — and
belongs in the same place. The alternative is every call site teeing its own copy,
which is precisely what the docstring forbids. The docstring gets rewritten to say
this, rather than being quietly contradicted.

**2. `rules.py` records resolution, not just failure.** Replace the write-only
`_missing_tools` with a record of both outcomes, plus two public functions:
`reset_tools()` and `tool_status() -> {"resolved": [...], "missing": [...]}`. The
existing dedupe behaviour is unchanged; the set simply stops being write-only.

Three states matter, not two: **resolved**, **missing**, and **never needed** — a
normalizer configured for `*.cpp` in a drop containing no C++ was never exercised, and
reporting it as missing would be a lie. "Never needed" is expressed as absence from
both lists.

**3. `compare.py` carries it out.** `compare()` calls `rules.reset_tools()` before the
two `prepare_copy` calls and returns the status as a sixth field on `Comparison`,
declared with a namedtuple default so every existing positional construction and the
test helper keep working. The data travels the arrow it already had, `rules →
compare → cli`; no caller reaches across a module boundary for a global.

**4. `cli._run` assembles the provenance dict** and adds it to the `shared` kwargs at
`manishtana/cli.py:141-147`, which already flows to all three builders:

```
provenance = {
    "tool_version": __version__,          # manishtana/__init__.py:9
    "config": args.config,
    "prompts_sha256": settings.digest,    # reaches the renderers for the first time
    "llm_ran": bool(analyses or summary),
    "files_real": len(comparison.real),
    "files_analyzed": len(analyses),
    "normalizers": comparison.tools,      # {"resolved": [...], "missing": [...]}
    "warnings": warnings(),
}
```

`files_analyzed < files_real` is the machine-readable form of the `max_files` cap,
which today only exists as prose in the `llm.py:78-82` warning. The two are at
different altitudes — one is a sentence for a person, one is a number CI compares — so
this is not the duplication `docs/UIUX-GUIDE.md:466-468` principle 10(e) forbids.

**5. Rendering.**

- `build_json` gains a top-level `"run_quality": "clean" | "degraded"` and a
  `"provenance"` object. `provenance` omits `prompts_sha256`, which `inputs` already
  carries — one fingerprint, one place. `run_quality` is `"degraded"` when any
  normalizer is missing, any warning was emitted, or `files_analyzed < files_real`
  while `llm_ran`. It is top-level and single-valued because gating on it must be one
  key lookup, not a traversal.
- `build_report` gains one header row (`| Run | degraded — 1 normalizer missing |`)
  and a collapsed `<details>` "Run notes" section carrying the tool version, the
  config path, the prompt digest, the normalizer lists and the warnings.
- `build_html` gains the same as a `<dt>/<dd>` pair in the existing header `<dl>` and
  a `<details>` panel, styled with existing tokens, no new CSS colours.
- **Both are absent entirely on a clean run** — not greyed, not "unknown" — which is
  what `docs/UIUX-GUIDE.md:445-448` principle 5 and `:465-466` principle 10(c) require.

Exit codes are deliberately not touched. Changing `0` to `1` on a degraded run would
break every existing wrapper, and `docs/UIUX-GUIDE.md:491-492` asks for something CI
can gate on, not for a different exit code. `run_quality` is that thing.

### The document conflict: principle 10(b) versus §7.1

`docs/UIUX-GUIDE.md:464-465` principle 10(b) says a new element must render "from the
existing `Comparison` and analyses **with no new inputs**". `docs/UIUX-GUIDE.md:483`
says "A provenance block is the fix" — and a provenance block cannot be derived from
`Comparison` and analyses, because its entire content is facts they do not contain.
The two cannot both be obeyed.

**§7.1 wins, and 10(b) gets corrected rather than ignored.** Three reasons:

1. **10(b) was already false when it was written.** `build_report` and `build_html`
   take `xref`, `brief`, `risk`, `commits` and `intent_shown` — five inputs that are
   neither `Comparison` nor analyses. `commits` and `intent_shown` were added by
   `1cc3571`; `git log -- docs/UIUX-GUIDE.md` shows the guide has not been touched
   since `2476fba`, which predates it. The guide describes a rule the code stopped
   obeying a commit later, without anyone noticing. That is a stale rule, not a
   blocking one.
2. **10(b)'s actual purpose is preserved.** It sits directly beside principle 5,
   "Never render a guess", and what it guards against is a renderer that goes and
   *fetches* or *re-derives* something — reads the filesystem, calls a model,
   recomputes a verdict. Provenance is the opposite of a guess: it is the most literal
   possible record of what the run did. `cli` establishes the facts; the builders stay
   pure functions of their arguments, which is the property `output.py:8-9` actually
   claims ("All three are pure: same inputs, same output, no I/O").
3. **The correction is one clause**, and it restates the real invariant:
   > (b) it renders from what the pipeline already established — the `Comparison`, the
   > analyses, and the run's own provenance — performing no I/O and deriving no new
   > verdict of its own.

## What the spike showed

`docs/proposals/spike_run_quality.py`, 50 lines, run on
Windows 11 / Python 3.12.10 / git 2.55.0 against pre-merge `main`. Two `.c` files:
`a.c` differs substantively, `b.c` differs only in whitespace. One rule,
`{"match": ["*.c"], "normalize": ["fmt"]}`. Identical fixture, identical config; the
only difference is whether `fmt` resolves to a program that exists.

```
Warning: tool 'manishtana-no-such-normalizer' is not installed - normalize step 'fmt' skipped, comparison continues without it.
normalizer present: counts={'real': 1, 'noise': 1, 'skipped': 0} noise=['b.c']
normalizer missing: counts={'real': 2, 'noise': 0, 'skipped': 0} noise=[]
BUG: same key set, same inputs 0ea2be6e4917..., nothing names the missing tool.
```

This reproduces the prior reviewer's table exactly. Then every assertion that ought to
fail, passes:

- `sorted(ok) == sorted(bad)` — identical top-level key sets.
- `ok["inputs"] == bad["inputs"]` — identical, including `prompts_sha256`
  `0ea2be6e4917…`. The one provenance field in the schema is blind to the difference,
  because `settings.py:218-222` digests prompts only.
- `ok["llm_ran"] == bad["llm_ran"]`.
- The string `manishtana-no-such-normalizer` appears nowhere in the serialized
  payload.
- None of `run_quality`, `provenance`, `normaliz`, `warn`, `degraded` appears anywhere
  in the serialized payload.

The `Warning:` line is the *only* trace that the second run was degraded, and it went
to stderr, interleaved with the tool's stdout, unreferenced by either report file. A
consumer reading `report.json` cannot tell `real:2, noise:0` ("two real changes") from
`real:2, noise:0` ("one real change and one the box could not normalize").

One incidental finding worth recording: the warning fires **once** across both
`prepare_copy` calls, because `rules._missing_tools` persists across them. That dedupe
is correct for stderr and is exactly why the set must be *reset per comparison* rather
than per `prepare_copy` if it is ever to be reported — hence `reset_tools()` living in
`compare()`, not in `prepare_copy()`.

## Cost

**M.** No new module, no new dependency, no change to the pipeline's shape.

| File | Change |
|---|---|
| `common.py` | +~12 lines: accumulate, `warnings()`, `reset_warnings()`, rewritten docstring |
| `rules.py` | +~12 lines: record resolved as well as missing, `tool_status()`, `reset_tools()` |
| `compare.py` | +~4 lines: reset, sixth `Comparison` field with a default |
| `cli.py` | +~15 lines: assemble the dict, add one `shared` key |
| `output.py` | +~45 lines across the three builders |
| `README.md` | JSON section, `schema_version: 1`→`2`, the version rule for consumers, and the "degraded run looks like a clean one" limitation now has a fix |
| `CLAUDE.md` | one clarifying sentence at `:104-106` |
| `docs/UIUX-GUIDE.md` | 10(b) corrected, `:312` `schema_version` fixed, §7.1 rough edges 1 and 4 struck |
| tests | ~10 new, all pure — provenance rendering, absence on a clean run, `run_quality` values, `warnings()` dedupe |

No test needs git, a subprocess or a network, so `CLAUDE.md:100-103` holds. The spike
itself does spawn git and a subprocess and therefore stays **out** of `tests/`; it is
a script to run, not a case to discover.

## Risks

- **`Comparison` gains a field.** Mitigated by a namedtuple default, so positional
  construction and the `comparison()` test helper at `tests/test_manishtana.py:48`
  keep working. Verified as a design choice, not by running it.
- **`warn()` becomes stateful.** Per-file warnings scale with file count (an
  unreadable file warns once per file), so the list is deduplicated on exact text and
  capped, with the cap disclosed as a `warnings_truncated` flag rather than silently
  swallowing the tail. Two comparisons in one process share the accumulator —
  irrelevant for a one-shot CLI, which is why `reset_warnings()` exists for tests.
- **Reports stop being byte-identical across boxes.** `config` path, warnings and
  normalizer lists are machine-specific by design. Anyone diffing two reports for
  equality must now exclude `provenance` — the same treatment `generated_at` already
  needs.
- **Warnings can carry paths from the tree under review**, and `config` carries a
  local path. Both now land in a file that gets handed to someone. `dir_a`/`dir_b`
  already do this, so the exposure is not new, but it widens.
- **`run_quality: "degraded"` will fire often** — a missing `clang-format` is the
  normal state of an air-gapped box, which is the target environment. If CI gates hard
  on it, every run fails on day one. That is arguably the correct alarm, but it is a
  rollout problem and the README must say so.
- **Two truths for "was this run OK".** `run_quality` and the warnings list can
  disagree if a future `warn()` call site is not degradation. Keeping `run_quality`
  derived from the warnings list rather than computed independently is the guard.

## Interaction with the pending airgap merge

`origin/feature/airgap-deployment` touches `rules.py`, `compare.py` and `gitdiff.py`.
It does **not** touch `common.py`, `cli.py` or `output.py` (verified with
`git diff --stat main...origin/feature/airgap-deployment`), so five of the eight files
above are conflict-free.

**Land airgap first, then rebase this onto it.** That branch carries the audit fix
replacing `shell=True` with argv — a security fix, already reviewed, merging clean.
This change is additive and cheap to rebase; putting a security fix behind a reporting
feature would be the wrong order.

Two overlaps, both textual, neither semantic:

- **`compare.py`.** Airgap rewrites `with tempfile.TemporaryDirectory() as tmp:` into
  `tmp = mkdtemp()` with a `try/finally` and a `_remove_tree` helper. My `reset_tools()`
  call and the sixth `Comparison` field sit immediately above and below that block. A
  conflict on the same hunk is likely; the resolution is mechanical — the reset goes
  before the `try`, the field goes in the `Comparison(...)` call airgap leaves
  untouched.
- **`rules.py`.** Airgap rewrites `_program_of` from string-parsing to `argv[0]`,
  turns `_command_for` into an argv list, and drops `_quote_name`. My recording line
  sits after `program = _program_of(...)` and before the `which()` check — a position
  that exists identically in both versions, so the semantics survive the rewrite
  intact.

The merge in fact **improves** this proposal. Post-airgap `_program_of` returns
`argv[0]`, an unquoted executable path, so the `resolved`/`missing` lists no longer
depend on `_program_of`'s quote-stripping heuristic for the `json-sort` alias
(`"C:\...\python.exe" -c "..."`). The names recorded get cleaner for free.

One behavioural note: airgap's `prepare_copy` skips symlinks, so fewer files reach
`_normalize_text`. That cannot make a resolved tool look missing, but it can make one
never be exercised — which is exactly why the three-state resolved/missing/never-needed
distinction above is not pedantry.

## Open questions

1. **Should `--fail-on-degraded` exist?** Deliberately not proposed here: rough edge 4
   asks for something CI can gate on, and `run_quality` is that. But a flag turning
   `run_quality == "degraded"` into exit code 1 is the obvious follow-up, and it should
   be one decision made once rather than three teams each writing a `jq` gate.
2. **Should `settings.digest` cover normalizers too?** It currently digests prompts
   only, which is why the spike's two runs share a fingerprint. Widening it would make
   the digest catch a *config* difference — but not the *box* difference this proposal
   is about, since the config was identical in both spike runs. Probably a separate
   change; possibly a second digest. Not resolved here.
3. **Is "resolved" strong enough?** `shutil.which` finding `clang-format` says nothing
   about its version, and two boxes with different `clang-format` versions produce
   different normalizations and therefore different verdicts. Recording a version
   string means running each tool with `--version`, which is a subprocess per tool and
   a per-tool flag convention. Named, not proposed.
4. **Does the downstream patch-verification service want `run_quality` to change the
   verdict?** This proposal only *reports*. Whether a degraded run should suppress the
   `real == 0 && noise > 0` verdict entirely is that service's call, and it needs the
   field before it can make it.
5. **Where does the version rule live canonically?** It is going into `CLAUDE.md` and
   `README.md`, in two audiences' words. A third copy in `docs/UIUX-GUIDE.md:312` is
   what let the number drift in the first place; that line should carry no version
   number at all.

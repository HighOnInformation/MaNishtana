# The cross-reference is blind outside C

Proposal, 2026-08-30. Spike: `docs/proposals/xref-languages-spike.py` (50 lines, run from
the repo root, about a minute). Every number below came out of a run, not a guess; where I
reasoned instead of measured, the text says so.

**Verdict: reshaped.** Extending the *regex* to more languages is the obvious move and it is
the wrong one — not because the definition patterns are weak, but because the half nobody
measured, the reference search, collapses to somewhere between 25% and 50% precision the
moment it leaves C's flat namespace. What survives is smaller and different: **disclose the
languages the cross-reference did not run on**, take **Python via `ast`** as a deliberate
special case, and **decline Java, C#, Go and Ada loudly** — including fixing the fact that
today they are not declined, they silently misfire.

---

## 1. The problem

`cross_reference` iterates every changed file and hands each one to a C heuristic:

- `manishtana/symbols.py:41` — `for rel, section in sections.items()`. No extension filter.
  Every language reaches the same extractor.
- `manishtana/symbols.py:28` — `_DEFINE_RE`, the only named form: `#define NAME`.
- `manishtana/symbols.py:80-99` — `_defined_name`. A definition must `text.endswith("{")`
  (line 91) and carry two tokens before the `(`. That is a C function signature and nothing
  else.
- `manishtana/symbols.py:102-124` — `_references`, a whole-word regex over every text file
  in `dir_b`. Language-blind by design; the docstring says so, and `README.md:434` repeats it
  to users.
- `config.json` ships rules for `*.c *.h *.cpp *.hpp *.json` only.

The failure shape is the problem, not the coverage. `manishtana/output.py:289` (Markdown) and
`manishtana/output.py:412` (HTML) both open with `if dangling:`. When the extractor finds
nothing, **no section is emitted at all** — not an empty one, not a note. `build_json`
(`manishtana/output.py:227`) emits `{"removed": {}, "dangling": {}}`. A reviewer reading a
report on a Python drop sees exactly what they would see if the change deleted nothing that
anything still uses. The tool's own rule is
*"treat a badge as evidence and its absence as **no information**"* (`docs/UIUX-GUIDE.md:218`),
but here absence is not even offered as absence — the region is simply gone, which reads as a
clean bill of health. And this is the feature the spec calls
*"the only part that improves the tool with the model switched off"*
(`docs/specs/2026-08-11-change-context.md`).

### Measured: what it actually finds

Running the shipped `_definitions` over Python, every line presented as an addition:

| corpus | definitions (`ast`) | shipped extractor finds |
|---|---|---|
| `manishtana/*.py` | 77 | **0** |
| 1036 importable stdlib modules | 44,115 | **11** |

Recall 0.00025. Not degraded — blind.

End to end, on a scratch copy of this package with one function deleted (`git diff` twice,
real `compare()`, real `cross_reference()`):

```
=== deleted warn() from common.py -- shipped ===
removed : {}
dangling: {}
```

`warn` is imported and called by five modules. The report would have said nothing.

### Measured: the languages that are *not* blind are worse

`_defined_name` never sees a file extension, so it runs on Java, C# and Go too. On idiomatic
sample lines (synthetic — I had no Java/C#/Go corpus on the box, and say so):

| line | shipped extractor returns |
|---|---|
| `public static void main(String[] args) {` | `main` — correct |
| `private void Dispose(bool disposing) {` | `Dispose` — correct |
| `func Fetch(ctx context.Context) error {` | `Fetch` — correct |
| `} catch (IOException e) {` | **`catch`** — invented |
| `go func() {` | **`func`** — invented |
| `func (r *Repo) Fetch(...) error {` | nothing — every Go method missed |
| `procedure Calibrate (X : in out Integer) is` | nothing |
| `def compute(a, b):` | nothing |

So the current state is not "C only". It is "C, plus a partially-correct guess on three
brace languages that also invents symbols named `catch` and `func`". A definition named
`catch` that then gets searched for across the tree is precisely the wrong badge the module
docstring says it would rather die than emit.

---

## 2. What the spike measured

### 2a. A Python regex extractor is fine, and that is not the point

`^(?:async\s+)?(?:def\s+(\w+)\s*\(|class\s+(\w+)\s*[(:])`, scored against `ast` per file:

| corpus | precision | recall | false positives |
|---|---|---|---|
| `manishtana/*.py` | 1.0000 | 1.0000 | 0 |
| 1036 stdlib modules, 44,115 defs | **0.9898** | **0.9993** | 498 |

A looser variant without the trailing `\(` / `[(:]` guard scored precision 0.9903 at recall
1.0000 (587 false) over the full 1797-module tree — tightening trades 52 misses for 89 fewer
false hits and barely moves the number. Every one of the 498 is a `def`/`class` line inside a
**docstring**: `abc.py:21 def my_abstract_method(self, arg1, arg2, argN):`,
`contextlib.py:278 def some_generator(<arguments>):`, `dataclasses.py:1315 class C:`. A
line-at-a-time reader cannot tell prose from code, and a diff hunk gives it no more context.

99% precision on the extractor sounds like a pass. It is not the number that reaches the
reviewer.

### 2b. The badge is the number that reaches the reviewer, and it is terrible

The reviewer never sees `removed`. They see `dangling` — "removed but still referenced". So
the spike simulates deleting each definition in a real tree and asks what the shipped
`_references` search would report:

| corpus | definitions | badges raised by whole-word text search |
|---|---|---|
| `manishtana` | 76 | 27 |
| stdlib `json` | 31 | 12 |
| stdlib `http` | 243 | 33 |
| stdlib `email` | 480 | 175 |
| stdlib `xml` | 520 | 204 |
| stdlib `logging` | 192 | 60 |

Two falsifiers are decidable without judgement. **Provably stale**: the name is still
*defined* somewhere else in the tree, so the symbol did not go away — 30% of `email`'s badges,
48% of `xml`'s, 40% of `logging`'s. **Attribute-only**: every hit is `obj.name`, never a bare
name, so no hit can be a call of the deleted module-level function. Discounting both leaves a
*ceiling* on precision — not a measurement of it, an upper bound:

| corpus | badges | provably stale | attribute-only | precision ≤ |
|---|---|---|---|---|
| `logging` | 60 | 24 | 21 | **0.25** |
| `xml` | 204 | 98 | 53 | **0.26** |
| `email` | 175 | 53 | 63 | **0.34** |
| `http` | 33 | 8 | 9 | **0.48** |
| `json` | 12 | 1 | 5 | **0.50** |

I then hand-checked the 16 `http` badges that survive both filters. Exactly one is real
(`HTTPStatus`, from `server.py:108 from http import HTTPStatus`). The other fifteen matched
English prose (`"""...clear the buffer"""`, `easy to read`, `human-readable`), a string
literal (`'Bad request syntax'`), a comment (`# Always flush before forking`), an unrelated
import (`import copy`), or a parameter name (`def __new__(cls, value, phrase, ...)`).
**1 of 16.**

Why C escapes this and Python does not is structural, and I measured the mechanism:
**28.4% of class-method names** in the stdlib are defined in more than one place, against
10.1% of module-level names. C's linker enforces one global namespace, which is what forces C
codebases into long, distinctive, prefix-carrying names — `legacy_calibrate`, not `read`.
Python's names are short English words (`read`, `send`, `close`, `key`, `value`, `handle`),
and a whole-word search for an English word inside a tree full of English prose returns
English prose. Of the whole-word hits for 40 sampled ambiguous method names, 35% were
attribute accesses on unrelated objects.

**This is the finding that kills the naive proposal.** A regex extension to Python would work
— and would then feed a search that emits three or four wrong badges for every right one, into
a report whose stated discipline is "never render a guess" (`docs/UIUX-GUIDE.md:445`). Today's
silence is bad. That would be worse.

### 2c. `ast` rescues it, and the rescue is not a heuristic

Python is a special case, because the correct extractor is already installed. `ast` also
gives something regex cannot: for each file, the set of names it *really uses* — imported by
name, or free in `Load` context (bound by no assignment, no parameter, no import, not a
builtin). Prose is not a name. A local variable called `key` is not a reference to a deleted
`key`. Same simulation, same corpora:

| corpus | badges, text search | badges, `ast` rule | reduction |
|---|---|---|---|
| `manishtana` | 27 | **14** | 48% |
| stdlib `json` | 12 | **3** | 75% |
| stdlib `http` | 33 | **1** | 97% |
| stdlib `email` | 175 | **25** | 86% |
| stdlib `xml` | 204 | **18** | 91% |

On this package — the one corpus where I can check every single row by hand — **all 14 are
real**: `analyze_changes` used by `cli.py`, `changed_files` and `diff_sections` by
`compare.py`, `prepare_copy`/`skipped_files`/`is_binary` by `compare.py` and `symbols.py`,
`main` by `__main__.py`, `warn` by five modules. **Precision 14/14.** The `http` survivor is
`HTTPStatus`; `json`'s three are `JSONDecoder`/`JSONDecodeError`/`JSONEncoder`, all reached
through `from .decoder import ...`. I spot-checked four stdlib survivors with grep and all
four were genuine imports followed by genuine uses (`from xml.dom.minidom import
_append_child`, then `_append_child(self.document, doctype)` at `expatbuilder.py:242`).

The `ast` rule also gets the "moved, not removed" case for free and better than today's
diff-local check: `_git` is defined in **both** `gitdiff.py:42` and `gitrepo.py:132`. Text
search raises a badge for it. The `ast` rule does not, because the name is still defined.

**Recall cost, measured and honest.** The rule missed five real references in this package —
`gitrepo.resolve`, `gitrepo.commits`, `gitrepo.export`, `gitrepo.intent_text`,
`gitrepo.GitError` — all the `module.function` form, plus `load`, because
`cli.py:26 from .settings import load as load_settings` binds the alias and my first cut
recorded the bound name instead of the imported one. Recording the imported name, and
counting `mod.attr` where `mod` is a name bound by an import statement, recovers all six
(badges 14 → 20 here, 3 → 6 in `json`, 25 → 67 in `email`, 18 → 33 in `xml`) and I could
identify only one false family among roughly 65 gains: `copy.copy(x)` registers a use of a
deleted symbol named `copy` (it appeared in `http` and `xml`). Skipping the case where the
attribute equals its base module's name removes it.

---

## 3. Approach

Three changes, in the order they should land. The first is worth doing even if the other two
are rejected.

### A. Decline loudly (the disclosure)

`symbols.py` gains an extension→language table and a third field:

```
CrossReference   namedtuple(removed, dangling, declined)
```

`declined` is `{language: [path]}` for real changes the cross-reference did not analyse —
unsupported extension, unknown extension, a file that would not parse. `cli.py` passes it
through the existing `shared` dict (`manishtana/cli.py:141`), so no builder signature changes;
`build_report` and `build_html` render it beside the existing section, and it must render
**even when `dangling` is empty**, since that is exactly the case this fixes. Copy along the
lines of: *"Cross-reference not run for 12 files (Ada, Java, unknown). A deletion in these
files would not appear above."*

This meets all five of `docs/UIUX-GUIDE.md:463`'s tests for a new element: it answers a
question the reviewer must answer to act (*did the tool actually look?*), it derives from the
existing `Comparison`, it degrades to absence when the list is empty, it needs no script, and
no neighbour carries it.

### B. Scope the C extractor to C

`_defined_name` runs only on `.c .h .cpp .hpp .cc .cxx .hh .hxx`. This is a bug fix: it
deletes the invented `catch` and `func` symbols, and it moves Java/C#/Go from a silent
partial guess into the honest `declined` list. It costs some genuine Java and C# method
detections that were working by accident — that is the right trade for a tool that prefers
absence to a wrong badge, and those detections were feeding the same broken reference search
anyway.

### C. Python, via `ast` only, both halves

- Definitions: `ast.parse` the changed file in `dir_a` and in `dir_b`; `removed` is what A
  defines and B no longer defines *anywhere in the tree*. No regex, no diff-line reading, no
  "moved" special case.
- References: a file references `S` when it imports `S` by name, uses `S` as a free name, or
  writes `mod.S` where `mod` is import-bound. Nothing else counts.
- Any file that will not parse — Python 2, syntax newer than the running interpreter,
  `RecursionError` on pathological nesting — goes to `declined` with its reason. **This is
  the argument for `ast` over regex that has nothing to do with precision: its failures are
  detectable.** A regex returns confident garbage on a Python 2 file; `ast` raises.

This needs `dir_a`, which `cross_reference` does not currently receive. Signature becomes
`cross_reference(sections, dir_b, dir_a=None)` — `cli.py:117` is the only caller, and the
default keeps the old behaviour. It does not disturb the import chain: `symbols` still
depends on `common` and `rules` only.

**The option I considered and rejected:** do only A, and add no language at all. It is
defensible — A carries most of the honesty win, and B removes the only wrong badges the
module can currently emit. I rejected it because Python is now common in the input, `ast` is
free, and 14/14 measured precision is better than the C extractor has ever been asked to
prove. But if C is cut for cost, cut C, not A.

---

## 4. Which languages are worth it

| language | verdict | why |
|---|---|---|
| **C / C++** | keep, scoped | Works because C has one flat namespace: names are long and globally unique, so a whole-word search is a decent proxy for a reference. |
| **Python** | **take, `ast` only** | The correct extractor is in the standard library; measured badge precision 14/14 on the corpus I can verify by hand; unparseable files decline themselves. |
| **Java, C#** | **decline** | The definition half is easy — they look like C. The reference half is not: methods live in class namespaces (28.4% of Python method names collide; Java and C# add overloads on top), and there is no stdlib parser to compute a `used` set from. A name-only search would inherit the 0.25–0.50 precision ceiling with no way out. |
| **Go** | **decline** | Same reason, plus the receiver form `func (r *Repo) Fetch(...)` defeats the token rule outright and methods are namespaced by receiver type. |
| **Ada** | **decline** | No stdlib parser, no corpus here to validate against, case-insensitive identifiers (so the whole-word regex is wrong before it starts), and `is` introduces bodies, packages, tasks and generic instantiations alike — the "must carry a body" discipline that makes the C rule safe has no cheap equivalent. |

The general rule this settles: **a language earns a cross-reference when the tool can compute
what a file *uses*, not merely what it *defines*.** C earns it by having a namespace flat
enough that text search approximates it. Python earns it because `ast` ships. Nothing else on
the list earns it under the standard-library constraint, and the honest answer for them is a
line in the report saying so.

---

## 5. Cost

**M.** Roughly:

- A (disclosure): ~40 lines in `symbols.py`, ~25 across the three builders, plus tests. Small.
- B (scoping): ~10 lines and a test. Trivial, and it deletes behaviour rather than adding it.
- C (Python via `ast`): ~90–120 lines in `symbols.py`. The binding walk is the bulk of it
  (`ImportFrom` / `Import` / `arg` / `Name`-in-`Store` / nested scopes) and the spike's
  `facts()` is a working 17-line skeleton of it.
- `README.md` limitations section and `docs/UIUX-GUIDE.md` both need updating; a new JSON key
  for `declined` bumps `schema_version` in `build_json`.

Runtime: `ast.parse` measured at **2.5 ms/file**. `_references` already reads every text file
in `dir_b`, so parsing the `.py` subset is the same order of cost. A 5,000-file tree adds
seconds, not minutes.

---

## 6. Risks

1. **`ast.parse` runs under the *tool's* interpreter, not the reviewed code's.** Under the
   Python 3.8 floor, `match` (3.10), `except*` (3.11) and `type X = ...` (3.12) all raise
   `SyntaxError`. That is a decline, not a wrong answer — but on a modern Python drop reviewed
   from a 3.8 box the decline list could be most of the tree, and the tool would look broken
   when it is merely honest. **Unverified: only 3.12 was available on this machine.**
2. **Dynamic references are invisible.** `getattr(mod, "handler")`, `__all__` strings,
   decorator registries, entry points, `importlib`. The `ast` rule under-reports these — the
   right direction, but it means "no badge" still means "no information".
3. **Two vocabularies in one table.** If Python reports `Class.method` and C reports bare
   names, the "Symbol" column stops meaning one thing. Open question below.
4. **Nested definitions.** The spike counted every `FunctionDef` under `ast.walk`, closures
   included. A closure is not a symbol another file can reference, so shipping this unchanged
   would inflate `removed` with names that could never dangle.
5. **Scope creep.** `symbols.py` is 124 lines with a deliberately small idea. Adding a binding
   analysis roughly doubles it, and a second module would violate the one-public-interface
   convention. Worth watching, not fatal.
6. **The `declined` list can be long and boring** on a mixed drop, competing for attention
   with real findings. It is a disclosure, not a finding, and must be recessed accordingly.

---

## 7. Open questions

1. Qualified names: does Python's `removed` report `Class.method` or bare `method`? Qualified
   is more precise and breaks the shared column; bare re-imports the 28.4% ambiguity into
   `removed` even though the `ast` rule keeps it out of `dangling`.
2. Should the reference search become language-aware — i.e. should a removed C symbol stop
   being searched for inside `.py` files? Today it is searched everywhere, which is sometimes
   right (FFI, build scripts) and usually noise.
3. Is `declined` a schema bump, or additive enough to leave `schema_version` at 1? It is a new
   key, so by `CLAUDE.md`'s rule it is additive, not breaking — worth confirming with the
   author.
4. Should `--no-llm` runs get a stronger disclosure than model runs? The cross-reference is
   the whole report in that mode, so its blind spots matter more there.
5. Should B (scoping the C extractor) ship on its own, ahead of everything else? It is a
   correctness fix for a wrong badge, and it does not depend on A or C.

# MaNishtana — rule-based directory comparison

*מה נשתנה* — "what has changed?" Compares two source directories and separates
**substantive change** from **noise**:
formatting, comments, whitespace, and generated timestamps or build numbers. Only
what survives the filter reaches a language model.

Standard library only — no pip, no virtualenv, nothing to install. Built to be
copied onto a closed network and run.

```bash
python3 -m manishtana v1 v2 -c config.json -o report.md -H report.html 2> report.warnings
```

## Contents

| Section | |
|---|---|
| [How it works](#how-it-works) | The pipeline, and what is deterministic versus what the model adds |
| [Requirements](#requirements) | Two required, two optional |
| [Getting it onto a closed network](#getting-it-onto-a-closed-network) | The transfer set: repo, image, OS packages |
| [Usage](#usage) | Flags, exit codes, what goes where |
| [Configuration](#configuration) | Rules, prompts, risk labels, normalizers, tuning |
| [What you get](#what-you-get) | Markdown, HTML and JSON |
| [Tests](#tests) · [Known limitations](#known-limitations) | |

Two companion documents: [`docs/UIUX-GUIDE.md`](docs/UIUX-GUIDE.md) for how to read
the report and the design rules behind it, and [`CLAUDE.md`](CLAUDE.md) for the
architecture and constraints if you are changing the code.

## How it works

**Stage 1 — deterministic. No model.**

Both directories are copied to a temporary directory and the config's rules are
applied to the copies: skipped files are deleted, source files are normalized
(comments stripped, formatting flattened), and generated lines are replaced with a
fixed token on both sides. Then `git diff` runs twice — once on the untouched
originals, once on the normalized copies. **A file that differs in the first pass
but not the second changed only in ways the rules were told to ignore.** That
subtraction is the definition of "noise only"; there is no separate detector.

Also deterministic: a **cross-reference** over the diffs, described below.

| Classification | Meaning |
|---|---|
| added / deleted / modified / renamed | Real change — survived normalization |
| noise only | Differs between the directories, identical after normalization |
| skipped | Matched a `skip` rule — never compared |

**Stage 2 — model analysis, in three passes.**

A file read in isolation cannot be understood, so the model is not simply handed
one diff at a time:

| Pass | Sees | Calls |
|---|---|---|
| **0 — Orientation** | An inventory of the whole change: files, statuses, churn, definitions added and removed, and anything deleted while still referenced. **Not the diffs.** | 1 (skipped for a single file) |
| **1 — Per file** | One file's diff, carrying the orientation brief so it is read in context | N |
| **2 — Reconciliation** | The inventory *and* every per-file analysis, asked for cross-file connections and which earlier conclusions the whole change revises | 1 |

**N + 2 calls** — one more than a naive per-file loop, regardless of drop size.
Pass 0 is cheap because it never carries code: on the sample project the whole
inventory is under 500 characters.

That separation is the point: the model does not spend its context on brace
placement, and the reader does not have to filter it out by hand.

### Cross-reference — computed, not guessed

Alongside the diff, the tool answers one question the model is bad at and search is
good at: **what did this change delete that something still uses?**

For every real change it extracts the definitions added and removed — function
definitions with a body, and `#define`s — then searches the new tree for the
removed names. Anything still named there gets its own section at the top of the
report:

| Symbol | Still named in |
|---|---|
| `legacy_calibrate` | `adc.c` |

A link error, found without a model, reported before the reviewer opens a diff.

Deliberately conservative, and not a compiler:

- A function must carry a body. A prototype moving between headers is not a change
  in what exists.
- A symbol removed from one file and defined in another is a **move**, not a
  removal, and is excluded.
- Matching is whole-word: `legacy_calibrate_v2` is not a reference to
  `legacy_calibrate`.
- It reads text, it does not parse C. A name in a comment or a string counts as a
  reference. **Verify each finding** — the report says so too.

Everything in this section works under `--no-llm`.

## Requirements

| Component | Version | Required? | Used for |
|---|---|---|---|
| Python | 3.8+ | **yes** | Standard library only — no pip dependencies |
| git | 2.30+ | **yes** | `git diff --no-index` is the comparison engine |
| `clang-format` | any | no | Formatting normalization for C/C++ |
| `gcc` | any | no | Powers `strip-comments` (preprocessor only, never a compile) |

You need `clang-format`, **not** clang — a standalone binary (`apt install
clang-format`, or `yum install clang-tools-extra`). Without the two optional tools
the run still produces a report, but formatting and comment changes count as real
changes, because nothing normalized them away.

`git` is the one dependency with no graceful failure: missing, you get a Python
traceback rather than a diagnostic. Check it first.

## Getting it onto a closed network

`airgap.sh` builds everything the target needs into `dist/`, checksums it, and
imports it on the far side.

```bash
./airgap.sh deps debian:12    # optional: the target distro's own .deb/.rpm packages
./airgap.sh pack              # repo bundle + container image + checksums
```

| Artifact | What it solves | Size |
|---|---|---|
| `manishtana.bundle` | The whole repository, **all history, one file**. Clone offline; bundle back out to return work | ~70 KB |
| `manishtana-image.tar.gz` | A container carrying Python, git, clang-format and gcc — no package repository needed | a few hundred MB |
| `deps/<distro>/` | The same four tools as native packages with an `install.sh` | varies |
| `SHA256SUMS`, `IMPORT.txt`, `airgap.sh` | Verification, instructions, and the importer itself | small |

Carry only what the target lacks:

| Target has | Carry |
|---|---|
| Python 3.8+ and git | the bundle alone |
| Docker | bundle + image |
| Neither | bundle + `deps/` |

**On the far side**, copy the whole `dist/` directory across, then:

```bash
./airgap.sh verify     # refuses to continue if the media corrupted anything
./airgap.sh import     # clones the repo, loads the image if one is present
./airgap.sh selftest   # runs the suite HERE, and prints the tool versions
```

`selftest` is the step people skip and should not: it proves the tool runs on
*that* box, not merely that the bytes arrived. It probes for a working interpreter
rather than trusting `PATH` — a `python3` that answers but does not run is a real
thing, and it would otherwise let the suite pass while testing nothing.

The checksum catches **corruption, not tampering**: if `SHA256SUMS` rides the same
media, anyone who alters a file can regenerate it. If tampering is in your threat
model, carry the digest through a separate channel.

### Installing the dependencies natively

`./airgap.sh deps <base-image>` downloads git, clang-format, gcc and python3 with
their transitive dependencies, from inside a container of the distro you name, so
architecture and versions match. On the far side, as root:

```bash
sh deps/debian-12/install.sh --dry-run    # check nothing is missing
sh deps/debian-12/install.sh
```

**Resolution happens against that container's package state**, so name the image
your target was installed from. `debian:12` packages will not install on Rocky 9,
and a target more minimal than the image may still want a dependency the image
already had — which is what `--dry-run` is for.

### Without any of that

The package is 253 KB of Python. Copying `manishtana/`, `tests/` and `config.json` by
hand works, and `manishtana/` must land whole — the modules import each other, so a
partial copy fails at import. Confirm with `python3 -m manishtana --help` and
`python3 -m unittest discover -s tests` before you need it.

### Running the container

```bash
mkdir -p out
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$PWD/v1:/work/v1:ro" -v "$PWD/v2:/work/v2:ro" \
  -v "$PWD/config.json:/work/config.json:ro" -v "$PWD/out:/work/out" \
  manishtana v1 v2 -c config.json -o out/report.md -H out/report.html \
  2> out/report.warnings
```

`WORKDIR` is `/work`; the package lives at `/opt/manishtana`, outside it, so a mount
cannot shadow it. Two things that will bite you if you skip them:

- **Pass `--user "$(id -u):$(id -g)"`**, or the reports land owned by root. The
  image sets no `USER` precisely so the caller can choose one.
- **Mount under short names.** The two directory arguments are printed verbatim as
  the side-by-side pane headers.

`llm.base_url` must be reachable from **inside** the container, so `localhost`
means the container itself. Use `host.docker.internal`, the host's LAN address, or
`--network host` on Linux.

### Sending work back out

A bundle works in both directions. Commit on the closed network, then
`git bundle create outbound.bundle --all`, carry it out, and `git fetch` from it.

## Usage

```bash
python3 -m manishtana DIR_A DIR_B -c config.json -o report.md
python3 -m manishtana DIR_A DIR_B -c config.json -o report.md --no-llm
python3 -m manishtana DIR_A DIR_B -c config.json -o report.md -H report.html -j report.json
```

| Flag | Default | Meaning |
|---|---|---|
| `DIR_A` | — | The old version. Order is meaning: A is the left pane and the `-` side |
| `DIR_B` | — | The new version |
| `-c`, `--config` | `config.json` | Config file. Prompt paths resolve against **its** directory |
| `-o`, `--output` | `report.md` | Markdown report — always produced |
| `-H`, `--html` | off | Also write a self-contained HTML report |
| `-j`, `--json` | off | Also write JSON for automated consumers |
| `--no-llm` | off | Skip model analysis; everything deterministic still runs |

### Comparing two revisions of a git repository

The two directories can come from a repository instead of a delivery. Both
revisions are exported to a temporary pair of trees and the same comparison runs
over them, so every rule, every output and every flag above behaves identically.

```bash
python3 -m manishtana --git /path/to/repo --commit HEAD -o report.md
python3 -m manishtana --git /path/to/repo --from v2.06 --to v2.12 -o report.md
python3 -m manishtana --git /path/to/repo --from origin/main            # --to defaults to HEAD
```

| Flag | Default | Meaning |
|---|---|---|
| `-g`, `--git` | off | Compare two revisions of this repository. `DIR_A`/`DIR_B` are then omitted |
| `--commit` | — | Review one commit: `REV^` to `REV`. Not combinable with `--from`/`--to` |
| `--from` | — | The old revision |
| `--to` | `HEAD` | The new revision |
| `--intent-context` | off | Also give the commit messages to the model — see below |

The export uses `git archive`, so nothing in your working tree is touched, no
`.git` directory reaches the comparison, and a dirty checkout does not affect the
result. A revision that does not resolve is an error before anything is exported.

**The commit messages are read, and by default withheld from the model.** They
appear in the report under *Stated intent*, beside an analysis written from the
diff alone. That is the point: you can then ask whether the change does what its
message claims, which is a question the report cannot answer if the message was
what it was told. `--intent-context` supplies them as context instead, and the
report then says so in place of the independence note — agreement with the
message is not evidence when the model was shown the message.

One line reaches stdout, on success only:

```
Report written to report.md — 7 real changes, 3 noise only, 1 skipped.
```

Everything else — every warning, every error — goes to stderr. **Capture it.**
Warnings never appear in the report, and some change how much you should trust it.
Exit codes: `0` written (even with warnings), `2` bad input or a failed write, `1`
an uncaught exception.

## Configuration

Every input is declarable. Rules were always external; the prompts, the risk labels
and the normalizer command lines now are too, so a reviewer can read and approve
the exact text that leaves the box without reading Python.

```json
{
  "llm": {
    "base_url": "http://localhost:8000/v1",
    "api_key": "",
    "model": "PLACEHOLDER-MODEL-NAME",
    "temperature": 0.2,
    "max_tokens": 1200,
    "max_chars_per_call": 24000,
    "max_files": 50,
    "timeout_sec": 180
  },
  "rules": [
    { "match": ["*.o", "*.log"], "skip": true },
    {
      "match": ["*.c", "*.h"],
      "normalize": ["strip-comments", "clang-format"],
      "ignore_lines": ["^\\s*#define\\s+BUILD_NUMBER\\b"]
    },
    { "match": ["*.json"], "normalize": ["json-sort"] }
  ]
}
```

Every section below `llm` and `rules` is **optional** and defaults to the built-in
values, so an existing config keeps working untouched.

### `rules` — the filter

| Field | Type | Meaning |
|---|---|---|
| `match` | list | `fnmatch` patterns tested against the relative path **and** the basename |
| `skip` | bool | Deleted from both copies — never compared |
| `normalize` | list | Commands run in order, stdin to stdout |
| `ignore_lines` | list | Every matching line becomes a fixed token on both sides |

**First matching rule wins**, and there is no negation — order specific before
general. A file matched by no rule is compared raw. Matching is case-insensitive on
Windows and case-sensitive on Linux, because `fnmatch` normalises case.

Why `ignore_lines` rather than git's `-I`: `-I` is global, so you cannot ignore one
regex in `.c` and a different one in `.json` in a single invocation. Substituting a
token on both sides achieves per-type ignoring inside one `git diff`, and preserves
line counts so numbering does not shift.

### `prompts` — what the model is told

```json
"prompts": {
  "orient_system":  "@prompts/orient.md",
  "file_system":    "@prompts/review-file.md",
  "summary_system": "@prompts/summarize.md",
  "file_user":      "{context}File: {path}\n\n```diff\n{diff}```"
}
```

An `@` prefix means "read this file", resolved against the config's directory, so
prose stays reviewable Markdown instead of JSON escapes.

| Key | Placeholders (required in bold) |
|---|---|
| `orient_system` | none |
| `orient_user` | **`{manifest}`** |
| `file_system` | none |
| `file_user` | **`{diff}`**, `{path}`, `{context}` |
| `part_user` | **`{diff}`**, `{path}`, `{context}`, `{part}`, `{parts}` |
| `context_block` | **`{brief}`** — how pass 0's output is introduced to pass 1 |
| `summary_system` | none |
| `summary_user` | **`{analyses}`**, `{manifest}` |
| `summary_item` | **`{analysis}`**, `{path}` |

Placeholders are validated at load: an unknown one, a missing required one, or a
typo'd key is a startup error with exit 2 — never a silently mangled prompt.

### `risk` — the labels extraction looks for

```json
"risk": { "file_label": "Risk", "summary_label": "Overall risk" }
```

**These must move with the prompts.** The extractor looks for the literal label the
prompt asks the model to write, so translating a prompt without changing the label
makes every risk badge silently disappear. They are one coupled input, which is why
they sit together.

### `normalizers` and `tuning`

```json
"normalizers": { "clang-format": "clang-format -style=LLVM -assume-filename={name}" },
"tuning": { "normalize_timeout_sec": 60, "ignored_token": "<<ignored-by-rule>>" }
```

Built-in aliases are `clang-format`, `strip-comments` (`gcc -fpreprocessed -dD -E -P
-x c -`) and `json-sort` (the running interpreter, so it needs nothing on `PATH`).
Any other string is run as a shell command as written. `{name}` is substituted with
the basename **already quoted for the shell** — do not add quotes of your own.
Filenames come from someone else's tree, and an unquoted `{name}` would let a file
called `x&mkdir OWNED&.c` run a command during the comparison.

Still compiled in, deliberately: the three lines of diff context, rename detection,
and the 0.5 similarity floor for character-level marking. Say the word if you want
them declarable too.

## What you get

| Output | Audience | Notes |
|---|---|---|
| `-o` Markdown | You, a ticket, a terminal | Unified diff, disclosures collapsed |
| `-H` HTML | A human reviewer, offline | Self-contained, side-by-side, no JavaScript |
| `-j` JSON | Agents, CI, review bots | `schema_version: 1` |

The report opens with the whole-change brief, the executive summary, and any
cross-reference findings — then the file table, then each change.

The HTML renders diffs **side by side with line matching**: two panes headed with
the directory each belongs to, an edited line carrying old and new on one row with
only the differing characters marked, insertions and deletions leaving the opposite
pane blank, and `⋯` where hunks were skipped. Hovering either side highlights its
counterpart, because corresponding lines share a table row — no script involved.
See [`docs/UIUX-GUIDE.md`](docs/UIUX-GUIDE.md) for the full visual language.

The JSON carries `brief`, `summary`, `counts`, `cross_reference` (`removed` and
`dangling`), `inputs.prompts_sha256` — a fingerprint of the exact prompt set used,
so a report is traceable to what produced it — and per-file `risk`, `analysis` and
`diff`.

**`risk` and `analysis` are genuinely nullable.** Binaries are never analysed, a
dead endpoint yields nothing, and files past `max_files` get no prose. Combined
with `llm_ran`, that is the branch an automated consumer must handle. The risk
level is regex-extracted from prose and is `null` whenever no level can be
extracted — it never means Low.

## Tests

```bash
python3 -m unittest discover -s tests
```

139 tests over the pure logic — rule matching, line ignoring, path handling, diff
splitting and alignment, symbol extraction and cross-referencing, settings
validation, the three-pass call sequence, risk extraction, and Markdown, HTML and
JSON rendering including HTML escaping. No git, no normalizers, no network.

## Known limitations

- **Diff line numbers refer to the normalized files**, not the originals. After
  comment-stripping and reformatting the numbering shifts and the text differs. Use
  the diff to understand the change; find it in the original by symbol name.
- **"Noise only" is asserted without evidence.** The report does not show what
  changed in a file it filtered, so the verdict cannot be audited from the report
  alone. Skipped files are disclosed as a blind spot; this one is the same and
  deserves the same treatment.
- **A degraded run looks like a clean one.** Warnings go only to stderr and the exit
  code is 0 either way. Capture stderr and keep it with the report.
- **First matching rule wins, and there is no negation.** You cannot express "all
  `.c` except `generated_*.c`"; put the specific rule first.
- **Files that are not valid UTF-8 are compared un-normalized** and warned about, so
  their formatting changes count as real. Decoding them lossily could make two
  genuinely different files look identical, which is the worse failure.
- **The cross-reference reads text, not C.** A name in a comment counts as a
  reference.
- **In `--git` mode the report's "Directory A/B" rows name temporary paths** that
  no longer exist once the run finishes. The revisions are on stdout and in the
  *Stated intent* section; the header rows are not yet the place to look.
- A failed model call does not stop the run — a warning goes to stderr and the
  report is produced without that analysis.
- Skipped files are never compared, so the report cannot say what changed in them.

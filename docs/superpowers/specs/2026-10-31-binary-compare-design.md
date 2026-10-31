# Binary compare — design

Date: 2026-10-31. Status: approved in conversation; implemented autonomously in four
stages while the owner was away. Decisions made without the owner are marked
**[Decided]** with the reason, so they can be reviewed and reversed.

## Problem

Today a binary that survives the skip rules is reported as
`Binary file — not analyzed, no diff shown.` Two questions go unanswered:

1. **Did it really change, or was it only rebuilt?** A rebuild changes timestamps,
   checksums and build IDs even when the code is identical.
2. **If it really changed, what changed?** Which functions were added, removed or
   modified, and what does the modified code now do?

## Shape: two layers

```
binary file pair
   │
   ├─ layer 1 (always, stdlib only) ── noise check ──► "noise only"  (stop)
   │                                   │
   │                                   └─ real ──► summary: sizes, hashes,
   │                                               differing byte ranges, strings
   │
   └─ layer 2 (optional, only for real binaries) ── function-level engines
            diaphora (IDA + Hex-Rays) · bindiff (BinExport + BinDiff) · ghidriff (Ghidra)
            ──► one merged function table, decompiled diffs to the model
```

### Layer 1 — noise check (stage 1)

The tool's core trick already defines noise: `git diff` runs on the originals and on
rule-normalized copies; what differs only in the first pass is noise. Binaries were
copied untouched, so a rebuilt binary was always "real".

Layer 1 adds a built-in normalize step, `binary-stamps`, that rewrites the *copy* of a
binary with its known build-noise fields set to zero. The existing double diff then
classifies a rebuild-only binary as **noise only** with no new detector.

Fields erased (module `manishtana/binnoise.py`, parsed with `struct`):

| Format | Field | Why it is noise |
|---|---|---|
| PE | COFF `TimeDateStamp` | link time |
| PE | optional header `CheckSum` | derived from the bytes, changes whenever anything does |
| PE | each debug directory entry's `TimeDateStamp` | link time again |
| PE | CodeView `RSDS` GUID and age | fresh per link, ties the binary to its PDB |
| ELF | `.note.gnu.build-id` descriptor | hash or random per build |
| ELF | `.gnu_debuglink` CRC | CRC of the separate debug file |

Anything else (unknown formats, Mach-O, firmware) is left as is — never guessed at.

**[Decided]** The step is opt-in per rule, like every other normalizer:
`{"match": ["*.exe", "*.dll", "*.so"], "normalize": ["binary-stamps"]}`. The default
config still skips `*.o *.a *.so *.exe`; changing a default skip into analysis would
silently change existing users' reports.

For binaries that still differ, `binnoise.summarize(a, b)` gives: format, both sizes,
both SHA-256, the number of differing byte ranges (same-offset comparison) and the
first few, and printable strings (≥ 6 chars) added and removed, capped.

### Layer 2 — function-level engines (stages 2–4)

Module `manishtana/binengines.py`. One result type:

```
FunctionChange(name_a, name_b, status, similarity, engines, pseudo_diff)
status: "modified" | "added" | "removed"
```

Adapters, each turning one tool's native output into that type:

| Engine | Stage | Runs | Reads |
|---|---|---|---|
| diaphora | 2 | `idat -A -S<diaphora.py>` per side (export), then `python diaphora.py a.sqlite b.sqlite -o d.sqlite` | Diaphora's result SQLite (`results`, `unmatched`) plus `pseudocode` from the exports |
| bindiff | 3 | BinExport per side (IDA plugin, or Ghidra extension), then `bindiff --primary --secondary --output_dir` | the `.BinDiff` SQLite (`function` table) |
| ghidriff | 4 | `ghidriff old new -o dir` (headless Ghidra through pyghidra) | its JSON |

**Tool discovery** — config `binary` section path first, then environment
(`IDADIR`, `GHIDRA_INSTALL_DIR`, `DIAPHORA_DIR`, `BINDIFF_DIR`), then `PATH`.

**Flags**
- `--check-binary-tools` prints what was found and where, and which engines are
  therefore runnable, then exits 0.
- `--binary-engines LIST` — comma list from `diaphora,bindiff,ghidriff`, or `none`.

**[Decided]** Default: run **every available engine** and merge. Functions are keyed
by name (old name, else new name). Where engines disagree on status, the merged
entry records each engine's view and the report flags it as a disagreement.

**The run seam.** Adapters take a `run(argv, cwd, env, timeout) -> (code, output)`
callable, the same pattern as the `chat` seam in `llm.py`, so tests never launch
IDA or Ghidra.

**Failure policy** (unchanged project rule): a missing tool or a failed engine warns
once and the run continues with what it has. Layer 1 always produces a result.

### Model analysis

**[Decided]** Decompiled diffs reach the model through the existing per-file pass, not
a new pass: `cli` gives `analyze_changes` a comparison whose `sections` carry a
unified diff of the changed functions' pseudo-code for each binary that has one, and
removes those binaries from the "not analysed" set. `llm.py` is unchanged. The usual
`max_chars_per_call` splitting applies.

### Output

- Markdown and HTML: the binary block shows the layer 1 summary, and when layer 2 ran,
  a function table (function, status, similarity, engines) with disagreements marked.
- JSON: each real binary gets a `binary_detail` object; `schema_version` 2 → 3.

## Stages and branches

Each stage on its own branch, branched from the previous stage's branch:

1. `feature/binary-noise` — layer 1, `binary-stamps`, summary, report block, JSON v3
2. `feature/binary-diaphora` — engine seam, discovery, flags, Diaphora adapter, model input
3. `feature/binary-bindiff` — BinDiff adapter, multi-engine merge and disagreement
4. `feature/binary-ghidriff` — ghidriff adapter

## Testing

- Unit tests stay stdlib-only, no subprocesses: synthetic PE/ELF built with `struct`;
  fake Diaphora and BinDiff databases built with `sqlite3`; fake ghidriff JSON; a
  fake `run`.
- Real-binary checks (not in the suite): two jq releases (1.7 and 1.7.1, Windows PE
  and Linux ELF) downloaded from GitHub, plus a re-stamped copy of each to simulate a
  rebuild. Ghidra 12.2 is installed on the dev box for the ghidriff (and, if the
  extension matches, BinExport) runs.
- **IDA is not on the dev box.** The Diaphora adapter and the IDA BinExport path are
  verified against the tools' documented formats only, and must be checked on the
  air-gapped network. The worklog lists exactly what to run.

# Worklog — binary compare

How this feature was built, stage by stage, so it can be replayed on another copy of
the repository (the air-gapped one) that has drifted with its own small changes.
Design: [`docs/superpowers/specs/2026-10-31-binary-compare-design.md`](../superpowers/specs/2026-10-31-binary-compare-design.md).

## Way of working

1. **Spec first.** The design is agreed and written down before code.
2. **One branch per stage, chained.** Each stage branches from the previous stage's
   branch, so every stage is reviewable on its own and the chain shows the order.
3. **Tests with the code.** Every behaviour gets a stdlib `unittest` in
   `tests/test_manishtana.py`; the suite never launches git subprocesses, IDA,
   Ghidra or the network. External tools sit behind a seam (`run`, like `chat`).
4. **Real check after tests.** Each stage is then exercised on real downloaded
   binaries (jq 1.7 vs 1.7.1, PE and ELF) and the result is recorded below.
5. **Decisions are written down** where they were made, marked **[Decided]**.
6. **Project rules hold** (`CLAUDE.md`): stdlib only, Python 3.8+, `%`-formatting, no
   annotations, docstrings explain *why*, README and JSON `schema_version` updated with
   behaviour.

## Replaying on the air-gapped copy

On this machine, export each stage as patches (one file per commit):

```bash
git format-patch main..feature/binary-noise            -o patches/1-noise
git format-patch feature/binary-noise..feature/binary-diaphora -o patches/2-diaphora
git format-patch feature/binary-diaphora..feature/binary-bindiff -o patches/3-bindiff
git format-patch feature/binary-bindiff..feature/binary-ghidriff -o patches/4-ghidriff
```

(or one `git bundle create binary.bundle main..feature/binary-ghidriff` to carry the
branches themselves.)

On the air-gapped copy:

```bash
git switch -c feature/binary-noise          # from its own main
git am -3 patches/1-noise/*.patch
python -m unittest discover -s tests        # must pass before the next stage
git switch -c feature/binary-diaphora
git am -3 patches/2-diaphora/*.patch
# ...and so on
```

When `git am -3` stops on a conflict, use the per-stage notes below: each change is
described by **intent** ("prepare_copy rewrites binaries when the rule asks for
binary-stamps"), so it can be re-applied by meaning when the surrounding lines
differ. Then `git add` the file and `git am --continue`.

---

## Stages

(Each stage section is filled in when the stage is done.)

### Stage 1 — `feature/binary-noise` (from `main` @ `1cc3571`)

**Goal:** a rebuilt binary is "noise only"; a really changed one is described.

| Commit | What and why |
|---|---|
| `056cec9` | Spec and this worklog. |
| `6a75e73` | Layer 1. See intents below. |

**Intent of each change** (use these when a patch does not apply cleanly):

1. **New `manishtana/binnoise.py`**: no dependencies inside the package.
   `normalize(data)` zeroes PE/ELF build-noise fields and returns `(bytes, fmt)`;
   unknown or malformed input comes back unchanged with `fmt=None`.
   `summarize(a, b)` gives sizes, SHA-256 (originals), differing regions (on
   *normalized* bytes, 4 KiB block scan then exact trim) and strings ≥ 8 chars.
2. **`rules.py`**: new public `BINARY_STEP = "binary-stamps"`. In `prepare_copy`,
   read `steps` *before* the `is_binary` check. A binary whose rule lists the step is
   rewritten by `_rewrite_binary` (calls `binnoise.normalize`). Text files drop the
   step from their list so it is never run as a shell command.
3. **`compare.py`**: new public `binary_pairs(comparison, dir_a, dir_b)` →
   `{rel: (old path | None, new path | None)}`. Renames pair old path in A with new
   path in B.
4. **`cli.py`** `_run`: after `compare`, build
   `binary = {rel: summarize(old, new) for ...binary_pairs(...)}` and add
   `"binary": binary` to the `shared` kwargs passed to all three renderers.
5. **`output.py`**: all three `build_*` take `binary=None`. Markdown and HTML show
   `_binary_facts(summary)` as a bullet list in place of "Binary file — not
   analyzed" (still shown when there is no detail). JSON adds `binary_detail` per
   real file and `schema_version` goes to **3**. CSS: `.facts` list style.
6. **Tests**: new `tests/test_binary.py` (synthetic PE32+, ELF64-LE and ELF32-BE
   built with `struct`); `test_manishtana.py` only changes the schema version
   assertion from 2 to 3.
7. **Docs**: README "Binaries — rebuilt or really changed?" section, JSON notes,
   one limitation; CLAUDE.md dependency line for `binnoise`.

**[Decided]** The default `config.json` keeps skipping `*.o *.a *.so *.exe`.
Opting in is a one-rule change, and a silent default change would alter existing
reports.

**[Decided]** `--check-binary-tools` moved from stage 1 to stage 2: layer 1 has no
external tools to check, and tool discovery belongs with the engines that need it.

**Verification**

- `python -m unittest discover -s tests` → 179 tests OK.
- Real binaries: jq 1.7 and 1.7.1 release builds from GitHub (Windows PE, MinGW;
  Linux ELF, static). Fixture: `v1/` and `v2/` with
  - `jq.exe`, `jq`: 1.7 vs 1.7.1 → **modified**, with a string diff showing the real
    source change (`def paths` rewritten).
  - `rebuilt.exe`: 1.7 vs 1.7 with COFF time stamp and checksum rewritten by a
    separate script (byte offsets, not binnoise) → **noise only**.
  - `rebuilt`: 1.7 vs 1.7 with the GNU build-id rewritten → **noise only**.
  - `patched.exe`: re-stamped *and* one code byte flipped at 0x1000 →
    **modified**, region exactly `0x1000–0x1001` (stamps excluded).
  - Config: `{"rules": [{"match": ["*.exe", "jq", "rebuilt"], "normalize": ["binary-stamps"]}]}`
  - Run: `python -m manishtana v1 v2 -c config.json -o r.md -H r.html -j r.json --no-llm`
    → `3 real changes, 2 noise only, 0 skipped`.
- Real-world observation: the jq PE (MinGW) has no debug directory, so only the
  link time and checksum are erased there. MSVC builds also carry CodeView and
  REPRO entries, which the synthetic test covers.

**To re-check on the air-gapped side:** take two builds of the same commit of one
of your own binaries (rebuild twice), put one in each tree, add the rule above, and
expect `noise only`. If it says `modified`, the region list shows which bytes still
differ: that is a noise field this step does not know yet. Typical causes are
embedded build paths or `__DATE__`/`__TIME__` strings.

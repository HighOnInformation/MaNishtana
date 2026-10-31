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

### Stage 2 — `feature/binary-diaphora` (from `feature/binary-noise` @ `5213762`)

**Goal:** for a binary that really changed, list the functions that changed, using
IDA + Diaphora, and give the decompiled diff to the model.

| Commit | What and why |
|---|---|
| `e16e68b` | Engine layer + Diaphora adapter. See intents below. |

**Intent of each change**

1. **New `manishtana/binengines.py`**: no dependencies inside the package.
   - `discover(settings.binary)` → `{idat, hexrays, diaphora, python}`. The order
     is config path (file or directory), then `IDADIR` / `DIAPHORA_DIR`, then
     `PATH`, then `C:\Program Files\IDA*` on Windows. It prefers `idat64` over
     `idat`, because before IDA 9 plain `idat` is the 32-bit build. Hex-Rays
     counts as present when `plugins/hexx64*` or `plugins/hexrays*` exists.
   - `runnable(tools)`, `describe_tools(tools)` (the `--check-binary-tools` text).
   - `diff_functions(old, new, engines, tools, settings.binary, run=None)` →
     `FunctionDiff(engines_that_succeeded, changes, notes)`.
   - `FunctionChange(name_a, name_b, status, similarity, views, pseudo_diff)`.
   - Diaphora adapter:
     - It copies each side into a private temp directory.
     - It runs `idat -A -B -S<diaphora.py> -L<log> <copy>` with
       `DIAPHORA_AUTO=1`, `DIAPHORA_EXPORT_FILE`, and `DIAPHORA_USE_DECOMPILER=1`
       if Hex-Rays was found.
     - It then runs `python diaphora.py old.sqlite new.sqlite -o diff.sqlite`.
     - It reads `results` (ratio < 1 means modified) and `unmatched` (`primary`
       means removed, `secondary` means added), plus `functions.pseudocode` from
       both exports.
   - `_merge` is already multi-engine: one entry per function, `views` per
     engine, "unchanged" for an engine that ran and did not list it. It was written
     here rather than in stage 3 because the result type needed its final shape.
   - `decompiled_diff(changes, limit)`: the most changed first, capped.
2. **`settings.py`**:
   - New `Settings.binary` section, validated like the others: `engines`, `ida`,
     `diaphora`, `python`, `timeout_sec`, `max_functions`, `max_decompiled`.
   - New prompt `decompiled_header`, which tells the model it is reading
     decompiler output.
3. **`cli.py`**:
   - `--check-binary-tools` prints and exits 0.
   - `--binary-engines LIST|none`. An unknown name is a usage error (exit 2).
   - `_binary_functions` runs the usable engines on binaries present on both
     sides, and warns once for engines that are missing.
   - Binaries with pseudo-code get `decompiled_header + diff` as their section and
     are removed from `binaries` in the comparison handed to `analyze_changes`.
     `llm.py` is unchanged.
   - Renderers get `functions`, `decompiled` and `max_functions`.
4. **`output.py`**: the function table (with an "engines disagree" flag), the
   "no function changed" case, the decompiled diff disclosure, and the binary's
   model analysis. JSON gains `binary_functions`, still schema 3 (not released
   between stages).
5. **Tests** (`tests/test_binary.py`): `FakeEngines` stands in for `run` and
   writes Diaphora-shaped SQLite. It covers discovery, classification, pseudo-code,
   the exact command lines and environment, quoting of a path with spaces, no
   Hex-Rays, a failed export, the decompiled-diff cap, rendering and the CLI flags.
6. **Docs**: the README section on function-level comparison, JSON fields and
   two limitations; the CLAUDE.md dependency line.

**[Decided]** The default `binary.engines` is `["diaphora"]` in this stage. It
widens as engines are added.

**[Decided]** Engines run only on binaries present on both sides. An added or
deleted binary has nothing to diff against, so it gets layer 1 only.

**Verification**

- Unit tests: 197 OK.
- **Real Diaphora, fake IDA.** IDA is not on the dev box. Diaphora was cloned
  (commit `35c7657`, 2026-10-25) and two export databases were built with
  Diaphora's own schema (`db_support/schema.py`), each holding three functions.
  Diaphora's real diff step (`python diaphora.py old.sqlite new.sqlite -o
  diff.sqlite`) ran on them. It stores the ratio as text (`'1.0000000'`); the
  adapter reads it correctly. Diaphora paired a removed function with an added one
  as a rename (`legacy_dump → fresh_feature`, 0.846).
- **Full pipeline.** A stand-in `idat64.bat` writes those exports when called with
  Diaphora's protocol. The rest is real: the real Diaphora diff, the real jq
  binaries for layer 1, and a mock OpenAI-compatible endpoint recording requests.
  Results:
  - The idat command line and environment were exactly as designed.
  - The report shows the function table and the decompiled diff.
  - The model got 5 calls (orientation, 3 binaries, summary), each per-file call
    beginning with `decompiled_header` and then the pseudo-code diff.
- **Not verified here:** a real IDA export. On the air-gapped box:
  1. `python -m manishtana --check-binary-tools -c config.json` should show IDA,
     Hex-Rays and Diaphora as found and `engine diaphora ready`.
  2. Run on two builds of one of your binaries with `--no-llm` first. The function
     table should list real function names.
  3. If the export fails, the warning carries the last lines of IDA's output. The
     IDA log is in the temp directory while the run is going (it is deleted after).

### Stage 3 — `feature/binary-bindiff` (from `feature/binary-diaphora` @ `940f508`)

**Goal:** add BinDiff as a second engine, through IDA or Ghidra, and make engine
disagreement visible.

| Commit | What and why |
|---|---|
| `b773c33` | BinDiff adapter, the stdin fix, `binary.java`, and the BinExport-for-Ghidra-12 fix. |

**Intent of each change**

1. **`binengines.py`**
   - `ENGINES` gains `bindiff`. `_NEEDS` becomes a list of *alternatives* per
     engine: BinDiff needs `bindiff` plus either `idat` with `binexport_ida`, or
     `ghidra` with `binexport_ghidra`. `_missing()` reports the nearest
     alternative's missing tools.
   - **Discovery:**
     - `bindiff`: the config, then `BINDIFF_DIR`, then `C:\Program Files\BinDiff`
       or `/opt/bindiff`, then `PATH`.
     - `binexport_ida`: `binexport*_ida*` in IDA's `plugins`, `IDAUSR`,
       `%APPDATA%\Hex-Rays\IDA Pro\plugins` or `~/.idapro/plugins`.
     - `ghidra`: the config or `GHIDRA_INSTALL_DIR`, recognised by
       `support/analyzeHeadless`.
     - `binexport_ghidra`: `Ghidra/Extensions/BinExport/lib/BinExport*.jar` or the
       user's extension directory.
     - `java`: the new config key.
   - **Adapter:**
     - It prefers IDA:
       `idat -A -OBinExportModule:<out> -OBinExportAutoAction:BinExportBinary <copy>`.
     - Otherwise it uses Ghidra:
       `analyzeHeadless <proj> p -import <copy> -scriptPath <dir> -postScript ManishtanaBinExport.java <out> -deleteProject`.
       The post-script is the string `_GHIDRA_BINEXPORT_SCRIPT`, written to the
       temp directory.
     - Then it runs
       `bindiff --primary=… --secondary=… --output_dir=… --output_format=bin`.
     - It reads the `function` table (similarity < 1 means modified). Added and
       removed come from the `.BinExport` call graphs (NORMAL functions only),
       read by the stdlib protobuf reader (`_proto_fields`, `_varint`).
   - **`_run` passes `stdin=DEVNULL`.** Found the hard way: Ghidra's launcher asked
     for a JDK path and waited on "Press any key".
   - `_env_for(tools, ghidra=True)` sets `JAVA_HOME` from `binary.java`.
2. **`settings.py`**: `binary` gains `bindiff`, `ghidra` and `java`, and the
   default `engines` becomes `["diaphora", "bindiff"]`.
3. **Tests**: `FakeBinDiff` writes real protobuf bytes, encoded by `pb_field` in
   the test, plus a `.BinDiff` table with BinDiff's real column list. The tests
   cover discovery, either exporter being enough, IDA preferred, the Ghidra
   post-script and `JAVA_HOME`, a failure becoming a note, the protobuf reader
   (types, unnamed functions, 64-bit addresses, unknown fields), merging two
   engines, the "unchanged" view, and the disagreement flag in the report.
4. **Docs**: README rows and config, discovery rules, two limitations, and
   [`docs/worklog/binexport-ghidra12/`](binexport-ghidra12/README.md) with the
   patch and the rebuild steps.

**[Decided]** BinDiff prefers IDA's exporter when both are present. BinExport's
own README calls the Ghidra one experimental.

**[Decided]** Added and removed functions count only `NORMAL` functions (not
library, imported or thunk), so imports appearing or disappearing do not flood
the table.

**[Decided]** BinDiff 8's own BinExport is patched rather than built from upstream
main. The newer prebuilt (for Ghidra 11.0.3) fails on Ghidra 12 with a
`NoSuchMethodError`, and the old one needed one line.

**Verification (real tools, real binaries)**

- Unit tests: 208 OK.
- **Tools installed for the check:**
  - Ghidra 12.2 (zip from GitHub, unpacked to `~/tools`).
  - Temurin JDK 25 (zip). Ghidra 12 refuses JDK 21.
  - BinDiff 8 (MSI, unpacked with `msiexec /a`, no install).
- **BinExport on Ghidra 12.2**, three attempts:
  1. As shipped, with only `version=12.2` set: it loaded and ran, then threw a
     `NullPointerException` in `buildFlowGraphs`.
  2. The newer release: `NoSuchMethodError` (`DefinedDataIterator.definedStrings`).
  3. The shipped one with the one-line fix: works. Each side of jq exported in
     about 65 s.
- Ghidra exits 0 when the post-script fails, so the adapter judges success by
  whether the export file exists. That is correct; it caught the failure above.
- **Real `.BinDiff` database:** the tables are `function`, `basicblock`,
  `instruction`, `file`, `metadata` and the two algorithm tables. The `function`
  columns are exactly as read. The protobuf reader parsed 949 NORMAL functions
  from the real jq `.BinExport`. `binexport2dump` (from BinDiff) agrees on the file.
- **Full CLI, `--no-llm`, engine bindiff (5m45s for two pairs):**
  - `patched.exe` (one byte flipped at 0x1000): **1 modified** (`sub_140001910`,
    0.98).
  - `jq.exe` 1.7 → 1.7.1: **31 modified, 3 added, 35 removed**.
- **Not verified here:** BinExport through IDA (no IDA on the dev box). On the
  air-gapped box, `--check-binary-tools` should list `BinExport plugin for IDA`
  (BinDiff's installer drops `binexport12_ida64.dll` into
  `BinDiff\Plugins\IDA Pro`; copy it into IDA's `plugins`).

### Stage 4 — `feature/binary-ghidriff` (from `feature/binary-bindiff` @ `99a0d95`)

**Goal:** a third engine, ghidriff, which needs Ghidra but no IDA licence.

| Commit | What and why |
|---|---|
| `a528702` | ghidriff adapter, settings, tests, and the ghidriff-for-Ghidra-12 fix. |
| `591bd96` | **Bug found by the real run:** default function names merge by address. |
| (this commit) | Worklog, and the check scripts in `docs/worklog/checks/`. |

**Intent of each change**

1. **`binengines.py`**
   - `ENGINES` gains `ghidriff`, which needs `ghidriff` and `ghidra`.
   - `_find_ghidriff(given)` takes a file or a directory such as a virtualenv
     (looking in `Scripts/` and `bin/`), then falls back to `PATH`.
   - **Adapter:**
     - It copies the two sides as `old_<name>` and `new_<name>`, since both go
       into one Ghidra project.
     - It runs
       `ghidriff --no-symbols -o out -p projects -s symbols -g gzfs --log-path … old new`
       with `GHIDRA_INSTALL_DIR` and `JAVA_HOME` set (`_env_for` sets both now).
     - It reads `out/json/*.json`, skipping `*.matches.json`.
   - **`_read_ghidriff`:**
     - `modified` counts only when the code changed: `'code'` in `diff_type`, or
       `ratio` < 1.
     - `added`/`deleted` skip `external` symbols.
     - The pseudo-code diff is rebuilt from `old.code`/`new.code` with 3 lines of
       context. ghidriff's own `diff` uses 1000, which is the whole function.
2. **`settings.py`**: `binary.ghidriff`. The default `engines` is now all three.
3. **Tests**: `FakeGhidriff` writes ghidriff's JSON shape. The tests cover
   virtualenv discovery, code-only modified, imports excluded, the 3-line diff,
   `--no-symbols`, distinct copy names, the environment, a failure becoming a
   note, and all three engines merged.
4. **Docs**: the README row, config and limitation;
   [`docs/worklog/ghidriff-ghidra12/`](ghidriff-ghidra12/README.md) with the
   patch and offline install steps; [`docs/worklog/checks/`](checks/) with
   `make_fixture.py` (builds the rebuild and patch fixture from any PE or ELF)
   and `mock_llm.py` (records what the model is sent).

**[Decided]** `--no-symbols` is always passed. Symbols would mean a network call
to a symbol server, and the tool's home has no network. A local symbol store could
be made configurable later, if wanted.

**[Decided]** ghidriff 1.0.0 is patched (3 lines) rather than pinned to an older
Ghidra. Ghidra 12.2 is current, and the patch only renames a constant Ghidra
removed.

**Verification (real tools, real binaries)**

- Unit tests: 213 OK.
- ghidriff 1.0.0 was installed with pip into `~/tools/ghidriff-venv`, which
  brought pyghidra 3.2.0.
- As released, on Ghidra 12.2:
  `AttributeError: … ConsoleTaskMonitor … has no attribute 'DUMMY_MONITOR'`.
  Upstream main still has it. With the patch, it runs.
- **ghidriff found no change in `patched.exe`, where BinDiff found one modified
  function.** ghidriff's rule adds a function to "modified" only if its mnemonics
  or block structure changed, and a flipped operand byte changes neither. This is
  an engine limitation, not an adapter bug. It is the case MaNishtana's
  "engines disagree" flag exists for.
- **Full CLI, both Ghidra engines plus the mock model** (`jq.exe` 1.7 → 1.7.1 and
  `patched.exe`):
  - **First run: found a merge bug.** BinExport names an unnamed function
    `sub_14007D650` and Ghidra names it `FUN_14007d650`, so merging by name kept
    the two apart. 141 of 142 functions were shown as "engines disagree" and
    counted twice. Fix `591bd96`: names of the form `sub_<hex>` / `FUN_<hex>`
    merge by the address they encode, and real names are untouched. Two unit
    tests cover it.
  - **Rerun after the fix (7m48s):**
    - `jq.exe`: 82 functions. The engines agree on 60 (34 removed, 25 modified,
      1 added) and disagree on 22. Those 22 are cases where one engine matched a
      function the other calls added or removed, i.e. shifted function
      boundaries. 74 entries carry ghidriff's decompiled C.
    - `patched.exe`: 1 function, flagged `bindiff: modified, ghidriff:
      unchanged`, as explained above.
    - Model: 3 calls. `jq.exe`'s decompiled diff was larger than
      `max_chars_per_call`, so it was split into 2 parts, each starting with the
      binary header. Then 1 summary. `patched.exe` had no pseudo-code (BinDiff
      gives none), so it stayed "not analysed" by the model, which is correct.

---

## Where it stands (2026-11-01)

| Stage | Branch | Verified with real tools here | Still to verify on the air-gapped box |
|---|---|---|---|
| 1 Noise check | `feature/binary-noise` | Yes: jq PE and ELF, re-stamped copies, one-byte patch | A real double build of your own binary |
| 2 Diaphora | `feature/binary-diaphora` | Diaphora's real diff step and the full pipeline, with IDA's export simulated | **A real IDA export** |
| 3 BinDiff | `feature/binary-bindiff` | Yes, through Ghidra 12.2 with the patched BinExport | BinExport through IDA |
| 4 ghidriff | `feature/binary-ghidriff` | Yes, Ghidra 12.2 with ghidriff patched | — |

Tool versions used for the checks: Python 3.12.10 (the code stays 3.8-compatible),
Ghidra 12.2 (`ghidra_12.2_PUBLIC_20261016`), Temurin JDK 25, BinDiff 8 with its
BinExport 12 extension (patched), ghidriff 1.0.0 (patched) with pyghidra 3.2.0, and
Diaphora at commit `35c7657`.

### Checklist for the air-gapped box

1. Replay the four stages (see *Replaying* above) and run the tests after each.
2. Put a `binary` section in your config with the paths to IDA, Diaphora, BinDiff,
   Ghidra, a JDK 25 for Ghidra, and the ghidriff virtualenv. Then run
   `python -m manishtana --check-binary-tools -c config.json`. Every engine you
   installed should say `ready`.
3. If you use BinDiff through Ghidra 12, apply
   [`binexport-ghidra12`](binexport-ghidra12/README.md). If you use ghidriff on
   Ghidra 12, apply [`ghidriff-ghidra12`](ghidriff-ghidra12/README.md).
4. Build a fixture:
   `python docs/worklog/checks/make_fixture.py fx --pe old.exe new.exe --elf old new`.
   Then run `python -m manishtana fx/v1 fx/v2 -c fx/config.json -o r.md --no-llm`.
   Expect `rebuilt*` to be noise only and `patched.exe` and `changed*` to be
   modified.
5. Add your `binary` section to `fx/config.json` and run again. With IDA present,
   this is the first real Diaphora run. Check that the function table shows real
   names and the decompiled diff looks like Hex-Rays output.
6. Optional: start `python docs/worklog/checks/mock_llm.py 18765 requests.jsonl`,
   set `llm.base_url` to `http://127.0.0.1:18765/v1`, and read `requests.jsonl` to
   see exactly what the model would be sent before pointing at the real endpoint.

### Stage 5 — `feature/binary-noise-plus` (from `feature/binary-ghidriff` @ `8cbe65d`)

**Goal:** recognise more rebuild noise in binaries without ever hiding a code change.

| Commit | What and why |
|---|---|
| `d4bda2d` | `ignore_strings` rule option, and the CodeView PDB path counts as noise. |
| `0f6e10e` | **Fixes from an independent review:** code stays protected when a PE/ELF header does not parse; `^`/`$` work; bad patterns fail at startup. |
| (this commit) | Worklog, and a test no longer writes `report.md` into the current directory. |

**Intent of each change**

1. **`binnoise.py`**
   - `normalize(data, ignore_strings=None)` and `summarize(a, b, ignore_strings=None)`.
   - `_ignore_strings` zeroes every regex match inside printable runs of 4 or more
     characters, matching each run on its own so that anchors work.
   - It never touches code. `_code_ranges` errs wide:
     - PE: executable or code sections, widened to 0x200 alignment, plus the
       headers.
     - ELF: executable `PT_LOAD` segments plus `SHF_EXECINSTR` sections.
     - If the ranges cannot be computed, nothing is touched.
   - The format for that decision comes from the magic bytes (`MZ`, `\x7fELF`),
     not from whether the stamp parser succeeded.
   - `binary-stamps` now also zeroes the CodeView PDB path (the whole RSDS
     payload after the signature).
2. **`rules.py`**:
   - `_rewrite_binary` passes the rule's `ignore_strings`, and also writes
     unknown formats when the patterns changed them.
   - New public `binary_ignore_strings(rules, rel)`, used by `cli` for the
     summary's regions.
3. **`cli.py`**:
   - `_check_ignore_strings` validates the patterns at startup (non-string or
     invalid regex means exit 2).
   - The summary uses the rule's patterns.
4. **Tests**:
   - `make_pe` gains `text=` and `section_flags=`.
   - The tests cover data vs code (PE and ELF), only the match zeroed, unknown
     formats, summary regions, the rule plumbing and the startup errors.
   - The four review attacks are regression tests: broken debug directory,
     garbage `e_shoff`, `MZ` without a PE signature, anchors.
5. **README**: the `ignore_strings` row, the paragraph with an example, and the PDB
   path in the PE field list.

**[Decided]** `ignore_strings` is an option of `binary-stamps`. On a rule without
that step it does nothing, so binary rewriting has one entry point.

**[Decided]** Formats that are neither PE nor ELF get the patterns over the whole
file. The user declared them for those files. The README warns that archives and
objects contain code.

**[Decided]** No compiler-built rebuild corpus. Downloading a portable compiler
(Zig) was blocked by the session's permission rules, so rebuilds are simulated by
rewriting build-specific bytes in the real jq binaries.

**How the work was checked**

- Unit tests: 231 at `0f6e10e`.
- **Real binaries:** jq 1.7 (PE) "rebuilt" with its version stamp `1.7-dirty`
  rewritten to `1.7-clean`.
  - With `"ignore_strings": ["1\\.7-[a-z]{5}"]`: **noise only**.
  - Without it: **modified**.
  - The stamp is at file offset 0x8B48F, in a data section. The pattern zeroed
    exactly those 9 bytes on top of the 7 stamp bytes.
- **Worst case:** a pattern matching everything (`[\s\S]+`) changed 48,768 data
  bytes and **0 code bytes**, on both the jq PE and the jq ELF.
- **Independent review:** a separate agent reviewed the commit cold, without my
  reasoning, and reproduced each finding on doctored copies of jq.
  - It found the real safety bug. A PE with an oversized debug directory, or an
    ELF with a garbage `e_shoff`, still loads and runs, but the stamp parser
    failed, the file was treated as "unknown", and a match-everything pattern
    zeroed 51–58 KB of code.
  - After `0f6e10e`, its own attack script reports 0 code bytes changed in every
    case.
  - It also found the `^` anchor problem.
- Not done: a rename between two paths that match different rules uses the new
  path's patterns for the summary's region list. This affects only the
  description, never the classification.

### Stage 6 — `feature/binary-merge` (from `feature/binary-noise-plus` @ `cb621f0`)

**Goal:** fewer false "engines disagree" flags by merging findings on function
address instead of function name.

| Commit | What and why |
|---|---|
| `34a4c72` | Addresses from every adapter; address-keyed merge with the added-into-modified fold. |
| (this commit) | Worklog. |

**Intent of each change**

1. **`binengines.py`**
   - `FunctionChange` gains `address_a` and `address_b`. They default to `None`, so
     existing six-argument constructions still work.
   - The adapters fill them:
     - Diaphora: `results.address`/`address2` and `unmatched.address`, hex text.
     - BinDiff: `address1`/`address2`, and the `.BinExport` vertex address for
       unmatched functions.
     - ghidriff: each record's `address`, which is Ghidra's `Address.toString()`,
       sometimes `ram:`-prefixed.
     - `_hex()` reads all three forms.
   - `_merge` works in two passes:
     1. Key by `("a", old address)`, else `("b", new address)`, else name. A
        default name (`sub_`/`FUN_`) stands in for the address when there is none.
     2. An entry known only on the new side folds into one whose new address is
        the same. An engine whose view there is `removed` becomes
        `removed+added`. An engine that already placed something at that address
        blocks the fold, because that means two functions.
   - `_combine` merges one finding into an entry. The first engine's view wins for
     an engine seen twice.
2. **`output.py`**: the JSON function entries carry `address_a`/`address_b`.
3. **Tests**: the fakes now give each function the same address in every engine,
   as real tools do. New `MergeByAddress` tests cover different names at one
   address, the fold, the guard, removed and added at one address staying apart,
   merging by default name when an engine gives no address, address parsing, and
   addresses in the JSON.
4. **README**: one paragraph on how merging works.

**Verification (real tools, real binaries)**

- Unit tests: 238 OK.
- Fixture: `docs/worklog/checks/make_fixture.py --pe jq-1.7.exe jq-1.7.1.exe`.
  Engines BinDiff and ghidriff, through Ghidra 12.2. The run took 12m53s.
- **`changed.exe` (jq 1.7 → 1.7.1):** 81 rows, down from 82 in stage 4. 21
  disagreements, down from 22. Every row carries addresses.
  - The one fold: BinDiff said modified X→Y, ghidriff said removed X and added Y.
    That is now one row.
  - **The other 21 are not merging errors.** For 12 functions ghidriff sees a
    change where BinDiff matched identical; for 5 it is the reverse; 3 are one
    engine's add or remove with no counterpart. These are genuine differences in
    what each engine counts as "changed", and they are exactly what the flag is
    for. Merging cannot remove them, and should not.
- **`patched.exe`** (the fixture flips the byte at file offset 0x500):
  - The byte is inside a `call rel32` (`e8 cc bc 07 00` → `e8 33 bc 07 00`), so
    the call now jumps somewhere else.
  - **Both engines report no changed function.** A different call target inside
    existing code does not change a function's structure, and both engines judge
    "changed" by structure.
  - Layer 1 still classifies the file as **modified**, with the region
    `0x500–0x501`, so nothing is hidden.
  - But the report then says the difference is "outside the functions… data,
    resources or layout", which is wrong here. Stage 7 fixes that text.
- Honest scale: the measured improvement on this pair is one row. Address keying
  matters most when engines name *real* symbols differently, for example Diaphora
  with IDA's names against Ghidra's. That case could not be measured without IDA.

#### Stage 6, review fixes

A separate review agent read `34a4c72` cold and reproduced four problems. All are
fixed in the commit after this worklog entry's predecessor, `Fix the address merge
found by review`.

1. **Folding dropped the new function's code.** The folded row kept X's
   "entirely removed" diff and discarded Y's. `_fold_diff` now rebuilds old → new
   from the two one-sided diffs, or keeps a real modification diff when one exists.
2. **A folded row could have status "removed".** This happened when the
   removed+added engine came first in `ENGINES`. A fold now always sets
   `modified`.
3. **The fold overwrote an engine's "unchanged".** An engine that never mentioned
   X matched it as identical elsewhere, so to it X and Y are two functions. The
   fold now requires every engine behind the "added" to have reported X as
   removed.
4. **The engines' address spaces differed for ELF shared objects and PIEs.**
   Ghidra rebases a zero-based `ET_DYN` to 0x100000 and IDA does not. Two
   different functions could then share a key and be merged, which hides a change.
   - Verified on a real file: Ghidra's sample `override.so` reports `ELF Original
     Image Base 0x0` and `Minimum Address 00100000` in ghidriff's metadata.
   - `_ghidra_rebase(path)` gives 0x100000 for exactly that case, and the
     Ghidra-based adapters (ghidriff, and BinDiff when it exports through Ghidra)
     subtract it.
   - Guard: findings at one address with different real symbol names are never
     merged (`_names_conflict`).

Also: BinDiff addresses at or above 2**63 come back from SQLite negative; they are
masked to unsigned.

Tests: `MergeReviewRegressions`, `GhidraRebase`, `BinDiffHighAddresses`. The
reviewer's own script, pointed at the fixed code, shows every case behaving.
**[Decided]** The rebase covers only `ET_DYN` with lowest load address 0, the
verified case. Relocatable `.o` files are not rebased.

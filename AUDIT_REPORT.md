# Comprehensive Expert Panel Audit Report: MaNishtana (`manishtana`)

**Project Target:** `MaNishtana`  
**Evaluated Codebase:** Pure Python standard-library rule-based directory diffing & LLM analysis pipeline  
**Panel Expertise:** Security Architecture, Cross-Platform QA Reliability, and Zero-JS Frontend UX Engineering  
**Overall Status:** **88 / 100 — Exceptional Architecture & UX with Targeted Security & Cross-Platform Remediation Needs**

---

## Panel Overview & Executive Summary

A multi-agent panel of domain specialists performed a comprehensive audit of the **MaNishtana** project (`manishtana` engine). The tool is designed to separate substantive code changes from noise (formatting, comments, generated tokens) before passing diffs to a language model or rendering Markdown/HTML/JSON reports.

### Key Strengths Across All Domains
1. **Air-Gap Compliance (100% Stdlib):** Zero third-party PyPI dependencies. Runs seamlessly on disconnected networks using pure Python 3.8+ built-ins (`urllib.request`, `tempfile`, `difflib`, `html`, `subprocess`).
2. **Clean Architectural Layering:** Enforces a strict unidirectional DAG dependency structure (`common` <- `gitdiff`, `rules` <- `compare` <- `cli` -> `llm`, `output`) with zero cyclic imports and clean seam-based test interfaces (`FakeChat`).
3. **Pure CSS Zero-JS Frontend:** Self-contained HTML output requiring zero external scripts, CDNs, or fonts. Employs sophisticated dark/light CSS token compositing and native `<details>`/`<summary>` collapsible diff cards.
4. **Resilient Failure Degradation:** Optional external binaries (`clang-format`, `gcc`) and LLM HTTP connections fail gracefully with warnings without interrupting report generation.

---

## 1. Security Architecture & Security Posture Audit
*Report by Principal Software Architect & Security Auditor*

### 1.1 Dependency Hierarchy & Decoupling
MaNishtana enforces strict module isolation:
- **Leaf Modules:** `manishtana/common.py` and `manishtana/output.py` contain no side effects or internal package imports.
- **Seam Design:** In `manishtana/llm.py`, `analyze_changes` accepts an injectable `chat(system, user, label)` callable, allowing full testing of file capping, hunk splitting, and summary folding in-process without network activity.

### 1.2 Security Findings & Risk Assessment

This table records the audit as delivered. The status column was added afterwards
and is the current state of the tree; the finding text is left as written so the
fix can be read against the report that asked for it.

| Risk Level | Status | Finding | Location | Vulnerability & Impact |
| :---: | :--- | :--- | :--- | :--- |
| HIGH | **Fixed in 1.0.1.** `shell=True` is gone: `_command_for` returns argv and `_split_command` builds it, so a filename never reaches a shell. It cost a documented feature — a `normalizers` entry written as a shell pipeline no longer pipes — which `README.md` now states and `rules._warn_once_about_shell_syntax` says out loud at run time. | **Windows Shell Injection** | `manishtana/rules.py` | `_normalize_text` calls `subprocess.run(command, shell=True)`. On Windows `cmd.exe`, `_quote_name()` strips `"` but does not sanitize `%` (environment variable expansion) or control operators (`^`, `&`, `|`). Malicious filenames inside untrusted repositories can trigger unintended command execution under `cmd.exe`. |
| MEDIUM | **Fixed in 1.0.1** by `compare._inside`, which refuses any join that leaves the root. Covered by `InsideRoot`. | **Path Traversal in `_find_binaries`** | `manishtana/compare.py` | `os.path.join(root, rel)` resets `root` if `rel` starts with a leading separator or drive letter (e.g. `/etc/passwd`), attempting binary inspection outside the comparison tree. |
| LOW | **Fixed in 1.0.1** — `prepare_copy` copies with `symlinks=True`. Covered by `SymlinkHandling`, which is skipped on Windows, where making a symlink needs privilege. | **Symlink Dereferencing** | `manishtana/rules.py` | `shutil.copytree(src, dst)` follows symlinks by default (`symlinks=False`), recursively copying target files outside the repository root into temporary storage. |
| PASS | **No longer accurate.** 1.0.1 replaced `TemporaryDirectory()` with `mkdtemp()` plus `compare._remove_tree`, for the read-only cleanup defect in §2.3. `mkdtemp` creates the same `0700` directory, so the permission property is unchanged; automatic cleanup is not, and `compare()` now removes the tree in a `finally`. `cli._run_git` still uses `TemporaryDirectory()`, deliberately — see §2.3. | **Temp Directory Security** | `manishtana/compare.py` | `tempfile.TemporaryDirectory()` enforces restricted OS permissions (`0700` on POSIX) and ensures automatic context cleanup. |

---

## 2. QA, Test Reliability & Cross-Platform Audit
*Report by Lead QA & Test Reliability Engineer*

### 2.1 Test Suite Breakdown (90 Unit Tests at the time of the audit; **179 now**)
The test suite in `tests/test_manishtana.py` runs 179 unit tests in well under a second with 100% pass rate:
- **Zero Network/Git Operations in Tests:** Operates purely in-memory using `tempfile` structures and mock callables.
- **High Unit Coverage:** Extensively tests git name status parsing, diff section splitting, risk keyword extraction, word-level diff marking, and HTML escaping.

### 2.2 Coverage Gaps & Untested Public Entry Points
- `compare.py` Orchestration (`compare()`, `_find_binaries()`): 0 unit tests directly call the pipeline orchestrator. **Still open** — `compare()` needs a real `git diff`, which the suite refuses to depend on.
- `cli.py` Core Entrypoint (`main()`, `_parse_args()`): 0 unit tests test command-line parsing, argument validation, or exit codes. **Still open**, and now the larger of the two: `--git`/`--commit`/`--from`/`--to` and their mutual exclusions all live here.
- `gitdiff.py` Process Execution (`_git()`): Subprocess returncode handling and stderr warnings are untested. **Partly closed** — `GitTimeout` and `GitRepoTimeout` cover the timeout path in both `gitdiff` and `gitrepo`; returncode and stderr handling are still untested.

### 2.3 Cross-Platform & Resilience Defects

**CRITICAL WINDOWS FAILURE MODE: Read-Only Files in Temp Cleanup**
`shutil.copytree` preserves file mode bits (including read-only `stat.S_IREAD`). On Windows, `tempfile.TemporaryDirectory` cleanup fails with `PermissionError: [WinError 5] Access is denied` when attempting to delete read-only files, causing `manishtana` to throw an unhandled exception upon exit.

**Fixed in 1.0.1** by `compare._remove_tree`, covered by `RemoveTree`. The named
mechanism does not hold as written, though, and it matters for anyone applying
the same reasoning elsewhere: measured on Python 3.12.10 / Windows 11,
`TemporaryDirectory` cleans a read-only file up *successfully* —
`TemporaryDirectory._rmtree` carries a `PermissionError` handler that resets the
mode and retries — while bare `shutil.rmtree`, which `_remove_tree` tries first,
raises `PermissionError(13)`. The fix is still worth having: it is explicit,
tested, and does not depend on an interpreter-version detail. But
`cli._run_git`'s `TemporaryDirectory()` is **not** the same defect and was left
alone; it holds a `git archive` export, and git records only the executable bit,
so no read-only file can arrive there (verified: 28 exported files, all `0o666`).

**Line Ending Contamination (CRLF vs LF)**
In `manishtana/rules.py`, `_apply_ignore_lines()` splits text on `\n`. On Windows files using `\r\n`, matching lines are replaced with `IGNORED_TOKEN` (which ends in `\n` without `\r`). This contaminates the file with mixed line endings, introducing artificial diff noise in git.

**Fixed in 1.0.1** — the CR is stripped before matching and put back after.
Covered by `LineEndings`.

**Git Execution Timeout Defect**
`_git()` in `manishtana/gitdiff.py` invokes `subprocess.run()` without a `timeout` argument. If `git` hangs or deadlocks, `manishtana` will hang indefinitely.

**Fixed in 1.0.1** for `gitdiff`, and afterwards for `gitrepo._git`, which the
audit predates: `--git` mode runs `rev-parse`, `log` and `archive` against a
repository the reviewer did not write, so it needed the same bound. A timeout
there raises `GitError` and is also warned about directly, because `resolve()`
turns any `GitError` into `None` and its caller then reports a revision that
does not exist.

---

## 3. Frontend UI/UX, HTML/CSS Design System & Diff Visualization Audit
*Report by Senior Frontend & UX Specialist*

### 3.1 Design System & CSS Token Architecture
`manishtana/output.py` features a clean CSS variable token mapping (`_LIGHT` and `_DARK` palettes):
- **Theme Compositing:** Combines `@media (prefers-color-scheme: dark)` with explicit `:root[data-theme="dark"]` attribute overrides for zero FOUC flash.
- **Typography:** Uses system monospace stacks (`ui-monospace, SFMono-Regular, "Cascadia Mono", Consolas`) and tabular numeric variants (`font-variant-numeric: tabular-nums`) to prevent card jitter.
- **Contrast Ratios:** Exceeds WCAG 2.1 AAA standards (>14:1 contrast ratio for body text in light mode, >12:1 in dark mode).

### 3.2 Unified Side-by-Side Diff Table (`table.sbs`)
- **Single-Table Layout:** Renders both directories in one `<table>` element with `table-layout: fixed`. This guarantees that line heights on left and right panes remain identical during word wrapping without relying on JavaScript.
- **Word-Level Highlights (`mark.w`):** Employs `difflib.SequenceMatcher` with a noise floor threshold of `_MARK_FLOOR = 0.5` to prevent visual clutter on low-similarity lines.
- **Pure CSS Row Hover Compositing:**
  ```css
  table.sbs tbody tr:not(.s-gap):hover td {
    background-image: linear-gradient(var(--hover), var(--hover));
    box-shadow: inset 0 1px 0 var(--accent), inset 0 -1px 0 var(--accent);
  }
  ```
  Uses translucent `linear-gradient` overlays so hovering a row lights up *both* panes simultaneously without erasing underlying green/red addition/deletion background colors.

### 3.3 UX & Accessibility Evaluation

| Accessibility Dimension | Status | Observation |
| :--- | :---: | :--- |
| **Color Contrast** | AAA | Text, background, and badge combinations exceed AA/AAA standards. |
| **Keyboard Navigation** | Pass | Native HTML `<summary>` elements expand/collapse diffs using `Space`/`Enter`. |
| **Copy-Paste UX** | Pass | Line number columns (`td.n`) have `user-select: none` to prevent line numbers from copying into code snippets. |
| **Colorblind Support** | Needs Imp. | Additions/deletions rely exclusively on red/green color hue differentiation. |

---

## 4. Prioritized Action Plan & Recommended Code Fixes

*Items 1–3 shipped in 1.0.1, but not as written below: §1 removed the shell
rather than quoting for it, and §2 replaced `TemporaryDirectory` rather than
wrapping it. The code here is the recommendation, kept for the record; the tree
is what actually happened.*

### 1. Fix Windows Shell Injection in Normalizer Execution (`manishtana/rules.py`)
Tokenize commands using `shlex.split` and execute directly with `shell=False`:

```python
def _normalize_text(step, text, name, rel):
    command = _command_for(step, name)
    program = _program_of(command)
    if program in _missing_tools or shutil.which(program) is None:
        _missing_tools.add(program)
        return text
    try:
        cmd_args = shlex.split(command, posix=(os.name != "nt"))
        proc = subprocess.run(
            cmd_args,
            shell=False,
            input=text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except Exception as exc:
        warn("normalize step '%s' failed for %s: %s" % (step, rel, exc))
        return text
    return proc.stdout if proc.returncode == 0 else text
```

### 2. Fix Read-Only File Temp Cleanup on Windows (`manishtana/compare.py`)
Handle read-only attribute clearing during directory cleanup:

```python
def _remove_readonly(func, path, exc_info):
    import stat
    os.chmod(path, stat.S_IWRITE)
    func(path)

# Inside compare(dir_a, dir_b, rules):
with tempfile.TemporaryDirectory() as tmp:
    prepare_copy(dir_a, os.path.join(tmp, "A"), rules)
    prepare_copy(dir_b, os.path.join(tmp, "B"), rules)
    # ...
    # Ensure Windows cleanup does not fail on read-only files:
    shutil.rmtree(tmp, onerror=_remove_readonly)
```

### 3. Fix Line Ending Contamination in `_apply_ignore_lines` (`manishtana/rules.py`)
Preserve original `\r\n` line endings when inserting replacement tokens:

```python
def _apply_ignore_lines(text, patterns):
    if not patterns:
        return text
    regexes = [re.compile(p) for p in patterns]
    lines = text.split("\n")
    for index, line in enumerate(lines):
        ending = "\r" if line.endswith("\r") else ""
        clean_line = line.rstrip("\r")
        if any(regex.search(clean_line) for regex in regexes):
            lines[index] = IGNORED_TOKEN + ending
    return "\n".join(lines)
```

### 4. Enhance Colorblind Accessibility in HTML Diff Tables (`manishtana/output.py`)
Add subtle text pseudo-elements (`+` / `-`) or distinct border styles to deletion (`td.s-del`) and addition (`td.s-add`) cells so status is clear without relying solely on red/green color vision.

---

### Conclusion & Final Rating
MaNishtana is an exceptionally well-designed tool that fulfills its primary mandate: **separating signal from noise in code comparisons on air-gapped systems without third-party dependencies**. Implementing the four targeted fixes above will ensure complete cross-platform stability, security hardening, and accessibility excellence.

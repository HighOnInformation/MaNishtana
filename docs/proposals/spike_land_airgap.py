"""Spike: what does merging origin/feature/airgap-deployment actually change?

Run it from the repository root:

    python docs/proposals/spike_land_airgap.py

Stdlib only, Python 3.8+, Windows and Linux — the same constraints as the tool.
It answers two questions with output rather than argument:

  1. Do the branch's two new docstrings emit SyntaxWarning on import?
  2. A `normalizers` entry written as a shell pipeline — which README documents
     as supported — still resolves and still runs after the branch removes
     `shell=True`. Does it still *normalize*, and does the run say so?

Question 2 is answered by running the whole comparison twice over one pair of
directories: once with the merged tree's argv normalizer, once with `main`'s
`shell=True` one, loaded out of git history into a throwaway package. The two
runs differ only in that function, so any difference in the verdict is caused by
the merge and by nothing else.

Requires `git` on PATH and a `main` ref; the halves that need them are skipped
with a message when either is missing.
"""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import warnings

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

PY = sys.executable

# Stage 1 passes text through untouched; stage 2 sorts the lines. Written this
# way round on purpose: with the pipeline honoured the file comes back sorted,
# and with the pipe treated as an argument stage 1 alone runs, exits 0, and
# hands back exactly what it was given.
PIPELINE = (
    '"%s" -c "import sys;sys.stdout.write(sys.stdin.read())"'
    ' | "%s" -c "import sys;sys.stdout.writelines(sorted(sys.stdin.readlines()))"'
) % (PY, PY)

UNSORTED = "beta\nalpha\ngamma\n"
SORTED = "alpha\nbeta\ngamma\n"


def heading(text):
    print("\n" + text)
    print("-" * len(text))


# --- question 1: SyntaxWarning at compile time -------------------------------


def probe_syntax_warnings():
    """Compile every module in the package and report the warnings it raises.

    compile() rather than import, because the warning is raised once at compile
    time and a cached .pyc silences it for every run after the first — which is
    why it is easy to miss locally and lands on the operator's first run.
    """
    heading("1. SyntaxWarning at compile time")
    package = os.path.join(ROOT, "manishtana")
    found = []
    for name in sorted(os.listdir(package)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(package, name)
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            compile(source, path, "exec")
        for item in caught:
            found.append((name, item.lineno, item.category.__name__, str(item.message)))
    if not found:
        print("  no warnings from any module")
    for name, lineno, category, message in found:
        print("  manishtana/%s:%s  %s: %s" % (name, lineno, category, message))
    print("  total: %d" % len(found))
    return found


# --- question 2: the shell-pipeline normalizer -------------------------------


def load_main_rules(tmp):
    """Import main's rules.py as its own package, or return None.

    Both implementations have to be live in one process to be compared, and both
    modules are called `manishtana.rules`. A second package directory holding the
    old file beside the unchanged `common.py` is the cheapest way to have both.
    """
    try:
        source = subprocess.check_output(
            ["git", "show", "main:manishtana/rules.py"], cwd=ROOT, stderr=subprocess.PIPE
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        print("  (skipped: cannot read main:manishtana/rules.py — %s)" % exc)
        return None
    package = os.path.join(tmp, "manishtana_main")
    os.makedirs(package)
    open(os.path.join(package, "__init__.py"), "w").close()
    shutil.copy(os.path.join(ROOT, "manishtana", "common.py"), package)
    with open(os.path.join(package, "rules.py"), "wb") as handle:
        handle.write(source)
    sys.path.insert(0, tmp)
    from manishtana_main import rules as main_rules

    return main_rules


def _short(value):
    text = repr(value)
    return text if len(text) < 72 else text[:69] + "..."


def probe_resolution(main_rules):
    """What each version turns a configured command line into."""
    from manishtana import rules

    heading("2a. How a shell-syntax normalizer resolves")
    for step in ("sort | uniq", "tr -d x && sort", PIPELINE):
        print("  step: %s" % (step if len(step) < 60 else step[:57] + "..."))
        after = rules._command_for(step, "sensor.c")
        print("    merged -> argv %s" % _short(after))
        print("    merged -> program %r" % rules._program_of(after))
        if main_rules is not None:
            before = main_rules._command_for(step, "sensor.c")
            print("    main   -> shell string %s" % _short(before))
            print("    main   -> program %r" % main_rules._program_of(before))


def run_normalizer(module, step):
    """Run one normalization step through `module`, capturing its warnings."""
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        reset_warning_state(module)
        out = module._normalize_text(step, UNSORTED, "sensor.c", "src/sensor.c")
    return out, stderr.getvalue().strip()


def reset_warning_state(module):
    """Both warn-once registries, so each probe reports what it alone provoked."""
    module._missing_tools.clear()
    if hasattr(module, "_shell_syntax_reported"):
        module._shell_syntax_reported.clear()


def probe_normalization(main_rules):
    from manishtana import rules

    heading("2b. What the normalizer actually does to the text")
    for label, module in (("main (shell=True)", main_rules), ("merged (argv)", rules)):
        if module is None:
            continue
        out, noise = run_normalizer(module, PIPELINE)
        print("  %s" % label)
        print("    input    %r" % UNSORTED)
        print("    output   %r" % out)
        print("    sorted?  %s" % (out == SORTED))
        print("    warnings %s" % (noise or "(none)"))
    out, noise = run_normalizer(rules, "sort | uniq")
    print("  merged (argv), README's own shape 'sort | uniq'")
    print("    output unchanged?  %s" % (out == UNSORTED))
    print("    warnings %s" % (noise or "(none)"))


def build_pair(base):
    """Two trees whose one file differs only in line order."""
    dir_a = os.path.join(base, "v1")
    dir_b = os.path.join(base, "v2")
    os.makedirs(dir_a)
    os.makedirs(dir_b)
    with open(os.path.join(dir_a, "sensor.c"), "w", newline="") as handle:
        handle.write(UNSORTED)
    with open(os.path.join(dir_b, "sensor.c"), "w", newline="") as handle:
        handle.write(SORTED)
    return dir_a, dir_b


def probe_end_to_end(main_rules):
    """The verdict the operator reads, with each normalizer in turn."""
    from manishtana import compare as compare_module
    from manishtana import rules

    heading("2c. The verdict, end to end")
    if shutil.which("git") is None:
        print("  (skipped: git is not on PATH)")
        return
    rule = [{"match": ["*.c"], "normalize": [PIPELINE]}]
    original = rules._normalize_text
    for label, impl in (("main (shell=True)", main_rules), ("merged (argv)", rules)):
        if impl is None:
            continue
        base = tempfile.mkdtemp()
        try:
            dir_a, dir_b = build_pair(base)
            rules._normalize_text = impl._normalize_text
            reset_warning_state(impl)
            reset_warning_state(rules)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = compare_module.compare(dir_a, dir_b, rule)
            print("  %s" % label)
            print("    real        %s" % sorted(result.real))
            print("    noise only  %s" % sorted(result.noise))
            print("    warnings    %s" % (stderr.getvalue().strip() or "(none)"))
        finally:
            rules._normalize_text = original
            shutil.rmtree(base, ignore_errors=True)


def main():
    print("MaNishtana — spike for docs/proposals/land-airgap-branch.md")
    print("python %s on %s" % (sys.version.split()[0], sys.platform))
    probe_syntax_warnings()
    tmp = tempfile.mkdtemp()
    try:
        main_rules = load_main_rules(tmp)
        probe_resolution(main_rules)
        probe_normalization(main_rules)
        probe_end_to_end(main_rules)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

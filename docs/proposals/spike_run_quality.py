"""Spike for run-quality-json.md. Run it: python docs/proposals/spike_run_quality.py

Same fixture, same config; only the normalizer's program differs. The counts flip, and
nothing in build_json's output says which run you are holding. These assertions pass.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from manishtana import rules
from manishtana.compare import compare
from manishtana.output import build_json
from manishtana.settings import load as load_settings

PRESENT = '"%s" -c "import sys;sys.stdout.write(str().join(sys.stdin.read().split()))"' % sys.executable
ABSENT = "manishtana-no-such-normalizer"
RULES = [{"match": ["*.c"], "normalize": ["fmt"]}]
A = {"a.c": "int f(void){return 1;}\n", "b.c": "int g(void){return 2;}\n"}
B = {"a.c": "int f(void){return 42;}\n", "b.c": "int  g( void )\n{\n  return 2;\n}\n"}


def _run(tmp, command, tag):
    """One full comparison, with the 'fmt' normalizer bound to the given command."""
    settings = load_settings({"normalizers": {"fmt": command}})
    trees = []
    for side, files in (("A", A), ("B", B)):
        root = os.path.join(tmp, tag + side)
        os.makedirs(root)
        for rel, text in files.items():
            with open(os.path.join(root, rel), "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
        trees.append(root)
    rules._missing_tools.clear()  # a module global, and it outlives a comparison
    return build_json("A", "B", compare(trees[0], trees[1], RULES, settings), {}, None, settings=settings)


with tempfile.TemporaryDirectory() as tmp:
    ok, bad = _run(tmp, PRESENT, "ok"), _run(tmp, ABSENT, "bad")
print("normalizer present: counts=%s noise=%s" % (ok["counts"], ok["noise"]))
print("normalizer missing: counts=%s noise=%s" % (bad["counts"], bad["noise"]))
assert ok["counts"] != bad["counts"], "the two verdicts should disagree"
blob = json.dumps(bad)
assert sorted(ok) == sorted(bad) and ok["inputs"] == bad["inputs"], "a field already differs"
assert ok["llm_ran"] == bad["llm_ran"] and ABSENT not in blob, "the missing tool is named"
assert not [w for w in ("run_quality", "provenance", "normaliz", "warn", "degraded") if w in blob]
print("BUG: same keys, same inputs %s, nothing names the missing tool." % bad["inputs"]["prompts_sha256"][:12])

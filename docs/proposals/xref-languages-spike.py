"""Spike behind xref-languages.md. Run from the repo root; takes about a minute. On real
Python: what the shipped C extractor finds, what a regex extension would find (precision
against ast), and how many "still referenced" badges a text search raises versus ast."""
import ast, builtins, glob, os, re, sys, sysconfig
sys.path.insert(0, os.getcwd())
from manishtana.symbols import _definitions

DEF_RE = re.compile(r"^(?:async\s+)?(?:def\s+(\w+)\s*\(|class\s+(\w+)\s*[(:])")
DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

def facts(text):
    """(defs, used, regex guesses). Used = imported by name, or free in Load context."""
    bound, used, load, defs = set(dir(builtins)), set(), set(), set()
    for n in ast.walk(ast.parse(text)):
        if isinstance(n, ast.ImportFrom):
            used.update(a.asname or a.name for a in n.names)
        elif isinstance(n, ast.Import):
            bound.update((a.asname or a.name).split(".")[0] for a in n.names)
        elif isinstance(n, DEFS): defs.add(n.name)
        elif isinstance(n, ast.arg): bound.add(n.arg)
        elif isinstance(n, ast.Name):
            (load if isinstance(n.ctx, ast.Load) else bound).add(n.id)
    guess = (DEF_RE.match(line.strip()) for line in text.split("\n"))
    return defs, used | (load - bound - defs), set(m.group(1) or m.group(2) for m in guess if m)

def report(label, paths, deep=True):
    files, sites, num = {}, {}, dict.fromkeys("defs tp fp C text ast".split(), 0)
    for p in paths:
        try:
            text = open(p, encoding="utf-8").read()
            files[p] = (text,) + facts(text)
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError): continue
        defs, guess = files[p][1], files[p][3]
        num["defs"] += len(defs); num["tp"] += len(guess & defs); num["fp"] += len(guess - defs)
        num["C"] += len(_definitions("\n".join("+" + l for l in text.split("\n")))[0])
        for name in defs: sites.setdefault(name, []).append(p)
    for name, where in (sorted(sites.items()) if deep else ()):
        rest = [p for p in files if p != where[0]]
        num["text"] += any(re.search(r"\b%s\b" % re.escape(name), files[p][0]) for p in rest)
        num["ast"] += len(where) == 1 and any(name in files[p][2] for p in rest)
    print("%-11s %4d files %6d defs | shipped C finds %4d | regex precision %.4f recall %.4f"
          " | badges: text %4d ast %3d" % (label, len(files), num["defs"], num["C"],
          num["tp"] / float(num["tp"] + num["fp"] or 1), num["tp"] / float(num["defs"] or 1),
          num["text"], num["ast"]))

lib = sysconfig.get_paths()["stdlib"]
report("manishtana", sorted(glob.glob("manishtana/*.py")))
for pkg in ("json", "http", "email", "xml"):
    report(pkg, sorted(glob.glob(os.path.join(lib, pkg, "**", "*.py"), recursive=True)))
report("ALL stdlib", glob.glob(lib + "/*.py") + glob.glob(lib + "/*/*.py"), deep=False)

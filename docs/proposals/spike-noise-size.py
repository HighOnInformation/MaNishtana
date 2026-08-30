"""Spike: what does showing the filtered-out diff cost in report size?

Run from the repo root: python docs/proposals/spike-noise-size.py [REPO REV_A REV_B]
Exports two revisions and reformats the newer end to end, so every file becomes noise.
"""

import os, sys, tempfile
from html import escape

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from manishtana import compare, gitdiff, gitrepo, output, settings

_FLATTEN = ("import sys;sys.stdout.write(chr(10).join(chr(32).join(l.split()) "
            "for l in sys.stdin.read().split(chr(10))))")
_RULES = [{"match": ["*"], "normalize": ["flatten-space"]}]

def _reformat(root):
    """Re-indent and pad every text file: the drop that reformatted everything."""
    for folder, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(folder, name)
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    text = handle.read()
            except (OSError, UnicodeDecodeError):
                continue
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("\n".join("  " + ln.rstrip() + " " for ln in text.split("\n")))

def main(repo, rev_a, rev_b):
    conf = {"normalizers": {"flatten-space": '"%s" -c "%s"' % (sys.executable, _FLATTEN)}}
    with tempfile.TemporaryDirectory() as tmp:
        before, after = os.path.join(tmp, "a"), os.path.join(tmp, "b")
        gitrepo.export(repo, rev_a, before)
        gitrepo.export(repo, rev_b, after)
        _reformat(after)
        result = compare.compare(before, after, _RULES, settings.load(conf, "."))
        noise = {k: v for k, v in gitdiff.diff_sections(before, after).items()
                 if k in result.noise}
        html = output.build_html(before, after, result, {}, None)
        block = "".join("<details><summary>%s</summary><pre>%s</pre></details>"
                        % (escape(k), escape(v)) for k, v in sorted(noise.items()))
        for key in sorted(noise, key=lambda k: -len(noise[k])):
            print("%8d  %s" % (len(noise[key]), key))
        print("real %d noise %d | noise %d chars | html %d -> %d (x%.2f)"
              % (len(result.real), len(noise), sum(len(v) for v in noise.values()),
                 len(html), len(html) + len(block), (len(html) + len(block)) / float(len(html))))

main(*(sys.argv[1:] or [os.path.join(os.path.dirname(__file__), "..", ".."), "HEAD~1", "HEAD"]))

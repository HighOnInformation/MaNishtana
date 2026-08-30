"""Spike for docs/proposals/git-first-class.md — `python docs/proposals/spike-git-header.py`
from the repository root. Runs the tool in --git mode over this repository
(--no-llm: offline and free), prints the header of all three artifacts, and exits
non-zero if any names the deleted export directory. Fails on `main`, passes here.
"""

import json
import os
import re
import subprocess
import sys
import tempfile

REV = "1cc3571"  # the commit that added --git


def main():
    out = tempfile.mkdtemp(prefix="spike-")
    md, js, html = (os.path.join(out, "r." + ext) for ext in ("md", "json", "html"))
    cmd = [sys.executable, "-m", "manishtana", "--git", ".", "--commit", REV,
           "-c", "config.json", "--no-llm", "-o", md, "-j", js, "-H", html]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr.decode("utf-8", "replace"))
        return 1

    text = open(md, encoding="utf-8").read()
    print("--- markdown header ---")
    print("\n".join(text.splitlines()[:9]))

    page = open(html, encoding="utf-8").read()
    print("\n--- html header ---")
    for pattern in (r"<title>.*?</title>", r"<dt>.*?</dt><dd>.*?</dd>"):
        print("\n".join(re.findall(pattern, page)))

    print("\n--- json source ---")
    payload = json.load(open(js, encoding="utf-8"))
    print(json.dumps(payload.get("source", "absent — this is schema %s"
                                 % payload.get("schema_version")), indent=2))

    # The export directory is deleted before the run returns. Naming it in an
    # artifact someone opens later is the defect this spike exists to catch.
    leaked = [name for name, body in (("md", text), ("html", page))
              if re.search(r"tmp\w{8}[\\/](?:before|after)", body)]
    print("\nartifacts naming the deleted export directory: %s" % (leaked or "none"))
    return 1 if leaked else 0


if __name__ == "__main__":
    sys.exit(main())

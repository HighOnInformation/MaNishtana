# ghidriff on Ghidra 12 — the three-line fix

ghidriff 1.0.0 is the latest release on PyPI and the GitHub main branch as of
2026-10-31. Its default engine (`VersionTrackingDiff`) crashes on Ghidra 12.2:

```
AttributeError: 'ghidra.util.task.ConsoleTaskMonitor' object has no attribute 'DUMMY_MONITOR'
```

Ghidra 12 removed the old `DUMMY_MONITOR` alias; `TaskMonitor.DUMMY` is the same
object under its current name. The patch in this directory changes the three places
that use it (`version_tracking_diff.py` twice, `ghidra_diff_engine.py` once).

## Installing ghidriff for MaNishtana

ghidriff is a Python package that drives Ghidra through pyghidra, so unlike
MaNishtana it needs pip. Keep it in its own virtualenv. On a networked machine:

```bash
python -m venv ghidriff-venv
ghidriff-venv/bin/pip install ghidriff            # Windows: ghidriff-venv\Scripts\pip
ghidriff-venv/bin/pip download ghidriff -d wheels # to carry onto the closed network
```

On the closed network, run `pip install --no-index --find-links wheels ghidriff`.
Then apply the patch inside site-packages:

```bash
cd ghidriff-venv/lib/python3.*/site-packages   # Windows: ghidriff-venv\Lib\site-packages
patch -p1 < /path/to/MaNishtana/docs/worklog/ghidriff-ghidra12/ghidriff-1.0.0.patch
```

Point MaNishtana at it with `"binary": {"ghidriff": "/path/to/ghidriff-venv", ...}`.
A directory is fine; `bin/ghidriff` or `Scripts\ghidriff.exe` is found inside it.

## What ghidriff does and does not report

ghidriff counts a function as *modified* only when its decompiled code differs
**and** its mnemonics or basic-block structure differ, or when its name, signature,
size or callers changed. A change that only alters an operand (a different
constant, the same instructions) is not reported. On the one-byte test patch,
ghidriff found no change and BinDiff found one modified function. With both
engines on, MaNishtana shows this as *engines disagree*.

Verified on 2026-10-31: Ghidra 12.2, Temurin JDK 25, ghidriff 1.0.0 with this patch,
pyghidra 3.2.0.

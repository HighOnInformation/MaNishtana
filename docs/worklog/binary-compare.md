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

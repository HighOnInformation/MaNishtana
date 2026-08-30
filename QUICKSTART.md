# MaNishtana — quick start

Compare two versions of a directory and get a report of what **actually**
changed, with formatting churn and comment edits filtered out rather than
counted as changes.

Everything needed is in the container image here. Nothing gets installed on your
machine, and you do not need root.

**Requires:** Docker (or Podman). That is the whole list.

---

## 1. Load the image

```bash
docker load -i manishtana-image.tar.gz
```

Reads the gzip directly — do not decompress it first. Takes a moment; it is
223 MB.

## 2. Check it works

```bash
docker run --rm --entrypoint sh manishtana -c 'gcc --version | head -1'
```

Should print a gcc version. If it does, you are ready.

## 3. Compare two directories

```bash
./run.sh old_version new_version
```

That is it. You get:

```
  report    out/report.md
  html      out/report.html
  warnings  out/report.warnings
```

Open `out/report.html` in a browser — it is self-contained, no server needed.

### No LLM endpoint yet?

The report has two halves: a diff-based half that always works, and an LLM
analysis of the real changes. Without an endpoint configured, run:

```bash
./run.sh old_version new_version --no-llm
```

and you get the full diff-based report with no LLM section. To wire up the LLM
later, copy `config.json` out of the repository, set `llm.base_url` and
`llm.model`, and put it beside `run.sh` — it is picked up automatically. Any
OpenAI-compatible endpoint works.

---

## What you should see

Given a file whose comments and formatting were rewritten but whose logic did
not change, plus a genuinely new file:

| File | Reported as |
|---|---|
| the reformatted one | `noise only` |
| the new one | `added` — a real change |

That separation is the point of the tool. It comes from normalizing both sides
with `gcc -fpreprocessed` and `clang-format` before diffing, which is why both
are baked into the image.

## Always read the warnings

`out/report.warnings` is separate from the report on purpose. Warnings never
appear in the report itself, and some of them change how much you should trust
it — a missing normalizer, a file that was not valid UTF-8, an LLM call that
timed out. `run.sh` prints them at the end if there are any.

## If something goes wrong

| Symptom | Cause |
|---|---|
| `image 'manishtana' not loaded` | Step 1 did not run, or ran in a different Docker context |
| `is not an existing directory` | The paths are relative to where you ran `run.sh` |
| Report is huge, everything looks changed | You are not running inside this image — gcc/clang-format are missing, so nothing is normalized |
| LLM section is empty | Endpoint unreachable. It degrades quietly by design; check `report.warnings` |

Full reference, native install without Docker, and OpenShift deployment:
see `INSTALL.md`.

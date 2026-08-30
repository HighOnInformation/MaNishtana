# MaNishtana — getting `gcc` onto the closed network

The repository is already across. What is missing is `gcc`. This set fixes that
by carrying the container image, which has `gcc`, `clang-format`, `git` and
Python 3.11 inside it — so **nothing has to be installed on the VM** and no root
access is required.

Target: an Ubuntu VM today, OpenShift later. Both are covered below.

## Why gcc matters here

`config.json` normalizes C/C++ with `strip-comments` then `clang-format`.
`strip-comments` is `gcc -fpreprocessed -dD -E -P -x c -`. With gcc absent the
tool does **not** fail — `rules.py:187` warns once per missing tool and carries
on — but every comment-only and formatting-only edit is then reported as a real
change instead of being filtered out as noise.

Measured on a two-file fixture with this image:

| | with gcc | without gcc |
|---|---|---|
| comment + formatting rewrite | `noise only` | counted as a **real change** |
| genuinely new function | real change | real change |

That gap is the whole reason to carry this.

---

## What is in this set

| File | What it is |
|---|---|
| `manishtana-image.tar.gz` | The container: Python 3.11 + git + clang-format + gcc |
| `manishtana.bundle` | The whole repository, full history, one file |
| `SHA256SUMS` | Checksums for everything here |
| `QUICKSTART.md` | One page, for someone who just wants a report — start there |
| `run.sh` | Wrapper around the docker run line |
| `airgap.sh` | verify / import / selftest helper |
| `IMPORT.txt` | The generic Unix notes (this file supersedes them) |
| `INSTALL.md` | This file |

## Do not try to rebuild the image on the closed network

It will fail, and the error is misleading. The Dockerfile installs git,
clang-format and gcc from Debian's repositories, so `docker build` needs the
network:

    E: Unable to locate package git
    E: Unable to locate package clang-format
    E: Unable to locate package gcc

That is why the prebuilt `manishtana-image.tar.gz` is carried across rather than
built on arrival. Cloning the repository alone does **not** get you gcc — the
repository carries source, and a git bundle carries commits, never binaries. If
you need to change the image, rebuild it on the connected side and carry a new
tarball over.

---

## 1. Verify before trusting the media

```bash
./airgap.sh verify
```

A mismatch means the media is corrupt. Stop — do not load a corrupt image.

## 2. Load the image

```bash
./airgap.sh import          # verifies, clones the bundle, loads the image
```

or, if the repo is already where you want it and you only need the image:

```bash
docker load -i manishtana-image.tar.gz     # podman load -i … works too
```

`docker load` reads the gzip directly — do not gunzip it first.

## 3. Confirm it runs HERE, not merely that it copied

```bash
./airgap.sh selftest
```

The line that matters for your problem:

```bash
docker run --rm --entrypoint sh manishtana -c 'gcc --version | head -1'
```

It must print a gcc version. Verified in this build: **gcc 14.2.0 (Debian)**,
clang-format 19.1.7, git 2.47.3.

---

## 4. Running it on the Ubuntu VM

```bash
mkdir -p out
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$PWD/v1:/work/v1:ro" \
  -v "$PWD/v2:/work/v2:ro" \
  -v "$PWD/config.json:/work/config.json:ro" \
  -v "$PWD/out:/work/out" \
  manishtana v1 v2 -c config.json -o out/report.md -H out/report.html \
  2> out/report.warnings
```

**Read `out/report.warnings` first.** Warnings never appear in the report itself,
and some change how much you should trust it. A `tool 'gcc' is not installed`
line there means you are still running degraded — which, from inside this image,
would mean you are not actually using this image.

`--user "$(id -u):$(id -g)"` keeps the reports owned by you instead of root. The
image sets no `USER` deliberately so that this works.

### If the LLM endpoint is not reached

`config.json` ships with `"base_url": "http://localhost:8000/v1"`. Inside a
container `localhost` is **the container itself**, so that address reaches
nothing — and a dead endpoint returns `None` rather than raising (`llm.py:18`),
so the analysis degrades quietly rather than erroring.

- Endpoint on the **same VM**: use `--network host` and keep `localhost`, or
  point at the VM's own LAN IP.
- Endpoint **elsewhere on the closed network**: use its IP or hostname.
- No endpoint: pass `--no-llm` for a diff-only report. The whole normalization
  path, gcc included, still applies.

---

## 5. Native on Ubuntu, without the container

Only if you would rather not use Docker. On the **connected** side, resolve the
packages against an image matching your VM:

```bash
./airgap.sh deps ubuntu:24.04      # or ubuntu:22.04 — must match the VM
./airgap.sh pack                   # folds deps/ into the checksummed set
```

Then on the VM, as root:

```bash
sh deps/ubuntu-24.04/install.sh --dry-run    # check nothing is missing
sh deps/ubuntu-24.04/install.sh
```

The distro and version must match the VM. Ubuntu 22.04 packages will not
install cleanly on 24.04.

---

## 6. OpenShift, later

The image is already built for it. One thing had to be fixed and is fixed in
this build:

OpenShift's default `restricted-v2` SCC runs the container as an **arbitrary
UID** that appears in no passwd file, placed in **group 0**. The original image
left `/work` as `root:root 755`, so that UID could not write and the run died
before producing a report. The Dockerfile now does:

```dockerfile
RUN chgrp 0 /work && chmod g+rwX /work
```

Verified: a run as `--user 1000670000:0` completes and writes its report. This
means **no `anyuid` SCC and no privileged service account are needed** — it runs
under the default restricted policy.

### Getting the image into a disconnected cluster

```bash
skopeo copy docker-archive:manishtana-image.tar.gz \
  docker://<your-mirror-registry>/manishtana:latest
```

### It is a batch job, not a service — run it as a Job

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: manishtana
spec:
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: manishtana
          image: <your-mirror-registry>/manishtana:latest
          args: ["v1", "v2", "-c", "config.json",
                 "-o", "out/report.md", "-H", "out/report.html"]
          workingDir: /work
          volumeMounts:
            - { name: sources, mountPath: /work/v1, subPath: v1, readOnly: true }
            - { name: sources, mountPath: /work/v2, subPath: v2, readOnly: true }
            - { name: config,  mountPath: /work/config.json, subPath: config.json, readOnly: true }
            - { name: out,     mountPath: /work/out }
      volumes:
        - name: sources
          persistentVolumeClaim: { claimName: manishtana-sources }
        - name: config
          configMap: { name: manishtana-config }
        - name: out
          persistentVolumeClaim: { claimName: manishtana-out }
```

Do **not** set `securityContext.runAsUser` — let OpenShift assign the UID. The
`fsGroup` it assigns makes the PVCs writable.

Read the warnings, which go to stderr:

```bash
oc logs job/manishtana
```

---

## Sending work back out

```bash
git bundle create outbound.bundle --all
```

Carry that out and fetch from it on the connected side.

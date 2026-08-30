# Deployment

Three stages, in the order you will actually meet them: a laptop, a single VM,
then OpenShift. Each stage ends with a check that proves the deployment works
*there*, rather than proving the files copied.

| Stage | For | Needs |
|---|---|---|
| [1. Local](#stage-1--local) | Developing, evaluating, debugging a report | Python 3.8+ and git, or Docker |
| [2. VM](#stage-2--vm) | A shared box, scheduled batches, air-gapped networks | Docker or Podman |
| [3. OpenShift](#stage-3--openshift) | Production, one run per repository under test | A namespace and a registry |

---

## Where this fits

MaNishtana is the *what actually changed* stage of a patch-verification
service — the service that asks whether a reported vulnerability was genuinely
fixed in a GitHub repository, rather than merely touched.

```
 fetch repo at the vulnerable revision  ---+
                                           +--> MaNishtana --> JSON --> verdict
 fetch repo at the claimed fix revision ---+     (real changes only)
```

This stage exists because the raw diff is a bad input to that question in both
directions:

- A three-line security fix buried inside a four-thousand-line reformat is
  invisible. MaNishtana normalizes both sides — `gcc -fpreprocessed` for
  comments, `clang-format` for layout — and reports only what survived. The
  reformat becomes `noise only`.
- A "fix" that is *only* comment changes looks substantial in a raw diff, and is
  a red flag. Here it collapses to `noise only`, which is exactly the signal the
  verdict stage needs.

The interface to the rest of the pipeline is `--json` (`schema_version: 1`):

```json
{
  "schema_version": 1,
  "counts":  { "real": 1, "noise": 2, "skipped": 0 },
  "llm_ran": true,
  "files":   [ { "file": "src/auth.c", "status": "M", "binary": false,
                 "risk": "...", "analysis": "...", "diff": "..." } ],
  "summary": { "text": "...", "risk": "..." }
}
```

`counts.real == 0` with `counts.noise > 0` is the machine-readable form of "they
changed the file but not the code" — usually the most interesting result the
service can get.

**Exit codes:** `0` success, `2` failure (unreadable config, missing directory,
bad arguments). Anything non-zero means the JSON is absent or incomplete — do
not parse it.

---

## Stage 1 — Local

### Option A: native

Fastest for development. No pip install; the tool is standard library only.

| Component | Version | Required | Used for |
|---|---|---|---|
| Python | 3.8+ | **yes** | The tool itself |
| git | 2.30+ | **yes** | `git diff --no-index` is the comparison engine |
| `clang-format` | any | no | Layout normalization for C/C++ |
| `gcc` (or `cpp`) | any | no | Comment stripping, preprocessor only — never compiles |

```bash
git clone https://github.com/HighOnInformation/MaNishtana
cd MaNishtana
python3 -m unittest discover -s tests
python3 -m manishtana v1 v2 -c config.json -o report.md --no-llm
```

Without `gcc` and `clang-format` the run still succeeds, but comment-only and
format-only edits count as **real changes** instead of noise — which is exactly
the signal the patch-verification service depends on. Treat them as required in
any environment whose output is trusted.

`cpp` alone is enough and is a quarter of the size: the tool only preprocesses,
never compiles. If you use it instead of `gcc`, override the normalizer in
`config.json`:

```json
"normalizers": { "strip-comments": "cpp -fpreprocessed -dD -E -P -x c -" }
```

### Option B: container

Carries `gcc`, `clang-format`, `git` and Python, so nothing is installed:

```bash
docker run --rm -v "$PWD:/work" uzanni/manishtana:1.0.1 \
    v1 v2 -c config.json -o report.md --no-llm
```

### Verify stage 1

```bash
python3 -m unittest discover -s tests        # expect: OK
python3 -m manishtana --help                 # expect: usage, exit 0
gcc --version && clang-format --version      # expect: versions, not "not found"
```

---

## Stage 2 — VM

A single host running batches. Docker or Podman; no Python needed on the host.

### Get the image

Connected:

```bash
docker pull uzanni/manishtana@sha256:bcef86e6ff38d69d9312d35b27292ee89c8907dbc92dbb9ee85db98c3242864b
```

Pin the digest rather than the `1.0.1` tag. A tag can be repointed; a digest
cannot, so the box always runs the image you tested.

Air-gapped — carry one file and load it:

```bash
docker load -i manishtana-image.tar.gz
```

`./airgap.sh pack` builds that file, the repository bundle and checksums into
`dist/`. The image **cannot be rebuilt on the closed network**: the Dockerfile
installs git, clang-format and gcc from Debian repositories, and without a
network the build fails with `E: Unable to locate package gcc`. Carry the image;
do not plan to build it there.

### Run

```bash
mkdir -p out
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$PWD/v1:/work/v1:ro" \
  -v "$PWD/v2:/work/v2:ro" \
  -v "$PWD/config.json:/work/config.json:ro" \
  -v "$PWD/out:/work/out" \
  uzanni/manishtana:1.0.1 \
  v1 v2 -c config.json -o out/report.md -H out/report.html -j out/report.json \
  2> out/report.warnings
```

`--user` keeps the reports owned by you rather than root; the image sets no
`USER` precisely so that works. Mount the sources read-only — the tool never
writes to them, and enforcing that stops a bad rule from touching the tree under
test.

**Always read `out/report.warnings`.** Warnings never appear in the report, and
several of them change how much you should trust it: a missing normalizer, a
file that was not valid UTF-8, an LLM call that timed out.

### Reaching the LLM endpoint

`config.json` ships with `http://localhost:8000/v1`. Inside a container
`localhost` is **the container**, so that address reaches nothing — and a dead
endpoint returns `None` rather than raising, so the analysis degrades quietly
instead of failing loudly.

| Endpoint location | What to use |
|---|---|
| Same VM | `--network host`, or the VM own LAN address |
| Elsewhere on the network | Its IP or hostname; bridge networking reaches the LAN |
| None yet | `--no-llm` — the whole diff and normalization path still applies |

### Scheduling

```ini
# /etc/systemd/system/manishtana@.service
[Unit]
Description=MaNishtana comparison for %i
After=docker.service

[Service]
Type=oneshot
WorkingDirectory=/srv/manishtana/%i
ExecStart=/usr/bin/docker run --rm --user 1000:1000 \
    -v /srv/manishtana/%i:/work uzanni/manishtana:1.0.1 \
    v1 v2 -c config.json -o out/report.md -j out/report.json
```

`Type=oneshot` is correct: this is a batch job that finishes, not a service.

### Verify stage 2

```bash
docker run --rm --entrypoint sh uzanni/manishtana:1.0.1 \
    -c 'git --version && clang-format --version && gcc --version | head -1'
docker run --rm --entrypoint python uzanni/manishtana:1.0.1 \
    -m unittest discover -s /opt/manishtana/tests
```

Then run a real comparison where you *know* one file was only reformatted, and
confirm it lands in `noise only`. That is the only check that proves the
normalizers are actually running.

---

## Stage 3 — OpenShift

One run per repository under test, so this is a **Job**, not a Deployment.

### Arbitrary UIDs — already handled

The default `restricted-v2` SCC runs the container as an arbitrary UID that
appears in no passwd file, placed in group 0. The image is built for that:

```dockerfile
RUN chgrp 0 /work && chmod g+rwX /work
```

Without it the run dies before writing anything. With it, **no `anyuid` SCC and
no privileged service account are required.** Do not set
`securityContext.runAsUser` — let OpenShift assign the UID, and let the
`fsGroup` it assigns make the volumes writable.

### Mirror the image

Disconnected clusters:

```bash
skopeo copy docker-archive:manishtana-image.tar.gz \
    docker://<mirror-registry>/manishtana:1.0.1
```

Connected:

```bash
skopeo copy docker://uzanni/manishtana:1.0.1 \
    docker://<internal-registry>/manishtana:1.0.1
```

### Configuration and secrets

`config.json` holds an `llm.api_key`. Split it: the non-secret part in a
ConfigMap, the key in a Secret exposed as an environment variable. Do not bake a
key into the image, and do not put one in a ConfigMap.

```bash
oc create configmap manishtana-config --from-file=config.json
oc create secret generic manishtana-llm --from-literal=api-key='...'
```

### The Job

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: manishtana-verify
spec:
  backoffLimit: 2
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: manishtana
          image: <registry>/manishtana:1.0.1
          args:
            - v1
            - v2
            - -c
            - config.json
            - -o
            - out/report.md
            - -j
            - out/report.json
          workingDir: /work
          env:
            - name: MANISHTANA_LLM_KEY
              valueFrom:
                secretKeyRef: { name: manishtana-llm, key: api-key }
          resources:
            requests: { cpu: "250m", memory: "256Mi" }
            limits:   { cpu: "2",    memory: "2Gi" }
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

Memory scales with the largest single file compared, not with repository size:
normalization holds one file in memory at a time. 2Gi is generous for source
trees; raise it only for very large generated files.

### Warnings go to stderr

```bash
oc logs job/manishtana-verify
```

The report itself goes to the output volume. If you collect logs centrally, the
warnings are what to alert on — `tool 'gcc' is not installed` in a production
namespace means every result from that pod is degraded.

### Verify stage 3

```bash
oc run manishtana-check --rm -it --restart=Never \
   --image=<registry>/manishtana:1.0.1 --command -- \
   sh -c 'id && gcc --version | head -1 && touch /work/probe && echo WRITABLE'
```

Expect a UID in the millions, a gcc version, and `WRITABLE`. If `touch` fails,
the image predates the `/work` group-write fix — rebuild from a commit that
includes it.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Everything looks changed; report is enormous | Not running inside the image, or gcc/clang-format missing — nothing was normalized |
| `tool 'gcc' is not installed` in warnings | The optional normalizers are absent; results are degraded, not wrong |
| Permission denied writing the report on OpenShift | Image lacks the `/work` group-write fix, or `runAsUser` was set manually |
| LLM section empty, no error | Endpoint unreachable; it degrades quietly by design. Check the warnings |
| `is not an existing directory` | Paths are relative to `workingDir` and the mount point, not the host |
| `E: Unable to locate package gcc` during build | Building on a closed network. Carry the prebuilt image instead |
| Exit code 2 | Config unreadable, directory missing, or bad arguments — JSON will be absent |

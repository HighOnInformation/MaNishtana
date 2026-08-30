#!/usr/bin/env bash
# run.sh — compare two directories with MaNishtana, in the container.
#
#   ./run.sh OLD_DIR NEW_DIR [extra args...]
#
# Writes out/report.md, out/report.html and out/report.warnings.
# Everything after the two directories is passed straight through, so
# `./run.sh v1 v2 --no-llm` does what you would expect.

set -euo pipefail

IMAGE="${MANISHTANA_IMAGE:-manishtana}"
OUT="${MANISHTANA_OUT:-out}"

die() { printf 'error: %s\n' "$1" >&2; exit 1; }

[ $# -ge 2 ] || die "usage: ./run.sh OLD_DIR NEW_DIR [extra args...]"

A="$1"; B="$2"; shift 2
[ -d "$A" ] || die "'$A' is not a directory"
[ -d "$B" ] || die "'$B' is not a directory"

command -v docker > /dev/null 2>&1 || die "docker not found on PATH"

# On Git Bash / MSYS the docker binary is a Windows program: it cannot resolve
# the POSIX paths this shell produces, and rather than failing it invents an
# empty directory at the mount point — which surfaces much later as the baffling
# "config.json: Is a directory". Hand it Windows paths, and stop MSYS rewriting
# the container-side half of each -v. A no-op everywhere else.
if command -v cygpath > /dev/null 2>&1; then
    export MSYS_NO_PATHCONV=1
    hostpath() { cygpath -w "$1"; }
else
    hostpath() { printf '%s' "$1"; }
fi
docker image inspect "$IMAGE" > /dev/null 2>&1 \
    || die "image '$IMAGE' not loaded — run: docker load -i manishtana-image.tar.gz"

# Absolute paths: docker will not accept relative ones for a bind mount.
A="$(cd "$A" && pwd)"
B="$(cd "$B" && pwd)"
mkdir -p "$OUT"
OUT_ABS="$(cd "$OUT" && pwd)"

# The config is optional. Without one on the host, the image's own copy is used.
mounts=(-v "$(hostpath "$A"):/work/v1:ro" -v "$(hostpath "$B"):/work/v2:ro"         -v "$(hostpath "$OUT_ABS"):/work/out")
config_arg=(-c /opt/manishtana/config.json)
if [ -f config.json ]; then
    mounts+=(-v "$(hostpath "$(pwd)/config.json"):/work/config.json:ro")
    config_arg=(-c config.json)

    # A placeholder model means the LLM half is not configured yet. The tool
    # degrades quietly there (a dead endpoint returns None rather than raising),
    # so say it out loud instead of letting a thin report look like a full one.
    if grep -q 'PLACEHOLDER-MODEL-NAME' config.json 2> /dev/null \
       && ! printf '%s\n' "$@" | grep -qx -- '--no-llm'; then
        printf 'warning: config.json still has PLACEHOLDER-MODEL-NAME — the LLM\n' >&2
        printf 'warning: analysis will silently produce nothing. Use --no-llm for a\n' >&2
        printf 'warning: diff-only report, or set llm.base_url and llm.model.\n' >&2
    fi
fi

# --user keeps the reports owned by the caller rather than by root. The image
# sets no USER precisely so this works.
docker run --rm --user "$(id -u):$(id -g)" "${mounts[@]}" "$IMAGE" \
    v1 v2 "${config_arg[@]}" \
    -o out/report.md -H out/report.html "$@" \
    2> "$OUT_ABS/report.warnings" || {
        status=$?
        printf 'the run failed (exit %d). Warnings:\n' "$status" >&2
        cat "$OUT_ABS/report.warnings" >&2
        exit "$status"
    }

echo
echo "  report    $OUT/report.md"
echo "  html      $OUT/report.html"
echo "  warnings  $OUT/report.warnings"
if [ -s "$OUT_ABS/report.warnings" ]; then
    echo
    echo "There are warnings. They are never in the report itself, and some of"
    echo "them change how much you should trust it. Read them:"
    sed 's/^/      /' "$OUT_ABS/report.warnings"
fi

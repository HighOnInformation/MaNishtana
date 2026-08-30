#!/usr/bin/env bash
# airgap.sh — build one transfer set containing everything the closed network
# needs, and import it on the far side.
#
#   ./airgap.sh deps <img>  download the target distro's OS packages
#   ./airgap.sh pack        build the transfer set into dist/
#   ./airgap.sh verify      check the transfer set against its checksums
#   ./airgap.sh import      verify, clone the repo, load the image
#   ./airgap.sh selftest    run the test suite, natively and inside the image
#
# The set holds up to three artifacts, because they solve different problems:
#
#   manishtana.bundle        the whole git repository, all history, one file.
#                            Clone from it offline; bundle back out to return work.
#   manishtana-image.tar.gz  a container carrying python, git, clang-format and
#                            gcc, so the target needs no package repository.
#   deps/<distro>/           the same tools as native .deb/.rpm packages, for a
#                            target that has no docker. Run install.sh as root.
#
# The bundle alone is enough when the target already has Python 3.8+ and git.
# Carry the image if the target runs docker, or deps/ if it does not.

set -euo pipefail

IMAGE="${MANISHTANA_IMAGE:-manishtana}"
DIST="${MANISHTANA_DIST:-dist}"
BUNDLE="manishtana.bundle"
TARBALL="manishtana-image.tar.gz"
SUMS="SHA256SUMS"

say()  { printf '==> %s\n' "$1"; }
warn() { printf 'warning: %s\n' "$1" >&2; }
die()  { printf 'error: %s\n' "$1" >&2; exit 1; }

have_docker() {
    command -v docker > /dev/null 2>&1 && docker info > /dev/null 2>&1
}

sha() {
    if command -v sha256sum > /dev/null 2>&1; then sha256sum "$@"
    elif command -v shasum > /dev/null 2>&1; then shasum -a 256 "$@"
    else die "no sha256sum or shasum — cannot checksum this transfer"
    fi
}

do_pack() {
    git rev-parse --git-dir > /dev/null 2>&1 || die "not inside a git repository"
    if [ -n "$(git status --porcelain)" ]; then
        warn "working tree is dirty — a bundle carries commits only, so"
        warn "uncommitted changes will NOT cross. Commit first if you meant to."
    fi

    mkdir -p "$DIST"
    rm -f "$DIST/$BUNDLE" "$DIST/$TARBALL" "$DIST/$SUMS"

    say "bundling the repository (all branches, full history)"
    git bundle create "$DIST/$BUNDLE" --all
    git bundle verify "$DIST/$BUNDLE" > /dev/null || die "the bundle did not verify"

    if have_docker; then
        say "building $IMAGE"
        docker build -t "$IMAGE" .
        say "saving $TARBALL"
        docker save "$IMAGE" | gzip > "$DIST/$TARBALL"
    else
        warn "docker unavailable — packing the repository only."
        warn "The target will then need Python 3.8+ and git 2.30+ of its own."
    fi

    cp "$0" "$DIST/airgap.sh"

    # The reader-facing files travel inside the set, not only in the repository.
    # The target may never see a checkout — often dist/ is the only thing that
    # crosses — and a transfer set that cannot explain itself is a support call.
    for doc in QUICKSTART.md INSTALL.md run.sh; do
        if [ -f "$doc" ]; then cp "$doc" "$DIST/$doc"; fi
    done

    write_import_notes

    say "checksumming"
    # Recursive, so deps/<distro>/*.deb are covered too. Sorted for a stable file.
    (
        cd "$DIST"
        find . -type f ! -name "$SUMS" | LC_ALL=C sort | while IFS= read -r f; do
            sha "$f"
        done > "$SUMS"
    )

    echo
    say "transfer set ready in $DIST/"
    ( cd "$DIST" && ls -lh | tail -n +2 | awk '{printf "      %-24s %s\n", $9, $5}' )
    echo
    echo "Copy the whole $DIST/ directory across, then run:  ./airgap.sh import"
}

write_import_notes() {
    cat > "$DIST/IMPORT.txt" <<'EOF'
MaNishtana — importing on the closed network
===========================================

1. Verify the transfer before trusting any of it:

       ./airgap.sh verify

   A mismatch means the media is corrupt. Do not continue.

2. If this set has a deps/ directory and the target lacks git, gcc,
   clang-format or Python, install them first, as root:

       sh deps/<distro>/install.sh --dry-run    # check nothing is missing
       sh deps/<distro>/install.sh

   Only git and Python 3.8+ are required. Without clang-format and gcc the
   tool still runs, but formatting and comment changes count as real changes.

3. Import:

       ./airgap.sh import

   Clones the repository from manishtana.bundle into ./manishtana, and loads the
   container image if one was included.

4. Confirm it runs HERE, not merely that it copied:

       ./airgap.sh selftest

Running it
----------

Native (needs Python 3.8+ and git 2.30+):

    cd manishtana
    python3 -m manishtana v1 v2 -c config.json -o report.md -H report.html \
        2> report.warnings

Container (needs only docker):

    mkdir -p out
    docker run --rm --user "$(id -u):$(id -g)" \
      -v "$PWD/v1:/work/v1:ro" -v "$PWD/v2:/work/v2:ro" \
      -v "$PWD/config.json:/work/config.json:ro" -v "$PWD/out:/work/out" \
      manishtana v1 v2 -c config.json -o out/report.md -H out/report.html \
      2> out/report.warnings

Read the .warnings file first. Warnings never appear in the report itself, and
some of them change how much you should trust it.

Sending work back out
---------------------

Commit on the closed network, then bundle in the other direction:

    git bundle create outbound.bundle --all

Carry that file out and fetch from it on the connected side.
EOF
}

do_verify() {
    local dir="."
    [ -f "$SUMS" ] || dir="$DIST"
    [ -f "$dir/$SUMS" ] || die "$SUMS not found — run this from the transfer set"
    say "verifying the transfer set"
    ( cd "$dir" && sha --check --ignore-missing "$SUMS" ) \
        || die "CHECKSUM MISMATCH — the transfer is corrupt, do not import it"
    echo "checksums ok"
}

do_import() {
    do_verify
    local dir="."
    [ -f "$BUNDLE" ] || dir="$DIST"

    if [ -e manishtana ]; then
        warn "./manishtana already exists — skipping the clone"
    else
        say "cloning the repository from $BUNDLE"
        git clone "$dir/$BUNDLE" manishtana
        # A bundle leaves 'origin' pointing at a file that will not stay there.
        # Drop it so nobody is misled into thinking they can fetch from it.
        ( cd manishtana && git remote remove origin )
    fi

    if [ -f "$dir/$TARBALL" ]; then
        have_docker || die "$TARBALL is present but docker is not available here"
        say "loading the container image"
        docker load < "$dir/$TARBALL"
    else
        warn "no image in this set — native use only (needs Python 3.8+ and git)"
    fi

    echo
    say "imported. Confirm it runs here:  ./airgap.sh selftest"
}

# Being on PATH is not the same as working: a Windows Store stub answers to
# `python3`, prints a partial line and exits 49. Probe before trusting it, or the
# selftest silently tests nothing and reports success.
find_python() {
    local candidate
    for candidate in python3 python; do
        if command -v "$candidate" > /dev/null 2>&1 \
           && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
              > /dev/null 2>&1; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

do_selftest() {
    local failed=0
    local py=""
    py=$(find_python) || warn "no working Python 3.8+ on PATH"

    if [ -n "$py" ]; then
        local root="."
        [ -d manishtana/tests ] && root="manishtana"
        if [ -d "$root/tests" ]; then
            say "native suite"
            ( cd "$root" && "$py" -m unittest discover -s tests ) || failed=1
            say "native tools"
            "$py" --version
            git --version || warn "git is missing — the comparison cannot run natively"
        else
            warn "no tests/ directory here — skipping the native suite"
        fi
    fi

    if have_docker && docker image inspect "$IMAGE" > /dev/null 2>&1; then
        say "packaged suite, inside the image"
        docker run --rm --entrypoint python "$IMAGE" \
            -m unittest discover -s /opt/manishtana/tests || failed=1
        say "bundled tools"
        docker run --rm --entrypoint sh "$IMAGE" -c \
            'git --version && clang-format --version && gcc --version | head -1' || failed=1
    else
        warn "image not loaded — skipping the container checks"
    fi

    [ "$failed" -eq 0 ] || die "selftest FAILED — do not rely on this copy"
    echo
    say "selftest passed"
}

# --- OS packages -------------------------------------------------------------
# git, clang-format and gcc are native binaries; they cannot be vendored into a
# repository in any portable way. What CAN be carried is the target distro's own
# packages, downloaded on the connected side inside a container of that distro so
# the architecture and versions match.
#
# The resolution happens against the CONTAINER's package state, so the image must
# match the target. debian:12 packages will not install on Rocky 9, and a target
# more minimal than the image may still want a transitive dependency that the
# image already had. Verify with `install.sh --dry-run` on the target.

APT_PACKAGES="${MANISHTANA_APT_PACKAGES:-git clang-format gcc python3}"
DNF_PACKAGES="${MANISHTANA_DNF_PACKAGES:-git clang-tools-extra gcc python3}"

do_deps() {
    local base="${1:-}"
    [ -n "$base" ] || die "usage: ./airgap.sh deps <base-image>   e.g. debian:12, rockylinux:9"
    have_docker || die "downloading OS packages needs docker on this (connected) side"

    local slug out
    slug=$(printf '%s' "$base" | tr '/:' '--')
    out="$DIST/deps/$slug"
    mkdir -p "$out"
    rm -f "$out"/*.deb "$out"/*.rpm 2> /dev/null || true

    say "resolving packages inside $base"
    docker run --rm -v "$(cd "$out" && pwd):/out" "$base" sh -c "
        set -e
        if command -v apt-get > /dev/null 2>&1; then
            apt-get update
            apt-get install -y --no-install-recommends --download-only --reinstall $APT_PACKAGES
            cp /var/cache/apt/archives/*.deb /out/
        elif command -v dnf > /dev/null 2>&1; then
            dnf install -y --downloadonly --downloaddir=/out \
                --setopt=install_weak_deps=False $DNF_PACKAGES
        elif command -v yum > /dev/null 2>&1; then
            yum install -y --downloadonly --downloaddir=/out $DNF_PACKAGES
        else
            echo 'no apt-get, dnf or yum in this image' >&2; exit 1
        fi
    " || die "package download failed inside $base"

    write_install_script "$out" "$base"
    say "packages for $base:"
    ( cd "$out" && ls -1 | sed 's/^/      /' )
    echo
    echo "Re-run './airgap.sh pack' to fold these into the checksummed set."
}

write_install_script() {
    local out="$1" base="$2"
    cat > "$out/install.sh" <<EOF
#!/usr/bin/env sh
# Offline install of manishtana's dependencies, resolved against $base.
# Run as root on the target:  sh install.sh   (or: sh install.sh --dry-run)

set -eu
cd "\$(dirname "\$0")"

if ls ./*.deb > /dev/null 2>&1; then
    if [ "\${1:-}" = "--dry-run" ]; then
        dpkg --dry-run -i ./*.deb
    else
        # dpkg does not order by dependency; a second pass settles the first
        # pass's unmet ordering, which is normal and not an error.
        dpkg -i ./*.deb || dpkg -i ./*.deb
    fi
elif ls ./*.rpm > /dev/null 2>&1; then
    if [ "\${1:-}" = "--dry-run" ]; then
        rpm -Uvh --test ./*.rpm
    else
        rpm -Uvh --replacepkgs ./*.rpm
    fi
else
    echo "no .deb or .rpm here" >&2; exit 1
fi

echo
echo "Installed. Verify:"
echo "  python3 --version   # needs 3.8+"
echo "  git --version       # needs 2.30+"
echo "  clang-format --version"
echo "  gcc --version"
EOF
    chmod +x "$out/install.sh"
}

case "${1:-pack}" in
    pack)     do_pack ;;
    deps)     do_deps "${2:-}" ;;
    verify)   do_verify ;;
    import)   do_import ;;
    selftest) do_selftest ;;
    -h|--help|help) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//' ;;
    *) die "unknown command '${1}' — try: pack | deps | verify | import | selftest" ;;
esac

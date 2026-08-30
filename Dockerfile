# MaNishtana — air-gap delivery image.
#
# Bundles the one dependency the tool cannot degrade without (git) and the two
# optional normalizers (clang-format, gcc), so the closed network needs no
# package repository. See README.md for the build / transfer / run sequence.
#
# For a reproducible transfer artifact, pin the base image by digest:
#   docker inspect --format '{{index .RepoDigests 0}}' python:3.11-slim
# and replace the FROM line with the python@sha256:... value it prints.

FROM python:3.11-slim

LABEL org.opencontainers.image.title="MaNishtana" \
      org.opencontainers.image.description="Rule-based directory comparison, with LLM analysis of the real changes only" \
      org.opencontainers.image.source="https://github.com/HighOnInformation/MaNishtana"

RUN apt-get update \
 && apt-get install -y --no-install-recommends git clang-format gcc \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# The compared directories arrive as bind mounts, so they are owned by whatever
# uid the host uses. Without this, git refuses to touch a tree it considers to
# have "dubious ownership" and the comparison fails before it starts.
RUN git config --system --add safe.directory '*'

# The package lives outside the working directory on purpose: a bind mount over
# /work must not be able to shadow it.
ENV PYTHONPATH=/opt/manishtana \
    PYTHONDONTWRITEBYTECODE=1
COPY manishtana/ /opt/manishtana/manishtana/
COPY tests/ /opt/manishtana/tests/
COPY config.json /opt/manishtana/config.json

# Prove the copy is intact while the network still exists. The suite needs no
# git, no normalizers and no endpoint, so this is a pure integrity check — and
# it is the same command the target box re-runs after the transfer.
RUN python -m unittest discover -s /opt/manishtana/tests \
 && python -m manishtana --help > /dev/null

# No USER is set, deliberately. Nothing here needs a particular uid — git's
# config is system-wide and nothing under /opt is written at runtime — so the
# caller passes --user "$(id -u):$(id -g)" and the reports land owned by them
# rather than by root. Hardcoding a uid would break exactly that, and would
# also make every bind-mounted output directory unwritable.
WORKDIR /work

# OpenShift's restricted SCC assigns an arbitrary uid that appears in no passwd
# file and places it in group 0, so a root:root 755 /work is not writable there
# and the run dies before it writes anything. Group-own by 0 and grant group
# write: inert under docker, where the caller's --user already works, and the
# difference between running and not running under OpenShift.
RUN chgrp 0 /work && chmod g+rwX /work

ENTRYPOINT ["python", "-m", "manishtana"]
CMD ["--help"]

FROM python:3.11-slim-bookworm AS builder
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl unixodbc-dev \
    && rm -rf /var/lib/apt/lists/*
ARG POETRY_VERSION=2.4.1
RUN pip install --upgrade pip "poetry==${POETRY_VERSION}"
WORKDIR /app
ENV POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=1 \
    POETRY_VIRTUALENVS_CREATE=1 \
    POETRY_CACHE_DIR=/tmp/poetry_cache
COPY pyproject.toml poetry.lock ./
COPY README.md ./
COPY src ./src
COPY examples ./examples
RUN poetry install --no-root --only main \
    && poetry build --format wheel \
    && .venv/bin/pip install --no-deps dist/*.whl \
    # Strip build/install tooling (pip, setuptools, wheel) from the runtime
    # venv: a running service never installs packages, and these ship known
    # CVEs — including the ones setuptools vendors internally
    # (jaraco.context / wheel). QueryGate resolves package metadata via
    # importlib.metadata (stdlib), not pkg_resources, so nothing at runtime
    # needs them. Keeps the shipped image free of HIGH/CRITICAL dependency CVEs
    # (verified by the CI Trivy gate). See TODO.md item 30 phase 2 / item 89.
    && .venv/bin/pip uninstall -y pip setuptools wheel \
    && rm -rf $POETRY_CACHE_DIR dist

FROM python:3.11-slim-bookworm AS production
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# unixodbc + the Microsoft ODBC driver are only needed for MSSQL connections;
# skip this layer if you only connect to Postgres.
#
# Base image is pinned to -bookworm rather than the floating python:3.11-slim
# tag: that tag moved to Debian 13 (trixie) and Microsoft's debian/13 apt
# repo is signed with a key (EE4D7792F748182B) their own microsoft.asc key
# file doesn't contain — a known upstream issue as of 2026
# (microsoft/linux-package-repositories#305, #253), not something fixable
# from here. bookworm + msodbcsql18 is a supported, working combination.
#
# The key must be dearmored into a binary keyring, not piped straight into
# trusted.gpg.d/*.asc — Debian's apt (via sqv) rejects an ASCII-armored key
# there. /usr/share/keyrings/microsoft-prod.gpg is the exact path
# Microsoft's own prod.list already references via signed-by=.
# `msodbcsql18` is proprietary and accepted under EULA, and the base image's
# "slim" dpkg config path-excludes /usr/share/doc/*, so the driver's own
# LICENSE.txt was declared by the package but never landed in the image — we
# were shipping Microsoft's driver without its licence text. The path-include
# below must be written BEFORE the install for dpkg to honour it. Measured
# 2026-08-21 (TODO item 196): 124 OS packages, of which msodbcsql18 was one of
# only three with no copyright file present.
RUN printf 'path-include /usr/share/doc/msodbcsql18/*\n' \
      > /etc/dpkg/dpkg.cfg.d/msodbcsql18-licence \
    && apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg unixodbc \
    && curl -sSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && curl -sSL https://packages.microsoft.com/config/debian/12/prod.list -o /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && apt-get purge -y gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# The base python:3.11-slim image ships pip/setuptools/wheel in the SYSTEM
# site-packages, and setuptools vendors CVE-bearing copies of jaraco.context /
# wheel. QueryGate runs entirely from /app/.venv (its entrypoint's shebang
# targets the venv interpreter) and never uses the system interpreter's tooling,
# so remove it to keep the shipped image free of HIGH/CRITICAL dependency CVEs.
# Verified by the CI Trivy gate. See TODO.md item 30 phase 2 / item 89.
RUN rm -rf /usr/local/lib/python3.11/site-packages/setuptools* \
    /usr/local/lib/python3.11/site-packages/pip \
    /usr/local/lib/python3.11/site-packages/pip-* \
    /usr/local/lib/python3.11/site-packages/wheel \
    /usr/local/lib/python3.11/site-packages/wheel-* \
    /usr/local/lib/python3.11/site-packages/pkg_resources \
    /usr/local/lib/python3.11/site-packages/_distutils_hack \
    /usr/local/lib/python3.11/site-packages/distutils-precedence.pth
RUN addgroup --system querygate && adduser --system --ingroup querygate querygate
# BSL 1.1 requires the licence to be displayed conspicuously on each copy of the
# Licensed Work, and the container image is how QueryGate is distributed. The
# wheel already carries it (`License-File: LICENSE` in its METADATA); the image
# did not until this line. Asserted by `scripts/check_release_artifacts.py`.
COPY LICENSE /app/LICENSE
COPY --from=builder /app/.venv /app/.venv
RUN chown -R querygate:querygate /app
USER querygate

EXPOSE 8000
CMD ["querygate"]

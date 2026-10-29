#!/usr/bin/env bash
# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

set -e
cd "$(dirname "$0")/.."
ROOT=$(pwd)

VERSION=$(grep -E '^version\s*=' pyproject.toml | sed -E "s/.*=\s*['\"]?([^'\"]+)['\"]?/\1/")
if [[ -z "$VERSION" ]]; then
  echo "Could not get version from pyproject.toml"
  exit 1
fi

DIST_DIR="${ROOT}/dist"
RPMBUILD_DIR="${RPMBUILD_DIR:-$HOME/rpmbuild}"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
PKG_ROOT="${STAGE}/oreon-build-worker-${VERSION}"
PKG_SRC="${PKG_ROOT}/src/oreon_build"

mkdir -p "${PKG_SRC}/worker" "${PKG_SRC}/core" "${PKG_SRC}/services"

cp "${ROOT}/src/oreon_build/__init__.py" "${PKG_SRC}/__init__.py"
cp "${ROOT}/src/oreon_build/config.py" "${PKG_SRC}/config.py"
cp "${ROOT}/src/oreon_build/worker/__init__.py" "${PKG_SRC}/worker/__init__.py"
cp "${ROOT}/src/oreon_build/worker/main.py" "${PKG_SRC}/worker/main.py"
cp "${ROOT}/src/oreon_build/worker/iso_job.py" "${PKG_SRC}/worker/iso_job.py"
cp "${ROOT}/src/oreon_build/core/build_result.py" "${PKG_SRC}/core/build_result.py"
cp "${ROOT}/src/oreon_build/core/dnf_missing.py" "${PKG_SRC}/core/dnf_missing.py"
: > "${PKG_SRC}/core/__init__.py"
cp "${ROOT}/src/oreon_build/services/__init__.py" "${PKG_SRC}/services/__init__.py"
cp "${ROOT}/src/oreon_build/services/signing.py" "${PKG_SRC}/services/signing.py"

cat > "${PKG_ROOT}/pyproject.toml" << EOF
[build-system]
requires = ["setuptools>=61", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "oreon-build-worker"
version = "${VERSION}"
description = "Oreon Build Service worker"
requires-python = ">=3.11"
dependencies = [
    "httpx>=0.26.0",
    "pydantic>=2.5.0",
    "pydantic-settings>=2.1.0",
]

[project.scripts]
oreon-worker = "oreon_build.worker.main:main"

[tool.setuptools.packages.find]
where = ["src"]
EOF

mkdir -p "${DIST_DIR}"
TAR_NAME="oreon-build-worker-${VERSION}.tar.gz"
tar -C "${STAGE}" -czf "${DIST_DIR}/${TAR_NAME}" "oreon-build-worker-${VERSION}"

mkdir -p "${RPMBUILD_DIR}/SOURCES"
cp "${DIST_DIR}/${TAR_NAME}" "${RPMBUILD_DIR}/SOURCES/${TAR_NAME}"
cp "${ROOT}/deploy/oreon-worker.env.example" "${RPMBUILD_DIR}/SOURCES/oreon-worker.env.example"
rpmbuild -bs packaging/oreon-build-worker.spec

echo "SRPM: ${RPMBUILD_DIR}/SRPMS/oreon-build-worker-${VERSION}-*.src.rpm"

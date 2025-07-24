#!/usr/bin/env bash
# take the project sdist tarball and build the srpm

set -e
cd "$(dirname "$0")/.."
ROOT=$(pwd)

VERSION=$(grep -E '^version\s*=' pyproject.toml | sed -E "s/.*=\s*['\"]?([^'\"]+)['\"]?/\1/")
if [[ -z "$VERSION" ]]; then
  echo "Could not get version from pyproject.toml"
  exit 1
fi

# python -m build produces oreon_build_service-*.tar.gz (normalized name from oreon-build-service)
SDIST_NAME="oreon_build_service-${VERSION}.tar.gz"
DIST_DIR="${ROOT}/dist"
RPMBUILD_DIR="${RPMBUILD_DIR:-$HOME/rpmbuild}"

FORCE_REBUILD_SDIST="${FORCE_REBUILD_SDIST:-1}"
if [[ "${FORCE_REBUILD_SDIST}" == "1" ]]; then
  pip install -q build
  rm -f "${DIST_DIR}/${SDIST_NAME}" 2>/dev/null || true
  python -m build --sdist --outdir "${DIST_DIR}"
else
  if [[ ! -f "${DIST_DIR}/${SDIST_NAME}" ]]; then
    pip install -q build
    python -m build --sdist --outdir "${DIST_DIR}"
  fi
fi
if [[ ! -f "${DIST_DIR}/${SDIST_NAME}" ]]; then
  echo "Missing ${DIST_DIR}/${SDIST_NAME}"
  exit 1
fi

mkdir -p "${RPMBUILD_DIR}/SOURCES"
cp "${DIST_DIR}/${SDIST_NAME}" "${RPMBUILD_DIR}/SOURCES/oreon-build-service-${VERSION}.tar.gz"
cp "${ROOT}/deploy/oreon-worker.env.example" "${RPMBUILD_DIR}/SOURCES/oreon-worker.env.example"
rpmbuild -bs packaging/oreon-build-worker.spec

echo "SRPM: ${RPMBUILD_DIR}/SRPMS/oreon-build-worker-${VERSION}-1*.src.rpm"

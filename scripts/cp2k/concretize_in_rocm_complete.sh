#!/usr/bin/env bash
set -euo pipefail

# Run inside a rocm/dev-ubuntu-24.04:7.2.1-complete container.
# Expected mount: host repo at /work

ENV_NAME="${1:-cp2k-rocm-2026.1-gfx942}"
WORK_ROOT="/work"
ENV_DIR="${WORK_ROOT}/spack-envs/${ENV_NAME}"

if [[ ! -d "${ENV_DIR}" ]]; then
  echo "[error] Spack env directory not found: ${ENV_DIR}" >&2
  exit 1
fi

echo "[info] Environment: ${ENV_NAME}"
echo "[info] Running stage1-like concretize in rocm complete"

# Stage1-like apt mirror + base tool setup
sed -i "s@//.*archive.ubuntu.com@//mirrors.ustc.edu.cn@g" /etc/apt/sources.list.d/ubuntu.sources || true
sed -i "s@//security.ubuntu.com@//mirrors.ustc.edu.cn@g" /etc/apt/sources.list.d/ubuntu.sources || true

apt-get update -qq
apt-get install -y --no-install-recommends \
  bash build-essential gfortran git cmake ninja-build python3 ca-certificates \
  pkgconf autoconf automake libtool curl wget file patch unzip bzip2 xz-utils zlib1g-dev libssl-dev binutils
apt-get clean
rm -rf /var/lib/apt/lists/*

SPACK_ROOT="/opt/spack-exe"
mkdir -p "${SPACK_ROOT}"
tar -axf "${WORK_ROOT}/assets/spack-v1.1.0.tar.gz" --strip-components=1 -C "${SPACK_ROOT}"

# shellcheck disable=SC1091
. "${SPACK_ROOT}/share/spack/setup-env.sh"
echo "[info] Spack version: $(spack --version)"

# Stage1-like bootstrap + mirror registration
spack bootstrap add --trust local-sources "${WORK_ROOT}/assets/bootstrap/metadata/sources"
spack bootstrap add --trust local-binaries "${WORK_ROOT}/assets/bootstrap/metadata/binaries"
spack bootstrap disable github-actions-v2 || true
spack bootstrap disable github-actions-v0.6 || true
spack bootstrap disable spack-install || true
spack bootstrap now

spack mirror remove local-mirror >/dev/null 2>&1 || true
spack mirror add --scope site local-mirror "file://${WORK_ROOT}/assets/spack-mirror"

ln -sf /opt/rocm /opt/rocm/hip

spack compiler find || true
spack external find --all || true

# Copy env to writable local path to avoid writing into mounted tree during concretize.
BUILD_ENV="/opt/hpc-env"
rm -rf "${BUILD_ENV}"
cp -a "${ENV_DIR}" "${BUILD_ENV}"

if [[ -d "${BUILD_ENV}/repos" ]]; then
  if ! spack repo list 2>/dev/null | grep -q "${BUILD_ENV}/repos"; then
    echo "[info] Registering custom repo: ${BUILD_ENV}/repos"
    spack repo add --scope site "${BUILD_ENV}/repos"
  fi
  spack -e "${BUILD_ENV}" repo ls
fi

cd "${BUILD_ENV}"
echo "[info] Concretizing..."
spack -e . concretize -f

cp -f "${BUILD_ENV}/spack.lock" "${ENV_DIR}/spack.lock"
echo "[ok] Exported lock: ${ENV_DIR}/spack.lock"

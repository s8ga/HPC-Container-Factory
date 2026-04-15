#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_PATH="${SCRIPT_DIR}/repos"

echo "  [hook] cp2k-rocm-2026.1-gfx942 mirror-create.sh starting..."

if [[ ! -d "${REPO_PATH}" ]]; then
  echo "  [hook] repo path not found: ${REPO_PATH}" >&2
  exit 1
fi

if ! spack repo list 2>/dev/null | grep -q "${REPO_PATH}"; then
  echo "  [hook] Adding custom repo: ${REPO_PATH}"
  spack repo add --scope site "${REPO_PATH}"
else
  echo "  [hook] Custom repo already registered"
fi

echo "  [hook] Active repos:"
spack repo list
echo "  [hook] cp2k-rocm-2026.1-gfx942 mirror-create.sh complete"

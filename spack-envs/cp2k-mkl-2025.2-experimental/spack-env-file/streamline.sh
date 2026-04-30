#!/bin/bash
# ============================================================================
# streamline.sh — Per-env pipeline entrypoint
#
# Runs INSIDE the mirror-builder container.
# /work is bind-mounted to the project root.
#
# All logic is in scripts/spack-common.sh. This file only sets env-specific
# paths and delegates to the common dispatch. To adapt for a new environment,
# modify env.yaml — not this file.
# ============================================================================
set -euo pipefail

ENV_NAME="${ENV_NAME:?ENV_NAME not set}"
MIRROR_DIR="${MIRROR_DIR:-/work/assets/spack-mirror}"
ENV_DIR="/work/spack-envs/${ENV_NAME}/spack-env-file"
MODE="${1:?Usage: streamline.sh <concretize|mirror|all|verify>}"

source /work/scripts/spack-common.sh
streamline_parse_env
streamline_dispatch "${MODE}"


#!/bin/bash
# /usr/local/bin/hpc-shell-wrapper.sh
# Apptainer/Singularity SINGULARITY_SHELL target baked into Stage 2 runtime.
# Shows MOTD for interactive TTY shell sessions and execs /bin/bash cleanly,
# eliminating Apptainer %post mutations at SIF build time.

set -euo pipefail

if [[ -t 0 && -t 1 ]]; then
    "${HPC_MOTD_SCRIPT:-/usr/local/bin/hpc-motd.sh}" 2>/dev/null || true
fi

exec /bin/bash "$@"

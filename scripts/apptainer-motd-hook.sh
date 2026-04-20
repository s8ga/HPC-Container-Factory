#!/bin/sh
# /.singularity.d/env/99-motd.sh
# Apptainer auto-sources all *.sh in this directory at container startup.
# This script is the Apptainer equivalent of our Docker ENTRYPOINT motd hook.
#
# Key difference from Docker's ENTRYPOINT:
#   - This runs for ALL apptainer commands (shell/exec/run)
#   - We check APPTAINER_COMMAND to only show MOTD for 'shell'
#   - We must use POSIX sh (not bash) — Apptainer's embedded shell is dash/ash
#   - cp2k-motd.sh requires bash, so we call it via: bash /usr/local/bin/hpc-motd.sh
#
# IMPORTANT: This file MUST use POSIX sh syntax only (no [[ ]], no $'', no arrays).

# Only show MOTD for interactive shell sessions
if [ "${APPTAINER_COMMAND}" = "shell" ]; then
    # cp2k-motd.sh requires bash (arrays, [[ ]], $'' quoting)
    # Redirect stderr to suppress any non-critical warnings
    bash /usr/local/bin/hpc-motd.sh 2>/dev/null || true
fi

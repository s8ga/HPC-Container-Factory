#!/bin/bash
# /usr/local/bin/entrypoint.sh
# Container entrypoint: shows MOTD only for interactive shell sessions.
#
# Apptainer sets APPTAINER_COMMAND to 'shell', 'exec', or 'run'.
# - 'shell' → interactive login → show MOTD
# - 'exec'/'run' → non-interactive → skip MOTD
#
# Docker/Podman (OCI) do not set APPTAINER_COMMAND.
# In OCI mode, this entrypoint is only called for interactive shells
# (configured via ENV in Dockerfile), so MOTD is always shown.
#
# Usage in Dockerfile (final stage):
#   COPY --chmod=755 scripts/entrypoint.sh /usr/local/bin/entrypoint.sh
#   COPY --chmod=755 scripts/cp2k-motd.sh /usr/local/bin/hpc-motd.sh
#   ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
#   CMD ["bash"]
#   ENV BASH_ENV=/usr/local/bin/hpc-motd.sh   # Docker/Podman interactive shell MOTD

set -euo pipefail

# ── Show MOTD only for interactive shell sessions ────────────────────────

_should_show_motd() {
    # Apptainer: use APPTAINER_COMMAND
    if [[ "${APPTAINER_COMMAND:-}" == "shell" ]]; then
        return 0
    fi
    # OCI (Docker/Podman): if ENTRYPOINT is used without args,
    # the default CMD is "bash" — this is an interactive shell.
    # exec/run modes override ENTRYPOINT args, so we won't reach here.
    if [[ -z "${APPTAINER_COMMAND:-}" && "${1:-}" == "bash" ]]; then
        return 0
    fi
    return 1
}

if _should_show_motd "${1:-}"; then
    /usr/local/bin/hpc-motd.sh 2>/dev/null || true
fi

# ── Hand off to the container's main process ─────────────────────────────
exec "$@"

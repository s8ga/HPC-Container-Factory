#!/bin/bash
# ============================================================================
# spack-common.sh — Shared Spack utilities for containerized pipelines
#
# This script is sourced by per-env streamline.sh scripts.
# It provides common functions for:
#   - Spack bootstrap configuration
#   - Mirror creation with stats parsing
#   - Mirror verification
#   - Environment setup (create temp env from yaml+lock)
#
# Expected environment (set by caller or build-mirror-in-container.sh):
#   ENV_NAME   — spack environment name (e.g. cp2k-opensource-2025.2)
#   MIRROR_DIR — absolute path inside container for mirror output
#   ENV_DIR    — path to env directory (derived: /work/spack-envs/${ENV_NAME})
#
# Usage (inside container):
#   source /work/scripts/spack-common.sh
#   spack_bootstrap
#   mirror_create /path/to/env-dir /path/to/mirror-dir
#   mirror_verify /path/to/env-dir /path/to/mirror-dir
# ============================================================================
set -euo pipefail

# ── Colors ─────────────────────────────────────────────────────────────────
_SC_RED='\033[0;31m'
_SC_GREEN='\033[0;32m'
_SC_YELLOW='\033[1;33m'
_SC_BLUE='\033[0;34m'
_SC_NC='\033[0m'

_sc_info()  { echo -e "${_SC_BLUE}[INFO]${_SC_NC}  $*"; }
_sc_ok()    { echo -e "${_SC_GREEN}[OK]${_SC_NC}    $*"; }
_sc_warn()  { echo -e "${_SC_YELLOW}[WARN]${_SC_NC}  $*"; }
_sc_error() { echo -e "${_SC_RED}[ERROR]${_SC_NC} $*" >&2; }

# ============================================================================
# install_system_pkgs — Install system packages using declared package manager
#
# Reads from streamline.sh config section:
#   PKG_MANAGER  — e.g. "apt-get", "dnf", "yum"
#   INSTALL_ARGS — e.g. "install -y --no-install-recommends"
#   SYSTEM_PKGS  — space-separated list of package names
#
# Does NOT auto-detect anything. Pure executor.
# ============================================================================
install_system_pkgs() {
    if [[ -z "${SYSTEM_PKGS:-}" ]]; then
        _sc_info "No system packages declared — skipping"
        return 0
    fi

    _sc_info "Installing system packages (${PKG_MANAGER})..."
    ${PKG_MANAGER} ${INSTALL_ARGS} ${SYSTEM_PKGS} 2>/dev/null \
        && _sc_ok "System packages installed" \
        || _sc_warn "Some system packages may have failed to install"
}

# ============================================================================
# spack_bootstrap — Source spack + configure local bootstrap mirror
#
# Prerequisites: /opt/spack installed, /work/assets/bootstrap available
# After this: spack commands are available, bootstrap uses local mirror
# ============================================================================
spack_bootstrap() {
    _sc_info "Configuring Spack bootstrap..."
    . /opt/spack/share/spack/setup-env.sh

    if [[ -d /work/assets/bootstrap/metadata/sources ]]; then
        spack bootstrap add --trust local-sources /work/assets/bootstrap/metadata/sources 2>/dev/null || true
        spack bootstrap add --trust local-binaries /work/assets/bootstrap/metadata/binaries 2>/dev/null || true
        spack bootstrap disable github-actions-v2 2>/dev/null || true
        spack bootstrap disable github-actions-v0.6 2>/dev/null || true
        spack bootstrap disable spack-install 2>/dev/null || true
        spack bootstrap now 2>/dev/null || true
        _sc_ok "Bootstrap configured from local mirror"
    else
        _sc_warn "No local bootstrap mirror found — using Spack defaults"
    fi

    echo "Spack version: $(spack --version)"
}

# ============================================================================
# _mirror_parse_stats — Parse spack mirror create output for statistics
#
# Arguments: $1 = log file path
# Returns:   Sets _PRESENT, _ADDED, _FAILED variables
# ============================================================================
_mirror_parse_stats() {
    local logfile="$1"
    _PRESENT=$(grep -oP '\d+(?=\s+already present)' "${logfile}" | tail -1 || echo "0")
    _ADDED=$(grep -oP '\d+(?=\s+added)' "${logfile}" | tail -1 || echo "0")
    _FAILED=$(grep -oP '\d+(?=\s+failed)' "${logfile}" | tail -1 || echo "0")
}

_mirror_report_stats() {
    echo ""
    echo "Mirror statistics:"
    echo "  Already present: ${_PRESENT}"
    echo "  Added:           ${_ADDED}"
    echo "  Failed:          ${_FAILED}"
}

# ============================================================================
# mirror_create — Download all source tarballs for a spack environment
#
# Arguments:
#   $1 = env_dir  — path to spack env directory (contains spack.yaml + lock)
#   $2 = mirror_dir — output path for mirror
#
# Creates a temporary env, activates it, runs spack mirror create.
# ============================================================================
mirror_create() {
    local env_dir="$1"
    local mirror_dir="$2"

    _sc_info "Setting up environment for mirror create..."

    local work_env="/tmp/spack-mirror-$(date +%s)"
    mkdir -p "${work_env}"
    cp "${env_dir}/spack.yaml" "${work_env}/spack.yaml"

    if [[ -f "${env_dir}/spack.lock" ]]; then
        cp "${env_dir}/spack.lock" "${work_env}/spack.lock"
        _sc_ok "Using existing spack.lock"
    else
        _sc_error "spack.lock not found — run concretize first"
        exit 1
    fi

    cd "${work_env}"
    spack env activate . 2>/dev/null || true

    mkdir -p "${mirror_dir}"

    _sc_info "Running: spack mirror create -d ${mirror_dir} --all -D"
    spack -e . mirror create -d "${mirror_dir}" --all -D 2>&1 | tee /tmp/mirror-output.log

    echo ""
    echo "=== Mirror creation complete ==="

    _mirror_parse_stats /tmp/mirror-output.log
    _mirror_report_stats

    if [[ "${_FAILED:-0}" -gt 0 ]]; then
        echo ""
        _sc_error "${_FAILED} package(s) failed to fetch!"
        exit 1
    fi

    _sc_ok "Mirror creation successful"
}

# ============================================================================
# mirror_verify — Verify mirror completeness by re-running mirror create
#
# Arguments:
#   $1 = env_dir  — path to spack env directory
#   $2 = mirror_dir — path to existing mirror
#
# Exits with error if any packages are missing.
# ============================================================================
mirror_verify() {
    local env_dir="$1"
    local mirror_dir="$2"

    _sc_info "Verifying mirror completeness..."
    echo ""

    local work_env="/tmp/spack-verify-$(date +%s)"
    mkdir -p "${work_env}"
    cp "${env_dir}/spack.yaml" "${work_env}/spack.yaml"

    if [[ -f "${env_dir}/spack.lock" ]]; then
        cp "${env_dir}/spack.lock" "${work_env}/spack.lock"
    else
        _sc_error "spack.lock not found — run concretize first"
        exit 1
    fi

    cd "${work_env}"
    spack env activate . 2>/dev/null || true

    _sc_info "Re-running: spack mirror create -d ${mirror_dir} --all -D"
    spack -e . mirror create -d "${mirror_dir}" --all -D 2>&1 | tee /tmp/verify-output.log

    _mirror_parse_stats /tmp/verify-output.log

    echo ""
    echo "Verification result:"
    echo "  Already present: ${_PRESENT}"
    echo "  Added:           ${_ADDED}"
    echo "  Failed:          ${_FAILED}"

    if [[ "${_FAILED:-0}" -gt 0 ]]; then
        echo ""
        _sc_error "${_FAILED} package(s) still missing!"
        exit 1
    fi

    _sc_ok "All packages available in mirror"
}

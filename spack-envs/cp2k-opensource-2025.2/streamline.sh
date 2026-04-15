#!/bin/bash
# ============================================================================
# streamline.sh — Generic per-env pipeline (configuration-driven)
#
# Runs INSIDE the mirror-builder container.
# /work is bind-mounted to the project root.
#
# To adapt for a new spack environment, modify ONLY the CONFIG SECTION below.
# Everything else is generic logic sourced from spack-common.sh.
#
# Configuration variables:
#   SPACK_ENV_NAME    — Name for the spack environment (e.g. cp2k-env)
#   EXTRA_APT_PKGS    — Space-separated list of additional apt packages
#   CUSTOM_REPOS      — Bash array of custom spack repos to register
#                         Each entry: "URL|BRANCH|SPARSE_PATH|NAMESPACE"
#   EXTRA_STEPS_FN    — (optional) Name of a function to run extra env-specific
#                         steps after hook but before concretize
#
# Usage:
#   streamline.sh <mode>
#     mode = concretize | mirror | all | verify
#
# Environment variables (set by build-mirror-in-container.sh):
#   ENV_NAME   — environment directory name (e.g. cp2k-opensource-2025.2)
#   MIRROR_DIR — absolute path inside container where mirror is written
# ============================================================================
set -euo pipefail

# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  CONFIG SECTION — Modify ONLY this section for new environments         ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

# Spack environment name (used in: spack env create <name>)
SPACK_ENV_NAME="cp2k-env"

# System package manager and install command
# Examples:
#   apt-get: PKG_MANAGER="apt-get" INSTALL_ARGS="install -y --no-install-recommends"
#   dnf:     PKG_MANAGER="dnf"     INSTALL_ARGS="install -y"
PKG_MANAGER="apt-get"
INSTALL_ARGS="install -y --no-install-recommends"

# System packages to install (space-separated)
# Fill in the full list your environment needs.
# mirror-builder base image already has some, but declaring them ensures
# completeness regardless of the base image.
SYSTEM_PKGS="
    bash build-essential ca-certificates curl
    environment-modules gfortran git openssh-client pkg-config
    unzip vim wget rsync nano cmake file automake bzip2
    xxd xz-utils zstd ninja-build patch pkgconf
    libncurses-dev libssh-dev libssl-dev libtool-bin
    lsb-release python3 python3-dev python3-pip python3-venv
    zlib1g-dev
"

# Custom Spack package repos to clone and register.
# Format: "GIT_URL|BRANCH|SPARSE_CHECKOUT_PATH|NAMESPACE"
# Set to empty array () if no custom repos needed.
CUSTOM_REPOS=(
    "https://github.com/cp2k/cp2k.git|support/v2025.2|tools/spack/cp2k_dev_repo|cp2k_dev_repo"
)

# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  END OF CONFIG — Do not modify below unless you know what you're doing    ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

# ── Derived paths ──────────────────────────────────────────────────────────
ENV_NAME="${ENV_NAME:?ENV_NAME not set}"
MIRROR_DIR="${MIRROR_DIR:-/work/assets/spack-mirror}"
ENV_DIR="/work/spack-envs/${ENV_NAME}"
MODE="${1:?Usage: streamline.sh <concretize|mirror|all|verify>}"

# ── Source common utilities ────────────────────────────────────────────────
source /work/scripts/spack-common.sh

# ============================================================================
# Step: Install system packages (delegates to spack-common.sh executor)
# ============================================================================
# PKG_MANAGER / INSTALL_ARGS / SYSTEM_PKGS are declared in the CONFIG section.
# install_system_pkgs() reads them directly — no auto-detection.
step_install_system_pkgs() {
    # Pre-update for apt-based systems
    if [[ "${PKG_MANAGER}" == "apt-get" ]]; then
        apt-get update -qq 2>/dev/null || true
    fi
    install_system_pkgs
    # Post-clean for apt-based systems
    if [[ "${PKG_MANAGER}" == "apt-get" ]]; then
        apt-get clean 2>/dev/null || true
        rm -rf /var/lib/apt/lists/* 2>/dev/null || true
    fi
}

# ============================================================================
# Step: Register custom Spack repos (replaces mirror-create.sh hook)
# ============================================================================
# Iterates over CUSTOM_REPOS array. Each entry: "URL|BRANCH|SPARSE_PATH|NAMESPACE"
# Performs: sparse clone → register with spack
step_register_repos() {
    if [[ ${#CUSTOM_REPOS[@]} -eq 0 ]]; then
        _sc_info "No custom repos configured — skipping"
        return 0
    fi

    for repo_entry in "${CUSTOM_REPOS[@]}"; do
        IFS='|' read -r git_url branch sparse_path namespace <<< "${repo_entry}"

        local repo_dir="/tmp/spack-repos/spack_repo/${namespace}"

        # Remove stale registration if present
        if spack repo list 2>/dev/null | grep -q "${namespace}"; then
            _sc_info "Removing stale ${namespace} registration..."
            spack repo remove "${namespace}" 2>/dev/null || true
        fi

        # Clone (sparse) if not already present
        if [[ ! -d "${repo_dir}" ]]; then
            _sc_info "Cloning ${namespace} (${branch})..."
            mkdir -p /tmp/spack-repos/spack_repo
            local clone_dir="/tmp/repo-clone-${namespace}"
            git clone --depth 1 --filter=blob:none --sparse \
                --branch "${branch}" "${git_url}" "${clone_dir}"
            cd "${clone_dir}"
            git sparse-checkout set "${sparse_path}"
            cp -a "${sparse_path}" "/tmp/spack-repos/spack_repo/"
            cd /tmp
            rm -rf "${clone_dir}"
            _sc_ok "Cloned to ${repo_dir}"
        else
            _sc_info "${namespace} already present at ${repo_dir}"
        fi

        # Register with Spack
        spack repo add "${repo_dir}"
        _sc_ok "Registered ${namespace} with Spack"
    done
}

# ============================================================================
# Step: Find compilers and external packages
# ============================================================================
step_find() {
    _sc_info "Finding system compilers..."
    spack compiler find
    _sc_ok "Compilers registered"

    _sc_info "Finding external packages..."
    spack external find --all --not-buildable
    _sc_ok "External packages registered"
}

# ============================================================================
# Step: Concretize — generate spack.lock
# ============================================================================
step_concretize() {
    _sc_info "Creating Spack environment '${SPACK_ENV_NAME}' from spack.yaml..."

    local work_env="/tmp/spack-env-$(date +%s)"
    mkdir -p "${work_env}"
    cp "${ENV_DIR}/spack.yaml" "${work_env}/spack.yaml"

    spack env create "${SPACK_ENV_NAME}" "${work_env}/spack.yaml"

    _sc_info "Concretizing (spack -e ${SPACK_ENV_NAME} concretize -f)..."
    spack -e "${SPACK_ENV_NAME}" concretize -f
    _sc_ok "Concretize complete"

    local lock_src="${SPACK_ROOT:-/opt/spack}/var/spack/environments/${SPACK_ENV_NAME}/spack.lock"
    local lock_dst="${ENV_DIR}/spack.lock"

    if [[ -f "${lock_src}" ]]; then
        if [[ -f "${lock_dst}" ]]; then
            if diff -q "${lock_dst}" "${lock_src}" >/dev/null 2>&1; then
                _sc_ok "spack.lock unchanged"
            else
                _sc_warn "spack.lock has changed — updating"
                cp -f "${lock_src}" "${lock_dst}"
                _sc_ok "spack.lock written to ${lock_dst}"
            fi
        else
            cp -f "${lock_src}" "${lock_dst}"
            _sc_ok "spack.lock written to ${lock_dst}"
        fi
    else
        _sc_error "Concretize did not produce spack.lock"
        exit 1
    fi
}

# ============================================================================
# Main dispatch
# ============================================================================
case "${MODE}" in
    concretize)
        echo "============================================================"
        echo " MODE: concretize"
        echo " Env:  ${ENV_NAME}"
        echo "============================================================"
        step_install_system_pkgs
        spack_bootstrap
        step_register_repos
        step_find
        step_concretize
        echo ""
        _sc_ok "Concretize pipeline complete"
        ;;
    mirror)
        echo "============================================================"
        echo " MODE: mirror"
        echo " Env:  ${ENV_NAME}"
        echo "============================================================"
        spack_bootstrap
        step_register_repos
        mirror_create "${ENV_DIR}" "${MIRROR_DIR}"
        echo ""
        _sc_ok "Mirror pipeline complete"
        ;;
    all)
        echo "============================================================"
        echo " MODE: all (concretize + mirror)"
        echo " Env:  ${ENV_NAME}"
        echo "============================================================"
        step_install_system_pkgs
        spack_bootstrap
        step_register_repos
        step_find
        step_concretize
        echo ""
        echo "------------------------------------------------------------"
        mirror_create "${ENV_DIR}" "${MIRROR_DIR}"
        echo ""
        echo "============================================================"
        _sc_ok "Full pipeline complete"
        ;;
    verify)
        echo "============================================================"
        echo " MODE: verify"
        echo " Env:  ${ENV_NAME}"
        echo "============================================================"
        spack_bootstrap
        step_register_repos
        mirror_verify "${ENV_DIR}" "${MIRROR_DIR}"
        echo ""
        _sc_ok "Verification complete"
        ;;
    *)
        _sc_error "Unknown mode: ${MODE}"
        echo "Usage: streamline.sh <concretize|mirror|all|verify>" >&2
        exit 1
        ;;
esac

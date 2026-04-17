#!/bin/bash
# ============================================================================
# spack-common.sh — Shared Spack utilities for containerized pipelines
#
# This script is sourced by per-env streamline.sh scripts.
# It provides ALL step functions and the dispatch logic:
#   - env.yaml parsing → bash variables
#   - step_install_system_pkgs, step_clean_stale_repos, step_register_repos
#   - step_find (compiler find only — NO external find)
#   - step_concretize
#   - spack_bootstrap, mirror_create, mirror_verify
#   - streamline_dispatch (mode router)
#
# Expected environment (set by streamline.sh before sourcing):
#   ENV_NAME   — spack environment name (e.g. cp2k-opensource-2025.2)
#   MIRROR_DIR — absolute path inside container for mirror output
#   ENV_DIR    — path to env directory (derived: /work/spack-envs/${ENV_NAME}/spack-env-file)
#   MODE       — concretize | mirror | all | verify
#
# Usage (inside container):
#   source /work/scripts/spack-common.sh
#   streamline_parse_env
#   streamline_dispatch "${MODE}"
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
# streamline_parse_env — Read env.yaml and export bash variables
#
# Sets: SPACK_ENV_NAME, SYSTEM_PKGS, PKG_MIRROR_SETUP, PKG_INSTALL_CMD,
#       CUSTOM_REPOS[], CUSTOM_REPO_COUNT
# ============================================================================
streamline_parse_env() {
    local env_yaml="${ENV_DIR}/env.yaml"
    if [[ ! -f "${env_yaml}" ]]; then
        _sc_error "env.yaml not found: ${env_yaml}"
        exit 1
    fi

    local _env_parser
    _env_parser=$(mktemp /tmp/env_parser_XXXXXX.py)
    cat > "${_env_parser}" << 'PYEOF'
import yaml, sys, shlex
with open(sys.argv[1]) as f:
    d = yaml.safe_load(f)

spack = d.get('spack', {})
print("SPACK_ENV_NAME='{}'".format(spack.get('env_name', 'cp2k-env')))

mb = d.get('mirror_builder', {})
pkgs = mb.get('system_pkgs', [])
print("SYSTEM_PKGS='{}'".format(' '.join(pkgs)))
pkg_mirror_setup = mb.get('pkg_mirror_setup', '')
pkg_cmd          = mb.get('pkg_install_cmd', '')
print('PKG_MIRROR_SETUP={}'.format(shlex.quote(pkg_mirror_setup)))
print('PKG_INSTALL_CMD={}'.format(shlex.quote(pkg_cmd)))

repos = spack.get('custom_repos', [])
for i, r in enumerate(repos):
    ns = r.get('namespace', '')
    if 'path' in r:
        print("CUSTOM_REPO_{}='local|{}|{}'".format(i, r['path'], ns))
    else:
        url = r.get('url', '')
        branch = r.get('branch', '')
        sparse = r.get('sparse_path', '')
        print("CUSTOM_REPO_{}='git|{}|{}|{}|{}'".format(i, url, branch, sparse, ns))
print('CUSTOM_REPO_COUNT={}'.format(len(repos)))
PYEOF

    eval "$(python3 "${_env_parser}" "${env_yaml}")"
    rm -f "${_env_parser}"

    # Reconstruct CUSTOM_REPOS array from indexed variables
    CUSTOM_REPOS=()
    for ((i=0; i<${CUSTOM_REPO_COUNT:-0}; i++)); do
        CUSTOM_REPOS+=("$(eval echo "\${CUSTOM_REPO_${i}}")")
    done
}

# ============================================================================
# step_install_system_pkgs — Configure mirrors + install system packages
# ============================================================================
step_install_system_pkgs() {
    if [[ -n "${PKG_MIRROR_SETUP:-}" ]]; then
        _sc_info "Configuring package mirrors..."
        sudo bash -c "${PKG_MIRROR_SETUP}"
    fi

    if [[ -n "${SYSTEM_PKGS:-}" && -n "${PKG_INSTALL_CMD:-}" ]]; then
        _sc_info "Installing system packages..."
        sudo bash -c "${PKG_INSTALL_CMD} ${SYSTEM_PKGS}"
        _sc_ok "System packages installed"
    else
        _sc_info "No system packages declared — skipping"
    fi
}

# ============================================================================
# step_clean_stale_repos — Remove stale site-level repo registrations
# ============================================================================
step_clean_stale_repos() {
    . /opt/spack/share/spack/setup-env.sh 2>/dev/null || true

    local stale_repos
    stale_repos=$(spack repo list 2>/dev/null | grep -v 'builtin' | awk '{print $2}' || true)
    if [[ -z "${stale_repos}" ]]; then
        return 0
    fi

    _sc_info "Cleaning stale site-level repo registrations..."
    while IFS= read -r repo_name; do
        [[ -z "${repo_name}" ]] && continue
        _sc_info "  Removing: ${repo_name}"
        spack repo remove "${repo_name}" 2>/dev/null || true
    done <<< "${stale_repos}"
}

# ============================================================================
# step_register_repos — Register custom Spack repos from env.yaml
# ============================================================================
step_register_repos() {
    _sc_info "Custom repos declared: ${#CUSTOM_REPOS[@]}"

    if [[ ${#CUSTOM_REPOS[@]} -eq 0 ]]; then
        _sc_info "No custom repos configured — skipping"
        return 0
    fi

    for repo_entry in "${CUSTOM_REPOS[@]}"; do
        IFS='|' read -r repo_type rest <<< "${repo_entry}"

        local namespace
        case "${repo_type}" in
            git)
                IFS='|' read -r git_url branch sparse_path namespace <<< "${rest}"
                ;;
            local)
                IFS='|' read -r repo_rel_path namespace <<< "${rest}"
                ;;
            *)
                _sc_warn "Unknown repo type: ${repo_type} — skipping"
                continue
                ;;
        esac

        if spack repo list 2>/dev/null | grep -q "${namespace}"; then
            _sc_info "Repo ${namespace} already registered — skipping"
            continue
        fi

        local repo_dir
        case "${repo_type}" in
            git)
                repo_dir="/tmp/spack-repos/spack_repo/${namespace}"
                if [[ -d "${repo_dir}" ]]; then
                    _sc_info "Using cached git repo: ${repo_dir}"
                    spack repo add "${repo_dir}"
                    _sc_ok "Registered ${namespace} (cached)"
                    continue
                fi
                rm -rf "${repo_dir}"
                _sc_info "Cloning ${namespace} (${branch})..."
                mkdir -p /tmp/spack-repos/spack_repo
                local clone_dir="/tmp/repo-clone-${namespace}"
                rm -rf "${clone_dir}"
                git clone --depth 1 --filter=blob:none --sparse \
                    --branch "${branch}" "${git_url}" "${clone_dir}"
                cd "${clone_dir}"
                git sparse-checkout set "${sparse_path}"
                cp -a "${sparse_path}" "/tmp/spack-repos/spack_repo/"
                cd /tmp
                rm -rf "${clone_dir}"
                _sc_ok "Cloned to ${repo_dir}"
                ;;
            local)
                repo_dir="${ENV_DIR}/${repo_rel_path}"
                if [[ ! -d "${repo_dir}" ]]; then
                    _sc_error "Local repo not found: ${repo_dir}"
                    continue
                fi
                _sc_info "Using local repo: ${repo_dir} (namespace=${namespace})"
                ;;
        esac

        spack repo add "${repo_dir}"
        _sc_ok "Registered ${namespace}"
    done
}

# ============================================================================
# step_find — Find compilers only (NO external find to avoid pollution)
# ============================================================================
step_find() {
    _sc_info "Finding system compilers..."
    spack compiler find
    _sc_ok "Compilers registered"
    # NOTE: No spack external find — mirror builder only downloads source
    # tarballs. External discovery is done by the build container (Dockerfile.j2).
}

# ============================================================================
# step_concretize — Generate spack.lock from spack.yaml
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

# ============================================================================
# streamline_dispatch — Main mode router (called by streamline.sh)
# ============================================================================
streamline_dispatch() {
    local mode="${1:?Usage: streamline_dispatch <concretize|mirror|all|verify>}"

    case "${mode}" in
        concretize)
            echo "============================================================"
            echo " MODE: concretize"
            echo " Env:  ${ENV_NAME}"
            echo "============================================================"
            step_install_system_pkgs
            step_clean_stale_repos
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
            step_clean_stale_repos
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
            step_clean_stale_repos
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
            step_clean_stale_repos
            spack_bootstrap
            step_register_repos
            mirror_verify "${ENV_DIR}" "${MIRROR_DIR}"
            echo ""
            _sc_ok "Verification complete"
            ;;
        *)
            _sc_error "Unknown mode: ${mode}"
            echo "Usage: streamline.sh <concretize|mirror|all|verify>" >&2
            exit 1
            ;;
    esac
}

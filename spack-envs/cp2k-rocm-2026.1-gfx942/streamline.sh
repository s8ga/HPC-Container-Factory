#!/bin/bash
# ============================================================================
# streamline.sh — Generic per-env pipeline
#
# Runs INSIDE the mirror-builder container.
# /work is bind-mounted to the project root.
#
# All configuration is read from spack-envs/<env>/env.yaml.
# To adapt for a new environment, modify env.yaml — not this file.
# ============================================================================
set -euo pipefail

ENV_NAME="${ENV_NAME:?ENV_NAME not set}"
MIRROR_DIR="${MIRROR_DIR:-/work/assets/spack-mirror}"
ENV_DIR="/work/spack-envs/${ENV_NAME}"
MODE="${1:?Usage: streamline.sh <concretize|mirror|all|verify>}"

# ── Read configuration from env.yaml ──────────────────────────────────────
ENV_YAML="${ENV_DIR}/env.yaml"
if [[ ! -f "${ENV_YAML}" ]]; then
    echo "[ERROR] env.yaml not found: ${ENV_YAML}" >&2
    exit 1
fi

# Single Python call: parse env.yaml → export all needed bash variables
# Write parser to temp file to avoid bash/python quoting conflicts
_env_parser=$(mktemp /tmp/env_parser_XXXXXX.py)
cat > "${_env_parser}" << 'PYEOF'
import yaml, sys, shlex
with open(sys.argv[1]) as f:
    d = yaml.safe_load(f)

# spack section
spack = d.get('spack', {})
print("SPACK_ENV_NAME='{}'".format(spack.get('env_name', 'cp2k-env')))

# mirror_builder section
mb = d.get('mirror_builder', {})
pkgs = mb.get('system_pkgs', [])
print("SYSTEM_PKGS='{}'".format(' '.join(pkgs)))
pkg_mirror_setup = mb.get('pkg_mirror_setup', '')
pkg_cmd          = mb.get('pkg_install_cmd', '')
print('PKG_MIRROR_SETUP={}'.format(shlex.quote(pkg_mirror_setup)))
print('PKG_INSTALL_CMD={}'.format(shlex.quote(pkg_cmd)))

# custom repos — type-prefixed encoding:
#   git|URL|BRANCH|SPARSE_PATH|NAMESPACE
#   local|PATH|NAMESPACE
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

eval "$(python3 "${_env_parser}" "${ENV_YAML}")"
rm -f "${_env_parser}"

# Reconstruct CUSTOM_REPOS array from indexed variables
CUSTOM_REPOS=()
for ((i=0; i<${CUSTOM_REPO_COUNT:-0}; i++)); do
    CUSTOM_REPOS+=("$(eval echo "\${CUSTOM_REPO_${i}}")")
done

# ── Source common utilities ────────────────────────────────────────────────
source /work/scripts/spack-common.sh

# ============================================================================
# Step: Configure mirrors + Install system packages (runtime)
# ============================================================================
step_install_system_pkgs() {
    # Configure package mirrors — just eval the oneliner from env.yaml
    if [[ -n "${PKG_MIRROR_SETUP:-}" ]]; then
        _sc_info "Configuring package mirrors..."
        sudo bash -c "${PKG_MIRROR_SETUP}"
    fi

    # Install system packages — just eval the command + pkg list from env.yaml
    if [[ -n "${SYSTEM_PKGS:-}" && -n "${PKG_INSTALL_CMD:-}" ]]; then
        _sc_info "Installing system packages..."
        sudo bash -c "${PKG_INSTALL_CMD} ${SYSTEM_PKGS}"
        _sc_ok "System packages installed"
    else
        _sc_info "No system packages declared — skipping"
    fi
}

# ============================================================================
# Step: Clean stale site-level repo registrations from previous runs
# ============================================================================
# Persistent containers retain site-scope repo registrations across runs.
# If /tmp files are lost (e.g. container restart), these stale registrations
# cause "Error constructing repository" warnings on every spack command.
step_clean_stale_repos() {
    . /opt/spack/share/spack/setup-env.sh 2>/dev/null || true

    local stale_repos
    stale_repos=$(spack repo list 2>/dev/null | grep -v "^builtin" | awk '{print $1}' || true)
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
# Step: Register custom Spack repos
# ============================================================================
# Iterates over CUSTOM_REPOS array. Each entry is type-prefixed:
#   "git|URL|BRANCH|SPARSE_PATH|NAMESPACE"  → sparse clone → register
#   "local|PATH|NAMESPACE"                  → direct register (path relative to env dir)
# Repos registered in order; later repos have higher priority (override builtin).
step_register_repos() {
    if [[ ${#CUSTOM_REPOS[@]} -eq 0 ]]; then
        _sc_info "No custom repos configured — skipping"
        return 0
    fi

    for repo_entry in "${CUSTOM_REPOS[@]}"; do
        IFS='|' read -r repo_type rest <<< "${repo_entry}"

        # Remove stale registration if present
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
            _sc_info "Removing stale ${namespace} registration..."
            spack repo remove "${namespace}" 2>/dev/null || true
        fi

        local repo_dir
        case "${repo_type}" in
            git)
                repo_dir="/tmp/spack-repos/spack_repo/${namespace}"
                # Always remove stale/incomplete dir before cloning
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

        # Register with Spack
        spack repo add "${repo_dir}"
        _sc_ok "Registered ${namespace} with Spack (priority: $(spack repo list 2>/dev/null | grep -n "${namespace}" | head -1 | cut -d: -f1))"
    done

    _sc_info "Full repo list:"
    spack repo list
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
        _sc_error "Unknown mode: ${MODE}"
        echo "Usage: streamline.sh <concretize|mirror|all|verify>" >&2
        exit 1
        ;;
esac

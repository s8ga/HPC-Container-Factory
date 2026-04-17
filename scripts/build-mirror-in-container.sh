#!/usr/bin/env bash
# ============================================================================
# build-mirror-in-container.sh
# Containerized Spack mirror generation for HPC-Container-Factory
#
# Avoids host environment pollution by running all Spack commands inside an
# isolated Podman container. Produces bootstrap cache and source mirror
# artifacts that can be packaged and distributed for offline installs.
#
# Usage:
#   ./scripts/build-mirror-in-container.sh -e <env-name> <command> [options]
#   ./scripts/build-mirror-in-container.sh --env <env-name> <command>
#   ./scripts/build-mirror-in-container.sh create-container --container-name <name>
#
# Commands:
#   image      Build the hpc-mirror-builder container image
#   create-container  Create (or start) a reusable mirror builder container
#   bootstrap  Generate bootstrap mirror on host (clingo, gnupg, patchelf)
#   concretize Re-concretize spack environment (requires streamline.sh in env dir)
#   mirror     Generate source mirror in container (requires concretize)
#   verify     Verify mirror completeness (re-run mirror create, expect 0 failed)
#   all        Run mirror → verify in sequence (bootstrap must be done separately)
#   status     Show current artifact status
#
# Options:
#   -e, --env <name>  Spack environment name under spack-envs/ (required for mirror/verify/all/status)
#   -n, --container-name <name>  Reusable mirror worker container name
#   --podman-opt <opt>  Extra podman run/create option (repeatable)
#
# NOTE:
#   Proxy access requires host networking. If non-host network is requested via
#   --podman-opt (e.g. --network=bridge), this script will warn.
#
# Environment variables:
#   MIRROR_BUILDER_IMAGE  Container image name (default: hpc-mirror-builder)
#   MIRROR_CONTAINER_NAME Reusable container name (default: hpc-mirror-builder-work)
#   PODMAN_CMD            Podman executable (default: podman)
#   ENV_NAME              Spack environment name (fallback if -e not given)
#   MIRROR_DIR            Override default mirror output path (default: assets/spack-mirror)
#   EXTRA_PODMAN_OPTS     Additional podman run options
#
# Hook mechanism:
#   If spack-envs/<name>/mirror-create.sh exists, it is executed inside the
#   container before concretize / mirror-create. Use it to register custom
#   package repos (e.g. cp2k_dev_repo) or perform other per-env setup.
# ============================================================================

set -euo pipefail

# ── Project paths ──────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ── Configurable defaults ──────────────────────────────────────────────────
MIRROR_BUILDER_IMAGE="${MIRROR_BUILDER_IMAGE:-hpc-mirror-builder}"
MIRROR_CONTAINER_NAME="${MIRROR_CONTAINER_NAME:-hpc-mirror-builder-work}"
PODMAN_CMD="${PODMAN_CMD:-podman}"
ENV_NAME="${ENV_NAME:-}"           # Required — set via -e or ENV_NAME
MIRROR_DIR_OVERRIDE="${MIRROR_DIR:-}"  # Optional override for mirror output path
EXTRA_PODMAN_OPTS="${EXTRA_PODMAN_OPTS:-}"

# ── CLI argument parsing ───────────────────────────────────────────────────
# We parse -e/--env before the subcommand so that env-aware defaults work.
CLI_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -e|--env)
            ENV_NAME="$2"
            shift 2
            ;;
        -n|--container-name)
            MIRROR_CONTAINER_NAME="$2"
            shift 2
            ;;
        --podman-opt)
            EXTRA_PODMAN_OPTS="${EXTRA_PODMAN_OPTS} $2"
            shift 2
            ;;
        *)
            CLI_ARGS+=("$1")
            shift
            ;;
    esac
done
set -- "${CLI_ARGS[@]}"

# ── Derived paths ──────────────────────────────────────────────────────────
DOCKERFILE="${PROJECT_ROOT}/containers/Dockerfile.mirror-builder"
BOOTSTRAP_DIR="${PROJECT_ROOT}/assets/bootstrap"

# ── Colors ─────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'  # No Color

# ── Logging helpers ────────────────────────────────────────────────────────
info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# ── Resolve env-dependent paths ───────────────────────────────────────────
# Must be called after ENV_NAME is known.
resolve_env_paths() {
    if [[ -z "${ENV_NAME}" ]]; then
        error "No environment specified. Use -e <name> or set ENV_NAME."
        echo "" >&2
        usage >&2
        exit 1
    fi
    SPACK_ENV_DIR="${PROJECT_ROOT}/spack-envs/${ENV_NAME}"
    # New layout: spack.yaml lives in spack-env-file/ subdirectory
    if [[ -d "${SPACK_ENV_DIR}/spack-env-file" ]]; then
        SPACK_ENV_DIR="${SPACK_ENV_DIR}/spack-env-file"
    fi
    # Container path mirrors host layout under /work bind-mount
    CONTAINER_ENV_DIR="/work/spack-envs/${ENV_NAME}"
    if [[ -d "${PROJECT_ROOT}/spack-envs/${ENV_NAME}/spack-env-file" ]]; then
        CONTAINER_ENV_DIR="${CONTAINER_ENV_DIR}/spack-env-file"
    fi
    if [[ -n "${MIRROR_DIR_OVERRIDE}" ]]; then
        MIRROR_DIR="${MIRROR_DIR_OVERRIDE}"
    else
        MIRROR_DIR="${PROJECT_ROOT}/assets/spack-mirror"
    fi
    HOOK_SCRIPT="${SPACK_ENV_DIR}/mirror-create.sh"
}

# ── Podman run helper ─────────────────────────────────────────────────────
# Common podman run invocation with all the right flags for rootless operation.
run_in_container() {
    local cmd="$1"
    warn_proxy_requirement_if_needed
    ${PODMAN_CMD} run --rm \
        ${EXTRA_PODMAN_OPTS} \
        --network=host \
        --userns=keep-id \
        -e HOME=/tmp/home \
        -v "${PROJECT_ROOT}:/work:Z" \
        "${MIRROR_BUILDER_IMAGE}" \
        bash -c "mkdir -p /tmp/home && ${cmd}"
}

container_network_mode() {
    local container_name="$1"
    ${PODMAN_CMD} inspect -f '{{.HostConfig.NetworkMode}}' "${container_name}" 2>/dev/null || echo "unknown"
}

extra_network_mode() {
    local -a opts
    local i=0
    read -r -a opts <<< "${EXTRA_PODMAN_OPTS}"

    while [[ ${i} -lt ${#opts[@]} ]]; do
        case "${opts[$i]}" in
            --network=*)
                echo "${opts[$i]#*=}"
                return 0
                ;;
            --net=*)
                echo "${opts[$i]#*=}"
                return 0
                ;;
            --network|--net)
                if [[ $((i + 1)) -lt ${#opts[@]} ]]; then
                    echo "${opts[$((i + 1))]}"
                    return 0
                fi
                ;;
        esac
        ((i += 1))
    done

    return 1
}

warn_proxy_requirement_if_needed() {
    local mode
    mode="$(extra_network_mode || true)"
    if [[ -n "${mode}" && "${mode}" != "host" ]]; then
        warn "Non-host network option detected ('${mode}'); proxy access may fail."
        warn "Use --network=host when network proxy is required."
    fi
}

# ============================================================================
# Commands
# ============================================================================

cmd_image() {
    info "Building mirror-builder image: ${MIRROR_BUILDER_IMAGE}"
    info "Dockerfile: ${DOCKERFILE}"
    info "Context: ${PROJECT_ROOT}"
    echo ""

    if [[ ! -f "${DOCKERFILE}" ]]; then
        error "Dockerfile not found: ${DOCKERFILE}"
        return 1
    fi

    ${PODMAN_CMD} build \
        --network=host \
        -t "${MIRROR_BUILDER_IMAGE}" \
        -f "${DOCKERFILE}" \
        "${PROJECT_ROOT}"

    ok "Image built: ${MIRROR_BUILDER_IMAGE}"
    echo ""
    ${PODMAN_CMD} images "${MIRROR_BUILDER_IMAGE}" --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}\t{{.CreatedAt}}"
}

cmd_create_container() {
    info "Ensuring reusable mirror container exists..."
    info "Container: ${MIRROR_CONTAINER_NAME}"
    info "Image: ${MIRROR_BUILDER_IMAGE}"
    warn_proxy_requirement_if_needed
    if [[ -n "${EXTRA_PODMAN_OPTS// }" ]]; then
        info "Extra podman opts:${EXTRA_PODMAN_OPTS}"
    fi
    echo ""

    if ! ${PODMAN_CMD} image exists "${MIRROR_BUILDER_IMAGE}" 2>/dev/null; then
        warn "Mirror builder image not found, building first"
        cmd_image
    fi

    if ${PODMAN_CMD} container exists "${MIRROR_CONTAINER_NAME}" 2>/dev/null; then
        local net_mode
        net_mode="$(container_network_mode "${MIRROR_CONTAINER_NAME}")"
        if [[ "${net_mode}" != "host" ]]; then
            warn "Existing container network mode is '${net_mode}', recreating with --network=host"
            warn "Non-host mode cannot reliably use host proxy settings."
            ${PODMAN_CMD} rm -f "${MIRROR_CONTAINER_NAME}" >/dev/null
        else
        local running
        running=$(${PODMAN_CMD} inspect -f '{{.State.Running}}' "${MIRROR_CONTAINER_NAME}" 2>/dev/null || echo "false")
        if [[ "${running}" == "true" ]]; then
            ok "Container already running: ${MIRROR_CONTAINER_NAME}"
            ${PODMAN_CMD} ps --filter "name=${MIRROR_CONTAINER_NAME}" --format "table {{.Names}}\t{{.Status}}\t{{.Image}}"
            return 0
        fi

        info "Starting existing container: ${MIRROR_CONTAINER_NAME}"
        ${PODMAN_CMD} start "${MIRROR_CONTAINER_NAME}" >/dev/null
        ok "Container started"
        ${PODMAN_CMD} ps --filter "name=${MIRROR_CONTAINER_NAME}" --format "table {{.Names}}\t{{.Status}}\t{{.Image}}"
        return 0
        fi
    fi

    info "Creating new container: ${MIRROR_CONTAINER_NAME}"
    ${PODMAN_CMD} create \
        --name "${MIRROR_CONTAINER_NAME}" \
        ${EXTRA_PODMAN_OPTS} \
        --network=host \
        --userns=keep-id \
        -e HOME=/tmp/home \
        -v "${PROJECT_ROOT}:/work:Z" \
        "${MIRROR_BUILDER_IMAGE}" \
        bash -lc 'mkdir -p /tmp/home && tail -f /dev/null' >/dev/null

    ${PODMAN_CMD} start "${MIRROR_CONTAINER_NAME}" >/dev/null
    ok "Container created and started"
    ${PODMAN_CMD} ps --filter "name=${MIRROR_CONTAINER_NAME}" --format "table {{.Names}}\t{{.Status}}\t{{.Image}}"
    echo ""
    info "Use this for debug: ${PODMAN_CMD} exec -it ${MIRROR_CONTAINER_NAME} bash"
}

cmd_bootstrap() {
    info "Generating bootstrap mirror on host..."
    info "Target: ${BOOTSTRAP_DIR}"
    echo ""

    # Bootstrap cache is pure file downloads (clingo, gnupg, patchelf, etc.)
    # No container needed — just source tarballs and binary packages.
    # Uses the Spack bundled in assets/spack-src/ on the host.

    # Skip if already populated
    if [[ -f "${BOOTSTRAP_DIR}/metadata/sources/metadata.yaml" ]] && \
       [[ -d "${BOOTSTRAP_DIR}/bootstrap_cache" ]] && \
       [[ -n "$(ls -A "${BOOTSTRAP_DIR}/bootstrap_cache" 2>/dev/null)" ]]; then
        local size
        size=$(du -sh "${BOOTSTRAP_DIR}" 2>/dev/null | cut -f1 || echo "?")
        ok "Bootstrap already populated (${size}) — skipping"
        info "  To regenerate, delete ${BOOTSTRAP_DIR} and re-run"
        return 0
    fi

    # Ensure output directory exists
    mkdir -p "${BOOTSTRAP_DIR}"

    # Source spack from the bundled source tree
    local spack_setup="${PROJECT_ROOT}/assets/spack-src/share/spack/setup-env.sh"
    if [[ ! -f "${spack_setup}" ]]; then
        error "Spack setup script not found: ${spack_setup}"
        error "Ensure assets/spack-src/ contains the Spack source tree"
        return 1
    fi

    (
        set -e
        # shellcheck disable=SC1090
        . "${spack_setup}"

        echo "=== Spack version: $(spack --version) ==="
        echo ""
        echo ">>> Running: spack bootstrap mirror --binary-packages ${BOOTSTRAP_DIR}"
        spack bootstrap mirror --binary-packages "${BOOTSTRAP_DIR}"
    )

    echo ""
    ok "Bootstrap mirror generated"
    local size
    size=$(du -sh "${BOOTSTRAP_DIR}" 2>/dev/null | cut -f1 || echo "unknown")
    info "Size: ${size}"
}

cmd_mirror() {
    resolve_env_paths
    info "Generating source mirror in container..."
    info "Environment: ${ENV_NAME}"
    info "Target: ${MIRROR_DIR}"
    echo ""

    if [[ ! -f "${SPACK_ENV_DIR}/spack.yaml" ]]; then
        error "spack.yaml not found: ${SPACK_ENV_DIR}/spack.yaml"
        return 1
    fi

    if [[ ! -f "${SPACK_ENV_DIR}/streamline.sh" ]]; then
        error "streamline.sh not found for this environment"
        error "Create spack-envs/${ENV_NAME}/streamline.sh to enable mirror"
        return 1
    fi

    mkdir -p "${MIRROR_DIR}"
    local mode="mirror"
    if [[ ! -f "${SPACK_ENV_DIR}/spack.lock" ]]; then
        warn "spack.lock NOT found — switching to 'all' mode (concretize + mirror)"
        mode="all"
    fi

    run_in_container "ENV_NAME=${ENV_NAME} MIRROR_DIR=/work/assets/spack-mirror bash ${CONTAINER_ENV_DIR}/streamline.sh ${mode}"

    echo ""
    ok "Source mirror generated"

    local size
    size=$(du -sh "${MIRROR_DIR}" 2>/dev/null | cut -f1 || echo "unknown")
    info "Mirror size: ${size}"

    local lock_status
    if [[ -f "${SPACK_ENV_DIR}/spack.lock" ]]; then
        lock_status="present"
    else
        lock_status="missing"
    fi
    info "spack.lock: ${lock_status}"

    local file_count
    file_count=$(find "${MIRROR_DIR}" -type f 2>/dev/null | wc -l || echo "0")
    info "Files in mirror: ${file_count}"
}

cmd_concretize() {
    resolve_env_paths
    info "Concretizing Spack environment..."
    info "Environment: ${ENV_NAME}"
    echo ""

    if [[ ! -f "${SPACK_ENV_DIR}/spack.yaml" ]]; then
        error "spack.yaml not found: ${SPACK_ENV_DIR}/spack.yaml"
        return 1
    fi

    if [[ ! -f "${SPACK_ENV_DIR}/streamline.sh" ]]; then
        error "streamline.sh not found for this environment"
        error "Create spack-envs/${ENV_NAME}/streamline.sh to enable concretize"
        return 1
    fi

    run_in_container "ENV_NAME=${ENV_NAME} MIRROR_DIR=/work/assets/spack-mirror bash ${CONTAINER_ENV_DIR}/streamline.sh concretize"

    echo ""
    ok "Concretize complete"

    if [[ -f "${SPACK_ENV_DIR}/spack.lock" ]]; then
        info "spack.lock: ${SPACK_ENV_DIR}/spack.lock ✓"
    else
        warn "spack.lock was not written — check output above"
    fi
}

cmd_verify() {
    resolve_env_paths
    info "Verifying mirror completeness..."
    echo ""

    if [[ ! -f "${SPACK_ENV_DIR}/spack.lock" ]]; then
        error "spack.lock not found — run 'concretize' or 'mirror' command first"
        return 1
    fi

    if [[ -z "$(ls -A "${MIRROR_DIR}" 2>/dev/null)" ]]; then
        error "Mirror directory is empty — run 'mirror' command first"
        return 1
    fi

    if [[ ! -f "${SPACK_ENV_DIR}/streamline.sh" ]]; then
        error "streamline.sh not found for this environment"
        return 1
    fi

    run_in_container "ENV_NAME=${ENV_NAME} MIRROR_DIR=/work/assets/spack-mirror bash ${CONTAINER_ENV_DIR}/streamline.sh verify"
    echo ""

    # Layer 2: Structure verification on host
    info "Layer 2: Structure verification (host side)"
    echo ""

    local layer2_ok=true

    # Check for broken symlinks
    local broken_links
    broken_links=$(find -L "${MIRROR_DIR}" -type l 2>/dev/null | head -5 || true)
    if [[ -n "${broken_links}" ]]; then
        error "Broken symlinks found in mirror:"
        echo "${broken_links}"
        layer2_ok=false
    else
        ok "No broken symlinks in mirror"
    fi

    # Check bootstrap metadata
    local metadata_file="${BOOTSTRAP_DIR}/metadata/sources/metadata.yaml"
    if [[ -f "${metadata_file}" ]] && [[ -s "${metadata_file}" ]]; then
        ok "Bootstrap metadata exists and is non-empty"
    else
        warn "Bootstrap metadata missing or empty: ${metadata_file}"
        layer2_ok=false
    fi

    # Check bootstrap binaries
    local binaries_ok=true
    for bin_json in clingo gnupg patchelf; do
        local json_path="${BOOTSTRAP_DIR}/metadata/binaries/${bin_json}.json"
        if [[ -f "${json_path}" ]] && [[ -s "${json_path}" ]]; then
            :
        else
            warn "Missing binary metadata: ${json_path}"
            binaries_ok=false
        fi
    done
    if [[ "${binaries_ok}" == true ]]; then
        ok "All bootstrap binary metadata present (clingo, gnupg, patchelf)"
    fi

    echo ""
    if [[ "${layer2_ok}" == true ]]; then
        ok "All verification layers passed ✓"
    else
        warn "Some structure checks failed (mirror may still be functional)"
    fi
}

cmd_all() {
    resolve_env_paths
    info "Running full pipeline: mirror → verify"
    info "(Bootstrap is host-side only; run 'bootstrap' separately if needed)"
    echo "============================================================"

    # Pre-flight: check bootstrap exists
    if [[ ! -f "${BOOTSTRAP_DIR}/metadata/sources/metadata.yaml" ]]; then
        warn "Bootstrap mirror not found — run './scripts/build-mirror-in-container.sh bootstrap' first"
    fi

    echo ""
    info "Step 1/2: Source mirror"
    echo "------------------------------------------------------------"
    cmd_mirror

    echo ""
    echo ""
    info "Step 2/2: Verification"
    echo "------------------------------------------------------------"
    cmd_verify

    echo ""
    echo "============================================================"
    ok "Full pipeline completed successfully"
    echo ""

    # Summary
    local bootstrap_size mirror_size
    bootstrap_size=$(du -sh "${BOOTSTRAP_DIR}" 2>/dev/null | cut -f1 || echo "?")
    mirror_size=$(du -sh "${MIRROR_DIR}" 2>/dev/null | cut -f1 || echo "?")
    echo "Artifacts:"
    echo "  assets/bootstrap/     ${bootstrap_size}  (host-generated)"
    echo "  assets/spack-mirror/  ${mirror_size}  (container-generated)"
    if [[ -f "${SPACK_ENV_DIR}/spack.lock" ]]; then
        echo "  spack-envs/${ENV_NAME}/spack.lock  ✓"
    fi
    echo ""
    echo "Ready for: podman build -f <Dockerfile-template> ."
    echo ""
    info "Hint: use '-e <env-name>' to specify a different environment"
}

cmd_status() {
    resolve_env_paths
    echo "HPC-Container-Factory — Mirror Status"
    echo "======================================="
    echo ""

    # Container image
    echo "📦 Container Image:"
    if ${PODMAN_CMD} image exists "${MIRROR_BUILDER_IMAGE}" 2>/dev/null; then
        ${PODMAN_CMD} images "${MIRROR_BUILDER_IMAGE}" --format "  {{.Repository}}:{{.Tag}}  {{.Size}}  {{.CreatedAt}}"
    else
        echo "  (not built — run './scripts/build-mirror-in-container.sh image')"
    fi
    echo ""

    echo "🧱 Mirror Worker Container:"
    if ${PODMAN_CMD} container exists "${MIRROR_CONTAINER_NAME}" 2>/dev/null; then
        ${PODMAN_CMD} ps -a --filter "name=${MIRROR_CONTAINER_NAME}" --format "  {{.Names}}  {{.Status}}  {{.Image}}"
    else
        echo "  (not created — run './scripts/build-mirror-in-container.sh create-container')"
    fi
    echo ""

    # Bootstrap mirror
    echo "🔧 Bootstrap Mirror:"
    if [[ -d "${BOOTSTRAP_DIR}" ]] && [[ -n "$(ls -A "${BOOTSTRAP_DIR}" 2>/dev/null)" ]]; then
        local bs_size
        bs_size=$(du -sh "${BOOTSTRAP_DIR}" 2>/dev/null | cut -f1 || echo "?")
        echo "  Path: ${BOOTSTRAP_DIR}"
        echo "  Size: ${bs_size}"
        # Check metadata
        if [[ -f "${BOOTSTRAP_DIR}/metadata/sources/metadata.yaml" ]]; then
            echo "  Metadata: ✓"
        else
            echo "  Metadata: ✗ (missing)"
        fi
    else
        echo "  (empty — run './scripts/build-mirror-in-container.sh bootstrap')"
    fi
    echo ""

    # Source mirror
    echo "💿 Source Mirror:"
    if [[ -d "${MIRROR_DIR}" ]] && [[ -n "$(ls -A "${MIRROR_DIR}" 2>/dev/null)" ]]; then
        local m_size m_files
        m_size=$(du -sh "${MIRROR_DIR}" 2>/dev/null | cut -f1 || echo "?")
        m_files=$(find "${MIRROR_DIR}" -type f 2>/dev/null | wc -l)
        echo "  Path: ${MIRROR_DIR}"
        echo "  Size: ${m_size}"
        echo "  Files: ${m_files}"
        # Check for broken symlinks
        local broken
        broken=$(find -L "${MIRROR_DIR}" -type l 2>/dev/null | wc -l)
        if [[ "${broken}" -gt 0 ]]; then
            echo "  Broken symlinks: ${broken} ⚠"
        else
            echo "  Broken symlinks: 0 ✓"
        fi
    else
        echo "  (empty — run './scripts/build-mirror-in-container.sh -e ${ENV_NAME} mirror')"
    fi
    echo ""

    # Spack environment
    echo "📋 Spack Environment:"
    echo "  Name: ${ENV_NAME}"
    echo "  Path: ${SPACK_ENV_DIR}"
    if [[ -f "${SPACK_ENV_DIR}/spack.lock" ]]; then
        echo "  spack.lock: ✓"
    else
        echo "  spack.lock: ✗ (will be generated on first 'mirror' run)"
    fi

    # Hook script
    echo ""
    echo "🪝 Hook Script:"
    if [[ -f "${HOOK_SCRIPT}" ]]; then
        echo "  ${HOOK_SCRIPT}: ✓"
        echo "  Content preview:"
        head -5 "${HOOK_SCRIPT}" | sed 's/^/    /'
    else
        echo "  (none — only builtin Spack repos will be used)"
    fi
    echo ""
}

# ============================================================================
# Main
# ============================================================================

usage() {
    sed -n '2,/^# =====/p' "$0" | grep '^#' | sed 's/^# \?//'
}

case "${1:-}" in
    image)
        cmd_image
        ;;
    create-container)
        cmd_create_container
        ;;
    bootstrap)
        cmd_bootstrap
        ;;
    concretize)
        cmd_concretize
        ;;
    mirror)
        cmd_mirror
        ;;
    verify)
        cmd_verify
        ;;
    all)
        cmd_all
        ;;
    status)
        cmd_status
        ;;
    help|--help|-h)
        usage
        ;;
    *)
        error "Unknown command: ${1:-}"
        echo "" >&2
        usage >&2
        exit 1
        ;;
esac

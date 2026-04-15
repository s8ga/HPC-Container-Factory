#!/usr/bin/env bash
# Prepare Spack bootstrap cache using the mirror builder container.
#
# Default behavior:
#   1) Ensure mirror builder image exists
#   2) Run `spack bootstrap mirror --binary-packages` in container
#   3) Validate generated metadata and print summary
#
# NOTE:
#   Proxy access requires host networking. Non-host network mode may fail.
#
# Examples:
#   ./scripts/prepare-bootstrap-cache.sh
#   ./scripts/prepare-bootstrap-cache.sh --force
#   ./scripts/prepare-bootstrap-cache.sh --create-container --use-container

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
MIRROR_SCRIPT="${SCRIPT_DIR}/build-mirror-in-container.sh"

PODMAN_CMD="${PODMAN_CMD:-podman}"
MIRROR_BUILDER_IMAGE="${MIRROR_BUILDER_IMAGE:-hpc-mirror-builder}"
MIRROR_CONTAINER_NAME="${MIRROR_CONTAINER_NAME:-hpc-mirror-builder-work}"
BOOTSTRAP_DIR="${BOOTSTRAP_DIR:-${PROJECT_ROOT}/assets/bootstrap}"
EXTRA_PODMAN_OPTS="${EXTRA_PODMAN_OPTS:-}"

FORCE=0
SKIP_IMAGE_BUILD=0
CREATE_CONTAINER=0
USE_CONTAINER=0

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

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

usage() {
    cat <<'EOF'
Usage:
  ./scripts/prepare-bootstrap-cache.sh [options]

Options:
  --force                  Remove existing assets/bootstrap before regenerate
  --skip-image-build       Do not build mirror builder image automatically
  --create-container       Ensure reusable mirror worker container is created
  --use-container          Run bootstrap generation via reusable container (podman exec)
  --container-name <name>  Reusable container name (default: hpc-mirror-builder-work)
  --image <name>           Mirror builder image name (default: hpc-mirror-builder)
  --podman <cmd>           Podman executable (default: podman)
  --podman-opt <opt>       Extra podman run/create option (repeatable)
  --bootstrap-dir <path>   Output bootstrap directory (default: assets/bootstrap)
  -h, --help               Show this help

Environment variables:
  PODMAN_CMD, MIRROR_BUILDER_IMAGE, MIRROR_CONTAINER_NAME, BOOTSTRAP_DIR, EXTRA_PODMAN_OPTS
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force)
            FORCE=1
            shift
            ;;
        --skip-image-build)
            SKIP_IMAGE_BUILD=1
            shift
            ;;
        --create-container)
            CREATE_CONTAINER=1
            shift
            ;;
        --use-container)
            USE_CONTAINER=1
            shift
            ;;
        --container-name)
            MIRROR_CONTAINER_NAME="$2"
            shift 2
            ;;
        --image)
            MIRROR_BUILDER_IMAGE="$2"
            shift 2
            ;;
        --podman)
            PODMAN_CMD="$2"
            shift 2
            ;;
        --podman-opt)
            EXTRA_PODMAN_OPTS="${EXTRA_PODMAN_OPTS} $2"
            shift 2
            ;;
        --bootstrap-dir)
            BOOTSTRAP_DIR="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            error "Unknown option: $1"
            echo ""
            usage
            exit 1
            ;;
    esac
done

if [[ ! -x "${MIRROR_SCRIPT}" ]]; then
    error "Mirror helper script missing or not executable: ${MIRROR_SCRIPT}"
    exit 1
fi

warn_proxy_requirement_if_needed

BOOTSTRAP_REAL="$(realpath -m "${BOOTSTRAP_DIR}")"
PROJECT_REAL="$(realpath -m "${PROJECT_ROOT}")"
if [[ "${BOOTSTRAP_REAL}" != "${PROJECT_REAL}"/* ]]; then
    error "--bootstrap-dir must be inside project root: ${PROJECT_ROOT}"
    error "Current: ${BOOTSTRAP_REAL}"
    exit 1
fi
BOOTSTRAP_REL="${BOOTSTRAP_REAL#${PROJECT_REAL}/}"
BOOTSTRAP_IN_CONTAINER="/work/${BOOTSTRAP_REL}"

if [[ ${FORCE} -eq 1 && -d "${BOOTSTRAP_REAL}" ]]; then
    warn "Removing existing bootstrap directory: ${BOOTSTRAP_DIR}"
    rm -rf "${BOOTSTRAP_REAL}"
fi

mkdir -p "${BOOTSTRAP_REAL}"

if [[ ${SKIP_IMAGE_BUILD} -eq 0 ]]; then
    info "Ensuring mirror builder image exists"
    MIRROR_BUILDER_IMAGE="${MIRROR_BUILDER_IMAGE}" PODMAN_CMD="${PODMAN_CMD}" EXTRA_PODMAN_OPTS="${EXTRA_PODMAN_OPTS}" \
        "${MIRROR_SCRIPT}" image
else
    info "Skipping image build (--skip-image-build)"
fi

if [[ ${CREATE_CONTAINER} -eq 1 ]]; then
    info "Ensuring reusable mirror container exists"
    MIRROR_BUILDER_IMAGE="${MIRROR_BUILDER_IMAGE}" \
    MIRROR_CONTAINER_NAME="${MIRROR_CONTAINER_NAME}" \
    EXTRA_PODMAN_OPTS="${EXTRA_PODMAN_OPTS}" \
    PODMAN_CMD="${PODMAN_CMD}" \
        "${MIRROR_SCRIPT}" create-container
fi

info "Generating bootstrap cache"
info "Output: ${BOOTSTRAP_REAL}"

generate_cmd='set -euo pipefail
. /opt/spack/share/spack/setup-env.sh
mkdir -p "'"${BOOTSTRAP_IN_CONTAINER}"'"
spack bootstrap mirror --binary-packages "'"${BOOTSTRAP_IN_CONTAINER}"'"'

if [[ ${USE_CONTAINER} -eq 1 ]]; then
    if ! ${PODMAN_CMD} container exists "${MIRROR_CONTAINER_NAME}" 2>/dev/null; then
        if [[ ${CREATE_CONTAINER} -eq 1 ]]; then
            :
        else
            error "Container ${MIRROR_CONTAINER_NAME} does not exist. Use --create-container first."
            exit 1
        fi
    fi

    net_mode="$(${PODMAN_CMD} inspect -f '{{.HostConfig.NetworkMode}}' "${MIRROR_CONTAINER_NAME}" 2>/dev/null || echo unknown)"
    if [[ "${net_mode}" != "host" ]]; then
        error "Container ${MIRROR_CONTAINER_NAME} is using network '${net_mode}', expected 'host'."
        error "Non-host mode cannot reliably use host proxy settings."
        error "Recreate it with: ./scripts/prepare-bootstrap-cache.sh --create-container --use-container"
        exit 1
    fi

    if [[ "$(${PODMAN_CMD} inspect -f '{{.State.Running}}' "${MIRROR_CONTAINER_NAME}" 2>/dev/null || echo false)" != "true" ]]; then
        info "Starting container: ${MIRROR_CONTAINER_NAME}"
        ${PODMAN_CMD} start "${MIRROR_CONTAINER_NAME}" >/dev/null
    fi

    ${PODMAN_CMD} exec "${MIRROR_CONTAINER_NAME}" bash -lc "${generate_cmd}"
else
    ${PODMAN_CMD} run --rm \
    ${EXTRA_PODMAN_OPTS} \
        --network=host \
        --userns=keep-id \
        -v "${PROJECT_ROOT}:/work:Z" \
        "${MIRROR_BUILDER_IMAGE}" \
        bash -lc "${generate_cmd}"
fi

metadata_file="${BOOTSTRAP_REAL}/metadata/sources/metadata.yaml"
if [[ ! -f "${metadata_file}" || ! -s "${metadata_file}" ]]; then
    error "Bootstrap metadata missing or empty: ${metadata_file}"
    exit 1
fi

missing=0
for f in \
    "${BOOTSTRAP_REAL}/metadata/binaries/clingo.json" \
    "${BOOTSTRAP_REAL}/metadata/binaries/gnupg.json" \
    "${BOOTSTRAP_REAL}/metadata/binaries/patchelf.json"; do
    if [[ ! -s "${f}" ]]; then
        warn "Missing metadata: ${f}"
        missing=1
    fi
done

files_count=$(find "${BOOTSTRAP_REAL}" -type f | wc -l)
size=$(du -sh "${BOOTSTRAP_REAL}" | cut -f1)

ok "Bootstrap cache prepared"
info "Files: ${files_count}"
info "Size: ${size}"
if [[ ${missing} -eq 1 ]]; then
    warn "Some optional binary metadata are missing"
else
    ok "Core binary metadata present (clingo, gnupg, patchelf)"
fi

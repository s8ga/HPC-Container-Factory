#!/usr/bin/env bash
# Shared job preamble for pipeline-cp2k.yml (prepare/binaries/package).
#
# The jobs share ONE script as their first step with `id: vars`.
#
# Inputs (environment; never interpolated into the script body):
#   BASE_ENV_NAME   dispatch env (inputs.env_name with its default applied)
#   CHANNEL         inputs.channel (nightly|stable)
#   FORK_URL        inputs.fork_url ('' for upstream)
#   FORK_BRANCH     inputs.fork_branch
#   NIGHTLY_DATE_IN optional externally-resolved date (default: today UTC)
#
# Emits to GITHUB_ENV:
#   ENV_NAME SPACK_VER NIGHTLY_DATE OWNER OCI_MIRROR
#   IMAGE_NAME ENV_SUFFIX IMG_REPO TAG_PREFIX
# Emits to GITHUB_OUTPUT:
#   date
set -euo pipefail

# Disk telemetry the dropped per-job blocks used to print (best-effort).
df -h / || true

: "${BASE_ENV_NAME:?BASE_ENV_NAME must be provided by the calling step}"
: "${CHANNEL:?CHANNEL must be provided by the calling step}"
FORK_URL="${FORK_URL:-}"
FORK_BRANCH="${FORK_BRANCH:-}"

# ── fork identity + source override ────────────────────────────────────────
if [[ "$CHANNEL" == "stable" && ( -n "$FORK_URL" || -n "$FORK_BRANCH" ) ]]; then
    echo "::error::fork inputs are nightly-only (stable consumes the committed lock)" >&2
    exit 1
fi
if [[ -n "$FORK_URL" || -n "$FORK_BRANCH" ]]; then
    # Fork identity is derived from the source itself:
    # <base>-fork-<owner>-<branch> — one slot per person+branch.
    BASE_ENV="${BASE_ENV_NAME%-fork}"
    U="${FORK_URL}"
    U="${U#*://}"; U="${U%.git}"
    if [[ "$U" == github.com/* ]]; then
        FORK_ID="${U#github.com/}"; FORK_ID="${FORK_ID%%/*}"
    else
        FORK_ID="${U%%/*}"
    fi
    FORK_ID=$(echo "$FORK_ID" | tr '[:upper:]' '[:lower:]')
    BRANCH_SLUG=$(echo "${FORK_BRANCH:-default}" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9._-]+/-/g; s/^[-.]+//; s/[-.]+$//')
    ENV_NAME="${BASE_ENV}-fork-${FORK_ID}-${BRANCH_SLUG}"
    scripts/apply-source-override.sh "$ENV_NAME" "$FORK_URL" "$FORK_BRANCH"
else
    ENV_NAME="$BASE_ENV_NAME"
    scripts/apply-source-override.sh "$ENV_NAME"
fi

# ── python deps (venv reused by the vars derivation below) ─────────────────
python3 -m venv venv
./venv/bin/pip install --quiet -r requirements.txt

# ── job variables (superset; extra vars are harmless) ──────────────────────
ENV_FILE="spack-envs/${ENV_NAME}/spack-env-file/env.yaml"
test -f "$ENV_FILE" || { echo "::error::unknown env: ${ENV_NAME}"; exit 1; }
export ENV_FILE
SPACK_VER="$(./venv/bin/python - <<'PY'
import os, yaml
spec = yaml.safe_load(open(os.environ["ENV_FILE"]))
print(spec["spack"]["version"])
PY
)"
# GHCR rejects image references containing uppercase characters.
OWNER="${GITHUB_REPOSITORY_OWNER,,}"
NIGHTLY_DATE="${NIGHTLY_DATE_IN:-$(date -u +%Y%m%d)}"

# Base image self-mirror: prefer immutable digest-keyed GHCR mirror tag so
# upstream registry GC cannot break old locks; fall back to upstream and
# populate GHCR on miss when PUSH_BASE_MIRROR=true.
UPSTREAM_BASE="$(./venv/bin/python - <<'PY'
import os, yaml
spec = yaml.safe_load(open(os.environ["ENV_FILE"]))
print((spec.get("images") or {}).get("builder", ""))
PY
)"
if [[ "${UPSTREAM_BASE}" == *"@sha256:"* ]]; then
    DIGEST="${UPSTREAM_BASE#*@sha256:}"
    PRE="${UPSTREAM_BASE%%@sha256:*}"
    LAST="${PRE##*/}"
    [[ "${LAST}" == *:* ]] && REL="${LAST#*:}-" || REL=""
    MIRROR_BASE="ghcr.io/${OWNER}/fedora-minimal:${REL}sha256-${DIGEST:0:12}"
    if podman pull -q "${MIRROR_BASE}" >/dev/null 2>&1; then
        echo "base image mirror hit: ${MIRROR_BASE}"
        sed -i "s|${UPSTREAM_BASE}|${MIRROR_BASE}|g" containers/Dockerfile.mirror-builder "${ENV_FILE}"
    elif podman pull -q "${UPSTREAM_BASE}"; then
        echo "base image mirror miss; pulled upstream ${UPSTREAM_BASE}"
        if [[ "${PUSH_BASE_MIRROR:-}" == "true" ]] \
            && podman tag "${UPSTREAM_BASE}" "${MIRROR_BASE}" \
            && podman push -q "${MIRROR_BASE}"; then
            echo "populated base image mirror: ${MIRROR_BASE}"
            sed -i "s|${UPSTREAM_BASE}|${MIRROR_BASE}|g" containers/Dockerfile.mirror-builder "${ENV_FILE}"
        fi
    fi
fi
{
    echo "ENV_NAME=${ENV_NAME}"
    echo "SPACK_VER=${SPACK_VER}"
    echo "NIGHTLY_DATE=${NIGHTLY_DATE}"
    echo "OWNER=${OWNER}"
    echo "OCI_MIRROR=oci://ghcr.io/${OWNER}/hpc-cf-buildcache"
    echo "IMAGE_NAME=${ENV_NAME%%-*}"
    echo "ENV_SUFFIX=${ENV_NAME#*-}"
    echo "IMG_REPO=ghcr.io/${OWNER}/${ENV_NAME%%-*}"
    # Remote tag prefix is the channel name: dated runtime tags read
    # -nightly-<date> / -stable-<date>; -release is the verified float the
    # sif job moves.
    echo "TAG_PREFIX=${CHANNEL}"
} >> "${GITHUB_ENV:?}"
echo "date=${NIGHTLY_DATE}" >> "${GITHUB_OUTPUT:?}"
echo "resolved env identity: ${ENV_NAME} (spack ${SPACK_VER}, ${CHANNEL} ${NIGHTLY_DATE})"

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

# ── pkg mirror: point Fedora mirror-builder at official metalink on US CI ───
# The USTC mirror is tuned for local builds; from GitHub's US runners it is
# a cross-pacific flake source. Switch ARG PKG_MIRROR=official and clear the
# env-level pkg_mirror_setup. Local builds stay untouched (ephemeral checkout).
sed -i 's/mirrors.ustc.edu.cn/official/g' containers/Dockerfile.mirror-builder
sed -i '/^  pkg_mirror_setup:/c\  pkg_mirror_setup: ""' \
  "spack-envs/${BASE_ENV_NAME}/spack-env-file/env.yaml"
# Explicit exit: set -e ignores a negated `! grep`.
if grep -q "ustc" containers/Dockerfile.mirror-builder; then
    echo "::error::containers/Dockerfile.mirror-builder still references ustc after the mirror rewrite" >&2
    exit 1
fi

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

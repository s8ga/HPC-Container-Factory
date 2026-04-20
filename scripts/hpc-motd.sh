#!/bin/bash
# /usr/local/bin/hpc-motd.sh
# Message of the Day for HPC containers.
# Called at shell login to display container identity and version info.
#
# Build-time ENV variables (set in Dockerfile):
#   HPC_CONTAINER_NAME  — e.g. cp2k-opensource, cp2k-opensource-avx512
#   HPC_CONTAINER_TAG   — e.g. 2025.2
#   HPC_CONTAINER_BUILD_TS — ISO 8601 build timestamp

# ── Colors (ANSI-C quoting: $'\033' stores real ESC bytes, not literal text) ──
if [[ -t 1 ]]; then
    RST=$'\033[0m'
    BLD=$'\033[1m'
    DIM=$'\033[2m'
    CYA=$'\033[36m'
    GRN=$'\033[32m'
    YLW=$'\033[33m'
    RED=$'\033[31m'
    B_CYA=$'\033[1;36m'
    B_GRN=$'\033[1;32m'
    B_WHT=$'\033[1;37m'
    B_RED=$'\033[1;31m'
else
    RST='' BLD='' DIM='' CYA='' GRN='' YLW='' RED=''
    B_CYA='' B_GRN='' B_WHT='' B_RED=''
fi

# ── Helpers ───────────────────────────────────────────────────────────────
repeat() { local ch="${1:--}" n="${2:-50}"; printf '%*s' "$n" '' | tr ' ' "$ch"; }

# ── Static info (from build-time ENV) ─────────────────────────────────────
IMAGE_NAME="${HPC_CONTAINER_NAME:-unknown}"
IMAGE_TAG="${HPC_CONTAINER_TAG:-unknown}"
BUILD_TS="${HPC_CONTAINER_BUILD_TS:-}"

# ── SIMD capability ──────────────────────────────────────────────────────

detect_simd() {
    local flags
    flags=$(grep -m1 'flags' /proc/cpuinfo 2>/dev/null || true)
    if echo "$flags" | grep -qw avx512f; then echo "AVX-512"
    elif echo "$flags" | grep -qw avx2;    then echo "AVX2"
    elif echo "$flags" | grep -qw avx;     then echo "AVX"
    elif echo "$flags" | grep -qw sse;     then echo "SSE"
    else echo "???"; fi
}

# ── CPU info ─────────────────────────────────────────────────────────────
CPU_MODEL=$(grep -m1 'model name' /proc/cpuinfo 2>/dev/null | sed 's/.*: //' || echo "Unknown")
NPROC=$(nproc 2>/dev/null || echo "?")
SIMD=$(detect_simd)

# SIMD display: green for AVX-512 (optimal), red + warning for lower tiers
case "$SIMD" in
    AVX-512) SIMD_DISPLAY="${GRN}${SIMD}${RST}" ;;
    *)       SIMD_DISPLAY="${B_RED}${SIMD}${RST} ${DIM}(might hinder performance)${RST}" ;;
esac

# ── Render ───────────────────────────────────────────────────────────────
W=54
LINE=$(repeat '-' "$W")

declare -a ROWS=(
    "Build Time|${DIM}${BUILD_TS}${RST}"
    "CPU|${DIM}${CPU_MODEL} (${NPROC} cores)${RST}"
    "SIMD|${SIMD_DISPLAY}"
)

LABEL_W=0
for row in "${ROWS[@]}"; do
    IFS='|' read -r label _ <<< "$row"
    (( ${#label} > LABEL_W )) && LABEL_W=${#label}
done

echo ""
echo " ${DIM}${LINE}${RST}"
echo " ${B_CYA}⬡${RST}  ${BLD}${B_GRN}${IMAGE_NAME}${RST} ${B_WHT}${IMAGE_TAG}${RST}"
echo " ${DIM}${LINE}${RST}"
for row in "${ROWS[@]}"; do
    IFS='|' read -r label value <<< "$row"
    [[ -z "$value" ]] && continue
    printf " ${BLD}%-${LABEL_W}s${RST}  %s\n" "$label" "$value"
done
echo " ${DIM}${LINE}${RST}"
echo ""

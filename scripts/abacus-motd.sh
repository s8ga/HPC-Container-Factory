#!/bin/bash
# /usr/local/bin/hpc-motd.sh
# ABACUS-specific Message of the Day.
# Displays container identity, hardware check, and runtime hints.
#
# Build-time ENV variables (set in Dockerfile):
#   HPC_CONTAINER_NAME     — e.g. abacus-opensource, abacus-opensource-avx512
#   HPC_CONTAINER_TAG      — e.g. 3.10.1-lts
#   HPC_CONTAINER_BUILD_TS — ISO 8601 build timestamp

set -euo pipefail

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
    B_YLW=$'\033[1;33m'
else
    RST='' BLD='' DIM='' CYA='' GRN='' YLW='' RED=''
    B_CYA='' B_GRN='' B_WHT='' B_RED='' B_YLW=''
fi

# ── Catch phrase (random, DIM style) ────────────────────────────────────
# Add your own phrases here!
CATCH_PHRASES=()

# ── Helpers ───────────────────────────────────────────────────────────────
repeat() { local ch="${1:--}" n="${2:-71}"; printf '%*s' "$n" '' | tr ' ' "$ch"; }
W=71
LINE=$(repeat '-' "$W")

# ── Static info (from build-time ENV) ─────────────────────────────────────
IMAGE_NAME="${HPC_CONTAINER_NAME:-unknown}"
IMAGE_TAG="${HPC_CONTAINER_TAG:-unknown}"
BUILD_TS="${HPC_CONTAINER_BUILD_TS:-}"

# ── Hardware detection ───────────────────────────────────────────────────
detect_simd() {
    local flags
    flags=$(grep -m1 'flags' /proc/cpuinfo 2>/dev/null || true)
    if echo "$flags" | grep -qw avx512f; then echo "AVX-512"
    elif echo "$flags" | grep -qw avx2;    then echo "AVX2"
    elif echo "$flags" | grep -qw avx;     then echo "AVX"
    elif echo "$flags" | grep -qw sse;     then echo "SSE"
    else echo "???"; fi
}

CPU_MODEL=$(grep -m1 'model name' /proc/cpuinfo 2>/dev/null | sed 's/.*: //' || echo "Unknown")
NPROC=$(nproc 2>/dev/null || echo "?")
SIMD=$(detect_simd)

# ── Memory info ──────────────────────────────────────────────────────────
read_meminfo() {
    awk -v unit="$1" '
        /^MemTotal:/   { total = $2 }
        /^MemAvailable:/ { avail = $2 }
        END {
            if (unit == "GiB") { f = 1024*1024; s = "GiB" }
            else               { f = 1024;       s = "MiB" }
            printf "%.0f/%.0f %s", (total - avail) / f, total / f, s
        }
    ' /proc/meminfo 2>/dev/null || echo "N/A"
}
MEM_INFO=$(read_meminfo GiB)

# ── SIMD status ──────────────────────────────────────────────────────────
case "$SIMD" in
    AVX-512) SIMD_STATUS="${GRN}OK${RST} — ${SIMD} detected" ;;
    *)       SIMD_STATUS="${B_RED}WARNING!${RST} Only ${B_RED}${SIMD}${RST} detected!" ;;
esac

# ── ABACUS executable ────────────────────────────────────────────────────
if command -v abacus &>/dev/null; then
    ABACUS_EXEC="abacus"
else
    ABACUS_EXEC="not found in PATH"
fi

# ── Render ───────────────────────────────────────────────────────────────
echo ""
echo " ${DIM}${LINE}${RST}"
echo " ${B_CYA}⬡${RST}  ${BLD}${B_GRN}${IMAGE_NAME}${RST} ${DIM}|${RST} ${BLD}Version:${RST} ${B_WHT}${IMAGE_TAG}${RST}"
echo " ${DIM}${LINE}${RST}"
echo "  ${BLD}Built At${RST}  : ${DIM}${BUILD_TS}${RST}"
echo ""
echo "  ${B_CYA}HARDWARE CHECK:${RST}"
echo "  ${BLD}CPU Model${RST} : ${DIM}${CPU_MODEL} (${NPROC} cores)${RST}"
echo "  ${BLD}Memory${RST}    : ${DIM}${MEM_INFO} (Used/Total)${RST}"
echo "  ${BLD}SIMD Stat${RST} : [${SIMD_STATUS}]"
if [[ "$SIMD" != "AVX-512" ]]; then
    echo "              ${DIM}(This *will* hinder performance)${RST}"
fi
echo ""
echo "  ${B_CYA}ENVIRONMENT:${RST}"
echo "  ${BLD}Executable${RST}: ${DIM}${ABACUS_EXEC}${RST}"
echo ""
echo "  ${B_YLW}HINT:${RST}"
echo "  ABACUS reads input from the current directory. To run:"
echo "  ${GRN}cd <work_dir> && mpirun "'$MPI_RUNVAR'" -np <N> -x OMP_NUM_THREADS=x abacus${RST}"
echo "  ${DIM}Customize: ${CYA}echo "'$MPI_RUNVAR'"${RST} ${DIM}to inspect, or override in your launch script.${RST}"
echo " ${DIM}${LINE}${RST}"
echo "  Type ${CYA}'abacus --version'${RST} for version info."
echo "  ${DIM}(Newer versions: ${CYA}'abacus --info'${RST}${DIM} for build details)${RST}"
echo " ${DIM}${LINE}${RST}"

echo ""
if [[ ${#CATCH_PHRASES[@]} -gt 0 ]]; then
    echo -e "  ${DIM}${CATCH_PHRASES[$(( RANDOM % ${#CATCH_PHRASES[@]} ))]}${RST}"
    echo ""
fi

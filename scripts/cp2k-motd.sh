#!/bin/bash
# /opt/bin/cp2k-motd.sh
# CP2K-specific Message of the Day.
# Displays container identity, hardware check, and runtime hints.
#
# Build-time ENV variables (set in Dockerfile):
#   HPC_CONTAINER_NAME     — e.g. cp2k-opensource, cp2k-opensource-avx512
#   HPC_CONTAINER_TAG      — e.g. 2025.2
#   HPC_CONTAINER_BUILD_TS — ISO 8601 build timestamp
#
# Runtime ENV variables (set in Dockerfile):
#   CP2K_DATA_DIR          — path to basis sets and potentials

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
# When using new line remeber to add 2 spaces at the beginning of the next line
CATCH_PHRASES=(
    "Happy computing! May your SCF converge swiftly."

    "Your electrons are in good hands."

    "Convergence is just a few iterations away... probably."

    "Powered by caffeine and quantum mechanics.\n  (but mostly caffeine)"

    "May your forces be balanced and energies low."

    "Ab initio, ad infinitum."

    "In CP2K we trust."

    "Remember, NO GARBAGE INPUT."
    
    "SCF GO SLOW? ME NO LIKE! NEED MOAR WAAAGH!"

    "SCF, SCF! WHAT IS YOUR PROBLEM? WHY YOU NO CONVERGE?!"

    "Imagine paying for a license just to see \n  'Electronic self-consistency was not achieved'."

    "Life is too short to spend it cat-ing POTCAR files."

    "CP2K: Giving you the freedom to simulate the world, one cell at a time."

    "Your simulation shouldn't wait for a vendor's permission."

    "Did you know you could use multiwfn to generate cp2k input files?\n  Just saying.\n  http://sobereva.com/multiwfn/"
)

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

# ── CP2K environment ─────────────────────────────────────────────────────
DATA_DIR="${CP2K_DATA_DIR:-/opt/spack-view/share/cp2k/data}"
if command -v cp2k.psmp &>/dev/null; then
    CP2K_EXEC="cp2k.psmp (MPI + OpenMP Hybrid)"
else
    CP2K_EXEC="cp2k.psmp (not found in PATH)"
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
echo "  ${BLD}Data Dir${RST}  : ${DIM}${DATA_DIR}${RST} ${DIM}(Basis sets, Potentials)${RST}"
echo "  ${BLD}Executable${RST}: ${DIM}${CP2K_EXEC}${RST}"
echo ""
echo "  ${B_YLW}HINT:${RST}"
echo "  To optimize performance, set:"
echo "  ${GRN}export OMP_NUM_THREADS=1${RST} ${DIM}(or your preferred threads per rank)${RST}"
echo "  ${DIM}Use ${CYA}mpirun -x OMP_NUM_THREADS=1${RST} ${DIM}to set it across all ranks.${RST}"
echo " ${DIM}${LINE}${RST}"
echo "  Type ${CYA}'cp2k.psmp --version'${RST} for more details."
echo " ${DIM}${LINE}${RST}"

echo ""
echo -e "  ${DIM}${CATCH_PHRASES[$(( RANDOM % ${#CATCH_PHRASES[@]} ))]}${RST}"
echo ""

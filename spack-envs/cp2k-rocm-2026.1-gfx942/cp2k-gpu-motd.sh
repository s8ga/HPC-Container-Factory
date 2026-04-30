#!/bin/bash
# /usr/local/bin/hpc-motd.sh
# CP2K ROCm GPU-specific Message of the Day.
# Displays container identity, GPU/CPU hardware check, and runtime hints.
#
# Build-time ENV variables (set in Dockerfile):
#   HPC_CONTAINER_NAME     — e.g. cp2k-rocm-gfx942
#   HPC_CONTAINER_TAG      — e.g. 2026.1
#   HPC_CONTAINER_BUILD_TS — ISO 8601 build timestamp
#
# Runtime ENV variables (set in Dockerfile):
#   CP2K_DATA_DIR          — path to basis sets and potentials
#   ROCM_PATH              — path to ROCm installation (default: /opt/rocm)

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
# When using new line remember to add 2 spaces at the beginning of the next line
CATCH_PHRASES=(
    "Did you know you could use multiwfn to generate cp2k input files?\n  Just saying.\n  http://sobereva.com/multiwfn/"

    "GPU go brrrrr."

    "May your SCF converge before the GPU memory runs out."

    "ROCm + CP2K: Because physics should not wait for CPU stalls."

    "Your wavefunctions are in good hands... and good GPUs."

    "Mi300X says: 'I have 192GB HBM3. Feed me more k-points.'"

    "Remember: GPU offload is a privilege, not a right.\n  Check your DBCSR settings!"

    "Imagine running DFT on CPU in 2026. Couldn't be you.\n  ...right?"
)

# ── Helpers ───────────────────────────────────────────────────────────────
repeat() { local ch="${1:--}" n="${2:-71}"; printf '%*s' "$n" '' | tr ' ' "$ch"; }
W=71
LINE=$(repeat '-' "$W")

# ── Static info (from build-time ENV) ─────────────────────────────────────
IMAGE_NAME="${HPC_CONTAINER_NAME:-unknown}"
IMAGE_TAG="${HPC_CONTAINER_TAG:-unknown}"
BUILD_TS="${HPC_CONTAINER_BUILD_TS:-}"

# ── ROCm / GPU detection ─────────────────────────────────────────────────
ROCM_PATH="${ROCM_PATH:-/opt/rocm}"

detect_rocm_version() {
    local ver_file="${ROCM_PATH}/.info/version"
    if [[ -f "$ver_file" ]]; then
        # e.g. "7.2.1-68" → "7.2.1"
        grep -oP '\d+\.\d+\.\d+' "$ver_file" 2>/dev/null | head -1 || echo "unknown"
    else
        echo "not found"
    fi
}

detect_gpus() {
    # Try rocm-smi first (most accurate)
    if command -v rocm-smi &>/dev/null; then
        rocm-smi --showproductname 2>/dev/null | grep -c 'GPU\[' || echo "0"
    # Fallback: check /dev/kfd (AMD GPU) or /dev/dri (generic)
    elif [[ -c /dev/kfd ]]; then
        # Count unique GPU IDs from kfd
        ls /dev/kfd &>/dev/null | wc -l
        # If kfd exists but we can't count, at least report 1
        echo "1" 2>/dev/null || echo "0"
    else
        echo "0"
    fi
}

get_gpu_name() {
    local idx="${1:-0}"
    if command -v rocm-smi &>/dev/null; then
        rocm-smi --showproductname 2>/dev/null | awk -F'│' "/GPU\\[${idx}\\]/{gsub(/^[ \\t]+|[ \\t]+$/, \"\", \$2); print \$2; exit}"
    fi
}

get_gpu_vram() {
    # rocm-smi returns VRAM in bytes or MiB depending on version
    if command -v rocm-smi &>/dev/null; then
        rocm-smi --showmeminfo vram 2>/dev/null | grep -oP '\d+\s*(MiB|GiB|KB|B)' | head -1 || echo "N/A"
    fi
}

ROCM_VER=$(detect_rocm_version)
GPU_COUNT=$(detect_gpus)

# ── CPU hardware detection ───────────────────────────────────────────────
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

# ── GPU status assessment ────────────────────────────────────────────────
if [[ "$GPU_COUNT" -gt 0 ]] && [[ "$ROCM_VER" != "not found" ]]; then
    GPU_STATUS="${GRN}OK${RST} — ${GPU_COUNT} AMD GPU(s) detected, ROCm ${ROCM_VER}"
    GPU_OK=true
elif [[ "$GPU_COUNT" -gt 0 ]]; then
    GPU_STATUS="${YLW}PARTIAL${RST} — GPU(s) detected but ROCm ${ROCM_VER}"
    GPU_OK=false
else
    GPU_STATUS="${B_RED}MISSING!${RST} No AMD GPU detected"
    GPU_OK=false
fi

# ── SIMD status ──────────────────────────────────────────────────────────
case "$SIMD" in
    AVX-512) SIMD_STATUS="${GRN}OK${RST} — ${SIMD} detected" ;;
    *)       SIMD_STATUS="${DIM}${SIMD}${RST} (GPU offload mode)" ;;
esac

# ── CP2K environment ─────────────────────────────────────────────────────
DATA_DIR="${CP2K_DATA_DIR:-/opt/spack-view/share/cp2k/data}"
if command -v cp2k.psmp &>/dev/null; then
    CP2K_EXEC="cp2k.psmp (MPI + OpenMP + GPU Offload)"
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
echo "  ${B_CYA}GPU CHECK:${RST}"
echo "  ${BLD}ROCm Ver${RST}  : ${DIM}ROCm ${ROCM_VER}${RST}"
echo "  ${BLD}GPU Stat${RST}  : [${GPU_STATUS}]"
if [[ "$GPU_OK" == true ]]; then
    # Show details for each detected GPU
    for ((i=0; i<GPU_COUNT; i++)); do
        GPU_NAME=$(get_gpu_name "$i" 2>/dev/null || echo "AMD GPU")
        GPU_VRAM=$(get_gpu_vram 2>/dev/null || echo "")
        if [[ -n "$GPU_NAME" ]]; then
            echo "  ${BLD}GPU[${i}]${RST}     : ${DIM}${GPU_NAME}${RST} ${GPU_VRAM:+${DIM}(${GPU_VRAM})${RST}}"
        fi
    done
else
    echo "              ${DIM}(GPU offload will NOT work without AMD GPU + ROCm)${RST}"
fi
echo ""
echo "  ${B_CYA}CPU CHECK:${RST}"
echo "  ${BLD}CPU Model${RST} : ${DIM}${CPU_MODEL} (${NPROC} cores)${RST}"
echo "  ${BLD}Memory${RST}    : ${DIM}${MEM_INFO} (Used/Total)${RST}"
echo "  ${BLD}SIMD Stat${RST} : [${SIMD_STATUS}]"
echo ""
echo "  ${B_CYA}ENVIRONMENT:${RST}"
echo "  ${BLD}Data Dir${RST}  : ${DIM}${DATA_DIR}${RST} ${DIM}(Basis sets, Potentials)${RST}"
echo "  ${BLD}Executable${RST}: ${DIM}${CP2K_EXEC}${RST}"
echo "  ${BLD}ROCM_PATH${RST} : ${DIM}${ROCM_PATH}${RST}"
echo ""
echo "  ${B_YLW}HINT:${RST}"
echo "  GPU offload launch:"
echo "  ${GRN}mpirun "'$MPI_RUNVAR'" -np <N> -x OMP_NUM_THREADS=x cp2k.psmp -i <input>.inp${RST}"
echo ""
echo "  ${B_YLW}GPU TIPS:${RST}"
echo "  ${DIM}• Set ${CYA}CP2K_USE_GPU_COUNT${RST} ${DIM}to control how many GPUs per rank.${RST}"
echo "  ${DIM}• Use ${CYA}ACC_DEVICE_TYPE=hip${RST} ${DIM}to force HIP backend (default for ROCm).${RST}"
echo "  ${DIM}• Verify GPU visibility: ${CYA}rocm-smi${RST}"
echo "  ${DIM}• Customize MPI vars: ${CYA}echo \"\$MPI_RUNVAR\"${RST}"
echo " ${DIM}${LINE}${RST}"
echo "  Type ${CYA}'cp2k.psmp --version'${RST} for more details."
echo " ${DIM}${LINE}${RST}"

echo ""
echo -e "  ${DIM}${CATCH_PHRASES[$(( RANDOM % ${#CATCH_PHRASES[@]} ))]}${RST}"
echo ""

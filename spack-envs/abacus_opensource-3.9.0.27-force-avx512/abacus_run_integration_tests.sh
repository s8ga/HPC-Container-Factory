#!/bin/bash
#
# abacus_run_integration_tests.sh — Batch run ABACUS integration tests (01–10)
#
# Auto-discovers the ABACUS install under /opt/spack/linux-x86_64_v3/
# and runs all integration test groups sequentially.
#
# Usage (inside container):
#   podman run --rm --network=host \
#     -v $PWD/scripts/abacus_run_integration_tests.sh:/tmp/run_tests.sh:ro \
#     abacus_opensource:3.9.0.27-force-avx512 bash /tmp/run_tests.sh

set -eu

TESTS="$(ls -d /opt/spack/linux-x86_64_v3/abacus-*/share/abacus/tests 2>/dev/null | head -1)"
if [[ -z "$TESTS" ]]; then
    echo "ERROR: Cannot find test dir under /opt/spack/linux-x86_64_v3/" >&2
    exit 1
fi

AUTOTEST="$TESTS/integrate/Autotest.sh"
if [[ ! -f "$AUTOTEST" ]]; then
    echo "ERROR: Autotest.sh not found at $AUTOTEST" >&2
    exit 1
fi

DIRS="01_PW 02_NAO_Gamma 03_NAO_multik 04_FF 05_rtTDDFT 06_SDFT 07_OFDFT 08_EXX 09_DeePKS 10_others"

echo "================================================================"
echo "  ABACUS Integration Tests"
echo "  $TESTS"
echo "================================================================"
echo ""

PASS=0
FAIL=0
SKIP=0
START=$(date +%s)

for dir in $DIRS; do
    if [[ ! -d "$TESTS/$dir" ]]; then
        echo "[SKIP] $dir not found"
        SKIP=$((SKIP + 1))
        continue
    fi

    cases="$TESTS/$dir/CASES_CPU.txt"
    if [[ ! -f "$cases" ]]; then
        echo "[SKIP] $dir: no CASES_CPU.txt"
        SKIP=$((SKIP + 1))
        continue
    fi

    n=$(grep -cE '^[^#].*_.*$' "$cases" 2>/dev/null || echo "?")

    echo "--- $dir ($n cases) ---"
    t0=$(date +%s)

    rc=0
    (cd "$TESTS/$dir" && bash "$AUTOTEST") || rc=$?

    t1=$(date +%s)
    if [[ $rc -eq 0 ]]; then
        echo "[PASS] $dir — $((t1 - t0))s"
        PASS=$((PASS + 1))
    else
        echo "[FAIL] $dir — $((t1 - t0))s (rc=$rc)"
        FAIL=$((FAIL + 1))
    fi
    echo ""
done

END=$(date +%s)
ELAPSED=$((END - START))
TOTAL=$((PASS + FAIL + SKIP))

echo "================================================================"
echo "  Summary"
echo "================================================================"
printf "  %-10s %d\n" "Total:"   "$TOTAL"
printf "  %-10s %d\n" "Passed:"  "$PASS"
printf "  %-10s %d\n" "Failed:"  "$FAIL"
printf "  %-10s %d\n" "Skipped:" "$SKIP"
printf "  %-10s %ds\n" "Time:"   "$ELAPSED"
echo "================================================================"

[[ $FAIL -gt 0 ]] && exit 1
exit 0

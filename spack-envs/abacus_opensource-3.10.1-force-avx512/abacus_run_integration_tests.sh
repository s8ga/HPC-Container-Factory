#!/bin/bash
#
# abacus_run_integration_tests.sh — Run ABACUS flat Autotest (3.10.1-lts)
#
# Auto-discovers share/abacus/tests under /opt/spack (short or padded
# install prefix). 3.10.1-lts ships a flat integrate/ tree (cases beside
# Autotest.sh + CASES_CPU.txt) — not the 01_PW…10_others grouping used by
# older ABACUS installs. Matches release evidence entrypoint.
#
# Usage (inside container):
#   podman run --rm --network=host \
#     -v $PWD/abacus_run_integration_tests.sh:/tmp/run_tests.sh:ro \
#     abacus_opensource:3.10.1-force-avx512 bash /tmp/run_tests.sh

set -eu

TESTS="$(ls -d /opt/spack/linux-x86_64_v3/abacus-*/share/abacus/tests 2>/dev/null | head -1)"
if [[ -z "$TESTS" ]]; then
    # padded_length installs nest under /opt/spack/__spack_path_placeholder__/...
    TESTS="$(find /opt/spack -type d -path '*/share/abacus/tests' 2>/dev/null | head -1)"
fi
if [[ -z "$TESTS" ]]; then
    echo "ERROR: Cannot find share/abacus/tests under /opt/spack/" >&2
    exit 1
fi

INTEGRATE="$TESTS/integrate"
AUTOTEST="$INTEGRATE/Autotest.sh"
CASES="$INTEGRATE/CASES_CPU.txt"
if [[ ! -f "$AUTOTEST" ]]; then
    echo "ERROR: Autotest.sh not found at $AUTOTEST" >&2
    exit 1
fi
if [[ ! -f "$CASES" ]]; then
    echo "ERROR: CASES_CPU.txt not found at $CASES" >&2
    exit 1
fi

n=$(grep -cE '^[^#].*_.*$' "$CASES" 2>/dev/null || echo "0")

echo "================================================================"
echo "  ABACUS Integration Tests (flat Autotest)"
echo "  $INTEGRATE"
echo "  cases: $n"
echo "================================================================"
echo ""

START=$(date +%s)
rc=0
(cd "$INTEGRATE" && bash "$AUTOTEST") || rc=$?
END=$(date +%s)
ELAPSED=$((END - START))

if [[ $rc -eq 0 ]]; then
    PASS=1
    FAIL=0
else
    PASS=0
    FAIL=1
fi
TOTAL=$((PASS + FAIL))

echo ""
echo "================================================================"
echo "  Summary"
echo "================================================================"
printf "  %-10s %d\n" "Total:"   "$TOTAL"
printf "  %-10s %d\n" "Passed:"  "$PASS"
printf "  %-10s %d\n" "Failed:"  "$FAIL"
printf "  %-10s %ds\n" "Time:"   "$ELAPSED"
echo "  Autotest rc: $rc"
echo "================================================================"

[[ $TOTAL -eq 0 ]] && { echo "ERROR: no Autotest run recorded" >&2; exit 1; }
[[ $FAIL -gt 0 ]] && exit 1
exit 0

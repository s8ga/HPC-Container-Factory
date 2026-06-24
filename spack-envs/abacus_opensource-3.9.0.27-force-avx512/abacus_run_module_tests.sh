#!/bin/bash
#
# abacus_run_module_tests.sh — Batch run ABACUS MODULE_* unit tests
#
# Auto-discovers the ABACUS install under /opt/spack/linux-x86_64_v3/
# and runs all unit-test executables. Each test runs from its own module
# directory so ./support/ resolves correctly.
#
# Usage (inside container):
#   podman run --rm --network=host \
#     -v $PWD/scripts/abacus_run_module_tests.sh:/tmp/run_tests.sh:ro \
#     abacus_opensource:3.9.0.27-force-avx512 bash /tmp/run_tests.sh

set -eu

TESTS="$(ls -d /opt/spack/linux-x86_64_v3/abacus-*/share/abacus/tests 2>/dev/null | head -1)"
if [[ -z "$TESTS" ]]; then
    echo "ERROR: Cannot find test dir under /opt/spack/linux-x86_64_v3/" >&2
    exit 1
fi

echo "================================================================"
echo "  ABACUS Module Unit Tests"
echo "  $TESTS"
echo "================================================================"
echo ""

PASS=0
FAIL=0
FAILED_TESTS=""
START=$(date +%s)

# Find all test executables in module subdirectories (source_*/test*/ or module_*/test*/)
# Skip CMake artifacts, support/, data/, and other non-test files
while IFS= read -r test_bin; do
    [[ -x "$test_bin" ]] || continue
    name=$(basename "$test_bin")
    dir=$(dirname "$test_bin")

    echo "--- $name ---"
    t0=$(date +%s)

    rc=0
    (cd "$dir" && timeout 30 ./"$name" 2>&1) || rc=$?

    t1=$(date +%s)
    if [[ $rc -eq 0 ]]; then
        echo "[PASS] $name — $((t1 - t0))s"
        PASS=$((PASS + 1))
    else
        echo "[FAIL] $name — $((t1 - t0))s (rc=$rc)"
        FAIL=$((FAIL + 1))
        FAILED_TESTS="${FAILED_TESTS}\n  $name"
    fi
    echo ""
done < <(find "$TESTS" -mindepth 3 -maxdepth 5 -type f -executable \
    -not -path "*/support/*" -not -path "*/data/*" \
    -not -path "*/CMakeFiles/*" -not -path "*/.spack/*" \
    -not -path "*/PP_ORB/*" \
    -not -path "*/01_PW/*" -not -path "*/02_NAO*" -not -path "*/03_NAO*" \
    -not -path "*/04_FF/*" -not -path "*/05_rt*" -not -path "*/06_SDFT/*" \
    -not -path "*/07_OFDFT/*" -not -path "*/08_EXX/*" -not -path "*/09_DeePKS/*" \
    -not -path "*/10_others/*" -not -path "*/integrate/*" \
    -not -path "*/libxc/*" -not -path "*/deepks/*" -not -path "*/performance/*" \
    -not -name "*.txt" -not -name "*.json" -not -name "*.sh" -not -name "*.py" \
    -not -name "*.cpp" -not -name "*.h" -not -name "*.cmake" \
    2>/dev/null | sort)

END=$(date +%s)
ELAPSED=$((END - START))
TOTAL=$((PASS + FAIL))

echo "================================================================"
echo "  Summary"
echo "================================================================"
printf "  %-10s %d\n" "Total:"   "$TOTAL"
printf "  %-10s %d\n" "Passed:"  "$PASS"
printf "  %-10s %d\n" "Failed:"  "$FAIL"
printf "  %-10s %ds\n" "Time:"   "$ELAPSED"
if [[ $FAIL -gt 0 ]]; then
    echo ""
    echo "  Failed tests:"
    echo -e "$FAILED_TESTS"
fi
echo "================================================================"

[[ $FAIL -gt 0 ]] && exit 1
exit 0

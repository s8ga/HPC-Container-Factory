#!/bin/bash
#
# abacus_run_module_tests.sh — Batch run ABACUS MODULE_* unit tests
#
# Auto-discovers share/abacus/tests under /opt/spack (short or padded
# install prefix) and runs all unit-test executables. Each test runs from
# its own module directory so ./support/ resolves correctly.
#
# Environment:
#   ABACUS_MODULE_TEST_TIMEOUT   Default per-test timeout in seconds (default: 30).
#                                HSolver / dav / cg binaries use max(default, 120).
#   ABACUS_MODULE_TEST_FULL_LOG  Set to 1 to keep full stdout/stderr (no truncation).
#
# Usage (inside container):
#   podman run --rm --network=host \
#     -v $PWD/abacus_run_module_tests.sh:/tmp/run_tests.sh:ro \
#     abacus_opensource:3.9.0.27-force-avx512 bash /tmp/run_tests.sh

set -eu

DEFAULT_TIMEOUT="${ABACUS_MODULE_TEST_TIMEOUT:-30}"
FULL_LOG="${ABACUS_MODULE_TEST_FULL_LOG:-0}"
# Slow solvers: allow longer than the default (at least 120s).
SLOW_TIMEOUT=$(( DEFAULT_TIMEOUT > 120 ? DEFAULT_TIMEOUT : 120 ))

# Head/tail caps for failed-test output (ignored when FULL_LOG=1).
FAIL_HEAD_LINES=80
FAIL_TAIL_LINES=40
# Short pass summary when truncating.
PASS_HEAD_LINES=5
PASS_TAIL_LINES=5

TESTS="$(ls -d /opt/spack/linux-x86_64_v3/abacus-*/share/abacus/tests 2>/dev/null | head -1)"
if [[ -z "$TESTS" ]]; then
    # padded_length installs nest under /opt/spack/__spack_path_placeholder__/...
    TESTS="$(find /opt/spack -type d -path '*/share/abacus/tests' 2>/dev/null | head -1)"
fi
if [[ -z "$TESTS" ]]; then
    echo "ERROR: Cannot find share/abacus/tests under /opt/spack/" >&2
    exit 1
fi

echo "================================================================"
echo "  ABACUS Module Unit Tests"
echo "  $TESTS"
echo "  default_timeout=${DEFAULT_TIMEOUT}s slow_timeout=${SLOW_TIMEOUT}s full_log=${FULL_LOG}"
echo "================================================================"
echo ""

PASS=0
FAIL=0
FAILED_TESTS=""
START=$(date +%s)

# Skip non-gtest basenames that are sometimes +x input files (rc=127 noise).
_is_fake_binary() {
    case "$1" in
        INPUT|KPT|STRU|STRU_REF|INPUT_ref|KPT_ref|STRU_ref|ORB|PP|PARAMS)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

_timeout_for() {
    local name="$1"
    case "$name" in
        *HSolver*|*HSOLVER*|*dav*|*cg*|*Dav*|*Cg*)
            echo "$SLOW_TIMEOUT"
            ;;
        *)
            echo "$DEFAULT_TIMEOUT"
            ;;
    esac
}

# Print captured output: full, or head+tail with omit count.
_emit_capped() {
    local file="$1"
    local head_n="$2"
    local tail_n="$3"
    local total
    total=$(wc -l < "$file" | tr -d ' ')
    if [[ "$FULL_LOG" == "1" ]] || [[ "$total" -le $((head_n + tail_n)) ]]; then
        cat "$file"
        return
    fi
    local omitted=$((total - head_n - tail_n))
    head -n "$head_n" "$file"
    echo "... [${omitted} lines omitted] ..."
    tail -n "$tail_n" "$file"
}

# Find all test executables in module subdirectories (source_*/test*/ or module_*/test*/)
# Skip CMake artifacts, support/, data/, and other non-test files
while IFS= read -r test_bin; do
    [[ -x "$test_bin" ]] || continue
    name=$(basename "$test_bin")
    dir=$(dirname "$test_bin")

    if _is_fake_binary "$name"; then
        echo "--- $name --- (skipped: non-gtest input basename)"
        echo ""
        continue
    fi

    to=$(_timeout_for "$name")
    echo "--- $name --- (timeout=${to}s)"
    t0=$(date +%s)

    out=$(mktemp)
    rc=0
    (cd "$dir" && timeout "$to" ./"$name" >"$out" 2>&1) || rc=$?

    t1=$(date +%s)
    if [[ $rc -eq 0 ]]; then
        _emit_capped "$out" "$PASS_HEAD_LINES" "$PASS_TAIL_LINES"
        echo "[PASS] $name — $((t1 - t0))s"
        PASS=$((PASS + 1))
    else
        _emit_capped "$out" "$FAIL_HEAD_LINES" "$FAIL_TAIL_LINES"
        echo "[FAIL] $name — $((t1 - t0))s (rc=$rc)"
        FAIL=$((FAIL + 1))
        FAILED_TESTS="${FAILED_TESTS}\n  $name"
    fi
    rm -f "$out"
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

[[ $TOTAL -eq 0 ]] && {
    echo "ERROR: no module tests discovered under $TESTS" >&2
    exit 1
}
[[ $FAIL -gt 0 ]] && exit 1
exit 0

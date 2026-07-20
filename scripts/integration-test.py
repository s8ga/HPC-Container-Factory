#!/usr/bin/env python3
"""Convenience wrapper: run opt-in integration tests via pytest.

Equivalent to:
    pytest tests/test_integration_spack.py \\
           tests/test_integration_abacus_l4.py -v --run-integration -s

Requires podman, the hpc-mirror-builder image (L3), versioned Spack assets,
and (for L4) a healthy/covered buildcache plus ABACUS consumer build inputs.
Missing assets skip the corresponding cells rather than false-green passes.
"""
import subprocess
import sys

sys.exit(subprocess.call([
    sys.executable, "-m", "pytest",
    "tests/test_integration_spack.py",
    "tests/test_integration_abacus_l4.py",
    "-v", "--run-integration", "-s",
]))

#!/usr/bin/env python3
"""Convenience wrapper: run the spack integration tests via pytest.

Equivalent to:
    pytest tests/test_integration_spack.py -v --run-integration

Requires podman, the hpc-mirror-builder image, and spack assets.
"""
import subprocess
import sys

sys.exit(subprocess.call([
    sys.executable, "-m", "pytest",
    "tests/test_integration_spack.py", "-v", "--run-integration", "-s",
]))

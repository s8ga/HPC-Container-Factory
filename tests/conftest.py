import sys
from pathlib import Path

import pytest

# Allow `import hpc_cf` without installing the package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def pytest_addoption(parser):
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="run integration tests (requires podman + spack assets)",
    )


def pytest_collection_modifyitems(config, items):
    """Skip @pytest.mark.integration tests unless --run-integration is given."""
    if config.getoption("--run-integration"):
        return
    skip_integration = pytest.mark.skip(
        reason="integration test — run with --run-integration (needs podman + assets)"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)

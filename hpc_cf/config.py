"""Path constants for the HPC Container Factory project."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = PROJECT_ROOT / "templates"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
TOOLS_DIR = PROJECT_ROOT / "tools"
SPACK_ENVS_DIR = PROJECT_ROOT / "spack-envs"
ASSETS_DIR = PROJECT_ROOT / "assets"

# Fixed filesystem contract shared by shipped Spack images and host-side
# operations that execute inside those images.
IMAGE_SPACK_ROOT = "/opt/spack-exe"
IMAGE_SPACK_SETUP_SCRIPT = f"{IMAGE_SPACK_ROOT}/share/spack/setup-env.sh"

# Default Spack version assumed when an env.yaml omits `spack.version`.
# Single source of truth — import this instead of hardcoding "1.1.0" everywhere.
# (Previously scattered across env.py, spack_ops.py, template.py.)
DEFAULT_SPACK_VERSION = "1.1.1"

APPTAINER_INSTALL_SCRIPT = TOOLS_DIR / "install-unprivileged.sh"
APPTAINER_LOCAL_PREFIX = TOOLS_DIR / "apptainer"

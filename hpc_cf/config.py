"""Path constants for the HPC Container Factory project."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = PROJECT_ROOT / "templates"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
TOOLS_DIR = PROJECT_ROOT / "tools"
SPACK_ENVS_DIR = PROJECT_ROOT / "spack-envs"
ASSETS_DIR = PROJECT_ROOT / "assets"

APPTAINER_INSTALL_SCRIPT = TOOLS_DIR / "install-unprivileged.sh"
APPTAINER_LOCAL_PREFIX = TOOLS_DIR / "apptainer"

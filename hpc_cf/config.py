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
# Single source of truth — import this instead of hardcoding versions everywhere.
DEFAULT_SPACK_VERSION = "1.2.2"

# Default Fedora minimal base OS pin for container builds (GA frozen repos).
DEFAULT_FEDORA_RELEASE = "45"
DEFAULT_FEDORA_IMAGE = (
    "registry.fedoraproject.org/fedora-minimal:45"
    "@sha256:05477647a380afaacc612d14f8fad016a604a7ff444dbc0f6b5a55f68a93403f"
)

# Default package mirror host for Dockerfile templates (template_vars.pkg_mirror
# / legacy apt_mirror). "official" (or empty string) keeps upstream repos; set
# HPC_CF_PKG_MIRROR or pass --pkg-mirror locally to opt into a local mirror.
DEFAULT_PKG_MIRROR = "official"
DEFAULT_APT_MIRROR = DEFAULT_PKG_MIRROR

APPTAINER_INSTALL_SCRIPT = TOOLS_DIR / "install-unprivileged.sh"
APPTAINER_LOCAL_PREFIX = TOOLS_DIR / "apptainer"

# Pinned Apptainer install-unprivileged.sh (release v1.5.2).
# Prefer the immutable commit URL over floating ``main`` / moving tags.
APPTAINER_INSTALL_SCRIPT_REF = "278038cd0562d281500a7c14940f2203632f67fc"
APPTAINER_INSTALL_SCRIPT_URL = (
    "https://raw.githubusercontent.com/apptainer/apptainer/"
    f"{APPTAINER_INSTALL_SCRIPT_REF}/tools/install-unprivileged.sh"
)
APPTAINER_INSTALL_SCRIPT_SHA256 = (
    "4d84a839a0d97177f2852b565ccbb60d8364a1ba7a6adaf00e654bfc229bba7e"
)

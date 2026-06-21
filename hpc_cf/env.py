"""env.yaml parsing and environment helpers."""

from __future__ import annotations

import hashlib
import logging
from functools import lru_cache
from pathlib import Path

try:
    import yaml
except ImportError as exc:
    raise ImportError(f"Required package not installed: {exc}. Install: pip install pyyaml") from exc

from hpc_cf.config import DEFAULT_SPACK_VERSION, PROJECT_ROOT, SPACK_ENVS_DIR

logger = logging.getLogger(__name__)


@lru_cache(maxsize=None)
def find_env_yaml(env_dir: Path) -> Path:
    """Resolve the env.yaml file for an environment directory.

    Single source of truth for env.yaml location. Prefers
    ``<env_dir>/spack-env-file/env.yaml`` (new layout) and falls back to
    ``<env_dir>/env.yaml`` (legacy/bare). Works whether *env_dir* is the
    top-level env directory or already the ``spack-env-file/`` subdirectory.

    Raises ``FileNotFoundError`` if neither exists — callers can rely on a
    concrete path rather than re-implementing the lookup (previously done
    inconsistently in 3 places with REVERSED order; see plan A2).
    """
    nested = env_dir / "spack-env-file" / "env.yaml"
    if nested.exists():
        return nested
    bare = env_dir / "env.yaml"
    if bare.exists():
        return bare
    raise FileNotFoundError(
        f"env.yaml not found in {env_dir} (looked for spack-env-file/env.yaml and env.yaml)"
    )


def load_env_yaml(template_path: Path | None) -> dict:
    """Load env.yaml associated with a Dockerfile.j2 template path."""
    if not template_path:
        return {}
    try:
        env_yaml = find_env_yaml(template_path.parent)
    except FileNotFoundError:
        return {}
    with env_yaml.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def list_available_envs() -> list[str]:
    """List environment directories under spack-envs/ that contain env.yaml."""
    envs: list[str] = []
    if SPACK_ENVS_DIR.exists():
        for d in sorted(SPACK_ENVS_DIR.iterdir()):
            if d.is_dir() and (
                (d / "spack-env-file" / "env.yaml").exists()
                or (d / "env.yaml").exists()
            ):
                envs.append(d.name)
    return envs


def spack_version_for_env(env_name: str | None) -> str:
    """Read spack.version from the given env's env.yaml.

    Returns :data:`DEFAULT_SPACK_VERSION` when env_name is None or env.yaml
    has no version.
    """
    if not env_name:
        return DEFAULT_SPACK_VERSION
    env_dir = SPACK_ENVS_DIR / env_name
    env_config = load_env_yaml(env_dir / "Dockerfile.j2") if (env_dir / "Dockerfile.j2").exists() else {}
    return env_config.get("spack", {}).get("version", DEFAULT_SPACK_VERSION)


def validate_manual_packages(env_config: dict) -> None:
    """Validate manual_packages entries from env.yaml.

    Each entry's ``file`` is resolved relative to the project root.
    If sha256 is provided, the checksum is verified.  Raises on missing
    file or checksum mismatch; warns when sha256 is absent.
    """
    manual_packages = env_config.get("manual_packages", [])
    if not manual_packages:
        return

    for mp in manual_packages:
        rel_path = mp["file"]
        mp_file = PROJECT_ROOT / rel_path

        if not mp_file.exists():
            raise FileNotFoundError(
                f"manual_packages: file not found: {rel_path}\n"
                f"  Expected: {mp_file}\n"
                f"  Place the file in the project before building."
            )

        sha256_expected = mp.get("sha256")
        if not sha256_expected:
            logger.warning(
                "⚠️  manual_packages: '%s' has NO sha256 checksum. "
                "Build reproducibility CANNOT be guaranteed.",
                rel_path,
            )
        else:
            actual = hashlib.sha256(mp_file.read_bytes()).hexdigest()
            if actual != sha256_expected:
                raise ValueError(
                    f"manual_packages: sha256 mismatch for '{rel_path}'\n"
                    f"  expected: {sha256_expected}\n"
                    f"  actual:   {actual}\n"
                    f"  Update env.yaml or replace the file."
                )
            logger.info("✅ manual_packages: '%s' sha256 verified", rel_path)


def validate_spack_assets(env_config: dict) -> None:
    """Verify the Spack tarball and bootstrap cache exist before an expensive build.

    The Dockerfile ``COPY assets/spack-v<ver>.tar.gz`` and
    ``COPY assets/bootstrap-<ver>`` fail the build if these are missing; this
    check surfaces the problem early (plan A6). Currently applies to all envs
    (all are Spack-based); gate on ``method == 'spack'`` once the no_spack
    path lands.
    """
    spack_version = env_config.get("spack", {}).get("version")
    if not spack_version:
        # Nothing to validate (and no spack build to drive); skip silently.
        return

    from hpc_cf.config import ASSETS_DIR

    tarball = ASSETS_DIR / f"spack-v{spack_version}.tar.gz"
    if not tarball.exists():
        raise FileNotFoundError(
            f"Spack tarball not found: {tarball}\n"
            f"  env.yaml declares spack.version={spack_version!r}. "
            f"Place the tarball under assets/ before building (the Dockerfile "
            f"COPY would fail ~20 min into the build otherwise)."
        )

    bootstrap = ASSETS_DIR / f"bootstrap-{spack_version}"
    if not bootstrap.is_dir():
        logger.warning(
            "Bootstrap cache missing: %s — run "
            "`python -m hpc_cf assets --prepare-bootstrap` (the Dockerfile "
            "COPYs it, so the build will fail if absent).",
            bootstrap,
        )

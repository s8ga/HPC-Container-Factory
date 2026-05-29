"""env.yaml parsing and environment helpers."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

try:
    import yaml
except ImportError as exc:
    raise ImportError(f"Required package not installed: {exc}. Install: pip install pyyaml") from exc

from hpc_cf.config import PROJECT_ROOT, SPACK_ENVS_DIR

logger = logging.getLogger(__name__)


def load_env_yaml(template_path: Path | None) -> dict:
    """Load env.yaml from the spack-env-file/ subdirectory or template directory."""
    if not template_path:
        return {}
    # New layout: spack-envs/<env>/Dockerfile.j2 + spack-envs/<env>/spack-env-file/env.yaml
    env_yaml = template_path.parent / "spack-env-file" / "env.yaml"
    if not env_yaml.exists():
        # Fallback: env.yaml alongside template (old layout)
        env_yaml = template_path.parent / "env.yaml"
    if not env_yaml.exists():
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

    Returns "1.1.0" as default when env_name is None or env.yaml has no version.
    """
    if not env_name:
        return "1.1.0"
    env_dir = SPACK_ENVS_DIR / env_name
    env_config = load_env_yaml(env_dir / "Dockerfile.j2") if (env_dir / "Dockerfile.j2").exists() else {}
    return env_config.get("spack", {}).get("version", "1.1.0")


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

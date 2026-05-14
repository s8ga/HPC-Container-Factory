"""env.yaml parsing and environment helpers."""

from __future__ import annotations

from pathlib import Path

try:
    import yaml
except ImportError as exc:
    raise ImportError(f"Required package not installed: {exc}. Install: pip install pyyaml") from exc

from hpc_cf.config import SPACK_ENVS_DIR


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

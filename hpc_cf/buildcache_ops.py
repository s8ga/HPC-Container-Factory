"""Pure shell/command builders for Spack filesystem buildcache operations."""

from __future__ import annotations

import shlex

from hpc_cf.config import IMAGE_SPACK_SETUP_SCRIPT


def _prelude(setup_script: str) -> str:
    return (
        "set -e\n"
        "set -o pipefail\n"
        f"source {shlex.quote(setup_script)}\n"
    )


def _coverage_hashes(env_name: str) -> str:
    env = shlex.quote(env_name)
    python = (
        "import spack.environment as ev; "
        "e=ev.active_environment(); "
        'print("\\n".join("/"+spec.dag_hash() for spec in e.all_specs() '
        "if not spec.external))"
    )
    return (
        f"mapfile -t spec_hashes < <(spack -e {env} python -c "
        f"{shlex.quote(python)})\n"
        'if ((${#spec_hashes[@]} == 0)); then\n'
        '    echo "No non-external concrete specs to check" >&2\n'
        "    exit 1\n"
        "fi\n"
        'echo "HPC_CF_CHECKED_SPEC_COUNT=${#spec_hashes[@]}"\n'
    )


def build_verify_script(
    *,
    env_name: str,
    store_path: str,
    setup_script: str = IMAGE_SPACK_SETUP_SCRIPT,
) -> str:
    """Check explicit non-external hashes; never rely on no-arg check."""
    env = shlex.quote(env_name)
    url = f"file://{store_path}"
    return _prelude(setup_script) + (
        f"{_coverage_hashes(env_name)}"
        f"spack -e {env} buildcache check "
        f"--mirror-url {shlex.quote(url)} \"${{spec_hashes[@]}}\"\n"
    )


def build_check_script(
    *,
    env_name: str,
    store_path: str,
    setup_script: str = IMAGE_SPACK_SETUP_SCRIPT,
) -> str:
    """Compatibility alias for callers using the original helper name."""
    return build_verify_script(
        env_name=env_name,
        store_path=store_path,
        setup_script=setup_script,
    )


def build_publish_script(
    *,
    env_name: str,
    store_path: str,
    setup_script: str = IMAGE_SPACK_SETUP_SCRIPT,
) -> str:
    """Build the mandatory push → update-index → explicit check sequence."""
    env = shlex.quote(env_name)
    path = shlex.quote(store_path)
    url = shlex.quote(f"file://{store_path}")
    return _prelude(setup_script) + (
        'echo "HPC_CF_BUILDCACHE_STEP=publish"\n'
        f"spack -e {env} buildcache push --unsigned --fail-fast {path}\n"
        'echo "HPC_CF_BUILDCACHE_STEP=update-index"\n'
        f"spack buildcache update-index {url}\n"
        'echo "HPC_CF_BUILDCACHE_STEP=check"\n'
        f"{_coverage_hashes(env_name)}"
        f"spack -e {env} buildcache check "
        f"--mirror-url {shlex.quote(f'file://{store_path}')} "
        '"${spec_hashes[@]}"\n'
    )

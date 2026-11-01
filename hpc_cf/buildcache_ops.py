"""Pure shell/command builders for Spack filesystem buildcache operations."""

from __future__ import annotations

import shlex
from collections.abc import Sequence

from hpc_cf.config import IMAGE_SPACK_SETUP_SCRIPT


def _prelude(setup_script: str) -> str:
    return (
        "set -e\n"
        "set -o pipefail\n"
        f"source {shlex.quote(setup_script)}\n"
    )


def _inventory_python(
    *,
    installed_only: bool,
    exclude_specs: Sequence[str] = (),
) -> str:
    predicate = (
        "(not spec.external) and spec.installed"
        if installed_only
        else "not spec.external"
    )
    excluded = sorted({str(name) for name in exclude_specs if name})
    if excluded:
        excl_literal = "{" + ", ".join(f'"{name}"' for name in excluded) + "}"
        predicate = f"{predicate} and (spec.name not in {excl_literal})"
    return (
        "import spack.environment as ev; "
        "e=ev.active_environment(); "
        f'print("\\n".join("/"+spec.dag_hash() for spec in e.all_specs() '
        f"if {predicate}))"
    )


def _installed_non_external_hashes(
    env_name: str,
    *,
    exclude_specs: Sequence[str] = (),
) -> str:
    """Emit mapfile of installed non-external concrete ``/dag_hash`` lines."""
    env = shlex.quote(env_name)
    python = _inventory_python(installed_only=True, exclude_specs=exclude_specs)
    return (
        f"mapfile -t installed_hashes < <(spack -e {env} python -c "
        f"{shlex.quote(python)})\n"
        'if ((${#installed_hashes[@]} == 0)); then\n'
        '    echo "No installed non-external concrete specs to push" >&2\n'
        "    exit 1\n"
        "fi\n"
        'echo "HPC_CF_PLANNED_SPEC_COUNT=${#installed_hashes[@]}"\n'
    )


def _coverage_hashes(
    env_name: str,
    *,
    exclude_specs: Sequence[str] = (),
) -> str:
    env = shlex.quote(env_name)
    python = _inventory_python(installed_only=False, exclude_specs=exclude_specs)
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
    exclude_specs: Sequence[str] = (),
) -> str:
    """Check explicit non-external hashes; never rely on no-arg check."""
    env = shlex.quote(env_name)
    url = f"file://{store_path}"
    return _prelude(setup_script) + (
        f"{_coverage_hashes(env_name, exclude_specs=exclude_specs)}"
        f"spack -e {env} buildcache check "
        f"--mirror-url {shlex.quote(url)} \"${{spec_hashes[@]}}\"\n"
    )


def build_check_script(
    *,
    env_name: str,
    store_path: str,
    setup_script: str = IMAGE_SPACK_SETUP_SCRIPT,
    exclude_specs: Sequence[str] = (),
) -> str:
    """Compatibility alias for callers using the original helper name."""
    return build_verify_script(
        env_name=env_name,
        store_path=store_path,
        setup_script=setup_script,
        exclude_specs=exclude_specs,
    )


def build_publish_script(
    *,
    env_name: str,
    store_path: str,
    setup_script: str = IMAGE_SPACK_SETUP_SCRIPT,
    exclude_specs: Sequence[str] = (),
) -> str:
    """Build push(installed) → update-index → explicit full-lock check.

    Push targets only non-external concrete specs already installed on disk so
    a partial producer install still publishes usable binaries. Proprietary
    ``exclude_specs`` (e.g. ``vasp``) are omitted from both push and check.
    """
    env = shlex.quote(env_name)
    path = shlex.quote(store_path)
    url = shlex.quote(f"file://{store_path}")
    return _prelude(setup_script) + (
        'echo "HPC_CF_BUILDCACHE_STEP=publish"\n'
        f"{_installed_non_external_hashes(env_name, exclude_specs=exclude_specs)}"
        f"{_coverage_hashes(env_name, exclude_specs=exclude_specs)}"
        'if ((${#installed_hashes[@]} < ${#spec_hashes[@]})); then\n'
        '    echo "HPC_CF_PARTIAL_PUBLISH=1"\n'
        "fi\n"
        f"spack -e {env} buildcache push --unsigned --fail-fast {path} "
        '"${installed_hashes[@]}"\n'
        'echo "HPC_CF_PUSHED_SPEC_COUNT=${#installed_hashes[@]}"\n'
        'echo "HPC_CF_BUILDCACHE_STEP=update-index"\n'
        f"spack buildcache update-index {url}\n"
        'echo "HPC_CF_BUILDCACHE_STEP=check"\n'
        f"spack -e {env} buildcache check "
        f"--mirror-url {shlex.quote(f'file://{store_path}')} "
        '"${spec_hashes[@]}"\n'
    )


def build_publish_script_oci(
    *,
    env_name: str,
    mirror_url: str,
    username_var: str | None = None,
    password_var: str | None = None,
    setup_script: str = IMAGE_SPACK_SETUP_SCRIPT,
    exclude_specs: Sequence[str] = (),
) -> str:
    """OCI-mirror publisher: register mirror, push installed hashes by name."""
    env = shlex.quote(env_name)
    cred_flags = ""
    if username_var and password_var:
        cred_flags = (
            f" --oci-username-variable {shlex.quote(username_var)}"
            f" --oci-password-variable {shlex.quote(password_var)}"
        )
    return _prelude(setup_script) + (
        'echo "HPC_CF_BUILDCACHE_STEP=publish"\n'
        f"{_installed_non_external_hashes(env_name, exclude_specs=exclude_specs)}"
        f"{_coverage_hashes(env_name, exclude_specs=exclude_specs)}"
        'if ((${#installed_hashes[@]} < ${#spec_hashes[@]})); then\n'
        '    echo "HPC_CF_PARTIAL_PUBLISH=1"\n'
        "fi\n"
        f"spack -e {env} mirror remove binary-cache >/dev/null 2>&1 || true\n"
        f"spack -e {env} mirror add --unsigned{cred_flags} binary-cache "
        f"{shlex.quote(mirror_url)}\n"
        f"spack -e {env} buildcache push --unsigned --fail-fast binary-cache "
        '"${installed_hashes[@]}"\n'
        'echo "HPC_CF_PUSHED_SPEC_COUNT=${#installed_hashes[@]}"\n'
        'echo "HPC_CF_BUILDCACHE_STEP=oci-count-check"\n'
        "if ((${#installed_hashes[@]} == ${#spec_hashes[@]})); then\n"
        '    echo "HPC_CF_CHECKED_SPEC_COUNT=${#spec_hashes[@]}"\n'
        "else\n"
        '    echo "HPC_CF_OCI_INCOMPLETE_COVERAGE=1"\n'
        '    echo "oci coverage incomplete: pushed ${#installed_hashes[@]}"\n'
        '         "of ${#spec_hashes[@]} non-external concrete specs" >&2\n'
        "    exit 1\n"
        "fi\n"
    )

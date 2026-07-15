"""env.yaml discovery helpers and static preflight validators.

Validators raise on errors for backwards compatibility. Prefer
:mod:`hpc_cf.validation` (:func:`~hpc_cf.validation.validate_environment`)
for structured ``ValidationReport`` output and profile selection.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from hpc_cf.config import DEFAULT_SPACK_VERSION
from hpc_cf.environment import (
    EnvironmentSpec,
    load_environment_spec,
    load_environment_spec_from_template,
    parse_environment_spec,
)
from hpc_cf.execution import ProjectLayout
from hpc_cf.validation import (
    ValidationProfile,
    assert_valid,
    collect_branch_consistency,
    collect_manual_packages,
    collect_spack_assets,
    collect_spack_yaml,
)

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
    inconsistently in 3 places with REVERSED order).
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
    """Deprecated: load env.yaml as a dict for template rendering.

    Prefer :func:`hpc_cf.environment.load_environment_spec` /
    :func:`load_environment_spec_from_template`.
    """
    logger.warning(
        "load_env_yaml is deprecated; use hpc_cf.environment.load_environment_spec"
    )
    if not template_path:
        return {}
    spec = load_environment_spec_from_template(template_path)
    if spec is None:
        return {}
    return spec.as_dict()


def list_available_envs(*, layout: ProjectLayout | None = None) -> list[str]:
    """List environment directories under spack-envs/ that contain env.yaml."""
    return (layout or ProjectLayout.default()).list_env_names()


def spack_version_for_env(
    env_name: str | None,
    *,
    layout: ProjectLayout | None = None,
) -> str:
    """Read spack.version from the given env's env.yaml.

    Returns :data:`DEFAULT_SPACK_VERSION` when env_name is None or env.yaml
    has no version.
    """
    if not env_name:
        return DEFAULT_SPACK_VERSION
    root = layout or ProjectLayout.default()
    env_dir = root.spack_envs_dir / env_name
    try:
        return load_environment_spec(env_dir).spack.version
    except FileNotFoundError:
        return DEFAULT_SPACK_VERSION


def _as_spec(env_config: dict | EnvironmentSpec) -> EnvironmentSpec:
    if isinstance(env_config, EnvironmentSpec):
        return env_config
    return parse_environment_spec(env_config, source="<dict>")


def _layout() -> ProjectLayout:
    return ProjectLayout.default()


def validate_manual_packages(env_config: dict | EnvironmentSpec) -> None:
    """Validate manual_packages entries from env.yaml.

    Each entry's ``file`` is resolved relative to the project root.
    If sha256 is provided, the checksum is verified.  Raises on missing
    file or checksum mismatch; warns when sha256 is absent.
    """
    import hashlib

    from hpc_cf.validation import ValidationReport, ValidationSeverity

    spec = _as_spec(env_config)
    root = _layout()
    findings = collect_manual_packages(spec, project_root=root.project_root)
    report = ValidationReport(profile="manual_packages")
    report.extend(findings)
    for f in findings:
        if f.severity is ValidationSeverity.WARNING:
            logger.warning("%s", f.message)
    report.raise_if_errors()
    for mp in spec.manual_packages:
        mp_file = root.project_root / mp.file
        if mp.sha256 and mp_file.is_file():
            actual = hashlib.sha256(mp_file.read_bytes()).hexdigest()
            if actual == mp.sha256:
                logger.info("✅ manual_packages: '%s' sha256 verified", mp.file)


def validate_spack_assets(env_config: dict | EnvironmentSpec) -> None:
    """Verify the Spack tarball and bootstrap cache exist before an expensive build.

    The Dockerfile ``COPY assets/spack-v<ver>.tar.gz`` and
    ``COPY assets/bootstrap-<ver>`` fail the build if these are missing; this
    check surfaces the problem early. Skipped when the build method does not
    require Spack assets (see :attr:`BuildMethod.requires_spack_assets`).
    """
    from hpc_cf.validation import ValidationReport, ValidationSeverity

    spec = _as_spec(env_config)
    findings = collect_spack_assets(spec, assets_dir=_layout().assets_dir)
    report = ValidationReport(profile="spack_assets")
    report.extend(findings)
    for f in findings:
        if f.severity is ValidationSeverity.WARNING:
            logger.warning("%s", f.message)
    report.raise_if_errors()


def validate_branch_consistency(env_dir: Path) -> None:
    """Ensure the cp2k git branch is parametrized consistently.

    After the 3.3 refactor every env's Dockerfile.j2 clones with
    ``-b {{ cp2k_branch }}`` and env.yaml declares it under template_vars.
    This catches a regression where someone re-hardcodes the branch or
    forgets the template_vars entry (which would fail under StrictUndefined).
    """
    from hpc_cf.validation import ValidationReport

    findings = collect_branch_consistency(env_dir)
    report = ValidationReport(profile="branch")
    report.extend(findings)
    report.raise_if_errors()


def validate_spack_yaml(env_dir: Path) -> None:
    """Basic spack.yaml sanity: parses; if repos.builtin.commit is set it
    must be a 40-char hex string."""
    from hpc_cf.validation import ValidationReport, ValidationSeverity

    findings = collect_spack_yaml(env_dir)
    report = ValidationReport(profile="spack_yaml")
    report.extend(findings)
    for f in findings:
        if f.severity is ValidationSeverity.WARNING:
            logger.warning("%s", f.message)
        elif f.severity is ValidationSeverity.INFO:
            logger.info(
                "Tip: add 'repos: builtin: commit: <sha>' to %s for reproducible "
                "concretization (prevents builtin recipe drift between builds).",
                f.path or env_dir,
            )
    report.raise_if_errors()


def run_static_checks(
    env_dir: Path,
    env_config: dict | EnvironmentSpec | None = None,
    *,
    profile: ValidationProfile | str = ValidationProfile.BUILD_INPUT,
    layout: ProjectLayout | None = None,
    allow_reconcretize: bool = False,
) -> None:
    """Run a validation profile and raise on errors.

    Default is ``build-input`` (pre-build suite). Shared by ``validate`` and
    ``build``. Use ``config`` for render-only paths that must not require
    large assets.
    """
    assert_valid(
        env_dir,
        profile,
        env_config=env_config,
        layout=layout or _layout(),
        allow_reconcretize=allow_reconcretize,
    )

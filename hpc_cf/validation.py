"""Structured validation profiles, findings, and reports.

Three profiles keep cheap render checks separate from build/asset
preconditions that need large on-disk inputs:

* ``config`` / ``template`` — schema, Dockerfile branch params, spack.yaml
  sanity.  Run on every Dockerfile render.
* ``build-input`` — config plus manual_packages, Spack tarball, and a
  non-empty ``spack.lock`` (unless ``--allow-reconcretize``).  Run before
  ``build``.
* ``assets`` — schema, spack.yaml, and Spack assets needed to prepare
  bootstrap/mirror.  Run before ``assets`` workflows that touch an env.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise ImportError(f"Required package not installed: {exc}. Install: pip install pyyaml") from exc

from hpc_cf.environment import (
    EnvironmentSpec,
    load_environment_spec,
    parse_environment_spec,
)
from hpc_cf.execution import ProjectLayout

logger = logging.getLogger(__name__)


class ValidationSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class ValidationProfile(str, Enum):
    """Named check suites selected by workflow."""

    CONFIG = "config"
    BUILD_INPUT = "build-input"
    ASSETS = "assets"

    @classmethod
    def parse(cls, value: str | ValidationProfile) -> ValidationProfile:
        if isinstance(value, ValidationProfile):
            return value
        # Accept "template" as an alias for the render-time profile.
        if value in ("template", "config/template"):
            return cls.CONFIG
        try:
            return cls(value)
        except ValueError as exc:
            known = ", ".join(p.value for p in cls)
            raise ValueError(
                f"Unknown validation profile {value!r}; expected one of: {known}"
            ) from exc


@dataclass(frozen=True)
class ValidationFinding:
    """One stable-coded check result."""

    code: str
    severity: ValidationSeverity
    message: str
    path: str | None = None
    fix_hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "path": self.path,
            "fix_hint": self.fix_hint,
        }


@dataclass
class ValidationReport:
    """Collected findings for one profile run."""

    profile: str
    env_name: str | None = None
    findings: list[ValidationFinding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(f.severity is ValidationSeverity.ERROR for f in self.findings)

    def errors(self) -> list[ValidationFinding]:
        return [f for f in self.findings if f.severity is ValidationSeverity.ERROR]

    def warnings(self) -> list[ValidationFinding]:
        return [f for f in self.findings if f.severity is ValidationSeverity.WARNING]

    def add(self, finding: ValidationFinding) -> None:
        self.findings.append(finding)

    def extend(self, findings: list[ValidationFinding]) -> None:
        self.findings.extend(findings)

    def raise_if_errors(self) -> None:
        """Raise the first error as a concrete exception for CLI abort."""
        errs = self.errors()
        if not errs:
            return
        first = errs[0]
        # Prefer legacy exception types expected by callers/tests.
        if first.code.startswith("spack_assets.") or first.code in (
            "manual_packages.missing",
            "spack_lock.missing",
        ):
            raise FileNotFoundError(_format_finding(first))
        raise ValueError(_format_finding(first))

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "profile": self.profile,
            "env": self.env_name,
            "error_count": len(self.errors()),
            "warning_count": len(self.warnings()),
            "findings": [f.to_dict() for f in self.findings],
        }

    def format_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False) + "\n"

    def format_text(self) -> str:
        lines: list[str] = []
        header = f"profile={self.profile}"
        if self.env_name:
            header += f" env={self.env_name}"
        lines.append(header)
        if not self.findings:
            lines.append("OK — no findings")
            return "\n".join(lines) + "\n"
        for f in self.findings:
            lines.append(_format_finding(f))
        status = "OK" if self.ok else "FAILED"
        lines.append(
            f"{status} — {len(self.errors())} error(s), "
            f"{len(self.warnings())} warning(s)"
        )
        return "\n".join(lines) + "\n"


def _format_finding(f: ValidationFinding) -> str:
    bits = [f"[{f.severity.value}] {f.code}: {f.message}"]
    if f.path:
        bits.append(f"  path: {f.path}")
    if f.fix_hint:
        bits.append(f"  fix: {f.fix_hint}")
    return "\n".join(bits)


def _schema_finding(message: str, path: str) -> ValidationFinding:
    return ValidationFinding(
        code="schema.invalid",
        severity=ValidationSeverity.ERROR,
        message=message,
        path=path,
        fix_hint="Fix env.yaml to match EnvironmentSpec schema_version 1.",
    )


def _as_spec(
    env_dir: Path,
    env_config: dict | EnvironmentSpec | None,
) -> tuple[EnvironmentSpec | None, list[ValidationFinding]]:
    """Load or adapt a spec; schema/parser failures become findings.

    Converting exceptions (instead of raising) keeps ``--format json`` output
    valid even when env.yaml is malformed.
    """
    findings: list[ValidationFinding] = []
    if isinstance(env_config, EnvironmentSpec):
        return env_config, findings
    if isinstance(env_config, dict):
        try:
            return parse_environment_spec(env_config, source=str(env_dir)), findings
        except (ValueError, TypeError) as exc:
            findings.append(_schema_finding(str(exc), str(env_dir)))
            return None, findings
    try:
        return load_environment_spec(env_dir), findings
    except FileNotFoundError as exc:
        findings.append(
            ValidationFinding(
                code="schema.env_yaml_missing",
                severity=ValidationSeverity.ERROR,
                message=str(exc),
                path=str(env_dir),
                fix_hint="Add spack-env-file/env.yaml (or env.yaml) under the env directory.",
            )
        )
        return None, findings
    except (ValueError, TypeError, yaml.YAMLError) as exc:
        findings.append(_schema_finding(str(exc), str(env_dir)))
        return None, findings


def collect_manual_packages(
    spec: EnvironmentSpec,
    *,
    project_root: Path,
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for mp in spec.manual_packages:
        rel_path = mp.file
        mp_file = project_root / rel_path
        if not mp_file.exists():
            findings.append(
                ValidationFinding(
                    code="manual_packages.missing",
                    severity=ValidationSeverity.ERROR,
                    message=f"manual_packages file not found: {rel_path}",
                    path=str(mp_file),
                    fix_hint="Place the file in the project before building.",
                )
            )
            continue
        if not mp.sha256:
            findings.append(
                ValidationFinding(
                    code="manual_packages.no_sha256",
                    severity=ValidationSeverity.WARNING,
                    message=(
                        f"manual_packages '{rel_path}' has no sha256 — "
                        "reproducibility cannot be guaranteed"
                    ),
                    path=str(mp_file),
                    fix_hint="Add sha256 under manual_packages in env.yaml.",
                )
            )
            continue
        actual = hashlib.sha256(mp_file.read_bytes()).hexdigest()
        if actual != mp.sha256:
            findings.append(
                ValidationFinding(
                    code="manual_packages.sha256_mismatch",
                    severity=ValidationSeverity.ERROR,
                    message=(
                        f"sha256 mismatch for '{rel_path}' "
                        f"(expected {mp.sha256}, actual {actual})"
                    ),
                    path=str(mp_file),
                    fix_hint="Update env.yaml or replace the file.",
                )
            )
    return findings


def collect_spack_assets(
    spec: EnvironmentSpec,
    *,
    assets_dir: Path,
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    if not spec.method.requires_spack_assets:
        return findings
    spack_version = spec.spack.version
    if not spack_version:
        return findings

    tarball = assets_dir / f"spack-v{spack_version}.tar.gz"
    if not tarball.exists():
        findings.append(
            ValidationFinding(
                code="spack_assets.tarball_missing",
                severity=ValidationSeverity.ERROR,
                message=f"Spack tarball not found: {tarball}",
                path=str(tarball),
                fix_hint=(
                    f"env.yaml declares spack.version={spack_version!r}. "
                    "Place the tarball under assets/ (or run assets bootstrap) "
                    "before build/assets — Dockerfile COPY would fail otherwise."
                ),
            )
        )

    bootstrap = assets_dir / f"bootstrap-{spack_version}"
    if not bootstrap.is_dir():
        findings.append(
            ValidationFinding(
                code="spack_assets.bootstrap_missing",
                severity=ValidationSeverity.WARNING,
                message=f"Bootstrap cache missing: {bootstrap}",
                path=str(bootstrap),
                fix_hint=(
                    "Run `python -m hpc_cf assets --prepare-bootstrap` "
                    "(Dockerfile COPYs it)."
                ),
            )
        )
    return findings


def collect_branch_consistency(env_dir: Path) -> list[ValidationFinding]:
    """CP2K-only: parametrized ``cp2k_branch`` must match Dockerfile clones.

    Non-CP2K envs (no ``cp2k_branch`` template var and no cp2k git clone)
    are skipped entirely so VASP/ABACUS do not run CP2K-centric checks.
    """
    findings: list[ValidationFinding] = []
    dockerfile = env_dir / "Dockerfile.j2"
    if not dockerfile.exists():
        return findings
    text = dockerfile.read_text(encoding="utf-8")
    uses_var = "{{ cp2k_branch }}" in text
    joined = text.replace("\\\n", " ")
    clone_lines = [
        line
        for line in joined.splitlines()
        if "git clone" in line and "cp2k" in line and "-b " in line
    ]

    tv: dict[str, Any] = {}
    env_yaml_path: Path | None = None
    try:
        spec = load_environment_spec(env_dir)
        env_yaml_path = spec.source_path
        tv = spec.template_vars
    except (FileNotFoundError, ValueError):
        pass

    if not uses_var and not clone_lines and "cp2k_branch" not in tv:
        return findings

    if uses_var and "cp2k_branch" not in tv:
        where = f" ({env_yaml_path})" if env_yaml_path else ""
        findings.append(
            ValidationFinding(
                code="branch.missing_template_var",
                severity=ValidationSeverity.ERROR,
                message=(
                    f"Dockerfile.j2 uses {{{{ cp2k_branch }}}} but env.yaml{where} "
                    "does not declare it under template_vars"
                ),
                path=str(dockerfile),
                fix_hint="Add template_vars.cp2k_branch in env.yaml.",
            )
        )

    for line in clone_lines:
        if "{{ cp2k_branch }}" not in line:
            findings.append(
                ValidationFinding(
                    code="branch.hardcoded",
                    severity=ValidationSeverity.ERROR,
                    message=(
                        "cp2k git clone hardcodes a branch instead of "
                        "{{ cp2k_branch }}"
                    ),
                    path=str(dockerfile),
                    fix_hint=f"Use -b {{{{ cp2k_branch }}}}: {line.strip()}",
                )
            )
    return findings


def collect_spack_lock(
    env_dir: Path,
    *,
    allow_reconcretize: bool = False,
) -> list[ValidationFinding]:
    """Require a non-empty spack.lock for image builds (fail-closed).

    When *allow_reconcretize* is true the missing lock is a warning so
    ``--allow-reconcretize`` builds can proceed knowingly.
    """
    candidates = [
        env_dir / "spack-env-file" / "spack.lock",
        env_dir / "spack.lock",
    ]
    lock = next(
        (c for c in candidates if c.is_file() and c.stat().st_size > 0),
        None,
    )
    if lock is not None:
        return []

    existing = next((c for c in candidates if c.exists()), None)
    path = str(existing or candidates[0])
    severity = (
        ValidationSeverity.WARNING
        if allow_reconcretize
        else ValidationSeverity.ERROR
    )
    return [
        ValidationFinding(
            code="spack_lock.missing",
            severity=severity,
            message=(
                "spack.lock missing or empty (required for method=spack "
                "image builds unless --allow-reconcretize)"
            ),
            path=path,
            fix_hint=(
                "Run `python -m hpc_cf assets --env <name> --allow-concretize` "
                "to produce a lock, or pass --allow-reconcretize to "
                "build/dockerfile (install may re-concretize)."
            ),
        )
    ]


def collect_spack_yaml(env_dir: Path) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    candidates = [env_dir / "spack-env-file" / "spack.yaml", env_dir / "spack.yaml"]
    spack_yaml = next((c for c in candidates if c.exists()), None)
    if spack_yaml is None:
        findings.append(
            ValidationFinding(
                code="spack_yaml.missing",
                severity=ValidationSeverity.ERROR,
                message="spack.yaml not found (required for method=spack)",
                path=str(env_dir),
                fix_hint=(
                    "Add spack.yaml next to env.yaml "
                    "(or under spack-env-file/)."
                ),
            )
        )
        return findings
    try:
        data = yaml.safe_load(spack_yaml.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        findings.append(
            ValidationFinding(
                code="spack_yaml.invalid",
                severity=ValidationSeverity.ERROR,
                message=f"spack.yaml is not valid YAML: {exc}",
                path=str(spack_yaml),
                fix_hint="Fix YAML syntax in spack.yaml.",
            )
        )
        return findings

    builtin = ((data.get("spack", {}) or {}).get("repos", {}) or {}).get("builtin", {}) or {}
    commit = builtin.get("commit")
    if commit is not None and not (isinstance(commit, str) and len(commit) == 40):
        findings.append(
            ValidationFinding(
                code="spack_yaml.commit_format",
                severity=ValidationSeverity.WARNING,
                message=f"repos.builtin.commit is not a 40-char hex string: {commit!r}",
                path=str(spack_yaml),
                fix_hint="Set repos.builtin.commit to a full 40-character git SHA.",
            )
        )
    elif commit is None:
        findings.append(
            ValidationFinding(
                code="spack_yaml.no_commit",
                severity=ValidationSeverity.INFO,
                message="repos.builtin.commit is unset",
                path=str(spack_yaml),
                fix_hint=(
                    "Add 'repos: builtin: commit: <sha>' for reproducible "
                    "concretization."
                ),
            )
        )
    return findings


def validate_environment(
    env_dir: Path,
    profile: ValidationProfile | str = ValidationProfile.BUILD_INPUT,
    *,
    env_config: dict | EnvironmentSpec | None = None,
    layout: ProjectLayout | None = None,
    allow_reconcretize: bool = False,
) -> ValidationReport:
    """Run the named validation profile and return a structured report."""
    profile = ValidationProfile.parse(profile)
    layout = layout or ProjectLayout.default()
    report = ValidationReport(profile=profile.value, env_name=env_dir.name)

    spec, schema_findings = _as_spec(env_dir, env_config)
    report.extend(schema_findings)
    if spec is None:
        return report

    # Shared config/template checks.
    if profile in (
        ValidationProfile.CONFIG,
        ValidationProfile.BUILD_INPUT,
        ValidationProfile.ASSETS,
    ):
        if profile is not ValidationProfile.ASSETS:
            report.extend(collect_branch_consistency(env_dir))
        if spec.method.runs_spack_validations:
            report.extend(collect_spack_yaml(env_dir))

    if profile is ValidationProfile.BUILD_INPUT:
        report.extend(
            collect_manual_packages(spec, project_root=layout.project_root)
        )
        if spec.method.runs_spack_validations:
            report.extend(
                collect_spack_assets(spec, assets_dir=layout.assets_dir)
            )
            report.extend(
                collect_spack_lock(
                    env_dir, allow_reconcretize=allow_reconcretize
                )
            )

    if profile is ValidationProfile.ASSETS:
        if spec.method.runs_spack_validations:
            report.extend(
                collect_spack_assets(spec, assets_dir=layout.assets_dir)
            )

    # Surface non-error findings via the logger (parity with legacy validators).
    for f in report.findings:
        if f.severity is ValidationSeverity.WARNING:
            logger.warning("%s", _format_finding(f))
        elif f.severity is ValidationSeverity.INFO:
            logger.info("%s", _format_finding(f))

    return report


def assert_valid(
    env_dir: Path,
    profile: ValidationProfile | str,
    *,
    env_config: dict | EnvironmentSpec | None = None,
    layout: ProjectLayout | None = None,
    allow_reconcretize: bool = False,
) -> ValidationReport:
    """Run a profile and raise on the first error finding."""
    report = validate_environment(
        env_dir,
        profile,
        env_config=env_config,
        layout=layout,
        allow_reconcretize=allow_reconcretize,
    )
    report.raise_if_errors()
    return report

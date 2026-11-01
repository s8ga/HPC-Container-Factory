"""Structured validation profiles, findings, and reports.

Three profiles keep cheap config checks separate from build/asset
preconditions that need large on-disk inputs:

* ``config`` / ``template`` — schema, Dockerfile branch params, spack.yaml
  sanity.  Used by ``validate --profile config`` (cheap static checks).
* ``build-input`` — config plus manual_packages, Spack tarball, complete
  bootstrap metadata, and a non-empty ``spack.lock`` (unless
  ``--allow-reconcretize``).  Run before ``build`` and ``dockerfile``.
* ``assets`` — schema, spack.yaml, and Spack assets needed to prepare
  bootstrap/mirror.  Run before ``assets`` workflows that touch an env.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar

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
from hpc_cf.lock_manifest import (
    LOCK_MANIFEST_FILENAME,
    compare_lock_inputs,
    read_lock_manifest,
)
from hpc_cf.repo_pins import check_env_repo_pins, expand_partial_includes

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BootstrapContract:
    """Single source of truth for a complete offline bootstrap mirror.

    Build-input validation and runtime ``SpackOps._verify_bootstrap`` share
    this required metadata set so preflight cannot pass a cache that later
    fails ``spack bootstrap now``.
    """

    BINARY_PACKAGES: ClassVar[tuple[str, ...]] = ("clingo", "gnupg", "patchelf")

    @classmethod
    def required_relative_paths(cls) -> tuple[Path, ...]:
        return (
            Path("metadata/sources/metadata.yaml"),
            Path("metadata/binaries/metadata.yaml"),
            *(Path(f"metadata/binaries/{name}.json") for name in cls.BINARY_PACKAGES),
        )

    @classmethod
    def required_paths(cls, bootstrap_dir: Path) -> tuple[Path, ...]:
        return tuple(bootstrap_dir / rel for rel in cls.required_relative_paths())

    @classmethod
    def invalid_paths(cls, bootstrap_dir: Path) -> list[Path]:
        """Return required paths that are missing or empty."""
        return [
            path
            for path in cls.required_paths(bootstrap_dir)
            if not path.is_file() or path.stat().st_size == 0
        ]

    @classmethod
    def is_complete(cls, bootstrap_dir: Path) -> bool:
        return not cls.invalid_paths(bootstrap_dir)


# Backward-compatible alias for callers that imported the old tuple.
BOOTSTRAP_METADATA_FILES = BootstrapContract.required_relative_paths()


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
        """Raise when any ERROR findings exist.

        The exception message aggregates all ERROR codes and counts (not
        just the first finding) so callers see the full failure set.
        Legacy exception types are preserved when every ERROR is a
        missing-file style code; otherwise ``ValueError`` is used.
        """
        errs = self.errors()
        if not errs:
            return

        def _is_missing_file(code: str) -> bool:
            return code.startswith("spack_assets.") or code in (
                "manual_packages.missing",
                "spack_lock.missing",
            )

        if len(errs) == 1:
            body = _format_finding(errs[0])
        else:
            codes = ", ".join(e.code for e in errs)
            details = "\n".join(_format_finding(e) for e in errs)
            body = (
                f"{len(errs)} validation errors ({codes}):\n{details}"
            )

        if all(_is_missing_file(e.code) for e in errs):
            raise FileNotFoundError(body)
        raise ValueError(body)

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
    from hpc_cf.shell_quote import confine_to_root

    findings: list[ValidationFinding] = []
    root = project_root.resolve()
    for mp in spec.manual_packages:
        rel_path = mp.file
        try:
            mp_file = confine_to_root(
                root / rel_path,
                root=root,
                label="manual_packages.file",
            )
        except ValueError as exc:
            findings.append(
                ValidationFinding(
                    code="manual_packages.path_escape",
                    severity=ValidationSeverity.ERROR,
                    message=str(exc),
                    path=rel_path,
                    fix_hint=(
                        "Use a path under the project root "
                        "(no '..' or absolute escapes)."
                    ),
                )
            )
            continue
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
    require_bootstrap: bool = False,
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
                    "Download it manually (QUICKSTART.md §2): "
                    "curl -fSL -o assets/spack-v"
                    f"{spack_version}.tar.gz "
                    "https://github.com/spack/spack/releases/download/v"
                    f"{spack_version}/spack-{spack_version}.tar.gz — "
                    "the assets bootstrap itself needs this tarball as input."
                ),
            )
        )

    bootstrap = assets_dir / f"bootstrap-{spack_version}"
    invalid_metadata = BootstrapContract.invalid_paths(bootstrap)
    if invalid_metadata:
        invalid_paths = ", ".join(str(path) for path in invalid_metadata)
        findings.append(
            ValidationFinding(
                code="spack_assets.bootstrap_missing",
                severity=(
                    ValidationSeverity.ERROR
                    if require_bootstrap
                    else ValidationSeverity.WARNING
                ),
                message=(
                    "Bootstrap cache missing or incomplete; expected non-empty "
                    f"metadata files: {invalid_paths}"
                ),
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


def collect_repo_pin_consistency(
    spec: EnvironmentSpec,
    env_dir: Path,
) -> list[ValidationFinding]:
    """Custom_repos ↔ template_vars pin consistency (dual-write guard).

    Same invariants as ``scripts/check-dual-write.py`` (see
    ``hpc_cf.repo_pins``): app-shaped CP2K/s8ga contracts plus key-name-
    agnostic pin-unity / repo-commit-var / staging-path / var-wiring checks.
    """
    dockerfile = env_dir / "Dockerfile.j2"
    dockerfile_text = (
        dockerfile.read_text(encoding="utf-8") if dockerfile.is_file() else ""
    )
    # Source-level checks look through {% include 'partials/...' %} so
    # partial-hosted clone/registration logic still counts as var usage.
    project_root = (
        env_dir.parent.parent
        if env_dir.parent.name == "spack-envs"
        else ProjectLayout.default().project_root
    )
    dockerfile_text = expand_partial_includes(dockerfile_text, project_root)
    errors = check_env_repo_pins(env_dir.name, spec, dockerfile_text)
    path = str(dockerfile if dockerfile.is_file() else env_dir)
    return [
        ValidationFinding(
            code="repo_pins.inconsistent",
            severity=ValidationSeverity.ERROR,
            message=error,
            path=path,
            fix_hint=(
                "env.yaml custom_repos and their template_vars copies must "
                "agree; run scripts/check-dual-write.py for the same "
                "diagnostics outside validate. Repo-pin bumps should go "
                "through `python -m hpc_cf pin set --env <name> --repo "
                "<ns> --commit <sha>` (atomic, guard-checked)."
            ),
        )
        for error in errors
    ]


def collect_buildcache_stage(
    spec: EnvironmentSpec,
    env_dir: Path,
) -> list[ValidationFinding]:
    """buildcache producer contract: enabled envs need the 4-stage template.

    ``buildcache build`` drives podman with ``--target builder-installed``
    and consumer renders expect the buildcache install partials; an enabled
    env on a 3-stage template dies at the podman step AND poisons the
    shared health state (round-2 audit 2.1c). Gate the template up front.
    """
    if not (spec.method.runs_spack_validations and spec.spack.buildcache.enabled):
        return []
    dockerfile = env_dir / "Dockerfile.j2"
    if not dockerfile.is_file():
        # Missing-template diagnostics belong to the render probe; nothing
        # stage-specific to check without a template.
        return []
    text = dockerfile.read_text(encoding="utf-8")
    if "AS builder-installed" in text:
        return []
    return [
        ValidationFinding(
            code="buildcache.dockerfile_missing_stage",
            severity=ValidationSeverity.ERROR,
            message=(
                "spack.buildcache.enabled is true but Dockerfile.j2 has no "
                "`AS builder-installed` stage — buildcache build would fail "
                "at podman --target and mark the shared cache unhealthy"
            ),
            path=str(dockerfile),
            fix_hint=(
                "Either migrate the template to the 4-stage producer shape "
                "(builder-installed + spack_buildcache_config.j2 + "
                "spack_buildcache_install.j2, see "
                "cp2k_opensource-2026.2-force-avx512) or set "
                "spack.buildcache.enabled: false."
            ),
        )
    ]


def is_nonempty_spack_lock(path: Path) -> bool:
    """Return True if *path* is a present, non-empty ``spack.lock`` file."""
    return path.is_file() and path.stat().st_size > 0


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
        (c for c in candidates if is_nonempty_spack_lock(c)),
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


def collect_lock_input_manifest(
    spec: EnvironmentSpec,
    env_dir: Path,
    *,
    allow_reconcretize: bool = False,
) -> list[ValidationFinding]:
    """Compare spack.lock provenance (lock-manifest.json) with current inputs.

    A non-empty lock alone proves nothing about WHICH inputs produced it.
    The manifest records the spack.yaml sha256 and env.yaml repo pins the
    concretizer saw; drift means the lock builds a stale dependency graph —
    an error unless ``--allow-reconcretize`` downgrades it, matching the
    missing-lock doctrine. Locks without a manifest (pre-dating the
    sidecar) get a warning until the next assets concretize refreshes it.
    """
    candidates = [
        env_dir / "spack-env-file" / "spack.lock",
        env_dir / "spack.lock",
    ]
    lock = next((c for c in candidates if is_nonempty_spack_lock(c)), None)
    if lock is None:
        return []  # collect_spack_lock owns the missing-lock finding

    try:
        manifest = read_lock_manifest(lock.parent)
    except ValueError as exc:
        return [
            ValidationFinding(
                code="lock.manifest_invalid",
                severity=ValidationSeverity.ERROR,
                message=str(exc),
                path=str(lock.parent / LOCK_MANIFEST_FILENAME),
                fix_hint=(
                    "Delete the manifest and re-run "
                    "`python -m hpc_cf assets --env <name> --allow-concretize` "
                    "to regenerate lock + manifest together."
                ),
            )
        ]
    if manifest is None:
        return [
            ValidationFinding(
                code="lock.provenance_unknown",
                severity=ValidationSeverity.WARNING,
                message=(
                    f"{lock.name} has no {LOCK_MANIFEST_FILENAME}: its inputs "
                    "(spack.yaml / custom_repos pins) cannot be verified"
                ),
                path=str(lock.parent / LOCK_MANIFEST_FILENAME),
                fix_hint=(
                    "Re-run `python -m hpc_cf assets --env <name> "
                    "--allow-concretize` to regenerate lock + manifest "
                    "together."
                ),
            )
        ]

    drift = compare_lock_inputs(manifest, spec, lock.parent)
    if not drift:
        return []
    severity = (
        ValidationSeverity.WARNING if allow_reconcretize else ValidationSeverity.ERROR
    )
    return [
        ValidationFinding(
            code="lock.stale_input",
            severity=severity,
            message="spack.lock predates its current inputs: " + "; ".join(drift),
            path=str(lock),
            fix_hint=(
                "Inputs changed after concretization. Re-run "
                "`python -m hpc_cf assets --env <name> --allow-concretize` "
                "(delete the stale lock first), or pass "
                "--allow-reconcretize to build knowingly."
            ),
        )
    ]


_COPY_LINE_RE = re.compile(r"^\s*COPY\s+(?P<rest>.+?)\s*$", re.MULTILINE)
_GLOB_CHARS = ("*", "?", "[")


def _copy_sources(rest: str) -> list[str]:
    """Extract source paths from the token stream after ``COPY``.

    Skips flag tokens (``--from=``-carrying lines are filtered by the
    caller; ``--chmod[=v]`` / ``--chown[=v]`` lose their value token too)
    and the final destination token. Returns [] when nothing is copyable
    from the build context (multi-stage ``--from`` sources, URLs).
    """
    if rest.startswith("["):
        try:
            entries = json.loads(rest)
        except json.JSONDecodeError:
            return []
        if not isinstance(entries, list) or len(entries) < 2:
            return []
        return [str(entry) for entry in entries[:-1]]
    tokens = rest.split()
    sources: list[str] = []
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token.startswith("--"):
            if "=" not in token:
                skip_next = True
            continue
        sources.append(token)
    return sources[:-1] if len(sources) >= 2 else []


def collect_render_copy_sources(
    rendered_text: str,
    project_root: Path,
    *,
    path: str = "<rendered Dockerfile>",
) -> list[ValidationFinding]:
    """Every COPY source in a rendered Dockerfile must exist in the context.

    COPY lines create dependencies the schema never sees (audit 2.9):
    ``libfakeintel/``, ``testsuite-file/``, scripts. A missing source used
    to surface only as a mid-build podman failure; it is a config error and
    fails closed right after render. Multi-stage ``COPY --from`` sources
    and URLs are image-side, not context-side, and are skipped.
    """
    findings: list[ValidationFinding] = []
    for match in _COPY_LINE_RE.finditer(rendered_text):
        rest = match.group("rest")
        if "--from=" in rest:
            continue
        for src in _copy_sources(rest):
            if "://" in src:
                continue
            if any(ch in src for ch in _GLOB_CHARS):
                if next(project_root.glob(src), None) is None:
                    findings.append(
                        ValidationFinding(
                            code="copy.source_missing",
                            severity=ValidationSeverity.ERROR,
                            message=f"COPY source glob matches nothing: {src}",
                            path=path,
                            fix_hint=(
                                f"Add the file under {project_root} or fix "
                                "the COPY line in the template."
                            ),
                        )
                    )
                continue
            if not (project_root / src).exists():
                findings.append(
                    ValidationFinding(
                        code="copy.source_missing",
                        severity=ValidationSeverity.ERROR,
                        message=f"COPY source not found: {src}",
                        path=path,
                        fix_hint=(
                            f"Add the file under {project_root} or fix the "
                            "COPY line in the template."
                        ),
                    )
                )
    return findings


def collect_mirror_content(layout: ProjectLayout) -> list[ValidationFinding]:
    """A configured-but-empty source mirror breaks the offline promise.

    An empty ``assets/spack-mirror`` silently falls back to upstream
    downloads when online and explodes mid-build when not — either way the
    air-gapped contract is gone without a word (audit 2.8). Warning, not
    error: online builds are a legitimate choice; the finding makes the
    fallback explicit. Factory metadata (``.hpc_cf/`` run manifests) does
    not count as mirror content.
    """
    mirror = layout.spack_mirror_dir
    if mirror.is_dir():
        content = [entry for entry in mirror.iterdir() if entry.name != ".hpc_cf"]
        if content:
            return []
        state = "no source content"
    else:
        state = "directory missing"
    return [
        ValidationFinding(
            code="mirror.empty",
            severity=ValidationSeverity.WARNING,
            message=f"source mirror {state}: {mirror}",
            path=str(mirror),
            fix_hint=(
                "Run `python -m hpc_cf assets --env <name>` to populate the "
                "mirror, or accept upstream fallback knowingly (online "
                "builds only)."
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
    # Length-only gate (not hex alphabet). Message must match this check.
    if commit is not None and not (isinstance(commit, str) and len(commit) == 40):
        findings.append(
            ValidationFinding(
                code="spack_yaml.commit_format",
                severity=ValidationSeverity.WARNING,
                message=(
                    f"repos.builtin.commit is not exactly 40 characters: {commit!r}"
                ),
                path=str(spack_yaml),
                fix_hint=(
                    "Set repos.builtin.commit to a 40-character string "
                    "(typically a full git SHA; this check validates length only)."
                ),
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
            report.extend(collect_repo_pin_consistency(spec, env_dir))
            report.extend(collect_buildcache_stage(spec, env_dir))
        if spec.method.runs_spack_validations:
            report.extend(collect_spack_yaml(env_dir))

    if profile is ValidationProfile.BUILD_INPUT:
        report.extend(
            collect_manual_packages(spec, project_root=layout.project_root)
        )
        if spec.method.runs_spack_validations:
            report.extend(
                collect_spack_assets(
                    spec,
                    assets_dir=layout.assets_dir,
                    require_bootstrap=True,
                )
            )
            report.extend(collect_mirror_content(layout))
            report.extend(
                collect_spack_lock(
                    env_dir, allow_reconcretize=allow_reconcretize
                )
            )
            report.extend(
                collect_lock_input_manifest(
                    spec,
                    env_dir,
                    allow_reconcretize=allow_reconcretize,
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

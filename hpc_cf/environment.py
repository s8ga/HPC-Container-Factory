"""Versioned EnvironmentSpec — authoritative env.yaml model and parser.

``EnvironmentSpec`` is the single typed representation of an environment's
``env.yaml``. Callers should prefer :func:`load_environment_spec` /
:func:`parse_environment_spec` over the legacy dict loaders in
:mod:`hpc_cf.env` and :func:`hpc_cf.spack_ops.load_env_config`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise ImportError(f"Required package not installed: {exc}. Install: pip install pyyaml") from exc

from hpc_cf.config import DEFAULT_SPACK_VERSION

logger = logging.getLogger(__name__)

SUPPORTED_SCHEMA_VERSION = 1


# ── Build method policy ──────────────────────────────────────────────────


class BuildMethod(Enum):
    """Build-mode discriminator and centralized policy for assets/templates."""

    SPACK = "spack"
    NO_SPACK = "no_spack"

    @classmethod
    def parse(cls, value: str | BuildMethod | None) -> BuildMethod:
        if isinstance(value, BuildMethod):
            return value
        if value is None or value == "":
            return cls.SPACK
        if not isinstance(value, str):
            raise ValueError(
                f"env.yaml method must be a string; got {type(value).__name__}"
            )
        try:
            return cls(value)
        except ValueError as exc:
            known = ", ".join(m.value for m in cls)
            raise ValueError(
                f"Unknown env.yaml method {value!r}; expected one of: {known}"
            ) from exc

    @property
    def requires_spack_assets(self) -> bool:
        """Whether assets workflow / COPY of spack tarball+bootstrap apply."""
        return self is BuildMethod.SPACK

    @property
    def default_template(self) -> str | None:
        """Shared template filename under ``templates/`` when per-env is absent."""
        if self is BuildMethod.NO_SPACK:
            return "Dockerfile.nospack.j2"
        return None

    @property
    def allows_mirror(self) -> bool:
        """Whether ``use_mirror`` is meaningful for this method."""
        return self is BuildMethod.SPACK

    @property
    def runs_spack_validations(self) -> bool:
        """Whether spack.yaml / spack asset preflight checks apply."""
        return self is BuildMethod.SPACK


# ── Nested config types ──────────────────────────────────────────────────


class RepoPhase(Enum):
    """Which workflows a custom repo applies to.

    ``IMAGE`` is for Dockerfile-/build-only repos (e.g. CP2K 2026.2
    force-AVX512 overrides that must not affect assets concretization).
    """

    ASSETS = "assets"
    IMAGE = "image"
    BOTH = "both"

    @classmethod
    def parse(cls, value: str | RepoPhase | None) -> RepoPhase:
        if isinstance(value, RepoPhase):
            return value
        if value is None or value == "":
            return cls.BOTH
        if not isinstance(value, str):
            raise ValueError(
                f"custom_repos phases must be a string; got {type(value).__name__}"
            )
        try:
            return cls(value)
        except ValueError as exc:
            known = ", ".join(m.value for m in cls)
            raise ValueError(
                f"Unknown custom_repos phases {value!r}; expected one of: {known}"
            ) from exc

    def applies_to(self, stage: Literal["assets", "image"]) -> bool:
        if self is RepoPhase.BOTH:
            return True
        return self.value == stage


class RepoScope(Enum):
    """Spack ``repo add --scope`` target."""

    ENV = "env"
    SITE = "site"

    @classmethod
    def parse(cls, value: str | RepoScope | None, *, default: RepoScope) -> RepoScope:
        if isinstance(value, RepoScope):
            return value
        if value is None or value == "":
            return default
        if not isinstance(value, str):
            raise ValueError(
                f"repo_scope must be a string; got {type(value).__name__}"
            )
        try:
            return cls(value)
        except ValueError as exc:
            known = ", ".join(m.value for m in cls)
            raise ValueError(
                f"Unknown repo_scope {value!r}; expected one of: {known}"
            ) from exc


class BuildcachePolicy(Enum):
    """Spack binary-cache install policy, independent of source mirrors."""

    NEVER = "never"
    AUTO = "auto"
    ONLY = "only"

    @classmethod
    def parse(cls, value: str | BuildcachePolicy | None) -> BuildcachePolicy:
        if isinstance(value, BuildcachePolicy):
            return value
        if value is None or value == "":
            return cls.NEVER
        if not isinstance(value, str):
            raise ValueError(
                f"buildcache policy must be a string; got {type(value).__name__}"
            )
        try:
            return cls(value)
        except ValueError as exc:
            known = ", ".join(item.value for item in cls)
            raise ValueError(
                f"Unknown buildcache policy {value!r}; expected one of: {known}"
            ) from exc


class BuildcacheCoverage(Enum):
    """Set of concrete specs expected to have binary packages.

    External specs are intentionally excluded: Spack does not push external
    compiler/runtime packages, so treating ``all_specs()`` as cacheable would
    make strict coverage checks fail for valid caches.
    """

    NON_EXTERNAL = "non_external"

    @classmethod
    def parse(
        cls,
        value: str | BuildcacheCoverage | None,
    ) -> BuildcacheCoverage:
        if isinstance(value, BuildcacheCoverage):
            return value
        if value is None or value == "":
            return cls.NON_EXTERNAL
        if not isinstance(value, str):
            raise ValueError(
                f"buildcache coverage must be a string; got {type(value).__name__}"
            )
        try:
            return cls(value)
        except ValueError as exc:
            raise ValueError(
                "Unknown buildcache coverage "
                f"{value!r}; expected: {cls.NON_EXTERNAL.value}"
            ) from exc


@dataclass
class BuildcacheConfig:
    """Per-environment binary-cache contract."""

    enabled: bool = False
    padded_length: int = 128
    policy: BuildcachePolicy = BuildcachePolicy.NEVER
    coverage: BuildcacheCoverage = BuildcacheCoverage.NON_EXTERNAL


@dataclass
class ImageConfig:
    builder: str = "debian:trixie"
    runtime: str = "debian:trixie-slim"
    output_name: str | None = None
    output_tag: str | None = None


@dataclass
class CustomRepo:
    type: Literal["git", "local"]
    namespace: str
    url: str | None = None
    branch: str | None = None
    sparse_path: str | None = None
    path: str | None = None
    # Which workflows register this repo (default: assets + image).
    phases: RepoPhase = RepoPhase.BOTH
    # Absolute path used by Dockerfile ``spack repo add`` (optional).
    # When unset, :func:`hpc_cf.spack_plan.default_image_path` derives one.
    image_path: str | None = None


@dataclass
class SpackPhasePolicy:
    """Per-workflow Spack contract (assets vs image/Dockerfile)."""

    update_builtin: bool = True
    repo_scope: RepoScope = RepoScope.ENV


@dataclass
class SpackConfig:
    version: str
    env_name: str
    custom_repos: list[CustomRepo] = field(default_factory=list)
    # Assets (concretize/mirror) default: env scope + update builtin.
    assets: SpackPhasePolicy = field(
        default_factory=lambda: SpackPhasePolicy(
            update_builtin=True,
            repo_scope=RepoScope.ENV,
        )
    )
    # Image/Dockerfile default: site scope + update builtin (historical
    # template behavior). Overrides make intentional differences visible
    # (e.g. VASP skips update_builtin; CP2K 2026.2 uses env scope).
    image: SpackPhasePolicy = field(
        default_factory=lambda: SpackPhasePolicy(
            update_builtin=True,
            repo_scope=RepoScope.SITE,
        )
    )
    buildcache: BuildcacheConfig = field(default_factory=BuildcacheConfig)


@dataclass
class MirrorBuilderConfig:
    system_pkgs: list[str] = field(default_factory=list)
    pkg_mirror_setup: str = ""
    pkg_install_cmd: str = ""


@dataclass
class ManualPackage:
    file: str
    dest: str | None = None
    sha256: str | None = None


@dataclass
class RuntimeConfig:
    """``method: no_spack`` runtime stage knobs."""

    copy_dirs: list[str] = field(default_factory=list)
    extra_pkgs: list[str] = field(default_factory=list)


@dataclass
class EnvironmentSpec:
    """Complete typed representation of an env.yaml (schema v1)."""

    schema_version: int = SUPPORTED_SCHEMA_VERSION
    method: BuildMethod = BuildMethod.SPACK
    images: ImageConfig = field(default_factory=ImageConfig)
    spack: SpackConfig = field(
        default_factory=lambda: SpackConfig(
            version=DEFAULT_SPACK_VERSION,
            # Programmatic construction must set env_name; YAML parse requires
            # it for method=spack (no implicit "cp2k-env" default).
            env_name="",
        )
    )
    mirror_builder: MirrorBuilderConfig = field(default_factory=MirrorBuilderConfig)
    manual_packages: list[ManualPackage] = field(default_factory=list)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    script: str = ""
    template_vars: dict[str, Any] = field(default_factory=dict)
    source_path: Path | None = None

    # Path helpers used by SpackOps (formerly on EnvConfig).
    @property
    def spack_user_dir_name(self) -> str:
        return f".spack-v{self.spack.version}"

    @property
    def spack_user_dir_in_container(self) -> str:
        return f"/work/assets/{self.spack_user_dir_name}"

    @property
    def bootstrap_dir_name(self) -> str:
        return f"bootstrap-{self.spack.version}"

    @property
    def bootstrap_dir_in_container(self) -> str:
        return f"/work/assets/{self.bootstrap_dir_name}"

    def as_dict(self) -> dict[str, Any]:
        """Serialize to an env.yaml-shaped dict (for deprecated loaders)."""
        images: dict[str, Any] = {
            "builder": self.images.builder,
            "runtime": self.images.runtime,
        }
        if self.images.output_name is not None:
            images["output_name"] = self.images.output_name
        if self.images.output_tag is not None:
            images["output_tag"] = self.images.output_tag

        repos: list[dict[str, Any]] = []
        for r in self.spack.custom_repos:
            if r.type == "git":
                entry: dict[str, Any] = {
                    "url": r.url,
                    "namespace": r.namespace,
                }
                if r.branch is not None:
                    entry["branch"] = r.branch
                if r.sparse_path is not None:
                    entry["sparse_path"] = r.sparse_path
            else:
                entry = {"path": r.path, "namespace": r.namespace}
            if r.phases is not RepoPhase.BOTH:
                entry["phases"] = r.phases.value
            if r.image_path is not None:
                entry["image_path"] = r.image_path
            repos.append(entry)

        manual: list[dict[str, Any]] = []
        for mp in self.manual_packages:
            entry = {"file": mp.file}
            if mp.dest is not None:
                entry["dest"] = mp.dest
            if mp.sha256 is not None:
                entry["sha256"] = mp.sha256
            manual.append(entry)

        return {
            "schema_version": self.schema_version,
            "method": self.method.value,
            "images": images,
            "spack": {
                "version": self.spack.version,
                "env_name": self.spack.env_name,
                "assets": {
                    "update_builtin": self.spack.assets.update_builtin,
                    "repo_scope": self.spack.assets.repo_scope.value,
                },
                "image": {
                    "update_builtin": self.spack.image.update_builtin,
                    "repo_scope": self.spack.image.repo_scope.value,
                },
                "buildcache": {
                    "enabled": self.spack.buildcache.enabled,
                    "padded_length": self.spack.buildcache.padded_length,
                    "policy": self.spack.buildcache.policy.value,
                    "coverage": self.spack.buildcache.coverage.value,
                },
                "custom_repos": repos,
            },
            "mirror_builder": {
                "system_pkgs": list(self.mirror_builder.system_pkgs),
                "pkg_mirror_setup": self.mirror_builder.pkg_mirror_setup,
                "pkg_install_cmd": self.mirror_builder.pkg_install_cmd,
            },
            "manual_packages": manual,
            "runtime": {
                "copy_dirs": list(self.runtime.copy_dirs),
                "extra_pkgs": list(self.runtime.extra_pkgs),
            },
            "script": self.script,
            "template_vars": dict(self.template_vars),
        }


# ── Parsing (fail-closed) ────────────────────────────────────────────────

_TOP_LEVEL_KEYS = frozenset({
    "schema_version",
    "method",
    "images",
    "spack",
    "mirror_builder",
    "manual_packages",
    "runtime",
    "script",
    "template_vars",
})
_IMAGES_KEYS = frozenset({"builder", "runtime", "output_name", "output_tag"})
_SPACK_KEYS = frozenset({
    "version", "env_name", "custom_repos", "assets", "image", "buildcache",
})
_PHASE_POLICY_KEYS = frozenset({"update_builtin", "repo_scope"})
_BUILDCACHE_KEYS = frozenset({
    "enabled", "padded_length", "policy", "coverage",
})
_CUSTOM_REPO_KEYS = frozenset({
    "url", "branch", "sparse_path", "namespace", "path", "phases", "image_path",
})
_MIRROR_BUILDER_KEYS = frozenset({
    "system_pkgs", "pkg_mirror_setup", "pkg_install_cmd",
})
_MANUAL_PACKAGE_KEYS = frozenset({"file", "dest", "sha256"})
_RUNTIME_KEYS = frozenset({"copy_dirs", "extra_pkgs"})


def _reject_unknown_keys(raw: dict[str, Any], allowed: frozenset[str], *, path: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        listed = ", ".join(repr(k) for k in unknown)
        raise ValueError(f"Unexpected key(s) at {path}: {listed}")


def _require_mapping(value: Any, *, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be a mapping")
    return value


def _require_list(value: Any, *, path: str) -> list[Any]:
    # Reject str (and bytes) so ``list("git")`` cannot become char lists.
    if isinstance(value, (str, bytes)) or not isinstance(value, list):
        raise ValueError(f"{path} must be a list")
    return value


def _require_str(value: Any, *, path: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{path} must be a string")
    return value


def _require_bool(value: Any, *, path: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{path} must be a boolean")
    return value


def _optional_str(value: Any, *, path: str) -> str | None:
    if value is None:
        return None
    return _require_str(value, path=path)


def _require_str_list(value: Any, *, path: str) -> list[str]:
    items = _require_list(value, path=path)
    return [_require_str(item, path=f"{path}[{i}]") for i, item in enumerate(items)]


def _parse_phase_policy(
    raw: Any,
    *,
    default: SpackPhasePolicy,
    label: str,
) -> SpackPhasePolicy:
    path = f"spack.{label}"
    if raw is None:
        return SpackPhasePolicy(
            update_builtin=default.update_builtin,
            repo_scope=default.repo_scope,
        )
    data = _require_mapping(raw, path=path)
    _reject_unknown_keys(data, _PHASE_POLICY_KEYS, path=path)
    if "update_builtin" in data:
        update = _require_bool(data["update_builtin"], path=f"{path}.update_builtin")
    else:
        update = default.update_builtin
    try:
        scope = RepoScope.parse(data.get("repo_scope"), default=default.repo_scope)
    except ValueError as exc:
        raise ValueError(f"{path}.repo_scope: {exc}") from exc
    return SpackPhasePolicy(update_builtin=update, repo_scope=scope)


def _parse_buildcache(raw: Any) -> BuildcacheConfig:
    if raw is None:
        return BuildcacheConfig()
    data = _require_mapping(raw, path="spack.buildcache")
    _reject_unknown_keys(data, _BUILDCACHE_KEYS, path="spack.buildcache")
    enabled = _require_bool(
        data.get("enabled", False),
        path="spack.buildcache.enabled",
    )
    padded_length = data.get("padded_length", 128)
    if type(padded_length) is not int:
        raise ValueError("spack.buildcache.padded_length must be a strict integer")
    if padded_length < 0:
        raise ValueError("spack.buildcache.padded_length must be >= 0")
    try:
        policy = BuildcachePolicy.parse(data.get("policy"))
    except ValueError as exc:
        raise ValueError(f"spack.buildcache.policy: {exc}") from exc
    try:
        coverage = BuildcacheCoverage.parse(data.get("coverage"))
    except ValueError as exc:
        raise ValueError(f"spack.buildcache.coverage: {exc}") from exc
    return BuildcacheConfig(
        enabled=enabled,
        padded_length=padded_length,
        policy=policy,
        coverage=coverage,
    )


def _parse_custom_repos(raw_repos: Any) -> list[CustomRepo]:
    if raw_repos is None:
        return []
    repos_list = _require_list(raw_repos, path="spack.custom_repos")
    repos: list[CustomRepo] = []
    for i, r in enumerate(repos_list):
        path = f"spack.custom_repos[{i}]"
        entry = _require_mapping(r, path=path)
        _reject_unknown_keys(entry, _CUSTOM_REPO_KEYS, path=path)
        if "namespace" not in entry:
            raise ValueError(f"{path} missing required 'namespace'")
        namespace = _require_str(entry["namespace"], path=f"{path}.namespace")
        try:
            phases = RepoPhase.parse(entry.get("phases"))
        except ValueError as exc:
            raise ValueError(f"{path}.phases: {exc}") from exc
        image_path = _optional_str(entry.get("image_path"), path=f"{path}.image_path")
        if "url" in entry:
            repos.append(
                CustomRepo(
                    type="git",
                    namespace=namespace,
                    url=_require_str(entry["url"], path=f"{path}.url"),
                    branch=(
                        _require_str(entry["branch"], path=f"{path}.branch")
                        if "branch" in entry and entry["branch"] is not None
                        else "main"
                    ),
                    sparse_path=_optional_str(
                        entry.get("sparse_path"), path=f"{path}.sparse_path"
                    ),
                    phases=phases,
                    image_path=image_path,
                )
            )
        elif "path" in entry:
            repos.append(
                CustomRepo(
                    type="local",
                    namespace=namespace,
                    path=_require_str(entry["path"], path=f"{path}.path"),
                    phases=phases,
                    image_path=image_path,
                )
            )
        else:
            raise ValueError(f"{path} needs either 'url' (git) or 'path' (local)")
    return repos


def _parse_manual_packages(raw: Any) -> list[ManualPackage]:
    if raw is None:
        return []
    items = _require_list(raw, path="manual_packages")
    out: list[ManualPackage] = []
    for i, mp in enumerate(items):
        path = f"manual_packages[{i}]"
        entry = _require_mapping(mp, path=path)
        _reject_unknown_keys(entry, _MANUAL_PACKAGE_KEYS, path=path)
        if "file" not in entry:
            raise ValueError(f"{path} must be a mapping with 'file'")
        out.append(
            ManualPackage(
                file=_require_str(entry["file"], path=f"{path}.file"),
                dest=_optional_str(entry.get("dest"), path=f"{path}.dest"),
                sha256=_optional_str(entry.get("sha256"), path=f"{path}.sha256"),
            )
        )
    return out


def _parse_images(raw: Any) -> ImageConfig:
    if raw is None:
        return ImageConfig()
    data = _require_mapping(raw, path="images")
    _reject_unknown_keys(data, _IMAGES_KEYS, path="images")
    builder = data.get("builder", "debian:trixie")
    runtime = data.get("runtime", "debian:trixie-slim")
    return ImageConfig(
        builder=_require_str(builder, path="images.builder"),
        runtime=_require_str(runtime, path="images.runtime"),
        output_name=_optional_str(data.get("output_name"), path="images.output_name"),
        output_tag=_optional_str(data.get("output_tag"), path="images.output_tag"),
    )


def _parse_spack(raw: Any, *, method: BuildMethod) -> SpackConfig:
    """Parse the ``spack:`` block.

    For ``method: spack``, ``env_name`` is mandatory (no implicit
    ``cp2k-env``). For ``method: no_spack``, a missing ``spack:`` section
    yields an empty placeholder config; if present, ``env_name`` is still
    optional.
    """
    require_env_name = method is BuildMethod.SPACK
    if raw is None:
        if require_env_name:
            raise ValueError(
                "spack.env_name is required when method is spack "
                "(declare it explicitly; no longer defaults to cp2k-env)"
            )
        return SpackConfig(
            version=DEFAULT_SPACK_VERSION,
            env_name="",
        )
    data = _require_mapping(raw, path="spack")
    _reject_unknown_keys(data, _SPACK_KEYS, path="spack")
    version = data.get("version", DEFAULT_SPACK_VERSION)
    raw_env_name = data.get("env_name")
    if raw_env_name is None or raw_env_name == "":
        if require_env_name:
            raise ValueError(
                "spack.env_name is required when method is spack "
                "(declare it explicitly; no longer defaults to cp2k-env)"
            )
        env_name = ""
    else:
        env_name = _require_str(raw_env_name, path="spack.env_name")
    assets_default = SpackPhasePolicy(
        update_builtin=True, repo_scope=RepoScope.ENV
    )
    image_default = SpackPhasePolicy(
        update_builtin=True, repo_scope=RepoScope.SITE
    )
    return SpackConfig(
        version=_require_str(version, path="spack.version"),
        env_name=env_name,
        custom_repos=_parse_custom_repos(data.get("custom_repos")),
        assets=_parse_phase_policy(
            data.get("assets"), default=assets_default, label="assets",
        ),
        image=_parse_phase_policy(
            data.get("image"), default=image_default, label="image",
        ),
        buildcache=_parse_buildcache(data.get("buildcache")),
    )


def _parse_mirror_builder(raw: Any) -> MirrorBuilderConfig:
    if raw is None:
        return MirrorBuilderConfig()
    data = _require_mapping(raw, path="mirror_builder")
    _reject_unknown_keys(data, _MIRROR_BUILDER_KEYS, path="mirror_builder")
    if "system_pkgs" in data and data["system_pkgs"] is not None:
        system_pkgs = _require_str_list(
            data["system_pkgs"], path="mirror_builder.system_pkgs"
        )
    else:
        system_pkgs = []
    setup = data.get("pkg_mirror_setup", "")
    if setup is None:
        setup = ""
    install = data.get("pkg_install_cmd", "")
    if install is None:
        install = ""
    return MirrorBuilderConfig(
        system_pkgs=system_pkgs,
        pkg_mirror_setup=_require_str(setup, path="mirror_builder.pkg_mirror_setup"),
        pkg_install_cmd=_require_str(install, path="mirror_builder.pkg_install_cmd"),
    )


def _parse_runtime(raw: Any) -> RuntimeConfig:
    if raw is None:
        return RuntimeConfig()
    data = _require_mapping(raw, path="runtime")
    _reject_unknown_keys(data, _RUNTIME_KEYS, path="runtime")
    if "copy_dirs" in data and data["copy_dirs"] is not None:
        copy_dirs = _require_str_list(data["copy_dirs"], path="runtime.copy_dirs")
    else:
        copy_dirs = []
    if "extra_pkgs" in data and data["extra_pkgs"] is not None:
        extra_pkgs = _require_str_list(data["extra_pkgs"], path="runtime.extra_pkgs")
    else:
        extra_pkgs = []
    return RuntimeConfig(copy_dirs=copy_dirs, extra_pkgs=extra_pkgs)


def _coerce_schema_version(raw: dict[str, Any], *, source: str) -> int:
    if "schema_version" not in raw or raw["schema_version"] is None:
        logger.warning(
            "%s lacks schema_version; treating as v%d (migration tip: add "
            "`schema_version: %d`).",
            source,
            SUPPORTED_SCHEMA_VERSION,
            SUPPORTED_SCHEMA_VERSION,
        )
        return SUPPORTED_SCHEMA_VERSION
    value = raw["schema_version"]
    # Reject bool/float/str: YAML ``true`` / ``1.0`` / ``"1"`` must not coerce.
    if type(value) is not int:
        raise ValueError(
            f"schema_version must be a strict integer in {source}, got {value!r}"
        )
    if value != SUPPORTED_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported schema_version: {value} in {source} "
            f"(supported: {SUPPORTED_SCHEMA_VERSION})"
        )
    return value


def parse_environment_spec(
    raw: Any,
    *,
    source: str = "<memory>",
    source_path: Path | None = None,
) -> EnvironmentSpec:
    """Parse a raw env.yaml mapping into an :class:`EnvironmentSpec`.

    Fail-closed: unknown keys, wrong types, and non-mapping roots raise
    ``ValueError``. ``template_vars`` remains an open mapping.
    """
    # Do not use ``raw or {}`` — falsy non-mappings like ``[]`` must not coerce.
    if raw is None:
        data: Any = {}
    else:
        data = raw
    if not isinstance(data, dict):
        raise ValueError(f"env.yaml root must be a mapping in {source}")

    _reject_unknown_keys(data, _TOP_LEVEL_KEYS, path="<root>")
    schema_version = _coerce_schema_version(data, source=source)
    method = BuildMethod.parse(data.get("method"))

    tv = data.get("template_vars")
    if tv is None:
        template_vars: dict[str, Any] = {}
    else:
        # Open mapping: any keys allowed; only the container type is checked.
        template_vars = dict(_require_mapping(tv, path="template_vars"))

    script_raw = data.get("script", "")
    if script_raw is None:
        script_raw = ""
    script = _require_str(script_raw, path="script")
    spack = _parse_spack(data.get("spack"), method=method)
    if method is BuildMethod.NO_SPACK and spack.buildcache.enabled:
        raise ValueError(
            "spack.buildcache.enabled cannot be true when method: no_spack"
        )

    return EnvironmentSpec(
        schema_version=schema_version,
        method=method,
        images=_parse_images(data.get("images")),
        spack=spack,
        mirror_builder=_parse_mirror_builder(data.get("mirror_builder")),
        manual_packages=_parse_manual_packages(data.get("manual_packages")),
        runtime=_parse_runtime(data.get("runtime")),
        script=script,
        template_vars=template_vars,
        source_path=source_path,
    )


def load_environment_spec(env_dir: Path) -> EnvironmentSpec:
    """Load and parse env.yaml for an environment directory."""
    from hpc_cf.env import find_env_yaml

    env_yaml = find_env_yaml(env_dir)
    with env_yaml.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if raw is None:
        raw = {}
    return parse_environment_spec(
        raw,
        source=str(env_yaml),
        source_path=env_yaml,
    )


def load_environment_spec_from_template(template_path: Path | None) -> EnvironmentSpec | None:
    """Load EnvironmentSpec using a Dockerfile.j2 path's parent as the env dir.

    Returns ``None`` when *template_path* is None or env.yaml is missing.
    """
    if not template_path:
        return None
    try:
        return load_environment_spec(template_path.parent)
    except FileNotFoundError:
        return None

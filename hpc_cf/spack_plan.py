"""Pure-data SpackEnvironmentPlan derived from EnvironmentSpec.

Both assets (:mod:`hpc_cf.spack_ops`) and Dockerfile rendering
(:mod:`hpc_cf.template`) consume this plan so env name, builtin update,
repo scope, and registration order share one explicit contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from hpc_cf.environment import (
    BuildcacheCoverage,
    BuildcacheMode,
    BuildcachePolicy,
    CustomRepo,
    EnvironmentSpec,
    RepoPhase,
    RepoScope,
    SpackPhasePolicy,
)


@dataclass(frozen=True)
class PlannedRepo:
    """One custom repo entry resolved for a workflow stage."""

    namespace: str
    type: Literal["git", "local"]
    phases: RepoPhase
    # Container path used by assets prepare/register scripts (relative to env
    # dir is resolved by callers via env_dir + path / git clone layout).
    url: str | None = None
    branch: str | None = None
    # Resolved pin (env.yaml commit, or the sidecar-injected tip for
    # floating repos once build input is resolved).
    commit: str | None = None
    sparse_path: str | None = None
    path: str | None = None
    # Absolute path expected in the rendered Dockerfile ``repo add`` line.
    image_path: str = ""


@dataclass(frozen=True)
class StagingClone:
    """One monorepo sparse clone staging several image-phase repos.

    Repos sharing (url, commit) are one git history: a single clone at
    ``clone_root`` with the union of ``sparse_paths`` stages them all,
    replacing the per-app handwritten clone blocks and their
    ``s8ga_*`` / ``force_avx512_*`` template_vars mirrors.
    """

    url: str
    commit: str
    clone_root: str
    sparse_paths: tuple[str, ...]


@dataclass(frozen=True)
class PhasePlan:
    """Spack steps for one workflow stage (assets or image)."""

    update_builtin: bool
    repo_scope: RepoScope
    repos: tuple[PlannedRepo, ...]
    env_name: str

    def scope_flag(self) -> str:
        """Value for ``spack repo add --scope``."""
        if self.repo_scope is RepoScope.ENV:
            return f"env:{self.env_name}"
        return "site"


@dataclass(frozen=True)
class BuildcachePlan:
    """Binary-cache contract kept separate from source-mirror configuration."""

    enabled: bool
    padded_length: int
    policy: BuildcachePolicy
    coverage: BuildcacheCoverage
    mode: BuildcacheMode = BuildcacheMode.LOCAL
    url: str | None = None
    username_var: str | None = None
    password_var: str | None = None
    exclude_specs: tuple[str, ...] = ()

    @property
    def check_excludes_external(self) -> bool:
        """Whether coverage checks must omit Spack external specs."""
        return self.coverage is BuildcacheCoverage.NON_EXTERNAL


@dataclass(frozen=True)
class SpackEnvironmentPlan:
    """Authoritative Spack contract for one environment.

    The plan is the reliable shared contract for **assets** scripts
    (prepare/register/mirror). For image Dockerfiles, ABACUS opensource and
    CP2K opensource force-avx512 (2026.1 / 2026.2) include
    ``templates/partials/spack_image_repos.j2`` (plan-driven
    ``custom_repos[].image_path``); other apps still stage/register custom repos
    via per-env Dockerfile.j2 + ``template_vars`` handwritten ``spack repo add``
    lines.
    """

    version: str
    env_name: str
    assets: PhasePlan
    image: PhasePlan
    buildcache: BuildcachePlan
    # Mirror registration scope is intentionally fixed to site (not read from
    # env.yaml). Kept independent of custom-repo scope so image
    # ``repo_scope: env`` never leaks into ``spack mirror add --scope``.
    mirror_scope: RepoScope = RepoScope.SITE

    def mirror_scope_flag(self) -> str:
        """Value for ``spack mirror add --scope`` (always site unless overridden in code)."""
        if self.mirror_scope is RepoScope.ENV:
            return f"env:{self.env_name}"
        return "site"


def default_image_path(repo: CustomRepo) -> str:
    """Derive Dockerfile registration path when ``image_path`` is unset."""
    if repo.image_path:
        return repo.image_path.rstrip("/")
    if repo.type == "local":
        local = (repo.path or "repos").strip("/")
        return f"/opt/spack-env-file/{local}"
    leaf = (repo.sparse_path or repo.namespace).rstrip("/").split("/")[-1]
    return f"/opt/spack-repo/spack_repo/{leaf}"


def _to_planned(repo: CustomRepo) -> PlannedRepo:
    return PlannedRepo(
        namespace=repo.namespace,
        type=repo.type,
        phases=repo.phases,
        url=repo.url,
        branch=repo.branch,
        commit=repo.commit,
        sparse_path=repo.sparse_path,
        path=repo.path,
        image_path=default_image_path(repo),
    )


def staging_clones(repos: tuple[PlannedRepo, ...]) -> tuple[StagingClone, ...]:
    """Group image-phase git repos into one sparse clone per (url, commit).

    Only repos whose explicit ``image_path`` equals
    ``<clone-root>/<sparse_path>`` participate: the staging partial clones
    at the root and sparse-checkouts the union of paths, so the suffix
    relationship is the data contract (repo-pin validation enforces it).
    Repos without a resolved commit (floating, sidecar missing) or with a
    plan-derived default image_path are staged by other means and skipped
    here.
    """
    groups: dict[tuple[str, str], list[PlannedRepo]] = {}
    order: list[tuple[str, str]] = []
    for repo in repos:
        if repo.type != "git" or not repo.url or not repo.sparse_path:
            continue
        if not repo.commit:
            continue
        image = (repo.image_path or "").rstrip("/")
        suffix = "/" + repo.sparse_path.strip("/")
        if not image.endswith(suffix):
            continue
        key = (repo.url, repo.commit)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(repo)

    clones: list[StagingClone] = []
    for key in order:
        url, commit = key
        members = groups[key]
        first = members[0]
        suffix = "/" + (first.sparse_path or "").strip("/")
        root = (first.image_path or "").rstrip("/")[: -len(suffix)]
        sparse_paths = tuple(
            dict.fromkeys((m.sparse_path or "").strip("/") for m in members)
        )
        clones.append(
            StagingClone(
                url=url,
                commit=commit,
                clone_root=root,
                sparse_paths=sparse_paths,
            )
        )
    return tuple(clones)


def _phase_plan(
    *,
    env_name: str,
    policy: SpackPhasePolicy,
    repos: list[CustomRepo],
    stage: Literal["assets", "image"],
) -> PhasePlan:
    planned = tuple(_to_planned(r) for r in repos if r.phases.applies_to(stage))
    return PhasePlan(
        update_builtin=policy.update_builtin,
        repo_scope=policy.repo_scope,
        repos=planned,
        env_name=env_name,
    )


def build_spack_environment_plan(spec: EnvironmentSpec) -> SpackEnvironmentPlan:
    """Build a pure-data plan from an :class:`EnvironmentSpec`."""
    env_name = spec.spack.env_name
    repos = list(spec.spack.custom_repos)
    return SpackEnvironmentPlan(
        version=spec.spack.version,
        env_name=env_name,
        assets=_phase_plan(
            env_name=env_name,
            policy=spec.spack.assets,
            repos=repos,
            stage="assets",
        ),
        image=_phase_plan(
            env_name=env_name,
            policy=spec.spack.image,
            repos=repos,
            stage="image",
        ),
        buildcache=BuildcachePlan(
            enabled=spec.spack.buildcache.enabled,
            padded_length=spec.spack.buildcache.padded_length,
            policy=spec.spack.buildcache.policy,
            coverage=spec.spack.buildcache.coverage,
            mode=spec.spack.buildcache.mode,
            url=spec.spack.buildcache.url,
            username_var=spec.spack.buildcache.username_var,
            password_var=spec.spack.buildcache.password_var,
            exclude_specs=tuple(
                dict.fromkeys(
                    mp.mirror_exclude_spec() for mp in spec.manual_packages
                )
            ),
        ),
    )


def plan_context(plan: SpackEnvironmentPlan) -> dict:
    """Jinja context fragment consumed by Dockerfile templates."""
    return {
        "spack_env_name": plan.env_name,
        "spack_version": plan.version,
        "spack_update_builtin": plan.image.update_builtin,
        "spack_repo_scope": plan.image.scope_flag(),
        "spack_repo_scope_kind": plan.image.repo_scope.value,
        "spack_mirror_scope": plan.mirror_scope_flag(),
        "spack_mirror_scope_kind": plan.mirror_scope.value,
        "spack_buildcache_enabled": plan.buildcache.enabled,
        "spack_buildcache_padded_length": plan.buildcache.padded_length,
        "spack_buildcache_policy": plan.buildcache.policy.value,
        "spack_buildcache_coverage": plan.buildcache.coverage.value,
        "spack_buildcache_mode": plan.buildcache.mode.value,
        "spack_buildcache_url": plan.buildcache.url,
        "spack_buildcache_username_var": plan.buildcache.username_var,
        "spack_buildcache_password_var": plan.buildcache.password_var,
        "spack_buildcache_exclude_specs": list(plan.buildcache.exclude_specs),
        "spack_buildcache_check_excludes_external": (
            plan.buildcache.check_excludes_external
        ),
        "spack_image_repos": [
            {
                "namespace": r.namespace,
                "type": r.type,
                "image_path": r.image_path,
            }
            for r in plan.image.repos
        ],
        "spack_staging_clones": [
            {
                "url": clone.url,
                "commit": clone.commit,
                "clone_root": clone.clone_root,
                "sparse_paths": list(clone.sparse_paths),
            }
            for clone in staging_clones(plan.image.repos)
        ],
        # Overridden by build_context(allow_reconcretize=...); default fail-closed.
        "allow_reconcretize": False,
    }

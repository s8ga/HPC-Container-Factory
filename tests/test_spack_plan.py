"""SpackEnvironmentPlan unit tests and all-env render contracts.

Dual-write must stay in sync: when a git ``custom_repos`` entry's branch / url /
sparse_path is also exposed via ``template_vars`` (e.g. ``cp2k_branch``,
``cp2k_dev_repo_path``), both sides must match and the rendered Dockerfile must
contain those values. Prefer a single source later; until image-repos wiring
lands, tests enforce the sync.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_cf.environment import (
    BuildMethod,
    CustomRepo,
    EnvironmentSpec,
    RepoPhase,
    RepoScope,
    SpackConfig,
    load_environment_spec,
    parse_environment_spec,
)
from hpc_cf.spack_plan import (
    SpackEnvironmentPlan,
    build_spack_environment_plan,
)
from hpc_cf.spack_ops import SpackOps
from hpc_cf.template import build_context, render_template, select_template
from hpc_cf.config import SPACK_ENVS_DIR

# template_vars key → CustomRepo attribute that must match when dual-written.
_DUAL_WRITE_FIELDS: tuple[tuple[str, str], ...] = (
    ("cp2k_branch", "branch"),
    ("cp2k_dev_repo_path", "sparse_path"),
)


def _spack_env_dirs() -> list[Path]:
    return [
        d
        for d in sorted(SPACK_ENVS_DIR.iterdir())
        if d.is_dir()
        and (
            (d / "spack-env-file" / "env.yaml").exists()
            or (d / "env.yaml").exists()
        )
    ]


def test_parse_repo_phases_and_image_path() -> None:
    spec = parse_environment_spec(
        {
            "schema_version": 1,
            "spack": {
                "version": "1.1.1",
                "env_name": "e",
                "custom_repos": [
                    {
                        "url": "https://example.com/a.git",
                        "namespace": "git-ns",
                        "phases": "both",
                    },
                    {
                        "url": "https://example.com/b.git",
                        "namespace": "img-only",
                        "phases": "image",
                        "image_path": "/opt/custom/b",
                    },
                    {"path": "repos", "namespace": "local-ns", "phases": "assets"},
                ],
            },
        }
    )
    assert spec.spack.custom_repos[0].phases is RepoPhase.BOTH
    assert spec.spack.custom_repos[1].phases is RepoPhase.IMAGE
    assert spec.spack.custom_repos[1].image_path == "/opt/custom/b"
    assert spec.spack.custom_repos[2].phases is RepoPhase.ASSETS


def test_parse_phase_policies() -> None:
    spec = parse_environment_spec(
        {
            "schema_version": 1,
            "spack": {
                "version": "1.2.0",
                "env_name": "vasp-env",
                "assets": {"update_builtin": True, "repo_scope": "env"},
                "image": {"update_builtin": False, "repo_scope": "site"},
                "custom_repos": [{"path": "repos", "namespace": "vasp-env"}],
            },
        }
    )
    assert spec.spack.assets.update_builtin is True
    assert spec.spack.assets.repo_scope is RepoScope.ENV
    assert spec.spack.image.update_builtin is False
    assert spec.spack.image.repo_scope is RepoScope.SITE


def test_plan_filters_by_phase_and_preserves_order() -> None:
    spec = EnvironmentSpec(
        spack=SpackConfig(
            version="1.2.0",
            env_name="cp2k-env",
            custom_repos=[
                CustomRepo(
                    type="git",
                    namespace="cp2k_dev",
                    url="https://github.com/cp2k/cp2k.git",
                    sparse_path="tools/spack/spack_repo/cp2k_dev",
                    phases=RepoPhase.BOTH,
                ),
                CustomRepo(
                    type="git",
                    namespace="s8_overrides",
                    url="https://github.com/s8ga/s8ga-spack-packages.git",
                    branch="master",
                    sparse_path="spack_repo/s8_overrides",
                    phases=RepoPhase.IMAGE,
                    image_path="/opt/s8ga-spack-packages/spack_repo/s8_overrides",
                ),
                CustomRepo(
                    type="local",
                    namespace="cp2k-env",
                    path="repos",
                    phases=RepoPhase.BOTH,
                ),
            ],
        )
    )
    plan = build_spack_environment_plan(spec)
    assert isinstance(plan, SpackEnvironmentPlan)
    assert plan.env_name == "cp2k-env"
    assert [r.namespace for r in plan.assets.repos] == ["cp2k_dev", "cp2k-env"]
    assert [r.namespace for r in plan.image.repos] == [
        "cp2k_dev",
        "s8_overrides",
        "cp2k-env",
    ]
    assert plan.image.repos[1].image_path.endswith("s8_overrides")
    assert plan.assets.repo_scope is RepoScope.ENV
    assert plan.image.repo_scope is RepoScope.SITE


def test_plan_defaults_match_historical_assets_vs_image() -> None:
    """Assets default to env scope; image defaults to site + update_builtin."""
    spec = parse_environment_spec(
        {
            "schema_version": 1,
            "spack": {
                "version": "1.1.1",
                "env_name": "cp2k-env",
                "custom_repos": [{"path": "repos", "namespace": "cp2k-env"}],
            },
        }
    )
    plan = build_spack_environment_plan(spec)
    assert plan.assets.update_builtin is True
    assert plan.assets.scope_flag() == "env:cp2k-env"
    assert plan.image.update_builtin is True
    assert plan.image.scope_flag() == "site"


def test_spack_ops_skips_image_only_repos() -> None:
    from tests.test_script_snapshots import CapturingContainer

    spec = EnvironmentSpec(
        spack=SpackConfig(
            version="1.2.0",
            env_name="cp2k-env",
            custom_repos=[
                CustomRepo(
                    type="git",
                    namespace="cp2k_dev",
                    url="https://github.com/cp2k/cp2k.git",
                    sparse_path="tools/spack/spack_repo/cp2k_dev",
                    phases=RepoPhase.BOTH,
                ),
                CustomRepo(
                    type="git",
                    namespace="s8_overrides",
                    url="https://github.com/s8ga/s8ga-spack-packages.git",
                    phases=RepoPhase.IMAGE,
                    image_path="/opt/s8/x",
                ),
                CustomRepo(
                    type="local",
                    namespace="cp2k-env",
                    path="repos",
                    phases=RepoPhase.BOTH,
                ),
            ],
        )
    )
    ops = SpackOps(spec, CapturingContainer())
    prep = ops._build_prepare_repos_script("/work/env")
    assert "cp2k_dev" in prep
    assert "s8_overrides" not in prep
    assert "/work/env/repos" in prep

    env_script = ops._build_prepare_environment_script(
        "/work/env", import_lock=False
    )
    assert "repo update builtin" in env_script
    assert env_script.count("--scope env:cp2k-env") == 2
    assert "s8_overrides" not in env_script


def test_spack_ops_honors_assets_update_builtin_false() -> None:
    from tests.test_script_snapshots import CapturingContainer

    spec = parse_environment_spec(
        {
            "schema_version": 1,
            "spack": {
                "version": "1.1.1",
                "env_name": "vasp-env",
                "assets": {"update_builtin": False, "repo_scope": "env"},
                "custom_repos": [{"path": "repos", "namespace": "vasp-env"}],
            },
        }
    )
    ops = SpackOps(spec, CapturingContainer())
    s = ops._build_prepare_environment_script("/work/env", import_lock=False)
    assert "repo update builtin" not in s
    assert "--scope env:vasp-env" in s


@pytest.mark.parametrize("env_dir", _spack_env_dirs(), ids=lambda p: p.name)
def test_all_spack_envs_plan_matches_rendered_dockerfile(env_dir: Path) -> None:
    """Rendered Dockerfile must agree with SpackEnvironmentPlan for image phase."""
    spec = load_environment_spec(env_dir)
    if spec.method is BuildMethod.NO_SPACK:
        pytest.skip("no_spack env has no Spack Dockerfile contract")

    plan = build_spack_environment_plan(spec)
    dockerfile = env_dir / "Dockerfile.j2"
    if not dockerfile.exists():
        pytest.skip("no per-env Dockerfile.j2")

    template_path = select_template(env_dir.name)
    context = build_context(
        use_mirror=False,
        build_only=False,
        app_version=env_dir.name,
        template_path=template_path,
    )
    rendered = render_template(template_path, context)

    assert f"spack env create {plan.env_name}" in rendered
    # Every -e <name> / env:<name> reference must match plan.env_name
    assert f"spack -e {plan.env_name}" in rendered
    assert plan.env_name != ""
    # Hardcoded sibling env names must not appear when they differ
    for other in ("cp2k-env", "vasp-env", "abacus-env"):
        if other != plan.env_name:
            assert f"spack env create {other}" not in rendered
            assert f"spack -e {other}" not in rendered
            assert f"env:{other}" not in rendered

    if plan.image.update_builtin:
        assert "repo update builtin" in rendered
        assert f"spack -e {plan.env_name} repo update builtin" in rendered
    else:
        assert "repo update builtin" not in rendered

    scope = plan.image.scope_flag()
    assert f"--scope {scope}" in rendered

    last = -1
    for repo in plan.image.repos:
        needle = repo.image_path.rstrip("/")
        idx = rendered.find(needle)
        assert idx >= 0, (
            f"{env_dir.name}: image repo {repo.namespace!r} path {needle!r} "
            f"missing from rendered Dockerfile"
        )
        assert idx > last, (
            f"{env_dir.name}: repo order drifted for {repo.namespace!r} "
            f"(path {needle!r})"
        )
        last = idx


@pytest.mark.parametrize("env_dir", _spack_env_dirs(), ids=lambda p: p.name)
def test_all_spack_envs_assets_plan_uses_declared_repos(env_dir: Path) -> None:
    """Assets plan repos must be a phase-filter of custom_repos in YAML order."""
    spec = load_environment_spec(env_dir)
    if spec.method is BuildMethod.NO_SPACK:
        pytest.skip("no_spack")
    plan = build_spack_environment_plan(spec)
    expected = [
        r.namespace
        for r in spec.spack.custom_repos
        if r.phases.applies_to("assets")
    ]
    assert [r.namespace for r in plan.assets.repos] == expected
    assert plan.assets.repo_scope is spec.spack.assets.repo_scope
    assert plan.assets.update_builtin is spec.spack.assets.update_builtin


def test_cp2k_2026_2_registers_avx512_repo_for_assets_and_image() -> None:
    env_dir = SPACK_ENVS_DIR / "cp2k_opensource-2026.2-force-avx512"
    spec = load_environment_spec(env_dir)
    plan = build_spack_environment_plan(spec)
    assets_ns = [r.namespace for r in plan.assets.repos]
    image_ns = [r.namespace for r in plan.image.repos]
    assert "s8_overrides" in assets_ns
    assert "s8_overrides" in image_ns
    assert assets_ns.index("cp2k_dev") < assets_ns.index("s8_overrides")
    assert assets_ns.index("s8_overrides") < assets_ns.index("cp2k-env")
    assert image_ns.index("cp2k_dev") < image_ns.index("s8_overrides")
    assert image_ns.index("s8_overrides") < image_ns.index("cp2k-env")
    assert plan.image.repo_scope is RepoScope.ENV
    assert plan.image.scope_flag() == "env:cp2k-env"


def test_vasp_image_skips_builtin_update() -> None:
    for name in ("vasp_mkl-6.6.0-avx2", "vasp_mkl-6.6.0-avx512"):
        spec = load_environment_spec(SPACK_ENVS_DIR / name)
        plan = build_spack_environment_plan(spec)
        assert plan.image.update_builtin is False
        assert plan.assets.update_builtin is True
        assert plan.image.repo_scope is RepoScope.SITE


def _cp2k_git_repo(spec: EnvironmentSpec) -> CustomRepo | None:
    """Prefer the git custom_repo that supplies the CP2K spack recipes."""
    for repo in spec.spack.custom_repos:
        if repo.type != "git" or not repo.url:
            continue
        if "cp2k" in repo.url.lower() or (repo.namespace or "").startswith("cp2k"):
            return repo
    return None


def _render_env_dockerfile(env_dir: Path) -> str:
    template_path = select_template(env_dir.name)
    context = build_context(
        use_mirror=False,
        build_only=False,
        app_version=env_dir.name,
        template_path=template_path,
    )
    return render_template(template_path, context)


@pytest.mark.parametrize("env_dir", _spack_env_dirs(), ids=lambda p: p.name)
def test_custom_repos_template_vars_cross_check_dockerfile(env_dir: Path) -> None:
    """Git custom_repos dual-written via template_vars must match the Dockerfile."""
    spec = load_environment_spec(env_dir)
    if spec.method is BuildMethod.NO_SPACK:
        pytest.skip("no_spack env has no Spack Dockerfile contract")
    if not (env_dir / "Dockerfile.j2").exists():
        pytest.skip("no per-env Dockerfile.j2")

    dual_keys = [k for k, _ in _DUAL_WRITE_FIELDS if k in spec.template_vars]
    if not dual_keys:
        pytest.skip("no dual-write template_vars for this env")

    repo = _cp2k_git_repo(spec)
    assert repo is not None, (
        f"{env_dir.name}: template_vars {dual_keys} require a matching git custom_repos entry"
    )

    for tv_key, attr in _DUAL_WRITE_FIELDS:
        if tv_key not in spec.template_vars:
            continue
        tv_val = spec.template_vars[tv_key]
        repo_val = getattr(repo, attr)
        assert repo_val, (
            f"{env_dir.name}: custom_repos.{attr} missing while template_vars.{tv_key}={tv_val!r}"
        )
        assert tv_val == repo_val, (
            f"{env_dir.name}: dual-write drift — template_vars.{tv_key}={tv_val!r} "
            f"!= custom_repos.{attr}={repo_val!r} (keep them synced)"
        )

    rendered = _render_env_dockerfile(env_dir)
    assert repo.url and repo.url in rendered, (
        f"{env_dir.name}: custom_repos url {repo.url!r} missing from rendered Dockerfile"
    )
    for tv_key, attr in _DUAL_WRITE_FIELDS:
        if tv_key not in spec.template_vars:
            continue
        value = str(spec.template_vars[tv_key])
        assert value in rendered, (
            f"{env_dir.name}: {tv_key}/{attr} value {value!r} missing from rendered Dockerfile"
        )

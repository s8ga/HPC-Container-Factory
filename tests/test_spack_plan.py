"""SpackEnvironmentPlan unit tests and all-env render contracts.

Dual-write must stay in sync: when a git ``custom_repos`` entry's branch / url /
sparse_path is also exposed via ``template_vars`` (e.g. ``cp2k_branch``,
``cp2k_dev_repo_path``), both sides must match and the rendered Dockerfile must
contain those values. ABACUS opensource and CP2K opensource force-avx512
(2026.1 / 2026.2) wire ``spack_image_repos`` for image registration; other apps
may still hand-write ``spack repo add`` until migrated.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_cf.environment import (
    BuildcacheCoverage,
    BuildcachePolicy,
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
    BuildcachePlan,
    SpackEnvironmentPlan,
    build_spack_environment_plan,
    plan_context,
)
from hpc_cf.spack_ops import SpackOps
from hpc_cf.template import build_context, render_template, select_template
from hpc_cf.config import SPACK_ENVS_DIR

# template_vars key → CustomRepo attribute that must match when dual-written.
_DUAL_WRITE_FIELDS: tuple[tuple[str, str], ...] = (
    ("cp2k_branch", "branch"),
    ("cp2k_dev_repo_path", "sparse_path"),
    ("cp2k_dev_repo_commit", "commit"),
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


def test_buildcache_plan_is_independent_from_source_mirror() -> None:
    spec = parse_environment_spec(
        {
            "schema_version": 1,
            "spack": {
                "version": "1.2.0",
                "env_name": "demo",
                "buildcache": {
                    "enabled": True,
                    "padded_length": 192,
                    "policy": "only",
                },
            },
        }
    )

    plan = build_spack_environment_plan(spec)
    assert isinstance(plan.buildcache, BuildcachePlan)
    assert plan.buildcache.enabled is True
    assert plan.buildcache.padded_length == 192
    assert plan.buildcache.policy is BuildcachePolicy.ONLY
    assert plan.buildcache.coverage is BuildcacheCoverage.NON_EXTERNAL
    assert plan.buildcache.check_excludes_external is True
    assert not hasattr(plan.buildcache, "mirror_scope")

    context = plan_context(plan)
    assert context["spack_buildcache_enabled"] is True
    assert context["spack_buildcache_padded_length"] == 192
    assert context["spack_buildcache_policy"] == "only"
    assert context["spack_buildcache_coverage"] == "non_external"
    assert context["spack_buildcache_check_excludes_external"] is True


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
        # Bound the match so "/.../repos" does not hit "/.../repos_cp2k_dev".
        terminators = frozenset("/ \"'\n\t&")
        idx = -1
        start = 0
        while True:
            cand = rendered.find(needle, start)
            if cand < 0:
                break
            end = cand + len(needle)
            if end >= len(rendered) or rendered[end] in terminators:
                idx = cand
                break
            start = cand + 1
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


@pytest.mark.parametrize(
    ("env_name", "cp2k_sha"),
    [
        ("cp2k_opensource-2025.2", "c3a8adfec55aed817436b8b681645390fd0dd801"),
        (
            "cp2k_opensource-2025.2-force-avx512",
            "c3a8adfec55aed817436b8b681645390fd0dd801",
        ),
        (
            "cp2k_opensource-2026.1-force-avx512",
            "757bb76a80be37bef49d7383b4f7fce122940895",
        ),
        (
            "cp2k_opensource-2026.2-force-avx512",
            "67b5da876dd6a76b8b021d5a04d1c81ba79a4c50",
        ),
    ],
)
def test_opensource_cp2k_pins_repo_commits(env_name: str, cp2k_sha: str) -> None:
    """Opensource CP2K pins cp2k git tip; force envs also pin shared s8ga SHA."""
    s8ga_pin = "d0ee3f460a2543c05c693317c767652abf964db7"
    env_dir = SPACK_ENVS_DIR / env_name
    spec = load_environment_spec(env_dir)
    rendered = _render_env_dockerfile(env_dir)
    cp2k_repo = _cp2k_git_repo(spec)
    assert cp2k_repo is not None
    assert cp2k_repo.commit == cp2k_sha
    assert spec.template_vars["cp2k_dev_repo_commit"] == cp2k_sha
    assert f'git checkout "{cp2k_sha}"' in rendered
    # Commit pin must still exercise {{ cp2k_branch }} (not comment-only).
    branch = spec.template_vars["cp2k_branch"]
    assert "git merge-base --is-ancestor HEAD" in rendered
    assert f"origin/{branch}" in rendered

    s8ga_repos = [
        repo
        for repo in spec.spack.custom_repos
        if repo.type == "git" and "s8ga" in (repo.url or "").lower()
    ]
    if not s8ga_repos:
        assert "s8ga_repo_commit" not in spec.template_vars
        return
    assert all(repo.commit == s8ga_pin for repo in s8ga_repos)
    assert spec.template_vars["s8ga_repo_commit"] == s8ga_pin
    assert f'git checkout "{s8ga_pin}"' in rendered


def test_vasp_image_skips_builtin_update() -> None:
    for name in ("vasp_mkl-6.6.0-avx2", "vasp_mkl-6.6.0-avx512"):
        spec = load_environment_spec(SPACK_ENVS_DIR / name)
        plan = build_spack_environment_plan(spec)
        assert plan.image.update_builtin is False
        assert plan.assets.update_builtin is True
        assert plan.image.repo_scope is RepoScope.SITE


def test_rocm_render_and_plan_keep_gpu_contract() -> None:
    env_dir = SPACK_ENVS_DIR / "cp2k_rocm-2026.1-gfx942"
    spec = load_environment_spec(env_dir)
    plan = build_spack_environment_plan(spec)
    rendered = _render_env_dockerfile(env_dir)

    assert spec.spack.version == "1.1.0"
    assert plan.env_name == "cp2k-env"
    assert [repo.namespace for repo in plan.image.repos] == [
        "cp2k_dev_repo",
        "cp2k-env",
    ]
    assert 'ARG AMDGPU_TARGETS="gfx942"' in rendered
    assert "ROCM_PATH=" in rendered
    assert "spack -e cp2k-env install" in rendered
    assert "/opt/rocm-export/lib" in rendered


def test_abacus_render_and_plan_keep_s8ga_repo_contract() -> None:
    env_dir = SPACK_ENVS_DIR / "abacus_opensource-3.9.0.27-force-avx512"
    spec = load_environment_spec(env_dir)
    plan = build_spack_environment_plan(spec)
    rendered = _render_env_dockerfile(env_dir)
    pin = "d0ee3f460a2543c05c693317c767652abf964db7"

    assert spec.spack.version == "1.2.0"
    assert plan.env_name == "abacus-env"
    assert [repo.namespace for repo in plan.assets.repos] == ["abacus", "s8_overrides"]
    assert [repo.namespace for repo in plan.image.repos] == ["abacus", "s8_overrides"]
    assert all(repo.commit == pin for repo in spec.spack.custom_repos)
    assert spec.template_vars["s8ga_repo_commit"] == pin
    assert "spack env create abacus-env" in rendered
    assert "AS builder-installed" in rendered
    assert "FROM builder-installed AS builder" in rendered
    assert f'git checkout "{pin}"' in rendered
    assert "/opt/s8ga-spack-packages/spack_repo/abacus" in rendered
    assert "/opt/s8ga-spack-packages/spack_repo/s8_overrides" in rendered
    assert "/opt/spack-env-file/repos/" not in rendered
    assert "abacus_run_integration_tests.sh" in rendered


@pytest.mark.parametrize(
    "env_name",
    [
        "abacus_opensource-3.9.0.27-force-avx512",
        "abacus_opensource-3.10.1-force-avx512",
    ],
)
def test_abacus_dockerfile_wires_spack_image_repos_partial(env_name: str) -> None:
    """ABACUS pilot: image repos come from plan partial, not handwritten dual add."""
    env_dir = SPACK_ENVS_DIR / env_name
    src = (env_dir / "Dockerfile.j2").read_text(encoding="utf-8")
    assert "{% include 'partials/spack_image_repos.j2' %}" in src
    # Sparse clone may still use template_vars paths; repo add must not.
    assert '/opt/s8ga-spack-packages/{{ s8ga_abacus_repo_path }}' not in src
    assert '/opt/s8ga-spack-packages/{{ s8ga_overrides_repo_path }}' not in src

    spec = load_environment_spec(env_dir)
    plan = build_spack_environment_plan(spec)
    rendered = _render_env_dockerfile(env_dir)

    assert plan.env_name == "abacus-env"
    assert plan.image.scope_flag() == "env:abacus-env"
    assert [r.image_path for r in plan.image.repos] == [
        "/opt/s8ga-spack-packages/spack_repo/abacus",
        "/opt/s8ga-spack-packages/spack_repo/s8_overrides",
    ]

    abacus_add = (
        "spack -e abacus-env repo add --scope env:abacus-env "
        "/opt/s8ga-spack-packages/spack_repo/abacus"
    )
    overrides_add = (
        "spack -e abacus-env repo add --scope env:abacus-env "
        "/opt/s8ga-spack-packages/spack_repo/s8_overrides"
    )
    assert abacus_add in rendered
    assert overrides_add in rendered
    assert rendered.count(abacus_add) == 1
    assert rendered.count(overrides_add) == 1
    assert rendered.index(abacus_add) < rendered.index(overrides_add)
    # Sparse clone staging remains in the per-env template.
    assert "git sparse-checkout set" in rendered
    assert "s8ga-spack-packages" in rendered


@pytest.mark.parametrize(
    ("env_name", "expected_image_paths"),
    [
        (
            "cp2k_opensource-2026.1-force-avx512",
            [
                "/opt/spack-repo/spack_repo/cp2k_dev_repo",
                "/opt/s8ga-spack-packages/spack_repo/s8_overrides",
                "/opt/spack-env-file/repos_cp2k_dev",
                "/opt/spack-env-file/repos",
            ],
        ),
        (
            "cp2k_opensource-2026.2-force-avx512",
            [
                "/opt/spack-repo/spack_repo/cp2k_dev",
                "/opt/s8ga-spack-packages/spack_repo/s8_overrides",
                "/opt/spack-env-file/repos",
            ],
        ),
    ],
)
def test_cp2k_force_avx512_dockerfile_wires_spack_image_repos_partial(
    env_name: str,
    expected_image_paths: list[str],
) -> None:
    """CP2K force-avx512: image repos from plan partial; sparse clone stays local."""
    env_dir = SPACK_ENVS_DIR / env_name
    src = (env_dir / "Dockerfile.j2").read_text(encoding="utf-8")
    assert "{% include 'partials/spack_image_repos.j2' %}" in src
    # Sparse clone may still use force_avx512_repo_path; handwritten dual add must not.
    assert (
        '/opt/s8ga-spack-packages/{{ force_avx512_repo_path }}' not in src
    )
    assert "repo add --scope {{ spack_repo_scope }}" not in src
    assert "{{ cp2k_dev_repo_path }}" in src  # staging cp remains
    assert "{{ force_avx512_repo_path }}" in src  # sparse-checkout remains

    spec = load_environment_spec(env_dir)
    plan = build_spack_environment_plan(spec)
    rendered = _render_env_dockerfile(env_dir)

    assert plan.env_name == "cp2k-env"
    assert plan.image.scope_flag() == "env:cp2k-env"
    assert [r.image_path for r in plan.image.repos] == expected_image_paths

    force_path = spec.template_vars["force_avx512_repo_path"]
    assert (
        f"/opt/s8ga-spack-packages/{force_path}"
        == "/opt/s8ga-spack-packages/spack_repo/s8_overrides"
    )
    s8ga = next(r for r in spec.spack.custom_repos if r.namespace == "s8_overrides")
    assert s8ga.image_path == f"/opt/s8ga-spack-packages/{force_path}"
    assert s8ga.sparse_path == force_path

    last = -1
    terminators = frozenset("/ \"'\n\t&")
    for path in expected_image_paths:
        prefix = f"spack -e cp2k-env repo add --scope env:cp2k-env {path}"
        matches: list[int] = []
        start = 0
        while True:
            cand = rendered.find(prefix, start)
            if cand < 0:
                break
            end = cand + len(prefix)
            if end >= len(rendered) or rendered[end] in terminators:
                matches.append(cand)
            start = cand + 1
        assert len(matches) == 1, (
            f"{env_name}: expected one bounded repo add for {path!r}, "
            f"found {len(matches)}"
        )
        assert matches[0] > last
        last = matches[0]

    # Sparse clone + cp2k_dev staging remain in the per-env template.
    assert "git sparse-checkout set" in rendered
    assert "s8ga-spack-packages" in rendered
    assert "cp -a /opt/cp2k/" in rendered


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

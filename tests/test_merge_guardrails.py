"""Merge guardrails: synthetic fixtures for A–D contracts.

Covers scope / mirror / phase / StrictUndefined, plus the optional
``spack_image_repos`` partial — without touching shipped application envs.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from hpc_cf.config import PROJECT_ROOT
from hpc_cf.environment import parse_environment_spec
from hpc_cf.spack_plan import build_spack_environment_plan, plan_context
from hpc_cf.template import build_context, render_template


def _write_synth_env(
    root: Path,
    *,
    env_yaml: str,
    dockerfile: str,
    name: str = "p5-synth",
) -> Path:
    env_root = root / "spack-envs" / name
    conf = env_root / "spack-env-file"
    conf.mkdir(parents=True)
    (conf / "env.yaml").write_text(env_yaml, encoding="utf-8")
    (conf / "spack.yaml").write_text("spack:\n  specs: [pkgconf]\n", encoding="utf-8")
    (env_root / "Dockerfile.j2").write_text(dockerfile, encoding="utf-8")
    return env_root


def test_unknown_phase_and_scope_fail_closed() -> None:
    """Unknown custom_repos phases / repo_scope must raise (Stream B)."""
    with pytest.raises(ValueError, match=r"phases|Unknown"):
        parse_environment_spec(
            {
                "schema_version": 1,
                "spack": {
                    "version": "1.1.1",
                    "env_name": "e",
                    "custom_repos": [
                        {"path": "repos", "namespace": "x", "phases": "build"}
                    ],
                },
            }
        )
    with pytest.raises(ValueError, match=r"repo_scope|Unknown"):
        parse_environment_spec(
            {
                "schema_version": 1,
                "spack": {
                    "version": "1.1.1",
                    "env_name": "e",
                    "image": {"repo_scope": "user"},
                },
            }
        )


def test_synthetic_scope_mirror_phase_and_strict_undefined(tmp_path: Path) -> None:
    """One synthetic env exercises mirror/repo scope, image-only phase, StrictUndefined."""
    env_yaml = (
        "schema_version: 1\n"
        "method: spack\n"
        "spack:\n"
        "  version: '1.1.1'\n"
        "  env_name: synth-env\n"
        "  image:\n"
        "    repo_scope: env\n"
        "    update_builtin: true\n"
        "  custom_repos:\n"
        "    - path: repos\n"
        "      namespace: both-repo\n"
        "      phases: both\n"
        "    - path: image-only\n"
        "      namespace: image-repo\n"
        "      phases: image\n"
        "template_vars:\n"
        "  required_token: ok\n"
    )
    dockerfile = (
        "FROM debian:trixie\n"
        "TOKEN={{ required_token }}\n"
        "{% include 'partials/spack_mirror.j2' %}\n"
        "{% include 'partials/spack_image_repos.j2' %}\n"
    )
    env_root = _write_synth_env(tmp_path, env_yaml=env_yaml, dockerfile=dockerfile)
    tpl = env_root / "Dockerfile.j2"

    # Missing template_vars entry → StrictUndefined failure.
    with pytest.raises((RuntimeError, Exception), match="required_token"):
        bad_ctx = build_context(
            use_mirror=True,
            build_only=False,
            app_version="p5-synth",
            template_path=tpl,
        )
        bad_ctx.pop("required_token", None)
        render_template(tpl, bad_ctx)

    ctx = build_context(
        use_mirror=True,
        build_only=False,
        app_version="p5-synth",
        template_path=tpl,
    )
    rendered = render_template(tpl, ctx)

    assert "TOKEN=ok" in rendered
    assert "spack mirror add --scope site " in rendered
    assert "spack mirror add --scope env:" not in rendered
    assert "spack -e synth-env repo add --scope env:synth-env" in rendered
    assert "/opt/spack-env-file/repos" in rendered
    assert "/opt/spack-env-file/image-only" in rendered

    from hpc_cf.environment import load_environment_spec

    plan = build_spack_environment_plan(load_environment_spec(env_root))
    assert [r.namespace for r in plan.assets.repos] == ["both-repo"]
    assert [r.namespace for r in plan.image.repos] == ["both-repo", "image-repo"]
    assert plan.mirror_scope_flag() == "site"
    assert plan.image.scope_flag() == "env:synth-env"


def test_spack_image_repos_partial_from_plan_context(tmp_path: Path) -> None:
    """Optional partial: register image repos from plan_context only (no app wiring)."""
    spec = parse_environment_spec(
        {
            "schema_version": 1,
            "spack": {
                "version": "1.1.1",
                "env_name": "demo-env",
                "image": {"repo_scope": "site", "update_builtin": False},
                "custom_repos": [
                    {
                        "path": "repos",
                        "namespace": "local-a",
                        "phases": "both",
                    },
                    {
                        "url": "https://example.com/r.git",
                        "namespace": "git-b",
                        "sparse_path": "tools/spack/spack_repo/git_b",
                        "phases": "image",
                        "image_path": "/opt/custom/git_b",
                    },
                ],
            },
        }
    )
    plan = build_spack_environment_plan(spec)
    ctx = {
        "use_mirror": False,
        "build_only": False,
        **plan_context(plan),
    }
    tpl = tmp_path / "Dockerfile.j2"
    tpl.write_text("{% include 'partials/spack_image_repos.j2' %}\n", encoding="utf-8")
    rendered = render_template(tpl, ctx)

    assert "spack -e demo-env repo add --scope site /opt/spack-env-file/repos" in rendered
    assert "spack -e demo-env repo add --scope site /opt/custom/git_b" in rendered
    # assets-only repos must not appear; both + image do, in YAML order.
    assert rendered.index("/opt/spack-env-file/repos") < rendered.index("/opt/custom/git_b")


def test_dual_write_guard_runs_independently() -> None:
    script = PROJECT_ROOT / "scripts" / "check-dual-write.py"
    result = subprocess.run(
        [sys.executable, str(script), "--project-root", str(PROJECT_ROOT)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "dual-write guard passed" in result.stdout


def test_dual_write_guard_rejects_drift(tmp_path: Path) -> None:
    env_dir = tmp_path / "spack-envs" / "cp2k-drift" / "spack-env-file"
    env_dir.mkdir(parents=True)
    (env_dir / "env.yaml").write_text(
        "schema_version: 1\nmethod: spack\n"
        "spack:\n"
        "  version: '1.1.1'\n"
        "  env_name: cp2k-env\n"
        "  custom_repos:\n"
        "    - url: https://github.com/cp2k/cp2k.git\n"
        "      branch: correct-branch\n"
        "      sparse_path: tools/spack/repo\n"
        "      namespace: cp2k\n"
        "template_vars:\n"
        "  cp2k_branch: wrong-branch\n"
        "  cp2k_dev_repo_path: tools/spack/repo\n",
        encoding="utf-8",
    )
    script = PROJECT_ROOT / "scripts" / "check-dual-write.py"
    result = subprocess.run(
        [sys.executable, str(script), "--project-root", str(tmp_path)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "cp2k_branch" in result.stdout
    assert "wrong-branch" in result.stdout
    assert "correct-branch" in result.stdout


def test_dual_write_guard_rejects_comment_only_cp2k_branch(tmp_path: Path) -> None:
    """Comment-only {{ cp2k_branch }} must not satisfy the dual-write guard."""
    env_yaml = (
        "schema_version: 1\nmethod: spack\n"
        "spack:\n"
        "  version: '1.2.0'\n"
        "  env_name: cp2k-env\n"
        "  custom_repos:\n"
        "    - url: https://github.com/cp2k/cp2k.git\n"
        "      branch: support/v2026.2\n"
        "      sparse_path: tools/spack/spack_repo/cp2k_dev\n"
        "      namespace: cp2k_dev\n"
        "template_vars:\n"
        "  cp2k_branch: support/v2026.2\n"
        "  cp2k_dev_repo_path: tools/spack/spack_repo/cp2k_dev\n"
    )
    dockerfile = (
        "# Branch {{ cp2k_branch }} is pinned at deadbeef\n"
        "RUN git clone --filter=blob:none --no-checkout "
        "https://github.com/cp2k/cp2k.git /opt/cp2k && "
        "git checkout deadbeef\n"
        "RUN cp -a /opt/cp2k/{{ cp2k_dev_repo_path }} /opt/spack-repo\n"
    )
    _write_synth_env(
        tmp_path,
        env_yaml=env_yaml,
        dockerfile=dockerfile,
        name="cp2k-comment-only-branch",
    )

    script = PROJECT_ROOT / "scripts" / "check-dual-write.py"
    result = subprocess.run(
        [sys.executable, str(script), "--project-root", str(tmp_path)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "cp2k_branch" in result.stdout
    assert "does not use" in result.stdout


@pytest.mark.parametrize(
    "template_vars, missing_key",
    [
        ("  cp2k_branch: correct-branch\n", "cp2k_dev_repo_path"),
        ("", "cp2k_branch"),
    ],
)
def test_dual_write_guard_rejects_missing_keys(
    tmp_path: Path,
    template_vars: str,
    missing_key: str,
) -> None:
    env_yaml = (
        "schema_version: 1\nmethod: spack\n"
        "spack:\n"
        "  version: '1.1.1'\n"
        "  env_name: cp2k-env\n"
        "  custom_repos:\n"
        "    - url: https://github.com/cp2k/cp2k.git\n"
        "      branch: correct-branch\n"
        "      sparse_path: tools/spack/repo\n"
        "      namespace: cp2k\n"
        f"template_vars:\n{template_vars}"
    )
    dockerfile = (
        "RUN git clone -b {{ cp2k_branch }} "
        "https://github.com/cp2k/cp2k.git /opt/cp2k\n"
        "RUN cp -a /opt/cp2k/{{ cp2k_dev_repo_path }} /opt/spack-repo\n"
    )
    _write_synth_env(
        tmp_path,
        env_yaml=env_yaml,
        dockerfile=dockerfile,
        name="cp2k-missing",
    )

    script = PROJECT_ROOT / "scripts" / "check-dual-write.py"
    result = subprocess.run(
        [sys.executable, str(script), "--project-root", str(tmp_path)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert missing_key in result.stdout
    if not template_vars:
        assert "cp2k_dev_repo_path" in result.stdout


def test_dual_write_guard_rejects_ambiguous_cp2k_git_repos(
    tmp_path: Path,
) -> None:
    env_yaml = (
        "schema_version: 1\nmethod: spack\n"
        "spack:\n"
        "  version: '1.1.1'\n"
        "  env_name: cp2k-env\n"
        "  custom_repos:\n"
        "    - url: https://github.com/cp2k/cp2k.git\n"
        "      namespace: cp2k-one\n"
        "    - url: https://example.com/cp2k-overlay.git\n"
        "      namespace: cp2k-two\n"
        "template_vars:\n"
        "  cp2k_branch: main\n"
        "  cp2k_dev_repo_path: tools/spack/repo\n"
    )
    _write_synth_env(
        tmp_path,
        env_yaml=env_yaml,
        dockerfile="RUN git clone https://github.com/cp2k/cp2k /opt/cp2k\n",
        name="cp2k-ambiguous",
    )

    script = PROJECT_ROOT / "scripts" / "check-dual-write.py"
    result = subprocess.run(
        [sys.executable, str(script), "--project-root", str(tmp_path)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "multiple CP2K git custom_repos match by namespace" in result.stdout


def test_dual_write_guard_rejects_mixed_namespace_and_url_matches(
    tmp_path: Path,
) -> None:
    env_yaml = (
        "schema_version: 1\nmethod: spack\n"
        "spack:\n"
        "  version: '1.1.1'\n"
        "  env_name: cp2k-env\n"
        "  custom_repos:\n"
        "    - url: https://github.com/cp2k/cp2k.git\n"
        "      namespace: cp2k-main\n"
        "    - url: https://example.com/cp2k-overlay.git\n"
        "      namespace: overlay\n"
        "template_vars:\n"
        "  cp2k_branch: main\n"
        "  cp2k_dev_repo_path: tools/spack/repo\n"
    )
    _write_synth_env(
        tmp_path,
        env_yaml=env_yaml,
        dockerfile="RUN git clone https://github.com/cp2k/cp2k /opt/cp2k\n",
        name="cp2k-mixed-ambiguous",
    )

    script = PROJECT_ROOT / "scripts" / "check-dual-write.py"
    result = subprocess.run(
        [sys.executable, str(script), "--project-root", str(tmp_path)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "multiple CP2K git custom_repos match" in result.stdout


def test_dual_write_guard_rejects_hardcoded_cp2k_repo_path(
    tmp_path: Path,
) -> None:
    env_yaml = (
        "schema_version: 1\nmethod: spack\n"
        "spack:\n"
        "  version: '1.1.1'\n"
        "  env_name: cp2k-env\n"
        "  custom_repos:\n"
        "    - url: https://github.com/cp2k/cp2k.git\n"
        "      branch: main\n"
        "      sparse_path: tools/spack/cp2k_dev_repo\n"
        "      namespace: cp2k\n"
        "template_vars:\n"
        "  cp2k_branch: main\n"
        "  cp2k_dev_repo_path: tools/spack/cp2k_dev_repo\n"
    )
    dockerfile = (
        "RUN git clone -b {{ cp2k_branch }} "
        "https://github.com/cp2k/cp2k.git /opt/cp2k\n"
        "RUN cp -a /opt/cp2k/tools/spack/cp2k_dev_repo /opt/spack-repo\n"
        "# {{ cp2k_dev_repo_path }}\n"
    )
    _write_synth_env(
        tmp_path,
        env_yaml=env_yaml,
        dockerfile=dockerfile,
        name="cp2k-hardcoded",
    )

    script = PROJECT_ROOT / "scripts" / "check-dual-write.py"
    result = subprocess.run(
        [sys.executable, str(script), "--project-root", str(tmp_path)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "hard-codes CP2K repo path" in result.stdout


def test_dual_write_guard_ignores_non_cp2k_env(tmp_path: Path) -> None:
    _write_synth_env(
        tmp_path,
        env_yaml=(
            "schema_version: 1\nmethod: spack\n"
            "spack:\n"
            "  version: '1.1.1'\n"
            "  env_name: other-env\n"
            "  custom_repos:\n"
            "    - url: https://example.com/other.git\n"
            "      branch: main\n"
            "      sparse_path: packages\n"
            "      namespace: other\n"
        ),
        dockerfile="FROM debian:trixie\n",
        name="not-cp2k",
    )

    script = PROJECT_ROOT / "scripts" / "check-dual-write.py"
    result = subprocess.run(
        [sys.executable, str(script), "--project-root", str(tmp_path)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_dual_write_guard_rejects_s8ga_commit_drift(tmp_path: Path) -> None:
    pin = "d0ee3f460a2543c05c693317c767652abf964db7"
    wrong = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    env_yaml = (
        "schema_version: 1\nmethod: spack\n"
        "spack:\n"
        "  version: '1.2.0'\n"
        "  env_name: abacus-env\n"
        "  custom_repos:\n"
        "    - url: https://github.com/s8ga/s8ga-spack-packages.git\n"
        "      branch: master\n"
        f"      commit: {pin}\n"
        "      sparse_path: spack_repo/abacus\n"
        "      namespace: abacus\n"
        "    - url: https://github.com/s8ga/s8ga-spack-packages.git\n"
        "      branch: master\n"
        f"      commit: {wrong}\n"
        "      sparse_path: spack_repo/s8_overrides\n"
        "      namespace: s8_overrides\n"
        "template_vars:\n"
        f"  s8ga_repo_commit: {pin}\n"
    )
    _write_synth_env(
        tmp_path,
        env_yaml=env_yaml,
        dockerfile="FROM debian:trixie\n",
        name="abacus-s8ga-drift",
    )

    script = PROJECT_ROOT / "scripts" / "check-dual-write.py"
    result = subprocess.run(
        [sys.executable, str(script), "--project-root", str(tmp_path)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "s8ga_repo_commit" in result.stdout
    assert "s8_overrides" in result.stdout
    assert wrong in result.stdout


def test_dual_write_guard_skips_s8ga_when_neither_side_pinned(
    tmp_path: Path,
) -> None:
    """s8ga custom_repos without commit or template var are skipped."""
    env_yaml = (
        "schema_version: 1\nmethod: spack\n"
        "spack:\n"
        "  version: '1.2.0'\n"
        "  env_name: cp2k-env\n"
        "  custom_repos:\n"
        "    - url: https://github.com/cp2k/cp2k.git\n"
        "      branch: support/v2026.2\n"
        "      sparse_path: tools/spack/spack_repo/cp2k_dev\n"
        "      namespace: cp2k_dev\n"
        "    - url: https://github.com/s8ga/s8ga-spack-packages.git\n"
        "      branch: master\n"
        "      sparse_path: spack_repo/s8_overrides\n"
        "      namespace: s8_overrides\n"
        "template_vars:\n"
        "  cp2k_branch: support/v2026.2\n"
        "  cp2k_dev_repo_path: tools/spack/spack_repo/cp2k_dev\n"
    )
    dockerfile = (
        "RUN git clone -b {{ cp2k_branch }} "
        "https://github.com/cp2k/cp2k.git /opt/cp2k\n"
        "RUN cp -a /opt/cp2k/{{ cp2k_dev_repo_path }} /opt/spack-repo\n"
    )
    _write_synth_env(
        tmp_path,
        env_yaml=env_yaml,
        dockerfile=dockerfile,
        name="cp2k-s8ga-unpinned",
    )

    script = PROJECT_ROOT / "scripts" / "check-dual-write.py"
    result = subprocess.run(
        [sys.executable, str(script), "--project-root", str(tmp_path)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_dual_write_guard_rejects_s8ga_commit_without_template_var(
    tmp_path: Path,
) -> None:
    pin = "d0ee3f460a2543c05c693317c767652abf964db7"
    env_yaml = (
        "schema_version: 1\nmethod: spack\n"
        "spack:\n"
        "  version: '1.2.0'\n"
        "  env_name: cp2k-env\n"
        "  custom_repos:\n"
        "    - url: https://github.com/cp2k/cp2k.git\n"
        "      branch: support/v2026.2\n"
        "      sparse_path: tools/spack/spack_repo/cp2k_dev\n"
        "      namespace: cp2k_dev\n"
        "    - url: https://github.com/s8ga/s8ga-spack-packages.git\n"
        "      branch: master\n"
        f"      commit: {pin}\n"
        "      sparse_path: spack_repo/s8_overrides\n"
        "      namespace: s8_overrides\n"
        "template_vars:\n"
        "  cp2k_branch: support/v2026.2\n"
        "  cp2k_dev_repo_path: tools/spack/spack_repo/cp2k_dev\n"
    )
    dockerfile = (
        "RUN git clone -b {{ cp2k_branch }} "
        "https://github.com/cp2k/cp2k.git /opt/cp2k\n"
        "RUN cp -a /opt/cp2k/{{ cp2k_dev_repo_path }} /opt/spack-repo\n"
    )
    _write_synth_env(
        tmp_path,
        env_yaml=env_yaml,
        dockerfile=dockerfile,
        name="cp2k-s8ga-commit-only",
    )

    script = PROJECT_ROOT / "scripts" / "check-dual-write.py"
    result = subprocess.run(
        [sys.executable, str(script), "--project-root", str(tmp_path)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "s8ga_repo_commit" in result.stdout
    assert "missing" in result.stdout


def test_dual_write_guard_accepts_matching_s8ga_commit(tmp_path: Path) -> None:
    pin = "d0ee3f460a2543c05c693317c767652abf964db7"
    env_yaml = (
        "schema_version: 1\nmethod: spack\n"
        "spack:\n"
        "  version: '1.2.0'\n"
        "  env_name: abacus-env\n"
        "  custom_repos:\n"
        "    - url: https://github.com/s8ga/s8ga-spack-packages.git\n"
        "      branch: master\n"
        f"      commit: {pin}\n"
        "      sparse_path: spack_repo/abacus\n"
        "      namespace: abacus\n"
        "    - url: https://github.com/s8ga/s8ga-spack-packages.git\n"
        "      branch: master\n"
        f"      commit: {pin}\n"
        "      sparse_path: spack_repo/s8_overrides\n"
        "      namespace: s8_overrides\n"
        "template_vars:\n"
        f"  s8ga_repo_commit: {pin}\n"
    )
    _write_synth_env(
        tmp_path,
        env_yaml=env_yaml,
        dockerfile="FROM debian:trixie\n",
        name="abacus-s8ga-ok",
    )

    script = PROJECT_ROOT / "scripts" / "check-dual-write.py"
    result = subprocess.run(
        [sys.executable, str(script), "--project-root", str(tmp_path)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "dual-write guard passed" in result.stdout

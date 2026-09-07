"""apply-source-override.sh: `<base>-fork[-<label>]` envs via symlink.

Contract: non-fork names are a no-op; fork names materialize a symlink to
the base env and (with fork_url/fork_branch) patch the env-local cp2k
recipe's git attribute / master branch plus the matching template vars in
env.yaml. Re-runs are idempotent (the already-patched state is accepted);
a missing base env fails closed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from hpc_cf.config import PROJECT_ROOT

SCRIPT = PROJECT_ROOT / "scripts" / "apply-source-override.sh"
BASE = "cp2k_opensource-master-force-avx512"
UPSTREAM = "https://github.com/cp2k/cp2k.git"
FORK = "https://github.com/someone/cp2k.git"


def _make_base(root: Path) -> None:
    conf = root / "spack-envs" / BASE / "spack-env-file" / "repos" / "packages" / "cp2k"
    conf.mkdir(parents=True)
    (conf / "package.py").write_text(
        '    git = "https://github.com/cp2k/cp2k.git"\n'
        '    version("master", branch="master", submodules=True)\n',
        encoding="utf-8",
    )
    (root / "spack-envs" / BASE / "spack-env-file" / "env.yaml").write_text(
        "  custom_repos:\n"
        "    - url: https://github.com/cp2k/cp2k.git\n"
        "      branch: master\n"
        "      sparse_path: tools/spack/spack_repo/cp2k_dev\n"
        "      namespace: cp2k_dev\n"
        "template_vars:\n"
        "  cp2k_branch: master\n"
        "  cp2k_dev_repo_commit: dd0921c61c802d1a0b9669263c27f207a68123dc\n",
        encoding="utf-8",
    )


def _run(root: Path, env: str, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), env, *extra],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def _fork_dir(root: Path, name: str) -> Path:
    return root / "spack-envs" / name


def test_non_fork_name_is_noop(tmp_path: Path) -> None:
    _make_base(tmp_path)
    result = _run(tmp_path, BASE)
    assert result.returncode == 0
    assert "nothing to do" in result.stdout
    assert not (_fork_dir(tmp_path, BASE)).is_symlink()


def test_fork_env_materializes_and_patches(tmp_path: Path) -> None:
    _make_base(tmp_path)
    env = f"{BASE}-fork-alice"
    result = _run(tmp_path, env, FORK, "experimental")
    assert result.returncode == 0, result.stderr
    link = _fork_dir(tmp_path, env)
    assert link.is_symlink() and link.resolve().name == BASE
    recipe = (link / "spack-env-file" / "repos" / "packages" / "cp2k" / "package.py").read_text(
        encoding="utf-8"
    )
    assert f'git = "{FORK}"' in recipe
    assert 'version("master", branch="experimental"' in recipe
    env_yaml = (link / "spack-env-file" / "env.yaml").read_text(encoding="utf-8")
    assert f'cp2k_source_repo_url: "{FORK}"' in env_yaml
    assert 'cp2k_branch: "experimental"' in env_yaml
    # the cp2k_dev recipe-repo float follows the fork too
    assert f"url: {FORK}" in env_yaml
    assert "branch: experimental" in env_yaml
    # writes go THROUGH the symlink: the base env is the single source of truth
    assert f'git = "{FORK}"' in (
        _fork_dir(tmp_path, BASE) / "spack-env-file" / "repos" / "packages" / "cp2k" / "package.py"
    ).read_text(encoding="utf-8")


def test_re_run_is_idempotent(tmp_path: Path) -> None:
    _make_base(tmp_path)
    env = f"{BASE}-fork"
    assert _run(tmp_path, env, FORK, "experimental").returncode == 0
    result = _run(tmp_path, env, FORK, "experimental")
    assert result.returncode == 0, result.stderr
    recipe = (
        _fork_dir(tmp_path, env) / "spack-env-file" / "repos" / "packages" / "cp2k" / "package.py"
    ).read_text(encoding="utf-8")
    assert recipe.count(f'git = "{FORK}"') == 1


def test_materialize_only_without_inputs(tmp_path: Path) -> None:
    _make_base(tmp_path)
    env = f"{BASE}-fork"
    result = _run(tmp_path, env)
    assert result.returncode == 0
    assert _fork_dir(tmp_path, env).is_symlink()
    recipe = (
        _fork_dir(tmp_path, env) / "spack-env-file" / "repos" / "packages" / "cp2k" / "package.py"
    ).read_text(encoding="utf-8")
    assert f'git = "{UPSTREAM}"' in recipe  # untouched


def test_branch_without_url_redirects_recipe_repo_float(tmp_path: Path) -> None:
    _make_base(tmp_path)
    env = f"{BASE}-fork"
    result = _run(tmp_path, env, "", "feature-x")
    assert result.returncode == 0, result.stderr
    env_yaml = (_fork_dir(tmp_path, env) / "spack-env-file" / "env.yaml").read_text(
        encoding="utf-8"
    )
    assert "url: https://github.com/cp2k/cp2k.git" in env_yaml  # url untouched
    assert "branch: feature-x" in env_yaml  # float branch redirected


def test_missing_base_fails_closed(tmp_path: Path) -> None:
    result = _run(tmp_path, f"{BASE}-fork", FORK, "experimental")
    assert result.returncode == 1
    assert "fork base env missing" in result.stderr

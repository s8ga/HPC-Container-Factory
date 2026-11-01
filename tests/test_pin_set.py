"""``hpc_cf pin set`` — atomic repo-pin bump across dual-write copies.

Bumping a monorepo pin used to be a 3+ site hand edit per env across many
envs. pin set matches repos by namespace/URL, replaces the old sha in
env.yaml surgically (comments survive), re-verifies with the repo-pin
guard, and tells you the lock is now stale.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_cf.cli import build_parser, run_new_cli
from hpc_cf.execution import ProjectLayout
from hpc_cf.pin import run_pin_set
from hpc_cf.requests import pin_request_from_args

PIN = "d0ee3f460a2543c05c693317c767652abf964db7"
NEW_PIN = "1234567890abcdef1234567890abcdef12345678"
S8GA_URL = "https://github.com/s8ga/s8ga-spack-packages.git"

_DOCKERFILE = (
    "RUN git clone --filter=blob:none --sparse --no-checkout "
    "{{ s8ga_repo_url }} /opt/s8ga-spack-packages && \\\n"
    "    git fetch --depth 1 origin {{ s8ga_repo_commit }} && \\\n"
    '    git sparse-checkout set "{{ s8ga_app_repo_path }}" '
    '"{{ s8ga_overrides_repo_path }}" && \\\n'
    '    git checkout "{{ s8ga_repo_commit }}"\n'
)


def _write_env(
    root: Path,
    name: str,
    *,
    commits: tuple[str, str] = (PIN, PIN),
    template_commit: str | None = PIN,
) -> Path:
    env_root = root / "spack-envs" / name
    conf = env_root / "spack-env-file"
    conf.mkdir(parents=True)
    (conf / "env.yaml").write_text(
        "schema_version: 1\nmethod: spack\n"
        "spack:\n"
        "  version: '1.2.0'\n"
        "  env_name: pin-env\n"
        "  custom_repos:\n"
        f"    - url: {S8GA_URL}\n"
        "      branch: master\n"
        f"      commit: {commits[0]}\n"
        "      sparse_path: spack_repo/app\n"
        "      namespace: app\n"
        "      image_path: /opt/s8ga-spack-packages/spack_repo/app\n"
        f"    - url: {S8GA_URL}\n"
        "      branch: master\n"
        f"      commit: {commits[1]}\n"
        "      sparse_path: spack_repo/s8_overrides\n"
        "      namespace: s8_overrides\n"
        "      image_path: /opt/s8ga-spack-packages/spack_repo/s8_overrides\n"
        "template_vars:\n"
        f"  s8ga_repo_url: {S8GA_URL}\n"
        + (
            f"  s8ga_repo_commit: {template_commit}\n"
            if template_commit is not None
            else ""
        )
        + "  s8ga_app_repo_path: spack_repo/app\n"
        "  s8ga_overrides_repo_path: spack_repo/s8_overrides\n",
        encoding="utf-8",
    )
    (conf / "spack.yaml").write_text("spack:\n  specs: [pkgconf]\n", encoding="utf-8")
    (env_root / "Dockerfile.j2").write_text(_DOCKERFILE, encoding="utf-8")
    return env_root


def _request(**kwargs) -> object:
    from hpc_cf.requests import PinSetRequest

    defaults = {
        "envs": ("demo",),
        "repo": "s8ga",
        "commit": NEW_PIN,
    }
    defaults.update(kwargs)
    return PinSetRequest(**defaults)


# --- happy path ----------------------------------------------------------------


def test_pin_set_updates_all_copies_and_passes_guard(tmp_path: Path) -> None:
    _write_env(tmp_path, "demo")
    rc = run_pin_set(_request(), layout=ProjectLayout(project_root=tmp_path))
    assert rc == 0
    text = (tmp_path / "spack-envs" / "demo" / "spack-env-file" / "env.yaml").read_text(
        encoding="utf-8"
    )
    assert NEW_PIN in text
    assert PIN not in text

    from hpc_cf.environment import load_environment_spec

    spec = load_environment_spec(tmp_path / "spack-envs" / "demo")
    assert all(
        repo.commit == NEW_PIN
        for repo in spec.spack.custom_repos
        if repo.type == "git"
    )
    assert spec.template_vars["s8ga_repo_commit"] == NEW_PIN


def test_pin_set_preserves_comments(tmp_path: Path) -> None:
    env_root = _write_env(tmp_path, "demo")
    env_yaml = env_root / "spack-env-file" / "env.yaml"
    original = env_yaml.read_text(encoding="utf-8")
    env_yaml.write_text(
        "# pinned at " + PIN + " — do not hand-edit, use pin set\n" + original,
        encoding="utf-8",
    )
    rc = run_pin_set(_request(), layout=ProjectLayout(project_root=tmp_path))
    assert rc == 0
    text = env_yaml.read_text(encoding="utf-8")
    assert "# pinned at " + NEW_PIN in text  # comment mentions updated too
    assert PIN not in text


def test_pin_set_dry_run_leaves_file_untouched(tmp_path: Path) -> None:
    env_root = _write_env(tmp_path, "demo")
    env_yaml = env_root / "spack-env-file" / "env.yaml"
    before = env_yaml.read_text(encoding="utf-8")
    rc = run_pin_set(_request(dry_run=True), layout=ProjectLayout(project_root=tmp_path))
    assert rc == 0
    assert env_yaml.read_text(encoding="utf-8") == before


def test_pin_set_noop_when_already_pinned(tmp_path: Path) -> None:
    _write_env(tmp_path, "demo")
    rc = run_pin_set(_request(commit=PIN), layout=ProjectLayout(project_root=tmp_path))
    assert rc == 0
    text = (tmp_path / "spack-envs" / "demo" / "spack-env-file" / "env.yaml").read_text(
        encoding="utf-8"
    )
    assert PIN in text and NEW_PIN not in text


# --- refusals -------------------------------------------------------------------


def test_pin_set_refuses_unknown_env(tmp_path: Path) -> None:
    rc = run_pin_set(_request(), layout=ProjectLayout(project_root=tmp_path))
    assert rc == 1


def test_pin_set_refuses_drifted_missing_var_copy(tmp_path: Path) -> None:
    _write_env(tmp_path, "demo", template_commit=None)
    env_yaml = (
        tmp_path / "spack-envs" / "demo" / "spack-env-file" / "env.yaml"
    )
    before = env_yaml.read_text(encoding="utf-8")
    rc = run_pin_set(_request(), layout=ProjectLayout(project_root=tmp_path))
    assert rc == 1
    assert env_yaml.read_text(encoding="utf-8") == before


def test_pin_set_refuses_inconsistent_repo_pair(tmp_path: Path) -> None:
    _write_env(
        tmp_path, "demo", commits=(PIN, NEW_PIN), template_commit=PIN
    )
    rc = run_pin_set(_request(), layout=ProjectLayout(project_root=tmp_path))
    assert rc == 1


def test_pin_set_refuses_floating_repo(tmp_path: Path) -> None:
    env_root = _write_env(tmp_path, "demo")
    env_yaml = env_root / "spack-env-file" / "env.yaml"
    text = env_yaml.read_text(encoding="utf-8").replace(
        f"      commit: {PIN}\n", "", 1
    )
    env_yaml.write_text(text, encoding="utf-8")
    rc = run_pin_set(_request(), layout=ProjectLayout(project_root=tmp_path))
    assert rc == 1


def test_pin_set_rejects_non_sha_commit(tmp_path: Path) -> None:
    _write_env(tmp_path, "demo")
    rc = run_pin_set(
        _request(commit="main"), layout=ProjectLayout(project_root=tmp_path)
    )
    assert rc == 1


# --- --all semantics -------------------------------------------------------------


def test_pin_set_all_skips_non_matching_envs(tmp_path: Path) -> None:
    _write_env(tmp_path, "demo")
    other = tmp_path / "spack-envs" / "other" / "spack-env-file"
    other.mkdir(parents=True)
    (other / "env.yaml").write_text(
        "schema_version: 1\nmethod: no_spack\n",
        encoding="utf-8",
    )
    rc = run_pin_set(
        _request(envs=(), all_envs=True),
        layout=ProjectLayout(project_root=tmp_path),
    )
    assert rc == 0
    demo_yaml = (
        tmp_path / "spack-envs" / "demo" / "spack-env-file" / "env.yaml"
    ).read_text(encoding="utf-8")
    assert NEW_PIN in demo_yaml
    assert "schema_version: 1" in (
        other / "env.yaml"
    ).read_text(encoding="utf-8")


# --- CLI mapping -----------------------------------------------------------------


def test_pin_set_request_from_cli_args() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["pin", "set", "--env", "a", "--env", "b", "--repo", "s8ga",
         "--commit", NEW_PIN, "--dry-run"]
    )
    request = pin_request_from_args(args)
    assert request.envs == ("a", "b")
    assert request.repo == "s8ga"
    assert request.commit == NEW_PIN
    assert request.dry_run is True
    assert request.all_envs is False


def test_pin_set_requires_env_or_all() -> None:
    with pytest.raises(SystemExit) as excinfo:
        run_new_cli(["pin", "set", "--repo", "s8ga", "--commit", NEW_PIN])
    assert excinfo.value.code == 2


def test_pin_set_rejects_env_and_all_combo() -> None:
    with pytest.raises(SystemExit) as excinfo:
        run_new_cli(
            ["pin", "set", "--env", "a", "--all", "--repo", "s8ga",
             "--commit", NEW_PIN]
        )
    assert excinfo.value.code == 2


def test_pin_set_cli_dispatch(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    from hpc_cf import pin as pin_module
    from hpc_cf.requests import PinSetRequest

    seen: list[PinSetRequest] = []

    def _fake_run(request: PinSetRequest, **_kwargs) -> int:
        seen.append(request)
        return 0

    monkeypatch.setattr(pin_module, "run_pin_set", _fake_run)
    rc = run_new_cli(
        ["pin", "set", "--all", "--repo", "s8ga", "--commit", NEW_PIN]
    )
    assert rc == 0
    assert seen and seen[0].all_envs is True and seen[0].repo == "s8ga"

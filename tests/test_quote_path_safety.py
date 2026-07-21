"""Adversarial quoting + path-confinement tests (P0-D / P0.5 non-sif).

Covers:
* ``shell_quote`` / Jinja ``shell_quote`` filter against spaces, quotes, ``$()``, newlines
* ``spack_image_repos.j2`` RUN lines emitting quoted tokens
* ``--env`` / ``--app-version`` / ``manual_packages.file`` cannot escape the project tree
* ``system_pkgs`` quoted per-package inside ``bash -c``
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hpc_cf.container import Container
from hpc_cf.environment import (
    EnvironmentSpec,
    ManualPackage,
    MirrorBuilderConfig,
    SpackConfig,
    parse_environment_spec,
)
from hpc_cf.execution import ProjectLayout
from hpc_cf.shell_quote import confine_to_root, shell_quote
from hpc_cf.spack_ops import SpackOps
from hpc_cf.spack_plan import build_spack_environment_plan, plan_context
from hpc_cf.template import render_template, resolve_build_input
from hpc_cf.validation import collect_manual_packages


# ── shell_quote helper ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        "plain",
        "has space",
        "has'quote",
        "cmd$(reboot)",
        "a\nb",
        "end\\",
        "",
    ],
)
def test_shell_quote_roundtrips_via_bash(raw: str) -> None:
    """Quoted form must survive ``bash -c 'printf %s …'`` without expansion."""
    quoted = shell_quote(raw)
    # Newlines inside single quotes are literal; bash -c still receives them.
    result = subprocess.run(
        ["bash", "-c", f"printf '%s' {quoted}"],
        capture_output=True,
        check=True,
    )
    assert result.stdout.decode("utf-8", errors="surrogateescape") == raw


def test_shell_quote_blocks_command_substitution_token() -> None:
    dangerous = "x$(echo OWNED)"
    quoted = shell_quote(dangerous)
    assert "$(" not in quoted or quoted.startswith("'")
    result = subprocess.run(
        ["bash", "-c", f"printf '%s' {quoted}"],
        capture_output=True,
        check=True,
        text=True,
    )
    assert result.stdout == dangerous
    assert "OWNED" not in result.stdout or result.stdout == dangerous


# ── spack_image_repos.j2 quoting ──────────────────────────────────────────


def test_spack_image_repos_quotes_adversarial_tokens(tmp_path: Path) -> None:
    spec = parse_environment_spec(
        {
            "schema_version": 1,
            "spack": {
                "version": "1.1.1",
                "env_name": "env with space",
                "image": {"repo_scope": "env"},
                "custom_repos": [
                    {
                        "path": "repos",
                        "namespace": "local-a",
                        "phases": "image",
                        "image_path": "/opt/path with space/repo",
                    },
                    {
                        "url": "https://example.com/r.git",
                        "namespace": "git-b",
                        "phases": "image",
                        "image_path": "/opt/p$(id)/repo",
                    },
                ],
            },
        }
    )
    plan = build_spack_environment_plan(spec)
    ctx = {**plan_context(plan), "use_mirror": False, "build_only": False}
    tpl = tmp_path / "Dockerfile.j2"
    tpl.write_text(
        "{% include 'partials/spack_image_repos.j2' %}\n", encoding="utf-8"
    )
    rendered = render_template(tpl, ctx)

    assert "spack -e 'env with space' repo add" in rendered
    assert "--scope 'env:env with space'" in rendered
    assert "'/opt/path with space/repo'" in rendered
    assert "'/opt/p$(id)/repo'" in rendered
    # Bare (unquoted) forms must not appear as shell words.
    assert " -e env with space " not in rendered
    assert " /opt/path with space/repo" not in rendered
    assert " /opt/p$(id)/repo" not in rendered


def test_spack_image_repos_quotes_newline_in_image_path(tmp_path: Path) -> None:
    spec = parse_environment_spec(
        {
            "schema_version": 1,
            "spack": {
                "version": "1.1.1",
                "env_name": "demo",
                "image": {"repo_scope": "site"},
                "custom_repos": [
                    {
                        "path": "repos",
                        "namespace": "evil",
                        "phases": "image",
                        "image_path": "/opt/a\nbad",
                    },
                ],
            },
        }
    )
    plan = build_spack_environment_plan(spec)
    ctx = {**plan_context(plan), "use_mirror": False, "build_only": False}
    tpl = tmp_path / "Dockerfile.j2"
    tpl.write_text(
        "{% include 'partials/spack_image_repos.j2' %}\n", encoding="utf-8"
    )
    rendered = render_template(tpl, ctx)
    assert shell_quote("/opt/a\nbad") in rendered


# ── path confinement ──────────────────────────────────────────────────────


def test_confine_to_root_rejects_parent_escape(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="escapes project root"):
        confine_to_root(root / ".." / "secret.txt", root=root, label="test")


def test_resolve_env_paths_rejects_dotdot(tmp_path: Path) -> None:
    layout = ProjectLayout(project_root=tmp_path)
    (tmp_path / "spack-envs").mkdir()
    (tmp_path / "secrets").mkdir()
    with pytest.raises(ValueError, match="--env"):
        layout.resolve_env_paths("../secrets")


def test_resolve_env_paths_rejects_absolute(tmp_path: Path) -> None:
    layout = ProjectLayout(project_root=tmp_path)
    (tmp_path / "spack-envs").mkdir()
    with pytest.raises(ValueError, match="--env"):
        layout.resolve_env_paths("/etc")


def test_resolve_env_paths_rejects_nested_slash(tmp_path: Path) -> None:
    layout = ProjectLayout(project_root=tmp_path)
    nested = tmp_path / "spack-envs" / "a" / "b"
    nested.mkdir(parents=True)
    with pytest.raises(ValueError, match="single directory name"):
        layout.resolve_env_paths("a/b")


def test_resolve_build_input_rejects_dotdot_app_version(tmp_path: Path) -> None:
    layout = ProjectLayout(project_root=tmp_path)
    (tmp_path / "spack-envs").mkdir()
    (tmp_path / "artifacts").mkdir()
    with pytest.raises(ValueError, match="escapes|app-version|--env"):
        resolve_build_input("../artifacts", layout=layout)


def test_manual_packages_path_escape_finding(tmp_path: Path) -> None:
    spec = EnvironmentSpec(
        schema_version=1,
        spack=SpackConfig(version="1.1.1", env_name="e"),
        manual_packages=[
            ManualPackage(file="../outside.tgz"),
        ],
    )
    findings = collect_manual_packages(spec, project_root=tmp_path)
    assert any(f.code == "manual_packages.path_escape" for f in findings)


def test_manual_packages_accepts_in_tree_file(tmp_path: Path) -> None:
    import hashlib

    pkg = tmp_path / "assets" / "ok.tgz"
    pkg.parent.mkdir(parents=True)
    pkg.write_bytes(b"abc")
    digest = hashlib.sha256(b"abc").hexdigest()
    spec = EnvironmentSpec(
        schema_version=1,
        spack=SpackConfig(version="1.1.1", env_name="e"),
        manual_packages=[ManualPackage(file="assets/ok.tgz", sha256=digest)],
    )
    findings = collect_manual_packages(spec, project_root=tmp_path)
    assert findings == []


# ── system_pkgs quoting ───────────────────────────────────────────────────


class _CapturingContainer(Container):
    def __init__(self) -> None:
        super().__init__(name="x", image="x")
        self.scripts: list[str] = []

    def exec(self, script, *, capture=False, check=True):  # type: ignore[override]
        self.scripts.append(script)
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )


def test_install_system_pkgs_quotes_each_package() -> None:
    env = EnvironmentSpec(
        schema_version=1,
        spack=SpackConfig(version="1.1.1", env_name="e"),
        mirror_builder=MirrorBuilderConfig(
            system_pkgs=["git", "pkg; reboot", "a b"],
            pkg_install_cmd="apt-get install -y",
        ),
    )
    ctr = _CapturingContainer()
    SpackOps(env, ctr).install_system_pkgs()
    assert ctr.scripts
    script = ctr.scripts[-1]
    # Per-package quotes survive inside the outer bash -c quote.
    assert shell_quote("pkg; reboot")[1:-1] in script or "'pkg; reboot'" in script
    assert "pkg; reboot" in script  # present only as a quoted token
    # Unquoted injection form must not appear as a bare word sequence.
    assert "install -y git pkg; reboot" not in script

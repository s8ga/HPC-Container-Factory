"""Tests for shared Jinja Dockerfile partials and ChoiceLoader."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from hpc_cf.config import DEFAULT_APT_MIRROR, SPACK_ENVS_DIR, TEMPLATES_DIR
from hpc_cf.template import build_context, render_template, select_template

PARTIALS_DIR = TEMPLATES_DIR / "partials"

# Baseline total of the 9 per-env Dockerfile.j2 files before dedupe (step 4).
PRE_DEDUPE_ENV_DOCKERFILE_LINES = 3585
# The budget scales with the live inventory so the guard keeps measuring
# per-env thinness rather than forbidding new environments.
PRE_DEDUPE_BASELINE_ENV_COUNT = 9

REQUIRED_PARTIALS = (
    "spack_install.j2",
    "spack_bootstrap.j2",
    "spack_mirror.j2",
    "spack_env_create.j2",
    "spack_view_enable.j2",
    "spack_strip.j2",
    "runtime_cleanup.j2",
    "locale_c_utf8.j2",
)

PILOT_ENV = "cp2k_opensource-master-force-avx512"

# Fixed timestamp so pilot render fingerprints stay stable across runs.
_PILOT_TS = "2026-07-16T00:00:00"


def _normalize_dockerfile(text: str) -> str:
    """Normalize for semantic compare: strip trailing WS, collapse blank runs."""
    lines = [ln.rstrip() for ln in text.splitlines()]
    out: list[str] = []
    blank = False
    for ln in lines:
        if not ln:
            if not blank:
                out.append("")
            blank = True
        else:
            out.append(ln)
            blank = False
    return "\n".join(out).strip() + "\n"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _render_pilot(*, use_mirror: bool = True, build_only: bool = False) -> str:
    tpl = select_template(PILOT_ENV)
    ctx = build_context(
        use_mirror=use_mirror,
        build_only=build_only,
        app_version=PILOT_ENV,
        template_path=tpl,
    )
    ctx["timestamp"] = _PILOT_TS
    return render_template(tpl, ctx)


def test_required_partials_exist() -> None:
    assert PARTIALS_DIR.is_dir(), f"missing {PARTIALS_DIR}"
    for name in REQUIRED_PARTIALS:
        assert (PARTIALS_DIR / name).is_file(), f"missing partial {name}"


def test_runtime_cleanup_removes_third_party_python_tests() -> None:
    cleanup = (PARTIALS_DIR / "runtime_cleanup.j2").read_text(encoding="utf-8")
    assert "-path '*/site-packages/*'" in cleanup
    assert r"\( -name tests -o -name test \)" in cleanup
    assert "-prune -exec rm -rf {} +" in cleanup


def test_choice_loader_includes_global_partial(tmp_path: Path) -> None:
    """Per-env templates must resolve includes under templates/partials/."""
    tpl = tmp_path / "Dockerfile.j2"
    tpl.write_text("{% include 'partials/spack_install.j2' %}\n", encoding="utf-8")
    out = render_template(tpl, {"spack_version": "1.1.1"})
    assert "COPY assets/spack-v1.1.1.tar.gz" in out
    assert "SPACK_ROOT=/opt/spack-exe" in out


def test_shared_spack_dockerfile_includes_shared_partials() -> None:
    """Shared templates/Dockerfile.spack.j2 drives all method=spack envs."""
    assert list(SPACK_ENVS_DIR.glob("*/Dockerfile.j2")) == []
    shared = TEMPLATES_DIR / "Dockerfile.spack.j2"
    assert shared.is_file()
    text = shared.read_text(encoding="utf-8")
    assert "{% include 'partials/spack_builder_prelude_fedora.j2' %}" in text
    assert "{% include 'partials/spack_env_create.j2' %}" in text
    assert "{% include 'partials/spack_consumer_finalize_body.j2' %}" in text
    assert not re.search(r"spack env create (cp2k-env|vasp-env|abacus-env)\b", text)
    assert "spack env create {{" not in text


def test_pilot_render_keeps_newline_between_partials() -> None:
    """Adjacent includes must not glue Dockerfile statements onto one line.

    Jinja defaults to stripping a template's trailing newline; without
    ``keep_trailing_newline=True`` that concatenates ``echo ...mirror`` with
    the next ``# comment`` / ``fi'`` with the next ``RUN``.
    """
    rendered = _render_pilot(use_mirror=True, build_only=False)
    assert "mirror\"#" not in rendered
    assert "mirror\"\n#" in rendered or 'mirror"\r\n#' in rendered
    assert "fi'RUN" not in rendered
    assert "fi'\nRUN" in rendered or "fi'\r\nRUN" in rendered


def test_all_spack_env_renders_set_c_utf8_locale() -> None:
    """Every shipped spack env render must set C.UTF-8 in builder and runtime."""
    from hpc_cf.env import list_available_envs

    envs = list_available_envs()
    assert len(envs) >= 1
    for env_name in envs:
        tpl = select_template(env_name)
        ctx = build_context(
            use_mirror=True,
            build_only=False,
            app_version=env_name,
            template_path=tpl,
        )
        rendered = render_template(tpl, ctx)
        assert "LANG=C.UTF-8" in rendered, env_name
        assert "LC_ALL=C.UTF-8" in rendered, env_name
        # Builder and runtime stages should each get the locale (two ENV blocks).
        assert rendered.count("LANG=C.UTF-8") >= 2, env_name


def test_pilot_render_preserves_spack_semantics() -> None:
    """Pilot render must keep shared Spack steps and CP2K-specific features."""
    rendered = _render_pilot(use_mirror=True, build_only=False)

    # Shared Spack contract steps (from partials).
    assert "spack env create cp2k-env" in rendered
    assert "spack bootstrap now" in rendered
    assert rendered.count("spack bootstrap now") == 1
    assert "local-mirror file:///opt/spack-mirror" in rendered
    assert "repo update builtin" in rendered
    assert "env view enable /opt/spack-view" in rendered
    assert "Cleanup complete" in rendered
    assert "hpc-shell-wrapper.sh" in rendered
    assert "--chown=hpc:hpc" not in rendered

    # Application-specific features driven by env vars/plan.
    assert "s8ga-spack-packages" in rendered
    assert "tools/regtesting" in rendered
    assert "cp2k-motd.sh" in rendered or "hpc-motd.sh" in rendered
    assert "{{" not in rendered
    assert "{%" not in rendered

    # Deterministic across re-renders with pinned timestamp.
    again = _render_pilot(use_mirror=True, build_only=False)
    assert _sha256(_normalize_dockerfile(rendered)) == _sha256(
        _normalize_dockerfile(again)
    )


def test_pilot_build_only_skips_runtime() -> None:
    rendered = _render_pilot(use_mirror=False, build_only=True)
    assert "AS builder" in rendered
    assert "AS runtime" not in rendered
    assert "spack env create cp2k-env" in rendered
    assert "local-mirror" not in rendered


def test_build_context_defaults_apt_mirror(monkeypatch) -> None:
    """Unset pkg/apt mirror defaults to official, with HPC_CF_PKG_MIRROR opt-in."""
    monkeypatch.delenv("HPC_CF_PKG_MIRROR", raising=False)
    tpl = select_template(PILOT_ENV)
    ctx = build_context(
        use_mirror=True,
        build_only=False,
        app_version=PILOT_ENV,
        template_path=tpl,
    )
    assert ctx["apt_mirror"] == DEFAULT_APT_MIRROR == "official"

    monkeypatch.setenv("HPC_CF_PKG_MIRROR", "mirrors.ustc.edu.cn")
    ctx_env = build_context(
        use_mirror=True,
        build_only=False,
        app_version=PILOT_ENV,
        template_path=tpl,
    )
    assert ctx_env["pkg_mirror"] == "mirrors.ustc.edu.cn"


def test_pilot_render_uses_default_pkg_mirror(monkeypatch) -> None:
    monkeypatch.delenv("HPC_CF_PKG_MIRROR", raising=False)
    rendered = _render_pilot(use_mirror=True, build_only=False)
    assert "mirrors.ustc.edu.cn" not in rendered
    assert "baseurl=https://" not in rendered
    assert "/usr/share/dnf5/repos.d/*updates*.repo" in rendered
    assert "--disablerepo='updates*,fedora-cisco-openh264'" in rendered


def _render_rpm_partial(
    tmp_path: Path, name: str, *, pkg_mirror: str
) -> str:
    """Render a Fedora RPM partial via ChoiceLoader (tmp wrapper + global partials)."""
    stub = tmp_path / "Dockerfile.j2"
    stub.write_text(f"{{% include 'partials/{name}' %}}\n", encoding="utf-8")
    return render_template(
        stub,
        {"pkg_mirror": pkg_mirror, "fedora_toolchain_pkgs": "gcc gcc-c++ gcc-gfortran"},
    )


def test_rpm_partial_custom_ustc_mirror(tmp_path: Path) -> None:
    out = _render_rpm_partial(
        tmp_path, "builder_rpm_fedora.j2", pkg_mirror="mirrors.ustc.edu.cn"
    )
    assert "baseurl=https://mirrors.ustc.edu.cn/fedora" in out
    assert "/usr/share/dnf5/repos.d/*updates*.repo" in out


def test_rpm_partial_custom_mirror(tmp_path: Path) -> None:
    out = _render_rpm_partial(
        tmp_path, "builder_rpm_fedora.j2", pkg_mirror="mirrors.example.com"
    )
    assert "baseurl=https://mirrors.example.com/fedora" in out
    assert "mirrors.ustc.edu.cn" not in out


@pytest.mark.parametrize("skip_value", ("", "official"))
def test_rpm_partial_empty_or_official_skips_mirror_rewrite(
    tmp_path: Path, skip_value: str
) -> None:
    out = _render_rpm_partial(
        tmp_path, "builder_rpm_fedora.j2", pkg_mirror=skip_value
    )
    assert "baseurl=https://" not in out
    assert "dnf5 install" in out


UPSTREAM_CP2K = "https://github.com/cp2k/cp2k.git"
FORK_CP2K = "https://github.com/someone/cp2k.git"


@pytest.mark.parametrize(
    "variant_ctx",
    [
        # lock-derived (floating master track; lock_source_commits injection)
        {"cp2k_source_commit": "a" * 40, "cp2k_branch": "master"},
        # pinned (dual-write commit copy)
        {"cp2k_dev_repo_commit": "b" * 40, "cp2k_branch": "master"},
        # floating clone of the branch tip
        {"cp2k_branch": "master"},
    ],
    ids=["lock-derived", "pinned", "floating"],
)
def test_upstream_clone_partial_honors_fork_url(
    tmp_path: Path, variant_ctx: dict[str, str]
) -> None:
    """Every variant of the upstream-clone partial must clone from
    template_vars.cp2k_source_repo_url when declared (fork builds), and fall
    back to upstream for plain envs — a regression here strands fork-only
    commits (git fetch against upstream fails)."""
    stub = tmp_path / "Dockerfile.j2"
    stub.write_text(
        "{% include 'partials/cp2k_upstream_clone.j2' %}\n", encoding="utf-8"
    )

    out = render_template(stub, dict(variant_ctx))
    assert UPSTREAM_CP2K in out
    assert FORK_CP2K not in out

    out = render_template(
        stub, {**variant_ctx, "cp2k_source_repo_url": FORK_CP2K}
    )
    assert FORK_CP2K in out
    assert UPSTREAM_CP2K not in out


def test_nospack_dockerfile_honors_apt_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shared no_spack template rewrites or skips APT mirror like Debian partials."""
    import shutil

    from hpc_cf.execution import ProjectLayout
    from hpc_cf.template import resolve_build_input

    layout = ProjectLayout(project_root=tmp_path)
    templates = tmp_path / "templates"
    templates.mkdir()
    shutil.copy2(
        TEMPLATES_DIR / "Dockerfile.nospack.j2",
        templates / "Dockerfile.nospack.j2",
    )
    partials = templates / "partials"
    partials.mkdir()
    shutil.copy2(
        TEMPLATES_DIR / "partials" / "locale_c_utf8.j2",
        partials / "locale_c_utf8.j2",
    )

    env_name = "nospack-apt"
    env_dir = tmp_path / "spack-envs" / env_name
    env_dir.mkdir(parents=True)

    def _write_env(apt_block: str) -> None:
        (env_dir / "env.yaml").write_text(
            "schema_version: 1\nmethod: no_spack\n"
            "images:\n  builder: debian:trixie\n  runtime: debian:trixie-slim\n"
            "spack:\n  version: '1.1.1'\n  env_name: e\n"
            "script: echo hi\n"
            f"{apt_block}",
            encoding="utf-8",
        )

    def _render(apt_block: str) -> tuple[dict, str]:
        _write_env(apt_block)
        resolved = resolve_build_input(env_name, None, layout=layout)
        ctx = build_context(
            use_mirror=False,
            build_only=False,
            app_version=env_name,
            template_path=resolved.render_template,
            resolved=resolved,
            layout=layout,
        )
        out = render_template(resolved.render_template, ctx, layout=layout)
        return ctx, out

    monkeypatch.delenv("HPC_CF_PKG_MIRROR", raising=False)
    ctx, out = _render("")
    assert ctx["apt_mirror"] == DEFAULT_APT_MIRROR == "official"
    assert "sed -i" not in out
    assert "deb.debian.org" not in out

    monkeypatch.setenv("HPC_CF_PKG_MIRROR", "mirrors.ustc.edu.cn")
    ctx, out = _render("")
    assert ctx["apt_mirror"] == "mirrors.ustc.edu.cn"
    assert out.count("mirrors.ustc.edu.cn") >= 2
    monkeypatch.delenv("HPC_CF_PKG_MIRROR", raising=False)

    ctx, out = _render("template_vars:\n  apt_mirror: mirrors.example.com\n")
    assert ctx["apt_mirror"] == "mirrors.example.com"
    assert "mirrors.example.com" in out
    assert "mirrors.ustc.edu.cn" not in out

    ctx, out = _render("template_vars:\n  apt_mirror: official\n")
    assert ctx["apt_mirror"] == "official"
    assert "sed -i" not in out
    assert "deb.debian.org" not in out

    ctx, out = _render('template_vars:\n  apt_mirror: ""\n')
    assert ctx["apt_mirror"] == ""
    assert "sed -i" not in out
    assert "deb.debian.org" not in out

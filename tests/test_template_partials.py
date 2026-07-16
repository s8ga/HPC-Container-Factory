"""Tests for shared Jinja Dockerfile partials and ChoiceLoader."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from hpc_cf.config import SPACK_ENVS_DIR, TEMPLATES_DIR
from hpc_cf.template import build_context, render_template, select_template

PARTIALS_DIR = TEMPLATES_DIR / "partials"

# Baseline total of the 9 per-env Dockerfile.j2 files before dedupe (step 4).
PRE_DEDUPE_ENV_DOCKERFILE_LINES = 3585

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

PILOT_ENV = "cp2k_opensource-2026.2-force-avx512"

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


def test_env_dockerfiles_include_shared_partials() -> None:
    """Every spack per-env Dockerfile must pull in the shared Spack partials."""
    dockerfiles = sorted(SPACK_ENVS_DIR.glob("*/Dockerfile.j2"))
    assert len(dockerfiles) >= 9
    for path in dockerfiles:
        env_name = path.parent.name
        text = path.read_text(encoding="utf-8")
        # Direct include or via debian builder prelude.
        has_install = (
            "{% include 'partials/spack_install.j2' %}" in text
            or "{% include 'partials/spack_builder_prelude_debian.j2' %}" in text
        )
        assert has_install, env_name
        assert "{% include 'partials/spack_env_create.j2' %}" in text, env_name
        assert (
            "{% include 'partials/spack_view_enable.j2' %}" in text
            or "{% include 'partials/spack_post_install.j2' %}" in text
        ), env_name
        # ROCm keeps a custom runtime cleanup (gcc-13 / ROCm LLVM).
        if "rocm" not in env_name:
            assert "{% include 'partials/runtime_cleanup.j2' %}" in text, env_name
        # No hardcoded env-create names left in the template source.
        assert not re.search(
            r"spack env create (cp2k-env|vasp-env|abacus-env)\b", text
        ), env_name
        # Env create must come from the shared partial (no inline duplicate).
        assert "spack env create {{" not in text, env_name


def test_duplicate_template_lines_reduced_at_least_40_percent() -> None:
    """Per-env Dockerfile line count should drop ≥40% vs pre-dedupe baseline."""
    env_lines = sum(
        len(p.read_text(encoding="utf-8").splitlines())
        for p in SPACK_ENVS_DIR.glob("*/Dockerfile.j2")
    )
    partial_lines = sum(
        len(p.read_text(encoding="utf-8").splitlines())
        for p in PARTIALS_DIR.glob("*.j2")
    )
    effective = env_lines + partial_lines
    assert env_lines <= PRE_DEDUPE_ENV_DOCKERFILE_LINES * 0.60, (
        f"per-env lines {env_lines} not ≤ 60% of {PRE_DEDUPE_ENV_DOCKERFILE_LINES}"
    )
    assert effective <= PRE_DEDUPE_ENV_DOCKERFILE_LINES * 0.70, (
        f"env+partials {effective} not ≤ 70% of baseline "
        f"{PRE_DEDUPE_ENV_DOCKERFILE_LINES}"
    )


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
    """Every shipped spack Dockerfile must set C.UTF-8 in builder and runtime."""
    dockerfiles = sorted(SPACK_ENVS_DIR.glob("*/Dockerfile.j2"))
    assert len(dockerfiles) >= 9
    for path in dockerfiles:
        env_name = path.parent.name
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
    assert "Runtime cleanup: removing development-only content" in rendered

    # Application-specific features stay in the per-env template.
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

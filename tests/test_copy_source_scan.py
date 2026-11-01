"""Rendered-Dockerfile COPY source scan (audit 2.9).

COPY lines create context-side dependencies no schema sees (libfakeintel/,
testsuite-file/, scripts). Missing sources now fail at render time in
generate_dockerfile and surface as findings in validate --profile config.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_cf.validation import collect_render_copy_sources


def _root(tmp_path: Path) -> Path:
    (tmp_path / "spack-envs" / "demo" / "libfakeintel").mkdir(parents=True)
    (tmp_path / "spack-envs" / "demo" / "libfakeintel" / "Makefile").write_text(
        "all:\n", encoding="utf-8"
    )
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "entry.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    return tmp_path


def test_existing_sources_pass(tmp_path: Path) -> None:
    root = _root(tmp_path)
    rendered = (
        "COPY spack-envs/demo/libfakeintel /opt/libfakeintel/\n"
        "COPY --chmod=644 scripts/entry.sh /usr/local/bin/entry.sh\n"
    )
    assert collect_render_copy_sources(rendered, root) == []


def test_missing_source_is_error(tmp_path: Path) -> None:
    root = _root(tmp_path)
    rendered = "COPY spack-envs/demo/testsuite-file/test.conf /opt/t/\n"
    findings = collect_render_copy_sources(rendered, root)
    assert len(findings) == 1
    assert findings[0].code == "copy.source_missing"
    assert "testsuite-file/test.conf" in findings[0].message
    assert findings[0].severity.value == "error"


def test_multistage_from_lines_are_skipped(tmp_path: Path) -> None:
    root = _root(tmp_path)
    rendered = (
        "COPY --from=builder --chown=hpc:hpc /opt/spack /opt/spack\n"
        "COPY --from=builder /opt/does-not-exist /opt/x\n"
    )
    assert collect_render_copy_sources(rendered, root) == []


def test_chown_with_separate_value_token(tmp_path: Path) -> None:
    root = _root(tmp_path)
    rendered = "COPY --chown=hpc:hpc spack-envs/demo/libfakeintel /opt/libfakeintel/\n"
    assert collect_render_copy_sources(rendered, root) == []


def test_glob_without_match_is_error(tmp_path: Path) -> None:
    root = _root(tmp_path)
    rendered = "COPY spack-envs/demo/*.conf /opt/conf/\n"
    findings = collect_render_copy_sources(rendered, root)
    assert any("*.conf" in f.message for f in findings)


def test_glob_with_match_passes(tmp_path: Path) -> None:
    root = _root(tmp_path)
    rendered = "COPY scripts/*.sh /opt/scripts/\n"
    assert collect_render_copy_sources(rendered, root) == []


def test_json_array_form(tmp_path: Path) -> None:
    root = _root(tmp_path)
    rendered = 'COPY ["scripts/entry.sh", "/usr/local/bin/entry.sh"]\n'
    assert collect_render_copy_sources(rendered, root) == []
    rendered_missing = 'COPY ["scripts/missing.sh", "/usr/local/bin/x.sh"]\n'
    findings = collect_render_copy_sources(rendered_missing, root)
    assert any("missing.sh" in f.message for f in findings)


def test_generate_dockerfile_fails_closed_on_missing_copy_source(
    tmp_path: Path,
) -> None:
    from hpc_cf.template import generate_dockerfile

    env_root = tmp_path / "spack-envs" / "demo"
    conf = env_root / "spack-env-file"
    conf.mkdir(parents=True)
    (conf / "env.yaml").write_text(
        "schema_version: 1\nmethod: spack\n"
        "spack:\n"
        "  version: '1.1.1'\n"
        "  env_name: demo-env\n",
        encoding="utf-8",
    )
    (conf / "spack.yaml").write_text("spack:\n  specs: [pkgconf]\n", encoding="utf-8")
    (conf / "spack.lock").write_text('{"_meta": {}}', encoding="utf-8")
    (env_root / "Dockerfile.j2").write_text(
        "FROM debian:trixie\n"
        "COPY spack-envs/demo/libfakeintel /opt/libfakeintel/\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="libfakeintel"):
        from hpc_cf.execution import ProjectLayout

        generate_dockerfile(
            template=env_root / "Dockerfile.j2",
            app_version="demo",
            output=tmp_path / "Dockerfile.out",
            use_mirror=False,
            build_only=True,
            layout=ProjectLayout(project_root=tmp_path),
        )


def test_continuation_lines_join_before_scan(tmp_path: Path) -> None:
    """COPY split across backslash continuations is one statement."""
    root = _root(tmp_path)
    rendered = (
        "COPY \\\n"
        "  spack-envs/demo/libfakeintel \\\n"
        "  /opt/libfakeintel/\n"
    )
    assert collect_render_copy_sources(rendered, root) == []

    missing = "COPY \\\n  spack-envs/demo/nope.conf \\\n  /opt/t/\n"
    findings = collect_render_copy_sources(missing, root)
    assert [f.code for f in findings] == ["copy.source_missing"]


def test_link_flag_takes_no_value(tmp_path: Path) -> None:
    """COPY --link is a boolean flag; it must not eat the first source."""
    root = _root(tmp_path)
    rendered = "COPY --link spack-envs/demo/libfakeintel /opt/libfakeintel/\n"
    assert collect_render_copy_sources(rendered, root) == []

    missing = "COPY --link spack-envs/demo/nope.conf /opt/t/\n"
    findings = collect_render_copy_sources(missing, root)
    assert [f.code for f in findings] == ["copy.source_missing"]
    assert "nope.conf" in findings[0].message


def test_sources_outside_project_root_are_rejected(tmp_path: Path) -> None:
    root = _root(tmp_path)
    findings = collect_render_copy_sources(
        "COPY ../outside/file /dst\n"
        "COPY /etc/passwd /dst\n",
        root,
    )
    assert [f.code for f in findings] == [
        "copy.source_outside_root",
        "copy.source_outside_root",
    ]
    assert findings[0].severity.value == "error"

"""validate --format json must never escape a bare exception.

`_cmd_validate` resolves build inputs BEFORE validate_environment, and the
resolve path can raise ValueError (sidecar drift, undeclared
lock_source_commits spec, malformed lock) while only FileNotFoundError was
converted to a finding — every other error killed the process mid-command
and the JSON consumer got a traceback on stdout (round-2 audit 2.13b).
"""

from __future__ import annotations

import json
from pathlib import Path

from hpc_cf.cli import run_new_cli


def _run_validate_cli(capsys, *extra: str) -> tuple[int, dict]:
    code = run_new_cli(
        ["validate", "--app-version", "synth-env", "--format", "json", *extra]
    )
    payload = json.loads(capsys.readouterr().out)
    return code, payload


def test_input_value_error_becomes_json_finding(
    capsys, monkeypatch
) -> None:
    def broken_resolve(*_args, **_kwargs):
        raise ValueError("no concrete spec named cp2k in spack.lock")

    monkeypatch.setattr("hpc_cf.cli.resolve_build_input", broken_resolve)
    code, payload = _run_validate_cli(capsys, "--profile", "build-input")
    assert code == 1
    assert payload["ok"] is False
    codes = [f["code"] for f in payload["findings"]]
    assert "build_input.invalid" in codes
    message = json.dumps(payload)
    assert "no concrete spec named cp2k" in message


def test_missing_template_still_reports_template_missing(
    capsys, monkeypatch
) -> None:
    def broken_resolve(*_args, **_kwargs):
        raise FileNotFoundError("Dockerfile.j2 not found")

    monkeypatch.setattr("hpc_cf.cli.resolve_build_input", broken_resolve)
    code, payload = _run_validate_cli(capsys)
    assert code == 1
    assert payload["ok"] is False
    assert "template.missing" in [f["code"] for f in payload["findings"]]


def test_undefined_render_var_becomes_json_finding(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """StrictUndefined raises jinja2.UndefinedError, which is NOT a
    RuntimeError — the probe must catch it and keep the report valid."""
    from hpc_cf.template import ResolvedBuildInput

    env_dir = tmp_path / "synth-env"
    env_dir.mkdir()
    (env_dir / "env.yaml").write_text(
        "schema_version: 1\nmethod: no_spack\n"
        "spack:\n  version: '1.2.0'\n  env_name: synth-env\n"
        "script: echo hi\n",
        encoding="utf-8",
    )
    tpl = env_dir / "Dockerfile.j2"
    tpl.write_text("FROM debian\nRUN echo {{ no_such_var }}\n", encoding="utf-8")

    monkeypatch.setattr(
        "hpc_cf.cli.resolve_build_input",
        lambda *_a, **_k: ResolvedBuildInput(
            environment_spec=None, environment_dir=env_dir, render_template=tpl
        ),
    )
    code, payload = _run_validate_cli(capsys, "--profile", "config")
    assert code == 1
    assert payload["ok"] is False
    assert "template.render" in [f["code"] for f in payload["findings"]]

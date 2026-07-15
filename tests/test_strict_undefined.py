"""Jinja StrictUndefined: missing template vars must fail at render time."""

from __future__ import annotations

from pathlib import Path

import pytest
from jinja2.exceptions import UndefinedError

from hpc_cf.template import render_template


def test_render_fails_on_missing_variable(tmp_path: Path) -> None:
    tpl = tmp_path / "Dockerfile.j2"
    tpl.write_text("FROM {{ missing_base }}\n", encoding="utf-8")
    with pytest.raises((UndefinedError, RuntimeError), match="missing_base"):
        render_template(tpl, {"timestamp": "t"})


def test_render_succeeds_when_variable_provided(tmp_path: Path) -> None:
    tpl = tmp_path / "Dockerfile.j2"
    tpl.write_text("FROM {{ base }}\n", encoding="utf-8")
    out = render_template(tpl, {"base": "debian:trixie"})
    assert "FROM debian:trixie" in out

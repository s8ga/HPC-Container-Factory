"""assets task verbs: run | status | prune (round-2 audit 2.8).

The legacy 17-flag surface made `--prune-mirror` alone trigger the full
hours-long pipeline (intent: see the dry-run report) and silently
dropped illegal flag combinations. The verbs make intent literal:
prune is standalone (persisted sidecar identity, single-survivor rule)
and stage flags on it are refused loudly.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from hpc_cf.assets import run_assets
from hpc_cf.execution import ProjectLayout
from hpc_cf.requests import AssetsRequest


def _request(task: str = "", env: str = "demo", **overrides: object) -> AssetsRequest:
    base: dict[str, object] = {"task": task, "env": env}
    base.update(overrides)
    return AssetsRequest(**base)  # type: ignore[arg-type]


def test_prune_verb_stands_alone(tmp_path: Path) -> None:
    layout = ProjectLayout(project_root=tmp_path)
    captured: dict[str, object] = {}

    def fake_prune(ctr, env, *, apply_plan, fresh_after, layout, reuse_sidecar):
        captured.update(
            apply_plan=apply_plan,
            fresh_after=fresh_after,
            reuse_sidecar=reuse_sidecar,
            env=env,
        )

    with (
        patch("hpc_cf.assets.run_mirror_prune", side_effect=fake_prune),
        patch("hpc_cf.assets._make_container"),
    ):
        run_assets(_request(task="prune"), layout=layout)

    assert captured["reuse_sidecar"] is True
    assert captured["apply_plan"] is False  # dry-run report by default
    assert captured["fresh_after"] == float("inf")  # nothing is this-run
    assert captured["env"] == "demo"


def test_prune_verb_refuses_stage_flags(tmp_path: Path) -> None:
    layout = ProjectLayout(project_root=tmp_path)
    with (
        patch("hpc_cf.assets.run_mirror_prune") as prune_mock,
        patch("hpc_cf.assets._make_container"),
        pytest.raises(ValueError, match="standalone"),
    ):
        run_assets(
            _request(task="prune", download_mirror=True), layout=layout
        )
    prune_mock.assert_not_called()


def test_prune_verb_requires_env(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="--env is required for prune"):
        run_assets(
            _request(task="prune", env=""), layout=ProjectLayout(project_root=tmp_path)
        )


def test_prune_without_persisted_sidecar_refuses(tmp_path: Path) -> None:
    """Standalone prune identity comes from the sidecar the creating run
    wrote; a missing sidecar is a hard refusal, not a silent no-op."""
    from hpc_cf.container import Container

    layout = ProjectLayout(project_root=tmp_path)
    ctr = Container(image="hpc-mirror-builder", name="hpc-mirror-builder-work")
    (layout.spack_mirror_dir).mkdir(parents=True)
    with pytest.raises(RuntimeError, match="no persisted fetch-spec sidecar"):
        from hpc_cf.assets import run_mirror_prune

        run_mirror_prune(
            ctr,
            "demo",
            apply_plan=False,
            fresh_after=float("inf"),
            layout=layout,
            reuse_sidecar=True,
        )


def test_status_verb_routes_to_run_status(tmp_path: Path) -> None:
    layout = ProjectLayout(project_root=tmp_path)
    with (
        patch("hpc_cf.assets.run_status") as status_mock,
        patch("hpc_cf.assets._make_container"),
    ):
        run_assets(_request(task="status"), layout=layout)
    status_mock.assert_called_once()


def test_run_verb_and_legacy_surface_both_reach_the_pipeline(
    tmp_path: Path,
) -> None:
    """`assets run` and the legacy flag-less invocation behave the same:
    both demand --env and start the default workflow (image+container)."""
    layout = ProjectLayout(project_root=tmp_path)
    for request in (_request(task="run"), _request(task="")):
        with (
            patch("hpc_cf.assets.ensure_image"),
            patch("hpc_cf.assets._make_container"),
            patch("hpc_cf.assets.run_bootstrap"),
            patch("hpc_cf.assets.run_mirror") as mirror_mock,
            patch("hpc_cf.assets.run_verify"),
            patch("hpc_cf.assets.spack_version_for_env", return_value="1.2.0"),
        ):
            run_assets(request, layout=layout)
        mirror_mock.assert_called_once()

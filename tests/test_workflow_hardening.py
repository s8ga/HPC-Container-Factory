"""Workflow hardening contracts (permissions / timeouts / concurrency).

Round-2 audit 2.6 + 2.2: pipeline jobs inherited the workflow-level
contents:write ceiling, ci-tests lacked read-only permissions and a
timeout, the pipeline concurrency group collapsed fork dispatches into
the upstream group, and stable-release happily ran env families the
pipeline does not serve. These tests pin the hardened shapes.
"""

from __future__ import annotations

import re

import yaml

from hpc_cf.config import PROJECT_ROOT

WORKFLOWS_DIR = PROJECT_ROOT / ".github" / "workflows"
ALL_WORKFLOWS = (
    "ci-tests.yml",
    "nightly-auto-retry.yml",
    "nightly-cp2k.yml",
    "pipeline-cp2k.yml",
    "regen-lock.yml",
    "stable-release.yml",
)


def _doc(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS_DIR / name).read_text(encoding="utf-8"))


def test_every_normal_job_declares_timeout() -> None:
    """Reusable-workflow caller jobs cannot carry timeout-minutes; every
    job that runs steps itself must declare one."""
    for name in ALL_WORKFLOWS:
        for job_id, job in _doc(name)["jobs"].items():
            if "uses" in job:
                continue
            assert "timeout-minutes" in job, f"{name}:{job_id}"


def test_ci_tests_is_read_only() -> None:
    assert _doc("ci-tests.yml")["permissions"] == {"contents": "read"}


def test_pipeline_jobs_downgrade_from_the_write_ceiling() -> None:
    """Only sif needs contents:write; GHCR-pushing jobs carry exactly
    contents:read + packages:write."""
    jobs = _doc("pipeline-cp2k.yml")["jobs"]
    for job_id in ("prepare", "binaries", "runtime"):
        assert jobs[job_id]["permissions"] == {
            "contents": "read",
            "packages": "write",
        }, job_id
    sif = jobs["sif"]["permissions"]
    assert sif["contents"] == "write"


def test_stable_release_grants_packages_only_at_the_call_site() -> None:
    doc = _doc("stable-release.yml")
    assert doc["permissions"] == {"contents": "write"}
    pipeline = doc["jobs"]["pipeline"]
    assert "uses" in pipeline
    assert pipeline["permissions"] == {"contents": "write", "packages": "write"}


def test_pipeline_concurrency_keys_on_all_three_dimensions() -> None:
    text = (WORKFLOWS_DIR / "pipeline-cp2k.yml").read_text(encoding="utf-8")
    match = re.search(r"^  group: (hpc-cf-pipeline-.+)$", text, re.MULTILINE)
    assert match, "pipeline concurrency group not found"
    group = match.group(1)
    for dimension in ("inputs.env_name", "inputs.fork_url", "inputs.fork_branch"):
        assert dimension in group, group
    # Three independent keys, not the old fallback chain that collapsed
    # fork dispatches onto a single axis.
    assert group.count("${{") == 3, group


def test_regen_lock_serializes_with_the_upstream_variant() -> None:
    """regen-lock must land in the same concurrency family as a nightly of
    the same env (upstream-default variant of the 3-dimension group)."""
    text = (WORKFLOWS_DIR / "regen-lock.yml").read_text(encoding="utf-8")
    assert "hpc-cf-pipeline-${{ inputs.env_name }}-upstream-default" in text


def test_stable_release_whitelists_env_families() -> None:
    """Non-opensource families (VASP/MKL/ROCm) must fail the resolve step
    in seconds instead of building for hours (2026-10-29 misroute)."""
    text = (WORKFLOWS_DIR / "stable-release.yml").read_text(encoding="utf-8")
    assert "cp2k_opensource-*|abacus_opensource-*" in text


# --- build-lock artifact + release assets ------------------------------------


def test_build_lock_artifact_carries_the_manifest() -> None:
    """prepare must ship lock-manifest.json with the lock, or the CI regen
    path throws away the provenance sidecar it just created (2.3)."""
    text = (WORKFLOWS_DIR / "pipeline-cp2k.yml").read_text(encoding="utf-8")
    upload = text[text.index("name: build-lock") :]
    upload = upload[: upload.index("retention-days")]
    assert "spack.lock" in upload
    assert "resolved-repos.yaml" in upload
    assert "lock-manifest.json" in upload
    # Both consumers (binaries producer, runtime consumer) place the
    # manifest beside the lock so validate sees provenance, not
    # provenance_unknown noise.
    assert text.count("downloaded-lock/lock-manifest.json") == 2


def test_dependency_graph_ships_and_prunes_with_the_manifest() -> None:
    """The extracted DependencyGraph-*.dot must be uploaded to the release
    and share the manifest assets' retention budget (2.15c)."""
    text = (WORKFLOWS_DIR / "pipeline-cp2k.yml").read_text(encoding="utf-8")
    assert 'RELEASE_ASSETS+=("DependencyGraph-' in text
    assert 'test("(BUILD_MANIFEST|DependencyGraph)-")' in text


def test_pipeline_preamble_is_deduplicated() -> None:
    """The four jobs share ONE preamble script; no near-verbatim copies of
    the apt-sed / fork-identity / venv / vars blocks may creep back
    (round-2 audit 2.5: 31 edits in two months, four same-day fixes)."""
    import subprocess

    script = WORKFLOWS_DIR.parent.parent / "scripts" / "ci_job_preamble.sh"
    assert script.is_file()
    subprocess.run(["bash", "-n", str(script)], check=True)

    text = (WORKFLOWS_DIR / "pipeline-cp2k.yml").read_text(encoding="utf-8")
    jobs = _doc("pipeline-cp2k.yml")["jobs"]
    for job_id in ("prepare", "binaries", "runtime", "sif"):
        steps = jobs[job_id]["steps"]
        preambles = [
            s for s in steps if "ci_job_preamble.sh" in str(s.get("run", ""))
        ]
        assert len(preambles) == 1, f"{job_id}: expected exactly one preamble"
    # The inline copies are gone: the sed/override/venv/vars bodies now live
    # only in the script.
    assert "Point the mirror-builder apt" not in text
    assert "Materialize fork env" not in text
    assert "Install python dependencies" not in text
    assert "Resolve job variables" not in text
    script_text = script.read_text(encoding="utf-8")
    # The fork/no-fork branches each call it once; the workflow never does.
    assert script_text.count("apply-source-override.sh") == 2
    assert "apply-source-override.sh" not in text

"""Tests for release identity resolution, metadata bundling, notes generation, and asset pruning."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

from hpc_cf.release_notes import (
    create_meta_bundle,
    plan_dated_asset_deletions,
    render_release_notes,
    resolve_release_identity,
)

FLAGSHIP = "cp2k_opensource-master-force-avx512"


def test_resolve_release_identity_all_tracks() -> None:
    nightly = resolve_release_identity(
        env_name=FLAGSHIP,
        channel="nightly",
        date_yyyymmdd="20261123",
        flagship_env=FLAGSHIP,
        ident="master@107f6cbd5cdf",
    )
    assert nightly.release_tag == "cp2k-nightly"
    assert nightly.dated_sif == "cp2k-master-20261123.sif"
    assert nightly.floating_sif == "cp2k-master.sif"
    assert nightly.meta_bundle == "cp2k-master-20261123-meta.tar.gz"
    assert nightly.create_flags == "--prerelease"
    assert nightly.release_title == "CP2K Nightly · 2026-11-23 (master@107f6cbd5cdf)"

    fork = resolve_release_identity(
        env_name=f"{FLAGSHIP}-fork-s8ga-bfgs-tr",
        channel="nightly",
        date_yyyymmdd="20261123",
        flagship_env=FLAGSHIP,
        ident="bfgs-tr@deadbeef1234",
    )
    assert fork.release_tag == "cp2k-fork-s8ga-bfgs-tr"
    assert fork.dated_sif == "cp2k-fork-s8ga-bfgs-tr-20261123.sif"
    assert fork.floating_sif == "cp2k-fork-s8ga-bfgs-tr.sif"
    assert fork.meta_bundle == "cp2k-fork-s8ga-bfgs-tr-20261123-meta.tar.gz"
    assert fork.create_flags == "--prerelease"
    assert "Fork (s8ga-bfgs-tr)" in fork.release_title

    stable_flagship = resolve_release_identity(
        env_name=FLAGSHIP,
        channel="stable",
        date_yyyymmdd="20261123",
        flagship_env=FLAGSHIP,
    )
    assert stable_flagship.release_tag == "cp2k-master-20261123"
    assert stable_flagship.dated_sif == "cp2k-master-20261123.sif"
    assert stable_flagship.floating_sif == ""
    assert stable_flagship.create_flags == "--latest"

    stable_sibling = resolve_release_identity(
        env_name="cp2k_opensource-2025.2-force-avx512",
        channel="stable",
        date_yyyymmdd="20261123",
        flagship_env=FLAGSHIP,
    )
    assert stable_sibling.release_tag == "cp2k-2025.2-20261123"
    assert stable_sibling.create_flags == "--latest=false"


def test_render_release_notes_extracts_spack_lock_and_fenced_blocks(tmp_path: Path) -> None:
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    (env_dir / "env.yaml").write_text(
        "schema_version: 1\nspack:\n  version: '1.2.2'\n  toolchain: gcc16\n",
        encoding="utf-8",
    )
    commit_sha = "107f6cbd5cdf96cb14b9941fc6f38e13e0f09e98"
    lock_payload = {
        "concrete_specs": {
            "h_cp2k": {
                "name": "cp2k",
                "version": "master",
                "arch": {"platform": "linux", "platform_os": "fedora45", "target": "x86_64_v3"},
                "parameters": {"commit": commit_sha},
            },
            "h_gcc": {"name": "gcc", "version": "16.2.1"},
            "h_mpi": {"name": "openmpi", "version": "5.0.11"},
            "h_blas": {"name": "openblas", "version": "0.3.34"},
            "h_fftw": {"name": "fftw", "version": "3.3.11"},
            "h_elpa": {"name": "elpa", "version": "2026.02.002"},
            "h_cosma": {"name": "cosma", "version": "2.8.5"},
            "h_libint": {"name": "libint", "version": "2.14.1-cp2k-lmax-7"},
            "h_libxc": {"name": "libxc", "version": "7.1.2"},
            "h_sirius": {"name": "sirius", "version": "7.12.0"},
            "h_gauxc": {"name": "gauxc", "version": "dev20260717"},
            "h_deepmd": {"name": "deepmdkit", "version": "3.2.0"},
            "h_torch": {"name": "py-torch", "version": "2.15.0"},
            "h_plumed": {"name": "plumed", "version": "2.10.1"},
            "h_dftd4": {"name": "dftd4", "version": "4.3.1"},
        }
    }
    lock_path = env_dir / "spack.lock"
    lock_path.write_text(json.dumps(lock_payload), encoding="utf-8")

    sif_path = tmp_path / "cp2k-master-20261123.sif"
    sif_path.write_bytes(b"\0" * (348 * 1024 * 1024 // 1024))  # non-empty for size check

    notes = render_release_notes(
        env_name=FLAGSHIP,
        channel="nightly",
        date_yyyymmdd="20261123",
        flagship_env=FLAGSHIP,
        repository="s8ga/HPC-Container-Factory",
        lock_path=lock_path,
        env_yaml_path=env_dir / "env.yaml",
        sif_path=sif_path,
        sif_size_bytes=348 * 1024 * 1024,
        oci_dated="ghcr.io/s8ga/cp2k_opensource:master-force-avx512-nightly-20261123",
        oci_floating="ghcr.io/s8ga/cp2k_opensource:master-force-avx512-release",
        ident="master@107f6cbd5cdf",
    )

    assert f"https://github.com/cp2k/cp2k/commit/{commit_sha}" in notes
    assert "348 MiB" in notes
    assert "Fedora 45" in notes
    assert "GCC 16.2.1" in notes
    assert "Spack 1.2.2" in notes
    assert "OpenMPI 5.0.11" in notes
    assert "ELPA 2026.02.002" in notes
    assert "GauXC (Skala) dev20260717" in notes
    assert "DeepMD-kit 3.2.0" in notes
    assert "PyTorch 2.15.0" in notes
    assert "```bash" in notes
    assert "https://github.com/s8ga/HPC-Container-Factory/releases/download/cp2k-nightly/cp2k-master.sif" in notes
    assert "apptainer exec cp2k-master.sif cp2k.psmp --version" in notes
    assert "\n  apptainer exec" not in notes


def test_create_meta_bundle_and_date_paired_pruning(tmp_path: Path) -> None:
    manifest = tmp_path / "BUILD_MANIFEST.txt"
    graph = tmp_path / "DependencyGraph.dot"
    lock = tmp_path / "spack.lock"
    manifest.write_text("manifest body\n", encoding="utf-8")
    graph.write_text("digraph {}\n", encoding="utf-8")
    lock.write_text('{"concrete_specs": {}}\n', encoding="utf-8")

    bundle = tmp_path / "cp2k-master-20261123-meta.tar.gz"
    create_meta_bundle(
        bundle,
        [
            ("BUILD_MANIFEST.txt", manifest),
            ("DependencyGraph.dot", graph),
            ("spack.lock", lock),
        ],
    )
    with tarfile.open(bundle, "r:gz") as tf:
        assert sorted(tf.getnames()) == ["BUILD_MANIFEST.txt", "DependencyGraph.dot", "spack.lock"]

    assets = [
        {"id": 1, "name": "cp2k-master.sif", "created_at": "2026-11-23T01:00:00Z"},
        {"id": 2, "name": "cp2k-master-20261123.sif", "created_at": "2026-11-23T01:00:01Z"},
        {"id": 3, "name": "cp2k-master-20261123-meta.tar.gz", "created_at": "2026-11-23T01:00:02Z"},
        {"id": 4, "name": "cp2k-master-20261122.sif", "created_at": "2026-11-22T01:00:01Z"},
        {"id": 5, "name": "cp2k-master-20261122-meta.tar.gz", "created_at": "2026-11-22T01:00:02Z"},
        {"id": 6, "name": "cp2k-master-20261121.sif", "created_at": "2026-11-21T01:00:01Z"},
        {"id": 7, "name": "cp2k-master-20261121-meta.tar.gz", "created_at": "2026-11-21T01:00:02Z"},
        {"id": 8, "name": "cp2k-master-20261120.sif", "created_at": "2026-11-20T01:00:01Z"},
        {"id": 9, "name": "cp2k-master-20261120-meta.tar.gz", "created_at": "2026-11-20T01:00:02Z"},
        # Legacy unwrapped artifacts must also be pruned once past keep budget.
        {"id": 10, "name": "BUILD_MANIFEST-cp2k_opensource-master-force-avx512-20261119.txt", "created_at": "2026-11-19T01:00:00Z"},
        {"id": 11, "name": "DependencyGraph-cp2k_opensource-master-force-avx512-20261119.dot", "created_at": "2026-11-19T01:00:00Z"},
    ]
    delete_ids = plan_dated_asset_deletions(assets, keep_dates=3)
    # Keeps floating (1) + 20261123 (2,3), 20261122 (4,5), 20261121 (6,7).
    assert delete_ids == [8, 9, 10, 11]

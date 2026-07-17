"""L0 contracts for the opaque global Spack buildcache store."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from hpc_cf.execution import (
    BuildcacheCoverageRecord,
    ProjectLayout,
    SharedBuildcacheStore,
)


def test_project_layout_keeps_source_mirror_buildcache_and_state_separate(
    tmp_path: Path,
) -> None:
    layout = ProjectLayout(project_root=tmp_path)

    assert layout.spack_mirror_dir == tmp_path / "assets" / "spack-mirror"
    assert layout.spack_buildcache_dir == tmp_path / "assets" / "spack-buildcache"
    assert layout.buildcache_state_dir == (
        tmp_path / "assets" / "spack-buildcache-state"
    )
    assert layout.buildcache_lock_path.parent == layout.buildcache_state_dir
    assert layout.buildcache_health_path.parent == layout.buildcache_state_dir
    assert layout.buildcache_coverage_dir.parent == layout.buildcache_state_dir
    assert layout.container_buildcache_dir() == "/opt/spack-buildcache"
    assert layout.container_publisher_buildcache_dir() == (
        "/work/assets/spack-buildcache"
    )


def test_store_initialization_is_opaque_and_only_creates_empty_root(
    tmp_path: Path,
) -> None:
    layout = ProjectLayout(project_root=tmp_path)
    store = SharedBuildcacheStore(layout)

    store.ensure_store_root()

    assert layout.spack_buildcache_dir.is_dir()
    assert list(layout.spack_buildcache_dir.iterdir()) == []
    assert layout.buildcache_state_dir.is_dir()
    assert layout.buildcache_state_dir not in layout.spack_buildcache_dir.parents


def test_publisher_excludes_consumers_and_uses_sibling_flock(tmp_path: Path) -> None:
    layout = ProjectLayout(project_root=tmp_path)
    store = SharedBuildcacheStore(layout)
    publisher_entered = threading.Event()
    release_publisher = threading.Event()
    consumer_entered = threading.Event()

    def publisher() -> None:
        with store.publisher_lock():
            publisher_entered.set()
            release_publisher.wait(timeout=5)

    def consumer() -> None:
        with store.consumer_lock():
            consumer_entered.set()

    publisher_thread = threading.Thread(target=publisher)
    publisher_thread.start()
    assert publisher_entered.wait(timeout=2)

    consumer_thread = threading.Thread(target=consumer)
    consumer_thread.start()
    time.sleep(0.05)
    assert not consumer_entered.is_set()

    release_publisher.set()
    publisher_thread.join(timeout=2)
    consumer_thread.join(timeout=2)
    assert consumer_entered.is_set()
    assert layout.buildcache_lock_path.is_file()
    assert not (layout.spack_buildcache_dir / ".hpc_cf").exists()


def test_consumers_share_lock_but_block_publisher(tmp_path: Path) -> None:
    store = SharedBuildcacheStore(ProjectLayout(project_root=tmp_path))
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_consumers = threading.Event()
    publisher_entered = threading.Event()

    def consumer(entered: threading.Event) -> None:
        with store.consumer_lock():
            entered.set()
            release_consumers.wait(timeout=5)

    consumers = [
        threading.Thread(target=consumer, args=(first_entered,)),
        threading.Thread(target=consumer, args=(second_entered,)),
    ]
    for thread in consumers:
        thread.start()
    assert first_entered.wait(timeout=2)
    assert second_entered.wait(timeout=2)

    def publish() -> None:
        with store.publisher_lock():
            publisher_entered.set()

    publisher = threading.Thread(target=publish)
    publisher.start()
    time.sleep(0.05)
    assert not publisher_entered.is_set()

    release_consumers.set()
    for thread in consumers:
        thread.join(timeout=2)
    publisher.join(timeout=2)
    assert publisher_entered.is_set()


def test_health_and_coverage_are_state_sidecars_not_store_internals(
    tmp_path: Path,
) -> None:
    layout = ProjectLayout(project_root=tmp_path)
    store = SharedBuildcacheStore(layout)
    store.ensure_store_root()
    spack_owned = layout.spack_buildcache_dir / "spack-created-sentinel"
    spack_owned.write_bytes(b"opaque")
    lock = tmp_path / "spack.lock"
    lock.write_bytes(b'{"concrete": true}\n')

    store.mark_unhealthy(
        run_id="run-1",
        failed_step="update-index",
        error="index failed",
    )
    unhealthy = store.read_health()
    assert unhealthy["healthy"] is False
    assert unhealthy["failed_step"] == "update-index"

    record = BuildcacheCoverageRecord(
        spack_version="1.2.0",
        builder_image_digest="sha256:builder",
        environment_provenance={
            "operating_systems": ["debian13"],
            "targets": ["x86_64_v3"],
            "compilers": None,
            "repo_commits": None,
        },
        padded_length=128,
        signing_policy="unsigned",
        check_returncode=0,
        checked_spec_count=12,
    )
    coverage_path = store.write_coverage(lock_path=lock, record=record)
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))

    assert coverage_path.parent == layout.buildcache_coverage_dir
    assert coverage_path.stem == coverage["lock_sha256"]
    assert coverage["coverage"] == "non_external"
    assert coverage["external_specs_excluded"] is True
    assert coverage["check_returncode"] == 0
    assert coverage["schema_version"] == 2
    assert coverage["environment_provenance"] == {
        "operating_systems": ["debian13"],
        "targets": ["x86_64_v3"],
        "compilers": None,
        "repo_commits": None,
    }
    assert spack_owned.read_bytes() == b"opaque"
    assert list(layout.spack_buildcache_dir.iterdir()) == [spack_owned]

    store.mark_healthy(run_id="run-1", coverage_path=coverage_path)
    healthy = store.read_health()
    assert healthy["healthy"] is True
    assert healthy["coverage_path"] == str(coverage_path)


def test_run_logs_and_provenance_live_only_in_sibling_state(tmp_path: Path) -> None:
    layout = ProjectLayout(project_root=tmp_path)
    store = SharedBuildcacheStore(layout)
    run = store.begin_run("cp2k/demo")
    run.log_path("push/index").write_text("spack output\n", encoding="utf-8")
    provenance_path = store.write_provenance(
        run,
        {
            "env": "cp2k/demo",
            "builder_image_digest": "sha256:builder",
        },
    )

    assert run.host_dir.parent == layout.buildcache_runs_dir
    assert run.log_path("push/index").name == "push_index.log"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    assert provenance["env"] == "cp2k/demo"
    assert provenance["run_id"] == run.run_id
    assert not layout.spack_buildcache_dir.exists()

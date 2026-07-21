"""P1-C: mirror-worker fingerprint labels — recreate on mismatch."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from hpc_cf.container import (
    WORKER_LABEL_FINGERPRINT,
    WORKER_LABEL_IMAGE_ID,
    Container,
)


def _ctr(tmp_path: Path, **kwargs) -> Container:
    return Container(
        name="hpc-mirror-builder-work",
        image="hpc-mirror-builder",
        project_root=tmp_path,
        **kwargs,
    )


def test_create_labels_new_worker_with_image_and_fingerprint(tmp_path: Path) -> None:
    ctr = _ctr(tmp_path)
    image_id = "sha256:abc123"
    calls: list[list[str]] = []

    def fake_run(args, *, capture=False, check=True):
        calls.append(list(args))
        cmd0 = args[0] if args else ""
        if cmd0 == "image" and args[1:2] == ["exists"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        if cmd0 == "image" and args[1:2] == ["inspect"]:
            return MagicMock(returncode=0, stdout=image_id + "\n", stderr="")
        if cmd0 == "container" and args[1:2] == ["exists"]:
            return MagicMock(returncode=1, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(ctr, "_run", side_effect=fake_run):
        ctr.create()

    create_cmds = [c for c in calls if c and c[0] == "create"]
    assert len(create_cmds) == 1
    create = create_cmds[0]
    labels = [
        create[i + 1]
        for i, tok in enumerate(create)
        if tok == "--label"
    ]
    assert f"{WORKER_LABEL_IMAGE_ID}={image_id}" in labels
    assert any(lab.startswith(f"{WORKER_LABEL_FINGERPRINT}=") for lab in labels)


def test_create_reuses_matching_running_worker(tmp_path: Path) -> None:
    ctr = _ctr(tmp_path)
    image_id = "sha256:deadbeef"
    fingerprint = ctr._worker_fingerprint()
    destroyed = False

    def fake_run(args, *, capture=False, check=True):
        nonlocal destroyed
        if args[:2] == ["image", "exists"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        if args[:2] == ["image", "inspect"]:
            return MagicMock(returncode=0, stdout=image_id + "\n", stderr="")
        if args[:2] == ["container", "exists"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        if args[0] == "inspect" and "NetworkMode" in args[-2]:
            return MagicMock(returncode=0, stdout="host\n", stderr="")
        if args[0] == "inspect" and "Labels" in args[-2]:
            payload = {
                WORKER_LABEL_IMAGE_ID: image_id,
                WORKER_LABEL_FINGERPRINT: fingerprint,
            }
            return MagicMock(
                returncode=0,
                stdout=json.dumps(payload) + "\n",
                stderr="",
            )
        if args[0] == "ps":
            # is_running / _ps_table
            return MagicMock(returncode=0, stdout="cid\n", stderr="")
        if args[0] == "rm":
            destroyed = True
            return MagicMock(returncode=0, stdout="", stderr="")
        if args[0] == "create":
            raise AssertionError("matching worker must not be recreated")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(ctr, "_run", side_effect=fake_run):
        ctr.create()

    assert not destroyed


def test_create_recreates_on_fingerprint_mismatch(tmp_path: Path) -> None:
    ctr = _ctr(tmp_path, extra_opts=["--dns=1.1.1.1"])
    image_id = "sha256:abc"
    destroyed = False
    created = False

    def fake_run(args, *, capture=False, check=True):
        nonlocal destroyed, created
        if args[:2] == ["image", "exists"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        if args[:2] == ["image", "inspect"]:
            return MagicMock(returncode=0, stdout=image_id + "\n", stderr="")
        if args[:2] == ["container", "exists"]:
            # exists until destroyed
            return MagicMock(
                returncode=0 if not destroyed else 1,
                stdout="",
                stderr="",
            )
        if args[0] == "inspect" and "NetworkMode" in "".join(args):
            return MagicMock(returncode=0, stdout="host\n", stderr="")
        if args[0] == "inspect" and "Labels" in "".join(args):
            payload = {
                WORKER_LABEL_IMAGE_ID: image_id,
                WORKER_LABEL_FINGERPRINT: "stale-fingerprint",
            }
            return MagicMock(
                returncode=0,
                stdout=json.dumps(payload) + "\n",
                stderr="",
            )
        if args[0] == "rm":
            destroyed = True
            return MagicMock(returncode=0, stdout="", stderr="")
        if args[0] == "create":
            created = True
            labels = [
                args[i + 1]
                for i, tok in enumerate(args)
                if tok == "--label"
            ]
            assert f"{WORKER_LABEL_IMAGE_ID}={image_id}" in labels
            assert any(
                lab.startswith(f"{WORKER_LABEL_FINGERPRINT}=") for lab in labels
            )
            return MagicMock(returncode=0, stdout="", stderr="")
        if args[0] == "start":
            return MagicMock(returncode=0, stdout="", stderr="")
        if args[0] == "ps":
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(ctr, "_run", side_effect=fake_run):
        ctr.create()

    assert destroyed
    assert created


def test_create_recreates_on_image_id_mismatch(tmp_path: Path) -> None:
    ctr = _ctr(tmp_path)
    fingerprint = ctr._worker_fingerprint()
    destroyed = False

    def fake_run(args, *, capture=False, check=True):
        nonlocal destroyed
        if args[:2] == ["image", "exists"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        if args[:2] == ["image", "inspect"]:
            return MagicMock(returncode=0, stdout="sha256:new\n", stderr="")
        if args[:2] == ["container", "exists"]:
            return MagicMock(
                returncode=0 if not destroyed else 1,
                stdout="",
                stderr="",
            )
        if args[0] == "inspect" and "NetworkMode" in "".join(args):
            return MagicMock(returncode=0, stdout="host\n", stderr="")
        if args[0] == "inspect" and "Labels" in "".join(args):
            payload = {
                WORKER_LABEL_IMAGE_ID: "sha256:old",
                WORKER_LABEL_FINGERPRINT: fingerprint,
            }
            return MagicMock(
                returncode=0,
                stdout=json.dumps(payload) + "\n",
                stderr="",
            )
        if args[0] == "rm":
            destroyed = True
            return MagicMock(returncode=0, stdout="", stderr="")
        if args[0] in ("create", "start", "ps"):
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(ctr, "_run", side_effect=fake_run):
        ctr.create()

    assert destroyed


def test_worker_fingerprint_changes_with_project_root(tmp_path: Path) -> None:
    a = _ctr(tmp_path / "a")
    b = _ctr(tmp_path / "b")
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert a._worker_fingerprint() != b._worker_fingerprint()

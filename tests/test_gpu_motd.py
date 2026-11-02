"""GPU MOTD must not crash on GPU-less hosts.

Round-2 audit 2.15b: `grep -c ... || echo "0"` prints 0 twice under
set -e (grep exits 1 on no match), so GPU_COUNT became $'0\\n0' and
`[[ "$GPU_COUNT" -gt 0 ]]` died with a syntax error; the /dev/kfd
branch emitted two lines the same way. Login-banner only, but it is
the first thing a user sees.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from hpc_cf.config import PROJECT_ROOT

SCRIPT = (
    PROJECT_ROOT
    / "spack-envs"
    / "cp2k_rocm-2026.1-gfx942"
    / "cp2k-gpu-motd.sh"
)


def _run_with_fake_bin(tmp_path: Path, rocm_smi_body: str | None) -> int:
    env_dir = tmp_path / "bin"
    env_dir.mkdir()
    if rocm_smi_body is not None:
        fake = env_dir / "rocm-smi"
        fake.write_text(f"#!/bin/sh\n{rocm_smi_body}\n", encoding="utf-8")
        fake.chmod(0o755)
    env = {
        "PATH": f"{env_dir}:/usr/bin:/bin",
        "HPC_CONTAINER_NAME": "cp2k-rocm-gfx942",
        "HPC_CONTAINER_TAG": "2026.1",
        "HPC_CONTAINER_BUILD_TS": "2026-01-01T00:00:00",
        "CP2K_DATA_DIR": "/opt/cp2k-data",
    }
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "syntax error" not in result.stderr, result.stderr
    return result.returncode


def test_motd_survives_rocm_smi_with_no_gpus(tmp_path: Path) -> None:
    """rocm-smi present, reports nothing (exit 0) — the exact shape that
    produced GPU_COUNT=$'0\\n0'."""
    assert _run_with_fake_bin(tmp_path, "exit 0") == 0


def test_motd_survives_rocm_si_erroring_out(tmp_path: Path) -> None:
    assert _run_with_fake_bin(tmp_path, "echo 'GPU[0]: AMD MI300X'; exit 0") == 0


def test_motd_survives_without_rocm_smi_and_without_gpu(tmp_path: Path) -> None:
    assert _run_with_fake_bin(tmp_path, None) == 0


def test_detect_gpus_emits_exactly_one_line(tmp_path: Path) -> None:
    """Extract detect_gpus and run all three branch shapes; multi-line
    output is the arithmetic-crash shape ($'0\\n0')."""
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("detect_gpus() {")
    end = text.index("\n}", start) + 2
    body = text[start:end]
    # Branch 3: textual swap of the hardcoded device test (mknod needs
    # root) — same semantics, controllable from the test.
    kfd_body = body.replace("[[ -c /dev/kfd ]]", '[[ -n "$FAKE_KFD" ]]')

    cases: list[tuple[str, str, dict[str, str]]] = [
        ("rocm-smi silent", body, {}),
        ("rocm-smi one gpu", body, {}),
        ("kfd fallback", kfd_body, {"FAKE_KFD": "yes"}),
    ]
    for i, (label, fn, extra_env) in enumerate(cases):
        bin_dir = tmp_path / f"case{i}"
        bin_dir.mkdir()
        if i == 0:
            fake = bin_dir / "rocm-smi"
            fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake.chmod(0o755)
        elif i == 1:
            fake = bin_dir / "rocm-smi"
            fake.write_text(
                "#!/bin/sh\necho 'GPU[0]: AMD Instinct MI300X'\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
        result = subprocess.run(
            ["bash", "-c", f"set -euo pipefail\n{fn}\ndetect_gpus"],
            env={"PATH": f"{bin_dir}:/usr/bin:/bin", **extra_env},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (label, result.stderr)
        lines = result.stdout.strip().splitlines()
        assert len(lines) == 1 and lines[0].isdigit(), (label, result.stdout)

"""SIF definition templates and testsuite confs must tell the truth.

Round-2 audit 2.15d: 9 of 13 *.def.j2 carried copy-paste rot — wrong
titles (a 2026.1 env titled 2025.2, an ROCm env titled
"Opensource AVX-512"), `--app-version` lines pointing at the wrong or
nonexistent env, an uppercase env name, and help text inviting users to
run a binary that does not exist (ABACUS.psmp). The VASP 6.6.1
testsuite confs still pinned --bind-to core + OMP_PLACES=cores against
the force-OpenMP preset (binding deliberately disabled).
"""

from __future__ import annotations

import re

from hpc_cf.config import PROJECT_ROOT

SPACK_ENVS = PROJECT_ROOT / "spack-envs"
_APP_VERSION_RE = re.compile(r"--app-version\s+(\S+)")


def test_def_templates_reference_their_own_env() -> None:
    """Every `--app-version` line in a *.def.j2 must name the env directory
    it lives in — the line is a copy-paste of the build command, and a
    wrong name sends users at another env's image."""
    for def_path in sorted(SPACK_ENVS.glob("*/*.def.j2")):
        text = def_path.read_text(encoding="utf-8")
        names = _APP_VERSION_RE.findall(text)
        assert names, f"{def_path}: no --app-version reference"
        for name in names:
            assert name == def_path.parent.name, (
                f"{def_path}: references env {name!r} but lives in "
                f"{def_path.parent.name!r}"
            )


def test_def_templates_do_not_invent_binaries_or_case() -> None:
    for def_path in sorted(SPACK_ENVS.glob("abacus_opensource-*/*.def.j2")):
        text = def_path.read_text(encoding="utf-8")
        assert "ABACUS.psmp" not in text, def_path
        assert "ABACUS_opensource" not in text, def_path


def test_def_titles_match_their_family() -> None:
    """Cheap cross-check: an ROCm env must not be titled "Opensource
    AVX-512", an MKL env must not be titled "Opensource"."""
    checks = {
        "cp2k_rocm-2026.1-gfx942": ("ROCm", ("Opensource", "AVX-512")),
        "cp2k_mkl-2025.2-experimental": ("MKL", ("Opensource",)),
        "cp2k_opensource-2026.1-force-avx512": ("2026.1", ("2025.2", "2026.2")),
        "cp2k_opensource-master-force-avx512": ("master", ("2025.2", "2026.2")),
    }
    for env, (must, forbidden) in checks.items():
        header = (SPACK_ENVS / env / "cp2k.def.j2").read_text(encoding="utf-8").splitlines()[0]
        assert must in header, (env, header)
        for word in forbidden:
            assert word not in header, (env, header)


def test_vasp_661_confs_follow_force_openmp_preset() -> None:
    """The 6.6.1 images disable MPI binding by design (force-OpenMP
    preset); the testsuite confs must not pin it back on."""
    for conf in sorted(
        SPACK_ENVS.glob("vasp_mkl-6.6.1-*/testsuite-file/spack_build_test*.conf")
    ):
        text = conf.read_text(encoding="utf-8")
        assert "--bind-to none" in text, conf
        assert "--bind-to core" not in text, conf
        assert "OMP_PLACES=cores" not in text, conf
        assert "OMP_PROC_BIND" not in text, conf

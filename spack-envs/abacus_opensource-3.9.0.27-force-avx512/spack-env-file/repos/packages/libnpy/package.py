# Copyright Spack Project Developers. See COPYRIGHT file for details.
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin.build_systems.generic import Package
from spack.package import *


class Libnpy(Package):
    """libnpy: header-only C++ library for reading and writing NumPy
    ``.npy``/``.npz`` files. Ships a single header ``include/npy.hpp``.

    Used by ABACUS (+mlalgo / +deepks) via the ``libnpy_INCLUDE_DIR`` CMake
    variable, to avoid ABACUS's FetchContent download at build time.
    """

    homepage = "https://github.com/llohse/libnpy"
    git = "https://github.com/llohse/libnpy.git"

    license("MIT")

    version("1.0.1", tag="v1.0.1", commit="890ea4fcda302a580e633c624c6a63e2a5d422f6")

    depends_on("cxx", type="build")

    def install(self, spec, prefix):
        install_tree("include", prefix.include)

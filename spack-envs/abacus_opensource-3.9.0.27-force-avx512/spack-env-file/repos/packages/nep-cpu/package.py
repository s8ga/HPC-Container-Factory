# Copyright Spack Project Developers. See COPYRIGHT file for details.
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import os

from spack_repo.builtin.build_systems.generic import Package
from spack.package import *


class NepCpu(Package):
    """NEP_CPU: standalone C++ implementation of the neuroevolution potential
    (NEP) from GPUMD. No external dependence. Used by ABACUS (+nep) via
    FindNEP.cmake, which expects ``<prefix>/include/nep.h`` and
    ``<prefix>/lib/libnep.a``.

    NEP_CPU ships no build system (no CMakeLists/Makefile), so the sources
    under ``src`` are compiled and archived into ``libnep.a`` manually,
    mirroring how other build-system-less libraries (e.g. libsvm) are packaged.

    IMPORTANT: use ``src/`` (``class NEP``), NOT ``libs/include/`` (stale
    ``class NEP3``). NEP_CPU v1.4 renamed NEP3 -> NEP in src/ (commit 036bd1f)
    but the libs/include/ copy was not synced. ABACUS expects class NEP.
    """

    homepage = "https://github.com/brucefan1983/NEP_CPU"
    git = "https://github.com/brucefan1983/NEP_CPU.git"

    license("GPL-3.0-or-later")

    version("1.4", tag="v1.4", commit="43b2ee64dd03e7e880cd343582b0de31b715c222")

    depends_on("cxx", type="build")

    def install(self, spec, prefix):
        mkdirp(prefix.lib)
        mkdirp(prefix.include)

        # Headers from src/ (nep.h carries class NEP matching ABACUS).
        for hdr in ("nep.h", "nep_utilities.h", "neighbor_nep.h",
                    "ewald_nep.h", "dftd3para.h"):
            install(join_path("src", hdr), prefix.include)

        # Compile src/*.cpp -> PIC objects, then archive into libnep.a.
        # The spack cxx wrapper injects optimization flags; we only add what
        # is required for a static lib meant to be linked into shared objects.
        cxx = which(os.environ["CXX"])
        include_flag = "-I{0}".format(prefix.include)
        objects = []
        for src in ("nep.cpp", "neighbor_nep.cpp", "ewald_nep.cpp"):
            obj = src.replace(".cpp", ".o")
            cxx("-fPIC", "-std=c++11", include_flag, "-c",
                join_path("src", src), "-o", obj)
            objects.append(obj)

        ar = which("ar")
        ar("rcs", join_path(prefix.lib, "libnep.a"), *objects)

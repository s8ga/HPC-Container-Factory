# Copyright Spack Project Developers. See COPYRIGHT file for details.
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin.build_systems.generic import Package

from spack.package import *


class Libri(Package):
    """LibRI: a header-only library for resolution-of-identity related methods,
    used by ABACUS for hybrid functional (EXX) calculations.

    Installed layout matches what ABACUS's ``cmake/FindLibRI.cmake`` expects:
    ``<prefix>/include/RI/version.h``.
    """

    homepage = "https://github.com/abacusmodeling/LibRI"
    url = "https://github.com/abacusmodeling/LibRI/archive/refs/tags/v0.2.1.1.tar.gz"
    list_url = "https://github.com/abacusmodeling/LibRI/releases"

    license("LGPL-3.0-or-later")

    version("0.2.1.1", sha256="cd33fd5428400ea696b82c9132878c07bf785847b3f56b1979e25a3a5fc0b311")
    version("0.2.1.0", sha256="66a5540daba36effdad6ce2fe5e8368b96ddd4a7e148af90894ef21dc20ff29f")
    version("0.2.0.0", sha256="1fbdcf1ae35fb24b93cc766b0ef89509c81c111fa3797b009d7a2c99f691d332")
    version("0.2.0", sha256="ad79dfbc3ed8ff066c85549a2737d29205dbf755b38ea216ab2ab42754f80389")
    version("0.1.1", sha256="51deb08aa373e54d2c123b57bfd4b3507accac0d496a94b766eaeadccd9e4bd0")
    version("0.1.0", sha256="4721276e35b64e96f349df9899039159da0215ae0c1df94523c5c25fab3f7f89")

    depends_on("cxx", type="build")

    def install(self, spec, prefix):
        install_tree("include", prefix.include)

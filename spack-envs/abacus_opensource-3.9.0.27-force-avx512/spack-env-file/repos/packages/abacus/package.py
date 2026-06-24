# Copyright Spack Project Developers. See COPYRIGHT file for details.
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import glob
import os

from spack_repo.builtin.build_systems.cmake import CMakePackage
from spack.package import *


class Abacus(CMakePackage):
    """ABACUS (Atomic-orbital Based Ab-initial Computation at UStc) is an
    open-source package for first-principles electronic structure calculations.

    This package supports two parallel release lines, verified via git branch
    topology (fork point c53f4456, 2025-01-02):

      * LTS line (origin/LTS): 3.10.x -- old build system
        (ENABLE_DEEPKS, module_* sources, Intel MKL only, no BuildInfo)
      * develop   (origin/develop): 3.9.0.x + 3.11.x -- new build system
        (ENABLE_MLALGO, source_* sources, GNU+MKL supported, BuildInfo)

    LTS 3.10.x is captured by ``@3.10``; develop by ``@3.9.0.10:`` (cutoff at
    v3.9.0.10 -- earlier 3.9.0.8/.9 are transitional and unsupported).
    """

    homepage = "https://abacus.deepmodeling.com/"
    git = "https://github.com/deepmodeling/abacus-develop.git"
    list_url = "https://github.com/deepmodeling/abacus-develop/releases"

    license("LGPL-3.0-or-later")

    # Build-time sanity check: verify the binary exists after install.
    sanity_check_is_file = [join_path("bin", "abacus")]

    # ------------------------------------------------------------------ #
    #  Versions                                                          #
    # ------------------------------------------------------------------ #
    # newest first. git tag + full commit SHA gives trusted provenance
    # (Spack strongly recommends pairing tags with commits) and keeps .git
    # in the stage so COMMIT_INFO=ON can `git describe` for `abacus --version`,
    # which matters for bug tracking. LTS tags carry a "-lts" suffix to
    # distinguish them from any future regular 3.10.x on the develop line.
    # develop line (new build system), newest first
    version("3.11.0-beta.4", tag="v3.11.0-beta4",
            commit="7cb8da759b96b6508515515cffd7dcfea9596fb6")
    version("3.9.0.27", tag="v3.9.0.27",
            commit="3a996b6eb50af8c2c07bc3ff39c67e6164d56faf")
    version("3.9.0.25", tag="v3.9.0.25",
            commit="2087308d9d09d57d0c6a9fe7c6dbbeb075a5dc40")
    version("3.9.0.20", tag="v3.9.0.20",
            commit="f1c5f1b47bb0209182a0da65ca6101a017844880")
    version("3.9.0.15", tag="v3.9.0.15",
            commit="b062b0758d3636799dc83fe6ddf480c412542c63")
    version("3.9.0.10", tag="v3.9.0.10",
            commit="a94c62d265d825f39a8d6b3b52c35e8ec68d887d")
    # LTS line (old build system)
    version("3.10.1-lts", tag="v3.10.1",
            commit="f71921fe848659deac8db319cd4311b55b5ad480")
    version("3.10.0-lts", tag="LTSv3.10.0",
            commit="1be7425f0e73dacbe9c9e7be3fbfc9df30d100fa")

    # ------------------------------------------------------------------ #
    #  Variants                                                          #
    # ------------------------------------------------------------------ #

    # Core build options (all versions)
    variant("mpi", default=True, description="Enable MPI parallelization")
    variant("openmp", default=True, description="Enable OpenMP support")
    variant(
        "lcao",
        default=True,
        description="Enable LCAO (linear combination of atomic orbitals) basis",
    )
    variant(
        "float-fftw",
        default=True,
        description="Enable single-precision FFTW backend (matches official CI default)",
    )
    variant(
        "native-optimization",
        default=False,
        description="Enable host-native CPU optimizations (-march=native)",
    )
    variant(
        "debug",
        default=False,
        description="Enable developer debug messages (DEBUG_INFO)",
    )
    variant(
        "mathlib",
        default=False,
        description="Build ABACUS libmath from source (USE_ABACUS_LIBM, "
        "only useful for old compilers lacking optimized libm)",
    )

    # Variants gated on LCAO and/or MPI (forced OFF by CMake otherwise)
    variant(
        "elpa",
        default=True,
        when="+lcao+mpi",
        description="Use ELPA for eigenvalue problems (requires LCAO+MPI)",
    )
    variant(
        "pexsi",
        default=False,
        when="+lcao+mpi",
        description="Enable PEXSI for large-scale electronic structure "
        "(requires LCAO+MPI)",
    )

    # Optional scientific libraries (all versions)
    variant("libri", default=False, description="Enable EXX with LibRI")
    variant("libxc", default=False, description="Enable LibXC functionality")
    variant("rapidjson", default=False, description="Enable RapidJSON usage")
    variant("deepmd", default=False, description="Enable DeePMD-kit (Deep Potential MD)")

    # ML: follow upstream option names exactly.
    #   LTS (3.10.x) + <=3.9.0.7 use ENABLE_DEEPKS
    #   develop @3.9.0.8: uses ENABLE_MLALGO (unified DeePKS + ML-KEDF)
    # Two variants with disjoint version ranges (the OR of two `when` clauses
    # on `mlalgo` covers develop 3.9.0.x + 3.11.x while excluding LTS 3.10.x,
    # which sits in between on the version axis). Requesting the absent
    # variant on a given version auto-errors (spack: "variant not found").
    variant(
        "deepks",
        default=False,
        when="@3.10",
        description="DeePKS (maps to ENABLE_DEEPKS, LTS line)",
    )
    variant(
        "mlalgo",
        default=False,
        when="@3.9.0.10:3.9.0.27",
        description="ML algorithms: DeePKS + ML-KEDF (maps to ENABLE_MLALGO, develop)",
    )
    variant(
        "mlalgo",
        default=False,
        when="@3.11.0-beta.1:",
        description="ML algorithms: DeePKS + ML-KEDF (maps to ENABLE_MLALGO, develop)",
    )

    # New features on the develop line (version-gated to their introduction)
    variant(
        "nep",
        default=False,
        when="@3.9.0.27:",
        description="Enable NEP neuroevolution potential (FindNEP.cmake)",
    )
    variant(
        "kml",
        default=False,
        when="@3.11.0-beta.3:",
        description="Enable Kunpeng Math Library (upstream stub, no backend lib)",
    )
    variant(
        "dftd4",
        default=False,
        when="@3.11.0-beta.4:",
        description="Enable DFT-D4 dispersion correction",
    )

    # Build unit tests + install test data for container regression testing.
    # Uses a bundled GoogleTest resource (no spack googletest dependency).
    variant(
        "tests",
        default=False,
        description="Build unit tests (GoogleTest) and install test data",
    )

    # ------------------------------------------------------------------ #
    #  Resources                                                         #
    # ------------------------------------------------------------------ #

    # GoogleTest source, staged to third_party/googletest/. FetchContent
    # picks it up via FETCHCONTENT_SOURCE_DIR_GOOGLETEST (see cmake_args),
    # so CMake never hits the network. Version pinned to v1.14.0.
    resource(
        name="googletest",
        url="https://github.com/google/googletest/archive/refs/tags/v1.14.0.zip",
        sha256="1f357c27ca988c3f7c6b4bf68a9395005ac6761f034046e9dde0896e3aba00e4",
        destination="third_party",
        placement="googletest",
        when="+tests",
    )

    # ------------------------------------------------------------------ #
    #  Dependencies                                                      #
    # ------------------------------------------------------------------ #

    # Language and build tools. ABACUS declares `project(... LANGUAGES CXX)`
    # only; the source tree has one .c file (mcd.c), so keep `c` for safety.
    depends_on("c", type="build")
    depends_on("cxx", type="build")
    depends_on("cmake@3.16:", type="build")

    # MPI
    depends_on("mpi", when="+mpi")

    # Core math libraries (virtual providers, solver picks one)
    depends_on("fftw-api@3")
    depends_on("blas")
    depends_on("lapack")
    depends_on("scalapack", when="+lcao+mpi")

    # LCAO dependencies
    depends_on("cereal", when="+lcao")
    depends_on("elpa", when="+lcao+elpa")

    # Optional libraries
    depends_on("libxc", when="+libxc")
    depends_on("libri", when="+libri")
    depends_on("libcomm", when="+libri")
    depends_on("pexsi", when="+pexsi")
    depends_on("rapidjson", when="+rapidjson")
    depends_on("deepmdkit", when="+deepmd")

    # ML: libtorch via py-torch + libnpy header (libnpy_INCLUDE_DIR avoids
    # ABACUS FetchContent download at build time).
    #
    # Upper bound 2.4: ABACUS uses the torch::linalg C++ namespace, which was
    # removed in pytorch 2.5 (2024-10; functions moved to torch::linalg_*
    # prefix). develop HEAD is mid-migration but still uses the old namespace,
    # so every ABACUS version (LTS + develop) requires py-torch <= 2.4.
    depends_on("py-torch@2.1:2.4 ~cuda", when="+deepks")
    depends_on("py-torch@2.1:2.4 ~cuda", when="+mlalgo")
    depends_on("libnpy", when="+deepks")
    depends_on("libnpy", when="+mlalgo")

    # NEP (self-built nep-cpu package; FindNEP expects <prefix>/{include,lib})
    depends_on("nep-cpu", when="+nep")

    # DFT-D4. ABACUS is CXX-only but ENABLE_DFTD4=ON calls
    # enable_language(Fortran) in CMakeLists.txt (beta.4+), so we
    # need the Fortran compiler even though we only link dftd4.
    # Force build_system=cmake: dftd4's meson build does not produce
    # dftd4Config.cmake, which ABACUS's find_package(dftd4) requires
    # (see PR #7380).
    depends_on("fortran", type="build", when="+dftd4")
    depends_on("dftd4@4.2.0: build_system=cmake", when="+dftd4")

    # OpenMP forward propagation: pick threaded BLAS/FFTW/MKL providers
    with when("+openmp"):
        depends_on("fftw+openmp", when="^[virtuals=fftw-api] fftw")
        depends_on(
            "openblas threads=openmp", when="^[virtuals=blas,lapack] openblas"
        )
        depends_on(
            "intel-oneapi-mkl threads=openmp",
            when="^[virtuals=blas,lapack,scalapack,fftw-api] intel-oneapi-mkl",
        )

    # Single-precision FFTW: ENABLE_FLOAT_FFTW links FFTW3::FFTW3_FLOAT.
    depends_on("fftw precision=float", when="+float-fftw ^[virtuals=fftw-api] fftw")

    # OpenMP reverse propagation: ensure elpa is built ~openmp when ABACUS is
    with when("~openmp"):
        depends_on("elpa~openmp", when="+elpa")

    # GNU+MKL: develop @3.9.0.25: supports the gf interface (cp2k pattern).
    # Force +gfortran so MKL ships mkl_gf_lp64 / mkl_gnu_thread for %gcc.
    depends_on(
        "intel-oneapi-mkl+gfortran threads=openmp",
        when="@3.9.0.25: +openmp ^[virtuals=blas] intel-oneapi-mkl %gcc",
    )

    # ------------------------------------------------------------------ #
    #  Conflicts                                                         #
    # ------------------------------------------------------------------ #

    # GNU+MKL is unsupported on LTS and on develop <3.9.0.25: FindMKL only
    # locates Intel Fortran interfaces there, so %gcc linking fails.
    conflicts(
        "%gcc ^intel-oneapi-mkl",
        when="@3.10",
        msg="LTS MKL only provides Intel Fortran interfaces. "
        "Use %intel-oneapi-compilers with intel-oneapi-mkl, or pick a "
        "non-MKL BLAS/FFTW provider (e.g. openblas + fftw).",
    )
    conflicts(
        "%gcc ^intel-oneapi-mkl",
        when="@:3.9.0.24",
        msg="develop <3.9.0.25: GNU+MKL not supported (same as LTS).",
    )

    # LTS PEXSI compile fix: upstream bug (missing #include + Gint_inout
    # signature mismatch) fixed in PR #6689 on develop, but not cherry-picked
    # to the LTS branch. See issue #6684.
    patch("lts-pexsi-compile.patch", when="@3.10 +pexsi")

    # v3.9.0.10 compile fix: uint64_t used without #include <cstdint>
    # (fixed in later develop versions)
    patch("v3.9.0.10-cstdint.patch", when="@3.9.0.10")

    # Fix PexsiPrepare non-copyable for GoogleTest parameterized tests.
    # Root cause: Parallel_2D declares move ops (= default) which implicitly
    # deletes the copy constructor (C++11 §12.8). PexsiPrepare contains
    # Parallel_Orbitals (inherits Parallel_2D) → non-copyable.
    #
    # Group A: source_hsolver layout — also needs missing scalapack_connector.h include
    patch("pexsi-tests-copyable-3.9.patch", when="@3.9.0.10:3.9.0.27 +pexsi +tests")
    patch("pexsi-tests-copyable-3.9.patch", when="@3.11.0-beta.4 +pexsi +tests")
    # Group B: module_hsolver layout — copy ctor only, no extra include needed
    patch("pexsi-tests-copyable-3.10.patch", when="@3.10 +pexsi +tests")

    # Skip esolver_dp_test when DeepMDKit is found but not linked to test target.
    # CMake defines __DPMD globally via add_compile_definitions, but only links
    # DeePMD::deepmd_c to the main binary — test targets fail to link.
    patch("esolver_dp_test-guard-3.9.patch", when="@3.9.0.10:3.9.0.27 +tests")
    patch("esolver_dp_test-guard-3.9.patch", when="@3.11.0-beta.4 +tests")
    patch("esolver_dp_test-guard-3.10.patch", when="@3.10 +tests")

    # ------------------------------------------------------------------ #
    #  Patch (+tests: rewrite NAO test deep paths)                       #
    # ------------------------------------------------------------------ #

    def patch(self):
        """Apply version-specific patches and +tests source rewrites."""
        if "+tests" in self.spec:
            # Rewrite deep relative paths for flat install layout.
            # Patterns: ../../../../../tests/PP_ORB/ and ../../../../../source/...
            # Use test* to also catch test_serial, test_pw, test_parallel, etc.
            test_cpp_dirs = [
                join_path(self.stage.source_path, "source", "*", "test*"),
                join_path(self.stage.source_path, "source", "*", "*", "test*"),
            ]
            for pattern in test_cpp_dirs:
                for cpp in glob.glob(join_path(pattern, "*.cpp")):
                    # tests/PP_ORB/ deep paths (4-5 levels)
                    filter_file(
                        r'"(\./)?(\.\./){4,5}tests/PP_ORB/',
                        '"./PP_ORB/',
                        cpp,
                    )
                    # source/.../test/ deep paths (5 levels, for test-specific data)
                    filter_file(
                        r'"(\.\./){5}source/source_basis/module_nao/test/',
                        '"./',
                        cpp,
                    )

    # ------------------------------------------------------------------ #
    #  CMake arguments                                                   #
    # ------------------------------------------------------------------ #

    def cmake_args(self):
        spec = self.spec
        args = [
            # --- shared variant -> option mapping (all versions) ---
            self.define_from_variant("ENABLE_MPI", "mpi"),
            self.define_from_variant("USE_OPENMP", "openmp"),
            self.define_from_variant("ENABLE_LCAO", "lcao"),
            self.define_from_variant("USE_ELPA", "elpa"),
            self.define_from_variant("ENABLE_LIBRI", "libri"),
            self.define_from_variant("ENABLE_LIBXC", "libxc"),
            self.define_from_variant("ENABLE_PEXSI", "pexsi"),
            self.define_from_variant("ENABLE_RAPIDJSON", "rapidjson"),
            self.define_from_variant("ENABLE_FLOAT_FFTW", "float-fftw"),
            self.define_from_variant(
                "ENABLE_NATIVE_OPTIMIZATION", "native-optimization"
            ),
            self.define_from_variant("DEBUG_INFO", "debug"),
            self.define_from_variant("USE_ABACUS_LIBM", "mathlib"),
            # --- shared force-disabled (CPU-only x86_64) ---
            self.define("USE_CUDA", False),
            self.define("USE_ROCM", False),
            self.define("USE_DSP", False),
            self.define("USE_CUDA_ON_DCU", False),
            self.define("ENABLE_CUSOLVERMP", False),
            self.define("GIT_SUBMODULE", False),
            # COMMIT_INFO=ON: git versions keep .git in the stage, so
            # `git describe` works and `abacus --version` shows the commit.
            self.define("COMMIT_INFO", True),
            # Official CI does not set CMAKE_BUILD_TYPE, so NDEBUG is never
            # defined and EXPECT_DEATH tests pass. Spack's build_type variant
            # adds -DNDEBUG via CMAKE_CXX_FLAGS_<TYPE>, which strips assert().
            # Replace the build-type flags to remove -DNDEBUG while keeping
            # optimization (-O3 for Release, -O3 -g for RelWithDebInfo).
            self.define("CMAKE_CXX_FLAGS_RELEASE", "-O3"),
            self.define("CMAKE_CXX_FLAGS_RELWITHDEBINFO", "-O3 -g"),
        ]

        # FFT backend: MKL (MKLROOT) vs FFTW3 (FFTW3_DIR).
        # FindMKL reads the *CMake variable* ${MKLROOT} (not the env var), so
        # it must be passed explicitly. intel-oneapi-mkl ships MKL under
        # <prefix>/mkl/<version>/{include,lib}; `latest` symlinks the active
        # version and is stable across MKL releases.
        if "^intel-oneapi-mkl" in spec:
            mkl = spec["intel-oneapi-mkl"]
            args.append(
                self.define("MKLROOT", join_path(mkl.prefix, "mkl", "latest"))
            )
        else:
            args.append(self.define("FFTW3_DIR", spec["fftw-api"].prefix))

        # Cereal: FindCereal expects CEREAL_INCLUDE_DIR = the include dir
        # itself (it searches for cereal/cereal.hpp under it).
        if "+lcao" in spec:
            args.append(
                self.define("CEREAL_INCLUDE_DIR", spec["cereal"].prefix.include)
            )

        # ELPA: FindELPA uses ELPA_DIR (prefix); it has a built-in #3589
        # guard that rejects /usr/include/elpa system hits when ELPA_DIR is set.
        if "+lcao+elpa" in spec:
            args.append(self.define("ELPA_DIR", spec["elpa"].prefix))

        # LibRI / LibComm: ENABLE_LIBRI auto-enables ENABLE_LIBCOMM in CMake,
        # but we pass it explicitly and both _DIR prefixes for clarity.
        if "+libri" in spec:
            args.append(self.define("ENABLE_LIBCOMM", True))
            args.append(self.define("LIBRI_DIR", spec["libri"].prefix))
            args.append(self.define("LIBCOMM_DIR", spec["libcomm"].prefix))

        # LibXC: FindLibxc uses Libxc_DIR (prefix) or pkg-config.
        if "+libxc" in spec:
            args.append(self.define("Libxc_DIR", spec["libxc"].prefix))

        # PEXSI: FindPEXSI requires PEXSI_DIR + ParMETIS_DIR + SuperLU_DIST_DIR
        # (ParMETIS and SuperLU_DIST libraries are REQUIRED). spack's pexsi
        # package already depends on parmetis + superlu-dist.
        if "+pexsi" in spec:
            args.append(self.define("PEXSI_DIR", spec["pexsi"].prefix))
            args.append(self.define("ParMETIS_DIR", spec["parmetis"].prefix))
            args.append(
                self.define("SuperLU_DIST_DIR", spec["superlu-dist"].prefix)
            )

        # DeePMD: variable-driven (DEFINED DeePMD_DIR enables it; no option).
        if "+deepmd" in spec:
            args.append(self.define("DeePMD_DIR", spec["deepmdkit"].prefix))

        # --- version-branched options (core architecture) ---
        if spec.satisfies("@3.10"):
            # LTS old build system
            args.append(self.define_from_variant("ENABLE_DEEPKS", "deepks"))
            # LTS still has ENABLE_PAW but requires libpaw_interface (no spack package).
            # Force OFF — PAW is not supported in this build.
            args.append(self.define("ENABLE_PAW", False))
            if "+deepks" in spec:
                self._add_torch_args(args, spec)
                args.append(
                    self.define(
                        "libnpy_INCLUDE_DIR", spec["libnpy"].prefix.include
                    )
                )

        elif spec.satisfies("@3.9.0.10:"):
            # develop new build system
            args.append(self.define_from_variant("ENABLE_MLALGO", "mlalgo"))
            # USE_SW exists @3.9.0.10: but x86 doesn't need Sunway.
            args.append(self.define("USE_SW", False))
            # ENABLE_PAW was removed @3.9.0.10: do not pass it.

            if "+mlalgo" in spec:
                self._add_torch_args(args, spec)
                args.append(
                    self.define(
                        "libnpy_INCLUDE_DIR", spec["libnpy"].prefix.include
                    )
                )

            # develop sub-branches (continuous version ranges within @3.9.0.10:)
            if spec.satisfies("@3.9.0.27:") and "+nep" in spec:
                args.append(self.define("NEP_DIR", spec["nep-cpu"].prefix))
            if spec.satisfies("@3.11.0-beta.3:"):
                args.append(self.define_from_variant("USE_KML", "kml"))
            if spec.satisfies("@3.11.0-beta.4:"):
                args.append(self.define_from_variant("ENABLE_DFTD4", "dftd4"))

        # Unit test support (+tests variant). GoogleTest source is staged
        # by resource() to third_party/googletest/. We tell CMake's
        # FetchContent to use that local copy instead of downloading.
        if "+tests" in spec:
            args.append(self.define("BUILD_TESTING", True))
            args.append(
                self.define(
                    "FETCHCONTENT_SOURCE_DIR_GOOGLETEST",
                    join_path(self.stage.source_path, "third_party", "googletest"),
                )
            )

        return args

    def _add_torch_args(self, args, spec):
        """Locate TorchConfig.cmake under py-torch.

        py-torch buries TorchConfig.cmake in its python site-packages tree
        (confirmed empirically with py-torch@2.12.0 and corroborated by cp2k's
        cmake_cp2k.sh: "PyTorch's TorchConfig.cmake is buried in the Python
        site-packages directory"). The python version in the path is dynamic,
        so the directory is glob-discovered rather than hard-coded.
        """
        torch = spec["py-torch"]
        pattern = join_path(
            str(torch.prefix),
            "lib",
            "python*",
            "site-packages",
            "torch",
            "share",
            "cmake",
            "Torch",
        )
        matches = glob.glob(pattern)
        if not matches:
            raise InstallError(
                "TorchConfig.cmake not found under py-torch prefix. "
                "Expected pattern: {0}".format(pattern)
            )
        args.append(self.define("Torch_DIR", matches[0]))

    # ------------------------------------------------------------------ #
    #  Test artifact installation (+tests variant)                       #
    # ------------------------------------------------------------------ #

    @run_after("install")
    def install_test_artifacts(self):
        """Collect all test artifacts, preserving module directory structure.

        Integration tests go flat to ``share/abacus/tests/`` (01_PW/, etc.).
        Module unit tests preserve their build directory structure so each
        binary sits next to its own ``support/`` directory — this avoids
        file conflicts when multiple modules have same-named support files
        with different contents (e.g. chg.cube).

        Layout::

            share/abacus/tests/
              01_PW/                          # integration tests (flat)
              integrate/Autotest.sh
              PP_ORB/
              source_io/test_serial/
                MODULE_IO_rho_io              # binary + support together
                support/chg.cube              # 36³ (this module's version)
              source_estate/test/
                MODULE_ESTATE_charge_test
                support/chg.cube              # 32³ (different content, no conflict)
              source_cell/test/
                MODULE_CELL_unitcell_test
                support/...

        Run unit tests with the abacus_run_module_tests.sh script.
        Run integration tests with abacus_run_integration_tests.sh.
        """
        if "+tests" not in self.spec:
            return

        dst = join_path(self.prefix.share, "abacus", "tests")
        src = self.stage.source_path
        build = self.build_directory

        # 1. Integration test data (INPUT/STRU/KPT/PP_ORB/Autotest.sh)
        install_tree(join_path(src, "tests"), dst)

        # 2. MODULE_* unit tests: copy from build dir preserving module structure.
        #    The build dir has binaries + support/ + data/ together (CMake's
        #    file(COPY support ...) puts them side by side). We copy everything
        #    except CMake artifacts, so each test runs with its own support files.
        _SKIP = {
            "CMakeFiles", "Makefile", "cmake_install.cmake",
            "CTestTestfile.cmake", "CMakeCache.txt", "cmake_pch",
        }
        for build_test_dir in glob.glob(join_path(build, "source", "*", "test*")) + \
                              glob.glob(join_path(build, "source", "*", "*", "test*")):
            if not os.path.isdir(build_test_dir):
                continue
            rel = os.path.relpath(build_test_dir, join_path(build, "source"))
            dst_module = join_path(dst, rel)
            mkdirp(dst_module)
            for item in os.listdir(build_test_dir):
                if item in _SKIP or item.endswith(".cmake") or item.endswith(".pch"):
                    continue
                src_item = join_path(build_test_dir, item)
                dst_item = join_path(dst_module, item)
                if os.path.isdir(src_item):
                    if not os.path.exists(dst_item):
                        install_tree(src_item, dst_item)
                elif os.path.isfile(src_item):
                    if not os.path.exists(dst_item):
                        install(src_item, dst_module)

        # 3. Copy shared test data (PP_ORB) into each module test dir.
        #    NAO and IO tests reference ./PP_ORB/ (rewritten by patch() from
        #    deep relative paths). PP_ORB is small and shared across modules.
        pp_orb_src = join_path(src, "tests", "PP_ORB")
        if os.path.isdir(pp_orb_src):
            for module_test_dir in glob.glob(join_path(dst, "source_*", "test*")) + \
                                    glob.glob(join_path(dst, "source_*", "*", "test*")) + \
                                    glob.glob(join_path(dst, "module_*", "test*")) + \
                                    glob.glob(join_path(dst, "module_*", "*", "test*")):
                if os.path.isdir(module_test_dir):
                    install_tree(pp_orb_src, join_path(module_test_dir, "PP_ORB"))

        # 4. Copy source-only test data files (.txt, .json, .html, etc.) that
        #    were not copied to the build dir by CMake's file(COPY).  The NAO
        #    deep-path rewrite in patch() turns "../../../source/.../test/FILE"
        #    into "./FILE", so these files must sit next to the binary.
        for src_test_dir in glob.glob(join_path(src, "source", "*", "test*")) + \
                             glob.glob(join_path(src, "source", "*", "*", "test*")):
            if not os.path.isdir(src_test_dir):
                continue
            rel = os.path.relpath(src_test_dir, join_path(src, "source"))
            dst_module = join_path(dst, rel)
            if not os.path.isdir(dst_module):
                continue
            for item in os.listdir(src_test_dir):
                if item in (".", "..", "CMakeFiles", "CMakeLists.txt"):
                    continue
                if item.endswith((".cpp", ".h", ".hpp", ".cmake")):
                    continue
                src_item = join_path(src_test_dir, item)
                dst_item = join_path(dst_module, item)
                if os.path.exists(dst_item):
                    continue
                if os.path.isdir(src_item):
                    install_tree(src_item, dst_item)
                elif os.path.isfile(src_item):
                    install(src_item, dst_module)

    # ------------------------------------------------------------------ #
    #  Stand-alone smoke tests (spack test run)                          #
    # ------------------------------------------------------------------ #

    def test_version(self):
        """ensure abacus --version works"""
        abacus = which(self.prefix.bin.abacus)
        out = abacus("--version", output=str.split, error=str.split)
        assert "ABACUS" in out

    def test_info(self):
        """ensure abacus --info shows build details"""
        if self.spec.satisfies("@3.10"):
            raise SkipTest("--info not available on LTS")
        abacus = which(self.prefix.bin.abacus)
        out = abacus("--info", output=str.split, error=str.split)
        assert "Compiler" in out

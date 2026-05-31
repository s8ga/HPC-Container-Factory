# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import os
import shutil
import re
import glob

from spack_repo.builtin.build_systems.cuda import CudaPackage
from spack_repo.builtin.build_systems.makefile import MakefilePackage

from spack.package import *


class Vasp(MakefilePackage, CudaPackage):
    """
    The Vienna Ab initio Simulation Package (VASP)
    is a computer program for atomic scale materials modelling,
    e.g. electronic structure calculations
    and quantum-mechanical molecular dynamics, from first principles.
    """

    homepage = "https://vasp.at"
    url = "file://{0}/vasp.5.4.4.pl2.tgz".format(os.getcwd())
    maintainers("snehring")
    manual_download = True

    version("6.6.0", sha256="9566f59b0ae2fc60f670a91153655d09dba13fe6cc6c54e9ca6bd03bbcd86384")

    variant("openmp", default=False, description="Enable openmp build")

    variant("cuda", default=False, description="Enables running on Nvidia GPUs")
    variant("fftlib", default=True, when="+openmp", description="Enables fftlib build")

    variant("shmem", default=True, description="Enable use_shmem build flag")
    variant("hdf5", default=False, description="Enabled HDF5 support")
    variant("libbeef", default=False, description="Enable Libbeef support")
    variant("libxc", default=False, description="Enable Libxc support")
    variant("wannier90", default=False, description="Enable wannier90 support")


    variant("vtst", default=False, description="Enable VTST modified code")
    resource(
        name="vtst_src",
        url="https://github.com/henkelmangroup/vtstcode/archive/2596bccc6ef684965cd528841ea617334fa50e13.tar.gz",
        sha256="4275937fd7e19155ae8c87b51c7e7b4b07e18645e84f3287098976d02c810f79",
        placement="vtst_src",
        when="+vtst"
    )

    variant("vaspsol", default=False, description="Enable VASPsol solvation model")

    patch("WANNIER90_WIN_MAXLEN_fix.patch", when='@6.2.0: +wannier90')

    patch("pot_electrostat_fix.patch",when='@6.6.0')

    variant("dftd4",  default=False, description="Enable DFT-D4 van der Waals correction")
    variant("sdftd3", default=False, description="Enable simple-DFT-D3 van der Waals correction")

    depends_on("dftd4",       when="+dftd4")
    depends_on("simple-dftd3", when="+sdftd3")

    variant("elpa", default=False, description="Enable ELPA eigenvalue solver")
    depends_on("elpa+openmp", when="+elpa+openmp")
    depends_on("elpa~openmp", when="+elpa~openmp")

    depends_on("c", type="build")
    depends_on("cxx", type="build")
    depends_on("fortran", type="build")

    depends_on("rsync", type="build")
    depends_on("blas")
    depends_on("lapack")
    depends_on("fftw-api")
    depends_on("fftw+openmp", when="+openmp ^[virtuals=fftw-api] fftw")
    depends_on("amdfftw+openmp", when="+openmp ^[virtuals=fftw-api] amdfftw")
    depends_on("amdblis threads=openmp", when="+openmp ^[virtuals=blas] amdblis")
    depends_on("openblas threads=openmp", when="+openmp ^[virtuals=blas] openblas")
    depends_on("mpi", type=("build", "link", "run"))
    # fortran oddness requires the below
    depends_on("openmpi%aocc", when="%aocc ^[virtuals=mpi] openmpi")
    depends_on("openmpi%gcc", when="%gcc ^[virtuals=mpi] openmpi")
    depends_on("scalapack")
    depends_on("nccl", when="+cuda")
    depends_on("hdf5+fortran+mpi", when="+hdf5")
    depends_on("libbeef", when="+libbeef")
    depends_on("libxc~fhc+fortran", when="+libxc")
    depends_on("wannier90", when="+wannier90")
    # at the very least the nvhpc mpi seems required
    requires("^nvhpc+mpi+lapack+blas", when="%nvhpc")

    conflicts(
        "%gcc@:8", msg="GFortran before 9.x does not support all features needed to build VASP"
    )
    requires("%nvhpc", when="+cuda", msg="vasp requires nvhpc to build the openacc build")
    # intel mkl/mpi conflicts with ilp64, which is a default behaviour
    requires("^intel-oneapi-mkl~ilp64", when="^intel-oneapi-mkl")
    requires("^intel-oneapi-mpi~ilp64", when="^intel-oneapi-mpi")
    # the mpi compiler wrappers in nvhpc assume nvhpc is the underlying compiler, seemingly
    conflicts("^[virtuals=mpi] nvhpc", when="%gcc", msg="nvhpc mpi requires nvhpc compiler")
    conflicts("^[virtuals=mpi] nvhpc", when="%aocc", msg="nvhpc mpi requires nvhpc compiler")
    conflicts("cuda_arch=none", when="+cuda", msg="CUDA arch required when building openacc port")

    def patch(self):
        if "+vtst" in self.spec:
            v_src = join_path(self.stage.source_path, "src")
            vt_src = join_path(self.stage.source_path, "vtst_src", "vtstcode6.6.0")

            for f in os.listdir(vt_src):
                if f.endswith((".F", ".f90")) and os.path.isfile(join_path(vt_src, f)):
                    shutil.copyfile(join_path(vt_src, f), join_path(v_src, f))

            pyamff_src = join_path(vt_src, "pyamff_fortran")
            pyamff_dst = join_path(v_src, "pyamff_fortran")
            if os.path.exists(pyamff_src):
                if os.path.exists(pyamff_dst):
                    shutil.rmtree(pyamff_dst)
                shutil.copytree(pyamff_src, pyamff_dst)

            m_file = join_path(v_src, "makefile")
            filter_file(r"LIB\s*=\s*lib\s+parser", "LIB = lib parser pyamff_fortran", m_file)
            filter_file(r"dependencies:\s*sources", "dependencies: sources libs", m_file)

            objs_list = "bfgs.o dynmat.o instanton.o lbfgs.o sd.o cg.o dimer.o bbm.o fire.o lanczos.o neb.o qm.o pyamff_fortran/*.o ml_pyamff.o opt.o".split()
            objs = "".join(f"\t{x} \\\n" for x in objs_list)
            filter_file(r"^(\s*)(chain\.o)", lambda m: f"{m.group(1)}{objs}chain.o", join_path(v_src, ".objects"))

            m_path = join_path(v_src, "main.F")
            with open(m_path, "r", encoding="utf-8") as f:
                txt = f.read()

            txt = re.sub(r"IF\s*\(\s*LCHAIN\s*\)\s*CALL\s+chain_init\s*\(\s*T_INFO\s*,\s*IO\s*\)", "CALL chain_init( T_INFO, IO)", txt)
            txt = re.sub(r"(CALL\s+CHAIN_FORCE\([^&]+&\s*\r?\n\s*)(LATT_CUR%A,)", r"\1TSIF,\2", txt)

            with open(m_path, "w", encoding="utf-8") as f:
                f.write(txt)

        if "+vaspsol" in self.spec:
            tty.msg("Overwriting solvation.F with local vaspsol_solvation.F...")
            v_src = join_path(self.stage.source_path, "src")
            
            sol_src = join_path(self.package_dir, "vaspsol_solvation.F")
            sol_dst = join_path(v_src, "solvation.F")
            
            if os.path.exists(sol_src):
                shutil.copyfile(sol_src, sol_dst)

            patch_file = join_path(self.package_dir, "vaspsol_660.patch")
            if os.path.exists(patch_file):
                patch_bin = which("patch")
                with working_dir(self.stage.source_path):
                    patch_bin("-p0", "-i", patch_file)
                tty.msg("Successfully applied local VASPsol 6.6.0 patch!")
        

    def edit(self, spec, prefix):
        cpp_options = [
            "-DMPI",
            "-DMPI_BLOCK=8000",
            "-Duse_collective",
            "-DCACHE_SIZE=4000",
            "-Davoidalloc",
            "-Duse_bse_te",
            "-Dtbdyn",
            "-Dfock_dblbuf",
            "-Dvasp6",
        ]

        if spec.satisfies("+vaspsol"):
            cpp_options.append("-Dsol_compat")

        objects_lib = ["linpack_double.o"]
        llibs = list(self.compiler.stdcxx_libs)
        cflags = ["-fPIC", "-DAAD_"]
        fflags = ["-w", "-ffpe-summary=none"]
        
        if spec.satisfies("target=x86_64_v4:"):
            prec_flag = ""
            if spec.satisfies("%gcc") or spec.satisfies("%aocc"):
                prec_flag = "-ffp-contract=off"
            elif spec.satisfies("%oneapi"):
                prec_flag = "-fp-model precise"

            if prec_flag:
                tty.msg(f"x86_64_v4+ target detected. Adding precision patch ({prec_flag})")
                fflags.append(prec_flag)

        fftw_api = spec["fftw-api"]
        incs = [fftw_api.headers.include_flags]
        if fftw_api.name == "intel-oneapi-mkl":
            incs.append(f"-I{join_path(fftw_api.headers.directories[0], 'fftw')}")

        if spec["blas"].name == "intel-oneapi-mkl":
            llibs.append("-Wl,--no-as-needed")  

        llibs.extend([spec["blas"].libs.ld_flags, spec["lapack"].libs.ld_flags])

        fc = [spec["mpi"].mpifc]
        fcl = [spec["mpi"].mpifc]

        omp_flag = "-fopenmp"

        if spec.satisfies("+shmem"):
            cpp_options.extend(["-Duse_shmem", "-Dshmem_bcast_buffer", "-Dshmem_rproj"])
            objects_lib.append("getshmem.o")

        include_string = "makefile.include."

        # gcc
        if spec.satisfies("%gcc"):
            include_string += "gnu"
            if spec.satisfies("+openmp"):
                include_string += "_omp"
            make_include = join_path("arch", include_string)
        # oneapi
        elif spec.satisfies("%oneapi"):
            include_string += "oneapi"
            if spec.satisfies("+openmp"):
                include_string += "_omp"
            make_include = join_path("arch", include_string)
            filter_file("^CC_LIB[ ]{0,}=.*$", f"CC_LIB={spack_cc}", make_include)
            filter_file("^CXX_PARS[ ]{0,}=.*$", f"CXX_PARS={spack_cxx}", make_include)
        # nvhpc
        elif spec.satisfies("%nvhpc"):
            qd_root = join_path(
                spec["nvhpc"].prefix,
                f"Linux_{spec['nvhpc'].target.family.name}",
                str(spec["nvhpc"].version.dotted),
                "compilers",
                "extras",
                "qd",
            )
            nvroot = join_path(spec["nvhpc"].prefix, f"Linux_{spec['nvhpc'].target.family.name}")
            cpp_options.extend(['-DHOST=\\"LinuxNV\\"', "-Dqd_emulate"])

            fflags.extend(["-Mnoupcase", "-Mbackslash", "-Mlarge_arrays"])
            incs.append(f"-I{join_path(qd_root, 'include', 'qd')}")
            llibs.extend([f"-L{join_path(qd_root, 'lib')}", "-lqdmod", "-lqd"])

            include_string += "nvhpc"
            if spec.satisfies("+openmp"):
                include_string += "_omp"
            if spec.satisfies("+cuda"):
                include_string += "_acc"
            make_include = join_path("arch", include_string)
            omp_flag = "-mp"
            filter_file(r"^QD[ \t]*\??=.*$", f"QD = {qd_root}", make_include)
            filter_file("NVROOT[ \t]*=.*$", f"NVROOT = {nvroot}", make_include)
        # aocc
        elif spec.satisfies("%aocc"):
            cpp_options.extend(['-DHOST=\\"LinuxAMD\\"', "-Dshmem_bcast_buffer", "-DNGZhalf"])
            fflags.extend(["-fno-fortran-main", "-Mbackslash", "-ffunc-args-alias"])
            if spec.satisfies("^amdfftw@4.0:"):
                cpp_options.extend(["-Dfftw_cache_plans", "-Duse_fftw_plan_effort"])
            if spec.satisfies("+openmp"):
                if spec.satisfies("@6.3.2:"):
                    include_string += "aocc_ompi_aocl_omp"
                elif spec.satisfies("@=6.3.0"):
                    include_string += "gnu_ompi_aocl_omp"
                else:
                    include_string += "gnu_omp"
            else:
                if spec.satisfies("@6.3.2:"):
                    include_string += "aocc_ompi_aocl"
                elif spec.satisfies("@=6.3.0"):
                    include_string += "gnu_ompi_aocl"
                else:
                    include_string += "gnu"
            make_include = join_path("arch", include_string)
            filter_file("^CC_LIB[ ]{0,}=.*$", f"CC_LIB={spack_cc}", make_include)
            if spec.satisfies("@6:6.3.0"):
                filter_file("gcc", f"{spack_fc} -Mfree", make_include, string=True)
                filter_file(
                    "-fallow-argument-mismatch", " -fno-fortran-main", make_include, string=True
                )
        # fj
        elif spec.satisfies("@6.4.3: target=a64fx %fj"):
            include_string += "fujitsu_a64fx"
            omp_flag = "-Kopenmp"
            fc.extend(["simd_nouse_multiple_structures", "-X03"])
            fcl.append("simd_nouse_multiple_structures")
            cpp_options.append('-DHOST=\\"FJ-A64FX\\"')
            fflags.append("-Koptmsg=2")
            llibs.extend(["-SSL2BLAMP", "-SCALAPACK"])
            if spec.satisfies("+openmp"):
                include_string += "_omp"
            make_include = join_path("arch", include_string)

        else:
            if spec.satisfies("+openmp"):
                make_include = join_path("arch", f"{include_string}{spec.compiler.name}_omp")
                # if the above doesn't work, fallback to gnu
                if not os.path.exists(make_include):
                    make_include = join_path("arch", f"{include_string}gnu_omp")
            else:
                make_include = join_path("arch", include_string + spec.compiler.name)
                if not os.path.exists(make_include):
                    make_include = join_path("arch", f"{include_string}gnu")
            cpp_options.append('-DHOST=\\"LinuxGNU\\"')

        if spec.satisfies("+openmp"):
            cpp_options.extend(["-D_OPENMP"])
            llibs.extend(["-ldl", spec["fftw-api:openmp"].libs.ld_flags])
            fc.append(omp_flag)
            fcl.append(omp_flag)
        else:
            llibs.append(spec["fftw-api"].libs.ld_flags)

        if spec.satisfies("^scalapack"):
            cpp_options.append("-DscaLAPACK")
            if spec.satisfies("%nvhpc"):
                llibs.append("-Mscalapack")
            else:
                llibs.append(spec["scalapack"].libs.ld_flags)

        if spec.satisfies("+cuda"):
            # openacc
            if spec.satisfies("@6.5.0:"):
                cpp_options.extend(["-DACC_OFFLOAD", "-DNVCUDA", "-DUSENCCL"])
            else:
                cpp_options.extend(["-D_OPENACC", "-DUSENCCL"])
            llibs.extend(["-cudalib=cublas,cusolver,cufft,nccl", "-cuda"])
            fc.append("-acc")
            fcl.append("-acc")
            cuda_flags = [f"cuda{str(spec['cuda'].version.dotted[0:2])}", "rdc"]
            for f in spec.variants["cuda_arch"].value:
                cuda_flags.append(f"cc{f}")
            fc.append(f"-gpu={','.join(cuda_flags)}")
            fcl.append(f"-gpu={','.join(cuda_flags)}")
            fcl.extend(list(self.compiler.stdcxx_libs))
            cc = [spec["mpi"].mpicc, "-acc"]
            if spec.satisfies("+openmp"):
                cc.append(omp_flag)
            filter_file("^CC[ \t]*=.*$", f"CC = {' '.join(cc)}", make_include)

        if spec.satisfies("+hdf5"):
            cpp_options.append("-DVASP_HDF5")
            llibs.append(spec["hdf5:fortran"].libs.ld_flags)
            incs.append(spec["hdf5"].headers.include_flags)

        if spec.satisfies("+libbeef"):
            cpp_options.append("-Dlibbeef")
            llibs.append(spec["libbeef"].libs.ld_flags)

        if spec.satisfies("+libxc"):
            cpp_options.append("-DUSELIBXC")
            llibs.append(spec["libxc:fortran"].libs.ld_flags)
            incs.append(spec["libxc"].headers.include_flags)

        if spec.satisfies("+wannier90"):
            cpp_options.append("-DVASP2WANNIER90")
            llibs.append(spec["wannier90"].libs.ld_flags)

        if spec.satisfies("%gcc@10:"):
            fflags.append("-fallow-argument-mismatch")

        filter_file(r"^VASP_TARGET_CPU[ ]{0,}\?=.*", "", make_include)

        if spec.satisfies("+fftlib"):
            cxxftlib = (
                f"CXX_FFTLIB = {spack_cxx} {omp_flag}"
                f" -DFFTLIB_THREADSAFE {' '.join(list(self.compiler.stdcxx_libs))}"
            )
            filter_file("^#FCL[ ]{0,}=fftlib.o", "FCL += fftlib/fftlib.o", make_include)
            filter_file("^#CXX_FFTLIB.*$", cxxftlib, make_include)

            fftw = spec["fftw-api"]
            fftw_inc_flags = fftw.headers.include_flags
            if fftw.name == "intel-oneapi-mkl":
                fftw_inc_flags += f" -I{join_path(fftw.headers.directories[0], 'fftw')}"

            filter_file(
                "^#INCS_FFTLIB.*$",
                f"INCS_FFTLIB = -I./include {fftw_inc_flags}",
                make_include,
            )
            filter_file(r"#LIBS[ \t]*\+=.*$", "LIBS = fftlib", make_include)
            llibs.append("-ldl")
            fcl.append(join_path("fftlib", "fftlib.o"))


        if spec.satisfies("+elpa"):
            cpp_options.append("-DELPA")

            elpa_prefix = spec["elpa"].prefix
            elpa_lib = elpa_prefix.lib64 if os.path.exists(elpa_prefix.lib64) else elpa_prefix.lib
        
            lib_name = "elpa_openmp" if spec.satisfies("+openmp") else "elpa"
            llibs.extend([f"-L{elpa_lib}", f"-l{lib_name}"])
            
            incs.append(f"-I{elpa_prefix.include}")
    
            elpa_mod_dir = elpa_prefix.join(f"include/{lib_name}-{spec['elpa'].version}/modules")
            
            if os.path.exists(elpa_mod_dir):
                incs.append(f"-I{elpa_mod_dir}")
            else:
                elpa_fallback_dir = elpa_prefix.join(f"include/{lib_name}-{spec['elpa'].version}")
                if os.path.exists(elpa_fallback_dir):
                    incs.append(f"-I{elpa_fallback_dir}")

        comp_vendor = "GNU" if self.compiler.name == "gcc" else self.compiler.name.upper()

        if spec.satisfies("+dftd4") or spec.satisfies("+sdftd3"):
            llibs.append("-Wl,--start-group")
            mctc_prefix = spec["mctc-lib"].prefix
            mctc_lib = mctc_prefix.lib64 if os.path.exists(mctc_prefix.lib64) else mctc_prefix.lib
            llibs.extend([f"-L{mctc_lib}", "-lmctc-lib"])
            
            incs.append(f"-I{mctc_prefix.include}")
            
            mctc_mod_dir = mctc_prefix.join("include/mctc-lib/modules")
            if os.path.exists(mctc_mod_dir):
                incs.append(f"-I{mctc_mod_dir}")

        if spec.satisfies("+dftd4"):
            cpp_options.append("-DDFTD4")
            if spec.satisfies("^dftd4@:3.7.0"):
                cpp_options.append("-DDFTD4_API_V3")

            d4_prefix = spec["dftd4"].prefix
            d4_lib = d4_prefix.lib64 if os.path.exists(d4_prefix.lib64) else d4_prefix.lib
            llibs.extend([f"-L{d4_lib}", "-ldftd4", "-lmulticharge"])
            
            incs.append(f"-I{d4_prefix.include}")
            
            d4_mod_dir = d4_prefix.join(f"include/dftd4/{comp_vendor}-{self.compiler.version}")
            if os.path.exists(d4_mod_dir):
                incs.append(f"-I{d4_mod_dir}")

        if spec.satisfies("+sdftd3"):
            cpp_options.append("-DSDFTD3")
            d3_prefix = spec["simple-dftd3"].prefix
            d3_lib = d3_prefix.lib64 if os.path.exists(d3_prefix.lib64) else d3_prefix.lib
            llibs.extend([f"-L{d3_lib}", "-ls-dftd3"])
            
            incs.append(f"-I{d3_prefix.include}")
            
            d3_mod_dir = d3_prefix.join(f"include/s-dftd3/{comp_vendor}-{self.compiler.version}")
            if os.path.exists(d3_mod_dir):
                incs.append(f"-I{d3_mod_dir}")

        if spec.satisfies("+dftd4") or spec.satisfies("+sdftd3"):
            llibs.append("-Wl,--end-group")

        # clean multiline CPP options at begining of file
        filter_file(r"^[ \t]+(-D[a-zA-Z0-9_=]+[ ]*)+[ ]*\\*$", "", make_include)
        # replace relevant variables in the makefile.include
        filter_file("^FFLAGS[ \t]*=.*$", f"FFLAGS = {' '.join(fflags)}", make_include)
        filter_file(r"^FFLAGS[ \t]*\+=.*$", "", make_include)
        filter_file(
            "^CPP_OPTIONS[ \t]*=.*$", f"CPP_OPTIONS = {' '.join(cpp_options)}", make_include
        )
        filter_file(r"^INCS[ \t]*\+?=.*$", f"INCS = {' '.join(incs)}", make_include)
        filter_file(r"^LLIBS[ \t]*\+?=.*$", f"LLIBS = {' '.join(llibs)}", make_include)
        filter_file(r"^LLIBS[ \t]*\+=[ ]*-.*$", "", make_include)
        filter_file("^CFLAGS[ \t]*=.*$", f"CFLAGS = {' '.join(cflags)}", make_include)
        filter_file(
            "^OBJECTS_LIB[ \t]*=.*$", f"OBJECTS_LIB = {' '.join(objects_lib)}", make_include
        )
        filter_file("^FC[ \t]*=.*$", f"FC = {' '.join(fc)}", make_include)
        filter_file("^FCL[ \t]*=.*$", f"FCL = {' '.join(fcl)}", make_include)

        os.rename(make_include, "makefile.include")

    def setup_build_environment(self, env: EnvironmentModifications) -> None:
        if self.spec.satisfies("+cuda %nvhpc"):
            env.set("NVHPC_CUDA_HOME", self.spec["cuda"].prefix)

    def build(self, spec, prefix):
        make("DEPS=1, all")

    def install(self, spec, prefix):
        install_tree("bin/", prefix.bin)

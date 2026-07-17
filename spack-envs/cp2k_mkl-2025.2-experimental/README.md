# cp2k_mkl-2025.2-experimental

CP2K 2025.2 实验版本，使用 **Intel oneAPI MKL 2025.3** + OpenMPI 5 工具链，包含所有可选依赖及 DLA-Future。

> **⚠️ 实验性质**
>
> 本版本使用 Intel MKL 替代 OpenBLAS 作为 BLAS/LAPACK 提供者，并启用了 DLA-Future。在构建过程中需要对 MKL 的 BLACS 层应用补丁以修复与 OpenMPI 5 的兼容性问题（详见下方"MKL 补丁"章节）。请充分测试后再用于生产环境。

## 镜像内容

### 核心包

| 包 | 版本 | 说明 |
|----|------|------|
| CP2K | 2025.2 | 主程序，所有 variant 均开启（含 `+dlaf`） |
| GCC | 14.2.0（外部） | 编译器 |
| OpenMPI | 5.0.8 | MPI 实现 |
| Intel oneAPI MKL | 2025.3.0 | BLAS/LAPACK/FFT/ScaLAPACK（`+shared +cluster +gfortran ~ilp64 threads=openmp mpi_family=openmpi`） |

### 全部依赖列表

```
deepmdkit@3.1.0       dftd4@3.7.0           elpa@2025.01.001
fftw@3.3.10           greenx@2.2            hdf5@1.14.6
lammps-user-pace      libint@2.9.0          libsmeagol@1.2
libvori@220621        libxc@7.0.0           libxsmm@1.17
pexsi@2.0.0           plumed@2.9.2          py-torch@2.7
sirius@7.9.0          spglib@2.5.0          spla@1.6.1
trexio@2.5.0          tblite@0.4.0          dbcsr@2.8.0
dla-future@0.10.0     dla-future-fortran@0.5.0
```

### CP2K 编译 Variant

```
+ace +deepmd +dftd4 +dlaf +elpa +greenx +grpp +hdf5 +libint +libvori
+libxc +mpi_f08 +pexsi +plumed +pytorch +sirius +smeagol +spglib +trexio
+vcsqnm +vdwxc ~cosma ~cuda ~rocm
smm=libxsmm lmax=6
```

### 与 `cp2k_opensource-2025.2` 的主要区别

| 项目 | opensource 版 | mkl-experimental 版 |
|------|--------------|-------------------|
| BLAS/LAPACK | OpenBLAS 0.3.29 | **Intel oneAPI MKL 2025.3.0** |
| DLA-Future | `~dlaf`（未启用） | **`+dlaf`（已启用）** |
| COSMA | `+cosma` | `~cosma`（未启用） |
| ELPA | `+openmp` | `+openmp +force_all_x86_kernel` |
| FFTW | `+openmp` | `+openmp +force_avx512` |
| MKL 补丁 | 无 | **需要**（修复 MKL BLACS 与 OpenMPI 5 的兼容性） |
| libfakeintel | 无 | **包含**（AMD CPU 上启用 MKL 优化内核） |

## MKL 专项修复

### MKL BLACS + OpenMPI 5 兼容性补丁

Intel MKL 2025.x 自带的 `libmkl_blacs_openmpi_lp64.so` 内置了一个 MKLMPI 垫片层，假定 MPI 句柄（`MPI_Comm`、`MPI_Group`、`MPI_Request`）为整数类型——这对 MPICH 系 MPI 成立，但 OpenMPI ≥ 4.x 中这些句柄是不透明指针。与 OpenMPI 5.x 配合使用时，会导致 `Cblacs_gridmap` 初始化阶段发生 **SIGSEGV**。

```
#0  PMPI_Group_c2f ()          libmpi.so.40
#1  MKLMPI_Comm_group ()       libmkl_blacs_openmpi_lp64.so.2
#2  Cblacs_gridmap ()          libmkl_blacs_openmpi_lp64.so.2
#3  BLACS_grid::BLACS_grid()   libsirius_cxx.so
```

**修复方式**：`mkl-patch/` 目录中的补丁将 MKLMPI 的 `X2COMM`/`X4COMM` 宏替换为正确的 `MPI_Comm_f2c`/`MPI_Comm_c2f` 调用，并重新构建 `libmkl_blacs_openmpi_lp64.so.2`。

### libfakeintel — AMD CPU 优化

MKL 通过 CPU 供应商检测决定使用哪条代码路径。在 AMD CPU 上，MKL 默认使用较慢的 SSE/AVX 代码路径，即使 CPU 支持 AVX2/AVX512。

`libfakeintel/` 提供了一个小型共享库，覆盖 `mkl_serv_intel_cpu_true()` 使其始终返回 1，欺骗 MKL 使用优化的 AVX2/AVX512 内核。在 AMD CPU 上通过 `LD_PRELOAD` 加载即可。

### 构建时 MKL 瘦身

构建过程中会清理 MKL 中不需要的组件以减小镜像体积：

- 移除 `compiler/` 目录（使用外部 GCC）
- 移除 `mpi/` 目录（使用 OpenMPI，不用 IntelMPI）
- 删除 `libmkl_blacs_intelmpi*`（IntelMPI BLACS）
- 删除 `libmkl_blacs_sgimpt*`（SGI BLACS）
- 删除 `libmkl_intel_thread*`（使用 GNU 线程层 `libmkl_gnu_thread`）

## SIMD 内核支持

本版本 ELPA 启用了 `+force_all_x86_kernel`，FFTW 启用了 `+force_avx512`，在 AVX2 构建机器上即可编译出 AVX512 内核：

| 内核类型 | ELPA | FFTW |
|---------|------|------|
| SSE | ✅ | ✅ |
| AVX | ✅ | ✅ |
| AVX2 | ✅ | ✅ |
| AVX512 | ✅（force_all_x86_kernel） | ✅（force_avx512） |

## 自定义 Spack 包

```
repos/packages/
├── cp2k/        # CP2K package.py（从 cp2k_dev_repo 基础上微调）
├── deepmdkit/   # 自定义 deepmdkit 包
├── dla-future/  # 自定义 DLA-Future 包（含 +ompi_grid_fix）
├── elpa/        # 自定义 ELPA 包（+force_all_x86_kernel variant）
├── fftw/        # 自定义 FFTW 包（+force_avx512 variant）
└── py-torch/    # 自定义 PyTorch 包
```

## 构建配置

- **基础镜像**：`debian:trixie`（构建）/ `debian:trixie-slim`（运行时）
- **Spack 目标架构**：`x86_64_v3`（支持 AVX2+）
- **MKL 线程层**：GNU OpenMP（`libmkl_gnu_thread`）
- **BLAS/LAPACK 提供者**：`intel-oneapi-mkl`

## 目录结构

```
cp2k_mkl-2025.2-experimental/
├── Dockerfile.j2                      # Dockerfile Jinja2 模板
├── cp2k.def.j2                        # Apptainer 定义文件模板
├── MPIRUN_GUIDE.md                    # MPI 运行指南
├── cp2k-mkl-experimental-motd.sh      # 容器 MOTD 脚本
├── libfakeintel/                      # AMD CPU MKL 优化库
│   ├── fakeintel.c
│   ├── Makefile
│   └── README.md
├── mkl-patch/                         # MKL BLACS 补丁
│   ├── mkl-compactable-layer-builder.sh
│   ├── mklmpi-impl-fix_v2.patch
│   └── README
└── spack-env-file/
    ├── spack.yaml                     # Spack 环境定义
    ├── env.yaml                       # 权威构建元配置（镜像、系统包等）
    ├── repos/                         # 自定义 Spack 包
    │   └── packages/
    │       ├── cp2k/
    │       ├── deepmdkit/
    │       ├── dla-future/
    │       ├── elpa/
    │       ├── fftw/
    │       └── py-torch/
    └── spack.lock                     # 锁定的依赖版本
```

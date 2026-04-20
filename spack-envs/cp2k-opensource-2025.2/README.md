# cp2k-opensource-2025.2

CP2K 2025.2 开源版本（纯 CPU），使用 OpenBLAS + OpenMPI 工具链，包含所有可选依赖。

## 镜像内容

### 核心包

| 包 | 版本 | 说明 |
|----|------|------|
| CP2K | 2025.2 | 主程序，所有 variant 均开启 |
| GCC | 14.2.0（外部） | 编译器 |
| OpenMPI | 5.0.8 | MPI 实现 |
| OpenBLAS | 0.3.29 | BLAS/LAPACK（`+dynamic_dispatch +fortran threads=openmp`） |

### 全部依赖列表

```
cosma@2.7.0          deepmdkit@3.1.0       dftd4@3.7.0
elpa@2025.01.001     fftw@3.3.10           greenx@2.2
hdf5@1.14.6          lammps-user-pace      libint@2.9.0
libsmeagol@1.2       libvori@220621        libxc@7.0.0
libxsmm@1.17         pexsi@2.0.0           plumed@2.9.2
py-torch@2.7         sirius@7.9.0          spglib@2.5.0
spla@1.6.1           trexio@2.5.0          tblite@0.4.0
dbcsr@2.8.0          netlib-scalapack@2.2.2
```

### CP2K 编译 Variant

```
+ace +cosma +deepmd +dftd4 +elpa +greenx +grpp +hdf5 +libint +libvori
+libxc +mpi_f08 +pexsi +plumed +pytorch +sirius +smeagol +spglib +trexio
+vcsqnm +vdwxc ~cuda ~dlaf ~rocm
smm=libxsmm lmax=6
```

### 不包含的依赖

| 包 | 原因 |
|----|------|
| DLA-Future (`~dlaf`) | 导致 CP2K regtest 出错；纯 CPU 版本中 ELPA 已足够，DLA-Future 主要为 GPU 版本设计 |

### SIMD 内核支持

本版本默认 `target=x86_64_v3`（AVX2），ELPA 和 FFTW 最高仅启用 AVX2 内核：

| 内核类型 | 状态 |
|---------|------|
| SSE | ✅ 始终启用 |
| AVX | ✅ 始终启用 |
| AVX2 | ✅ 默认启用 |
| AVX512 | ❌ **不包含** |

> **如果在 AVX512 机器上构建，想获得原生 AVX512 支持**（推荐方式，循环展开更充分，性能优于 workaround）：
>
> 1. 修改 `spack.yaml`，将 `target="x86_64_v3"` 改为 `target="x86_64_v4"`
> 2. 删除 `spack.lock`：`rm spack-env-file/spack.lock`
> 3. 重新生成 assets：`python generate.py assets --env cp2k-opensource-2025.2`
> 4. 重新构建容器
>
> 这样 ELPA 和 FFTW 会自动检测并编译 AVX512 内核，无需额外补丁。
>
> 如果构建机器没有 AVX512，但仍需部署到 AVX512 目标，请使用 [`cp2k-opensource-2025.2-force-avx512`](../cp2k-opensource-2025.2-force-avx512/)。

## 自定义 Spack 包

```
repos/packages/
└── cp2k/     # CP2K package.py（从 cp2k_dev_repo 基础上微调）
```

## 构建配置

- **基础镜像**：`debian:trixie`（构建）/ `debian:trixie-slim`（运行时）
- **Spack 目标架构**：`x86_64_v3`（支持 AV2+）
- **OpenBLAS**：启用 `+dynamic_dispatch`（运行时可选择最优内核）

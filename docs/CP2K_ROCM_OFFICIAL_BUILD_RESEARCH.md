# CP2K 官方 ROCm/HIP 构建流程研究

> 研究日期：2026-04-12
> 目标：了解 CP2K 上游如何构建 ROCm GPU 加速版本，为 MI300X 构建提供参考

---

## 1. 概览

CP2K 的 ROCm 构建采用 **toolchain 模式**（非 spack），通过 `install_cp2k_toolchain.sh` 脚本统一管理所有依赖的编译。HIP 构建的核心依赖库（DBCSR、COSMA、SPLA、SpFFT）都需要单独构建 HIP 版本。

### 构建命令速查

```bash
# Toolchain 方式（官方推荐）
./install_cp2k_toolchain.sh \
    --with-libxsmm=install \
    --with-openblas=system \
    --with-fftw=system \
    --enable-hip \
    --gpu-ver=Mi300
```

```bash
# CMake 方式（toolchain 完成后）
cmake -GNinja \
    -DCP2K_USE_ACCEL=HIP \
    -DCP2K_WITH_GPU=Mi300 \
    -DCP2K_USE_MPI=ON \
    -DCP2K_USE_LIBXC=ON \
    -DCP2K_USE_LIBINT2=ON \
    ..
```

---

## 2. 官方 Docker 构建

### 2.1 Docker 基础镜像配置

**文件**: `tools/docker/generate_dockerfiles.py` (L550-575)

```python
def install_deps_toolchain_hip_rocm(gpu_ver: str) -> str:
    return rf"""
FROM rocm/dev-ubuntu-24.04:7.2-complete

# Install some Ubuntu packages.
RUN apt-get update -qq && apt-get install -qq --no-install-recommends \
    hipblas                                                           \
    gfortran                                                          \
    mpich                                                             \
    libmpich-dev                                                      \
   && rm -rf /var/lib/apt/lists/*

# Remove LTO from Ubuntu's MPICH
RUN sed -i -e 's/-flto=auto//g' -e 's/-ffat-lto-objects//g' \
    /usr/lib/x86_64-linux-gnu/pkgconfig/mpich.pc \
    /usr/bin/*.mpich

# Setup HIP environment.
ENV ROCM_PATH /opt/rocm
ENV PATH ${{PATH}}:${{ROCM_PATH}}/bin
ENV LD_LIBRARY_PATH ${{LD_LIBRARY_PATH}}:${{ROCM_PATH}}/lib
ENV HIP_PLATFORM amd
RUN hipconfig

""" + install_toolchain(
        base_image="ubuntu",
        mpi_mode="mpich",
        enable_hip="yes",
        gpu_ver=gpu_ver,
        with_dbcsr="",
    )
```

**关键点**:

| 项目 | 值 | 说明 |
|------|-----|------|
| 基础镜像 | `rocm/dev-ubuntu-24.04:7.2-complete` | 含完整 ROCm SDK |
| 额外 apt 包 | `hipblas`, `gfortran`, `mpich`, `libmpich-dev` | hipfft 已在 ROCm complete 中 |
| MPI | **mpich**（非 openmpi） | Ubuntu 系统包 |
| LTO 修复 | 移除 mpich 的 `-flto=auto` 和 `-ffat-lto-objects` | 防止编译错误 |
| 环境变量 | `ROCM_PATH=/opt/rocm`, `HIP_PLATFORM=amd` | ROCm 运行环境 |

### 2.2 CI 生成的 Dockerfile

**文件**: `tools/docker/generate_dockerfiles.py` (L205-220)

```python
for gpu_ver in "Mi50", "Mi100":
    with OutputFile(f"Dockerfile.build_hip_rocm_{gpu_ver}", args.check) as f:
        f.write(install_deps_toolchain_hip_rocm(gpu_ver=gpu_ver))
        f.write(test_build(f"toolchain_hip_{gpu_ver}", "psmp"))
```

生成两个 Dockerfile：
- `Dockerfile.build_hip_rocm_Mi50`
- `Dockerfile.build_hip_rocm_Mi100`

> ⚠️ **Mi300 不在 CI 测试范围内**，但架构映射已支持。

---

## 3. CMake 配置

### 3.1 cmake_cp2k.sh — HIP Profile

**文件**: `cmake/cmake_cp2k.sh` (L178-198)

```bash
elif [[ "${PROFILE}" == "toolchain_hip_"* ]] && [[ "${VERSION}" == "psmp" ]]; then
  cmake \
    -GNinja \
    -DCMAKE_INSTALL_PREFIX="${INSTALL_PREFIX}" \
    -DDBCSR_DIR="${DBCSR_HIP_ROOT}/lib/cmake/dbcsr" \
    -DCP2K_WITH_GPU="${PROFILE:14}" \
    -DCP2K_USE_ACCEL=HIP \
    -DCP2K_USE_MPI=ON \
    -DCP2K_USE_LIBXC=ON \
    -DCP2K_USE_LIBINT2=ON \
    .. |& tee ./cmake.log
  CMAKE_EXIT_CODE=$?
```

Profile 命名规则：`toolchain_hip_<GPU型号>`，如 `toolchain_hip_Mi300`。
`${PROFILE:14}` 截取 GPU 型号字符串。

### 3.2 CMakeLists.txt — HIP 检测与编译标志

**文件**: `CMakeLists.txt` (L574-598)

```cmake
elseif(CP2K_USE_ACCEL MATCHES "HIP")
  message("\n------------------------------------------------------------")
  message("-                          HIP                             -")
  message("------------------------------------------------------------\n")
  message(INFO "${CMAKE_HIP_ARCHITECTURES}")
  enable_language(HIP)

  if(CMAKE_HIP_PLATFORM MATCHES "nvidia")
    find_package(CUDAToolkit)
  endif()

  # 优化标志
  if(NOT CMAKE_BUILD_TYPE AND (CMAKE_HIP_PLATFORM MATCHES "amd"))
    set(CMAKE_HIP_FLAGS "-O3")
  elseif(CMAKE_BUILD_TYPE STREQUAL "RelWithDebInfo")
    set(CMAKE_HIP_FLAGS "-O2 -g")
  elseif(CMAKE_BUILD_TYPE STREQUAL "Release")
    set(CMAKE_HIP_FLAGS "-O3")
  elseif(CMAKE_BUILD_TYPE STREQUAL "Debug")
    set(CMAKE_HIP_FLAGS "-O0 -g")
  endif()

  # 必需的 HIP 库
  find_package(hipfft REQUIRED IMPORTED CONFIG)
  find_package(hipblas REQUIRED IMPORTED CONFIG)

  set(CP2K_USE_HIP ON)

  # AMD 硬件原子操作（Mi250X 及以上）
  if(NOT CMAKE_HIP_PLATFORM OR (CMAKE_HIP_PLATFORM MATCHES "amd"))
    set(CMAKE_HIP_FLAGS "${CMAKE_HIP_FLAGS} -munsafe-fp-atomics")
  endif()
```

**关键依赖库**:
- `hipfft` — **REQUIRED**，HIP FFT 库
- `hipblas` — **REQUIRED**，HIP BLAS 库
- `-munsafe-fp-atomics` — AMD 平台专用标志，启用硬件原子操作

### 3.3 HIP 源文件

**文件**: `src/CMakeLists.txt` (L1498-1501)

```
grid/hip/grid_hip_collocate.cu
grid/hip/grid_hip_integrate.cu
grid/hip/grid_hip_context.cu
```

---

## 4. GPU 架构映射

### 4.1 支持的 GPU 列表

**文件**: `CMakeLists.txt` (L451-470)

| GPU | ISA | CP2K 变量 | CI 测试 |
|-----|-----|-----------|---------|
| Mi50 | gfx906 | `CP2K_GPU_ARCH_NUMBER_Mi50` | ✅ |
| Mi100 | gfx908 | `CP2K_GPU_ARCH_NUMBER_Mi100` | ✅ |
| Mi200 | gfx90a | `CP2K_GPU_ARCH_NUMBER_Mi200` | ❌ |
| Mi250 | gfx90a | `CP2K_GPU_ARCH_NUMBER_Mi250` | ❌ |
| **Mi300(A,X)** | **gfx942** | `CP2K_GPU_ARCH_NUMBER_Mi300` | ❌ |

**文件**: `CMakeLists.txt` (L278) — 支持的 HIP 架构列表

```cmake
set(CP2K_SUPPORTED_HIP_ARCHITECTURES "Mi50;Mi60;Mi100;Mi250;Mi300")
```

### 4.2 架构选择逻辑

CP2K 通过 `-DCP2K_WITH_GPU=Mi300` 指定 GPU 型号，CMakeLists.txt 内部映射到对应的 ISA（gfx942），再传递给 HIP 编译器。

---

## 5. 依赖库的 HIP 构建

这是 ROCm 构建最关键的部分。以下库需要单独构建 HIP 版本：

### 5.1 DBCSR（稀疏矩阵库）

**文件**: `tools/toolchain/scripts/stage9/install_dbcsr.sh` (L72-86)

```bash
if [ "${ENABLE_HIP}" == "__TRUE__" ]; then
  mkdir build-hip
  cd build-hip
  CMAKE_OPTIONS="${CMAKE_OPTIONS} -DUSE_ACCEL=hip -DWITH_GPU=Mi250"
  cmake \
    -DCMAKE_INSTALL_PREFIX=${pkg_install_dir}-hip \
    ${CMAKE_OPTIONS} .. \
    > cmake.log 2>&1 || tail_excerpt cmake.log
  make -j $(get_nprocs) > make.log 2>&1 || tail_excerpt make.log
  make -j $(get_nprocs) install > install.log 2>&1 || tail_excerpt install.log
  cd ..
fi
```

| 项目 | 值 |
|------|-----|
| 构建目录 | `build-hip/`（独立于 CPU 构建） |
| 安装目录 | `${pkg_install_dir}-hip` |
| CMake 标志 | `-DUSE_ACCEL=hip -DWITH_GPU=Mi250` |

### 5.2 COSMA（通信优化矩阵乘法）

**文件**: `tools/toolchain/scripts/stage4/install_cosma.sh` (L180-200)

```bash
if [ "$ENABLE_HIP" = "__TRUE__" ] && $(check_lib -lrocblas "rocm" &> /dev/null); then
  mkdir build-hip
  cd build-hip
  cmake \
    -DCOSMA_BLAS=ROCM \
    -DCOSMA_SCALAPACK=${cosma_sl} \
    -DCOSMA_WITH_TESTS=NO \
    -DCOSMA_WITH_APPS=NO \
    -DCOSMA_WITH_BENCHMARKS=NO ..
```

| 项目 | 值 |
|------|-----|
| 前置条件 | 检查 `librocblas` 是否可用 |
| CMake 标志 | `-DCOSMA_BLAS=ROCM` |

### 5.3 SPLA（专用并行线性代数）

**文件**: `tools/toolchain/scripts/stage8/install_spla.sh` (L83-104)

```bash
case "${GPUVER}" in
  Mi50 | Mi100 | Mi200 | Mi250)
    mkdir build-hip
    cd build-hip
    cmake \
      -DSPLA_FORTRAN=ON \
      -DSPLA_INSTALL=ON \
      -DSPLA_STATIC=ON \
      -DSPLA_GPU_BACKEND=ROCM \
      ..
```

| 项目 | 值 |
|------|-----|
| 支持的 GPU | Mi50, Mi100, Mi200, Mi250 |
| CMake 标志 | `-DSPLA_GPU_BACKEND=ROCM` |

### 5.4 SpFFT（稀疏 FFT）

**文件**: `tools/toolchain/scripts/stage8/install_spfft.sh` (L112-130)

与 SPLA 类似的构建模式，也支持 HIP backend。

### 5.5 依赖库 HIP 构建汇总

| 库 | HIP 标志 | 检查条件 | 构建阶段 |
|----|----------|----------|----------|
| DBCSR | `-DUSE_ACCEL=hip -DWITH_GPU=Mi250` | `ENABLE_HIP == __TRUE__` | Stage 9 |
| COSMA | `-DCOSMA_BLAS=ROCM` | `ENABLE_HIP` + `librocblas` | Stage 4 |
| SPLA | `-DSPLA_GPU_BACKEND=ROCM` | GPUVER in Mi50/Mi100/Mi200/Mi250 | Stage 8 |
| SpFFT | HIP backend | GPUVER in Mi50/Mi100/Mi200/Mi250 | Stage 8 |

> ⚠️ **注意**：SPLA 和 SpFFT 的 case 语句只列出了 Mi50-Mi250，**Mi300 未列出**。
> 这可能意味着需要 patch 脚本以添加 Mi300 支持，或者这些库已通过其他方式兼容 gfx942。

---

## 6. 官方文档

**文件**: `docs/technologies/accelerators/hip.md`

```markdown
# HIP / ROCm

- Use `-DCP2K_USE_ACCEL=HIP` to generally enable support for AMD GPUs
- Use `-DCP2K_ENABLE_GRID_GPU=OFF` to disable the GPU backend of the grid library.
- Use `-DCP2K_ENABLE_DBM_GPU=OFF` to disable the GPU backend of the sparse tensor library.
- Use `-DCP2K_ENABLE_PW_GPU=OFF` to disable the GPU backend of FFTs and gather/scatter operations.
- Use `-DCP2K_DBCSR_USE_CPU_ONLY=ON` to disable the GPU backend of DBCSR.
- Add `-DCP2K_USE_UNIFIED_MEMORY=ON` to enable unified memory support
  (experimental, only supports Mi250X and above)
- Add `-DCP2K_WITH_GPU=Mi50, Mi60, Mi100, Mi250, Mi300`.
  Architectures supported: Mi300(A,X), Mi300(gfx942), Mi250(gfx90a), Mi100(gfx908), Mi50(gfx906)
```

### 可单独开关的 GPU 子模块

| CMake 选项 | 控制的模块 | 默认 |
|-------------|-----------|------|
| `CP2K_ENABLE_GRID_GPU` | Grid 库 GPU 后端 | ON |
| `CP2K_ENABLE_DBM_GPU` | 稀疏张量 GPU 后端 | ON |
| `CP2K_ENABLE_PW_GPU` | FFT 和 gather/scatter GPU 后端 | ON |
| `CP2K_DBCSR_USE_CPU_ONLY` | DBCSR 使用 CPU | OFF |
| `CP2K_USE_UNIFIED_MEMORY` | 统一内存（实验性） | OFF |

---

## 7. 构建方案对比

### 方案 A：CP2K Toolchain（官方方式）

```
rocm/dev-ubuntu-24.04:7.2.1-complete
  → install_cp2k_toolchain.sh --enable-hip --gpu-ver=Mi300
  → cmake -DCP2K_USE_ACCEL=HIP -DCP2K_WITH_GPU=Mi300
  → ninja install
```

**优点**：
- 与官方 CI 一致，最可靠
- 所有依赖的 HIP 构建已内置处理
- 出问题时可直接参考官方 CI 日志

**缺点**：
- 构建时间长（toolchain 编译所有依赖）
- 无法复用 spack 缓存
- 需要处理 Mi300 未在 SPLA/SpFFT case 语句中的问题

### 方案 B：Spack + 手动 HIP 标志

```
rocm/dev-ubuntu-24.04:7.2.1-complete
  → spack install（CPU 依赖）
  → 手动构建 HIP 版本的 DBCSR/COSMA/SPLA/SpFFT
  → cmake -DCP2K_USE_ACCEL=HIP -DCP2K_WITH_GPU=Mi300
  → ninja install
```

**优点**：
- 与现有构建系统一致
- 可复用 spack 缓存，部分依赖不需要重编

**缺点**：
- 需要确保 spack 包支持 HIP 变体
- COSMA/SPLA/SpFFT 的 spack 包可能不支持 ROCm 后端
- 需要额外维护 HIP 构建逻辑

### 方案 C：混合（Toolchain for HIP libs + Spack for CPU deps）

```
rocm/dev-ubuntu-24.04:7.2.1-complete
  → spack install（CPU 依赖：libxc, libint, elpa, fftw...）
  → toolchain 单独构建 HIP 版 DBCSR/COSMA/SPLA/SpFFT
  → cmake -DCP2K_USE_ACCEL=HIP -DCP2K_WITH_GPU=Mi300
  → ninja install
```

**优点**：
- CPU 依赖复用 spack 缓存
- HIP 库用官方 toolchain 脚本，最可靠

**缺点**：
- 需要协调两套构建系统
- 复杂度最高

### 推荐

**先采用方案 A（Toolchain）**，确保能在 Mi300X 上成功构建。验证通过后再考虑是否迁移到方案 B 或 C 以优化构建速度。

---

## 8. Docker 多阶段构建参考

基于官方模式，推荐的 Docker 构建结构：

### Stage 1: Builder

```dockerfile
FROM rocm/dev-ubuntu-24.04:7.2.1-complete AS builder

# ROCm 环境
ENV ROCM_PATH=/opt/rocm
ENV HIP_PLATFORM=amd

# 安装构建依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    hipblas gfortran mpich libmpich-dev && rm -rf /var/lib/apt/lists/*

# 修复 MPICH LTO
RUN sed -i -e 's/-flto=auto//g' -e 's/-ffat-lto-objects//g' \
    /usr/lib/x86_64-linux-gnu/pkgconfig/mpich.pc /usr/bin/*.mpich

# 构建 toolchain + CP2K
# ... (install_cp2k_toolchain.sh --enable-hip --gpu-ver=Mi300)
# ... (cmake -DCP2K_USE_ACCEL=HIP -DCP2K_WITH_GPU=Mi300 ...)
```

### Stage 2: Runtime

```dockerfile
FROM rocm/dev-ubuntu-24.04:7.2.1 AS runtime

# 仅 copy CP2K 安装产物
COPY --from=builder /opt/cp2k /opt/cp2k

# 最小运行时依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates libgomp1 libmpich12 && rm -rf /var/lib/apt/lists/*
```

---

## 9. 待确认事项

1. **Mi300 在 SPLA/SpFFT 中的支持** — `install_spla.sh` 和 `install_spfft.sh` 的 case 语句只列出 Mi50-Mi250，需要确认 Mi300 是否已通过其他方式支持，或需要手动 patch
2. **MPICH vs OpenMPI** — 官方 ROCm 构建使用 mpich，我们 CPU 版使用 openmpi，是否保持一致？
3. **ROCm 7.2 vs 7.2.1** — 官方 CI 用 7.2，我们计划用 7.2.1，需确认兼容性
4. **DBCSR GPU 型号** — `install_dbcsr.sh` 硬编码 `Mi250`，Mi300 是否需要修改？
5. **HotAisle 宿主机内核驱动** — 需要 ROCm kernel driver ≥ 7.2 以支持 gfx942

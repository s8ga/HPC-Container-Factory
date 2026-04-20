# cp2k-opensource-2025.2-force-avx512

基于 `cp2k-opensource-2025.2` 的 AVX512 增强版本。在 AVX2 构建机器上强制编译 AVX512 内核，使容器可以在 AVX512 目标机器上自动获得更高的数值计算性能。

> **⚠️ 这是 workaround，不是最终方案**
>
> 本版本通过 per-object CFLAGS 仅为 ELPA/FFTW 的 AVX512 内核单独添加 `-mavx512f` 编译标志，而主库代码仍以 AVX2 为目标编译。这意味着循环展开、向量化等优化仅限于 AVX512 内核内部，无法惠及整个库。
>
> **真正的解决方案**是在 AVX512 机器上进行纯 AVX512 构建（`target=x86_64_v4`），让编译器对全部代码进行 AVX512 级别的优化——循环展开更充分、向量化更彻底，整体性能会显著优于本 workaround。
>
> 本版本仅适用于：无法获得 AVX512 构建机器，但部署目标是 AVX512 的场景。
>
> **优化范围**：本版本仅针对 **Diag（对角化）算法路径** 中的 ELPA 和 FFTW 进行了 AVX512 优化。CP2K 的 **OT（Orbital Transformation）** 算法路径暂未做专门的 AVX512 优化。如果你的主要工作负载以 OT 为主，此版本带来的性能提升有限。

## 与 `cp2k-opensource-2025.2` 的区别

### spack.yaml 差异

仅两处改动，其余完全相同：

| 包 | 普通版 | force-avx512 版 |
|----|--------|-----------------|
| `elpa` | `+openmp` | `+openmp +force_all_x86_kernel` |
| `fftw` | `+openmp` | `+openmp +force_avx512` |

CP2K 自身及所有其他依赖的版本、variant 完全一致（均不含 DLA-Future，因为纯 CPU 版本中 ELPA 已足够且 DLA-Future 会导致 regtest 出错）。

### 自定义 Spack 包 (local repos)

在普通版仅有 `cp2k` 自定义包的基础上，新增了两个自定义包：

```
repos/packages/
├── cp2k/          # 与普通版相同
├── elpa/          # 新增：自定义 ELPA 包，增加 +force_all_x86_kernel variant
│   ├── package.py
│   ├── force_all_x86_kernel.patch          # 补丁 configure.ac + Makefile.am
│   ├── force_avx512_configure.patch        # 补丁预生成 configure 脚本
│   └── force_avx512_makefile_in.patch      # 补丁预生成 Makefile.in
└── fftw/          # 新增：自定义 FFTW 包，增加 +force_avx512 variant
    └── package.py
```

### 运行时效果对比

**普通版** (`cp2k-opensource-2025.2`)：

```
ELPA 可用内核：SSE, AVX, AVX2（无 AVX512）
FFTW AVX512 symbols：0
```

**AVX512 版** (`cp2k-opensource-2025.2-force-avx512`)：

```
ELPA 可用内核：SSE, AVX, AVX2, AVX512（全部可用）
FFTW AVX512 symbols：大量（所有 AVX512 codelets）
```

部署到 AVX512 目标机器时，ELPA 和 FFTW 会通过运行时检测自动选择 AVX512 内核路径，无需任何额外配置。

## 适用场景

- ✅ 构建机器只有 AVX2，但部署目标有 AVX512（如 Ice Lake、Zen 4 等），且工作负载以 **Diag（对角化）** 算法为主
- ✅ 希望一个镜像同时兼容 AVX2 和 AVX512 机器（ELPA/FFTW 运行时自动选择最优内核）
- ⚠️ **不建议**在 AVX512 机器上直接构建时使用此版本——应直接用 `target=x86_64_v4` 构建纯 AVX512 版本，性能更优
- ⚠️ 以 **OT（Orbital Transformation）** 为主的工作负载不建议使用此版本（AVX512 优化未覆盖 OT 路径 / 未测试）

## 技术细节

ELPA 的 `+force_all_x86_kernel` 通过三补丁策略实现：

1. **`force_all_x86_kernel.patch`** — 修改 `configure.ac`（AVX512 编译测试从硬错误改为警告）和 `Makefile.am`（添加 per-object CFLAGS）
2. **`force_avx512_configure.patch`** — 直接修改预生成 `configure` 脚本（绕过 Spack autoreconf 跳过问题）
3. **`force_avx512_makefile_in.patch`** — 直接修改预生成 `Makefile.in`（添加 per-object `AM_CFLAGS += $(AVX512_CFLAGS)`）

三个补丁互不冲突，确保无论 Spack 的 `autoreconf` 阶段是否重新生成构建文件都能正常工作。

详细设计文档见 `docs/force_all_x86_kernel/`。

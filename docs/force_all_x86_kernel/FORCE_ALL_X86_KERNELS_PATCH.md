# AVX512 按需编译方案：ELPA & FFTW

## 一句话总结

在 AVX2 机器上编译 ELPA 和 FFTW 时，强制编译 AVX512 kernel（per-object `-mavx512f`），产出的库在 AVX2 机上仍可正常加载运行，部署到 AVX512 服务器后自动选择 AVX512 路径获得加速。

- **ELPA**：需要源码 patch（3 个阻断点需逐一解决）
- **FFTW**：天然支持，只需修改 Spack `package.py` 加一个 variant

---

## 目录

**Part I — ELPA**

1. [ELPA：目标与约束](#elpa目标与约束)
2. [ELPA：背景 — 为什么需要 patch](#elpa背景--为什么需要-patch)
3. [ELPA：技术方案](#elpa技术方案)
4. [ELPA：Patch 详细说明](#elpapatch-详细说明)
5. [ELPA：Spack 集成](#elpaspack-集成)
6. [ELPA：跨版本兼容性](#elpa跨版本兼容性)
7. [ELPA：使用方法](#elpa使用方法)
8. [ELPA：风险与注意事项](#elpa风险与注意事项)

**Part II — FFTW**

9. [FFTW：为什么不需要 patch](#fftw为什么不需要-patch)
10. [FFTW：AVX512 架构分析](#fftwavx512-架构分析)
11. [FFTW：运行时 dispatch 机制](#fftw运行时-dispatch-机制)
12. [FFTW：Spack 集成](#fftwsack-集成)

---

---

# Part I — ELPA

## ELPA：目标与约束

**目标**：在 AVX2（或更低）CPU 的构建机器上，编译包含 AVX512 kernel 的 ELPA 库，使该库可以被部署到 AVX512 服务器上，由 ELPA 的 autotune 机制在运行时自动选择最优 kernel。

**关键约束**：
- **主库代码**按构建机的默认 flags（如 AVX2）编译 → 库在 AVX2 机器上可正常加载运行
- **AVX512 kernel 文件**单独加 `-mavx512f` 编译 → 不影响全局 CFLAGS
- **不修改 Spack target**：不使用 `target=x86_64:v4`，否则全局代码要求 AVX512，在 AVX2 机器上完全无法运行
- **依赖 GCC 的 per-object CFLAGS 能力**：GNU Make 的 target-specific variable 语法（`file.lo: AM_CFLAGS += ...`）

**使用场景**：CP2K 等应用使用 ELPA 进行大规模对角化计算，ELPA autotune 在运行时检测 CPU 类型并选择最优 kernel。构建在 AVX2 CI/开发机上，部署到 AVX512 计算节点。

---

## ELPA：背景 — 为什么需要 patch

### ELPA 的 kernel 选择控制链

```
Spack spec.target
  ↓  (package.py configure_args)
--enable-avx512-kernels / --disable-avx512-kernels
  ↓  (configure.ac ELPA_SELECT_KERNELS 宏)
use_real_avx512_block2=yes/no, ...
  ↓  (configure.ac need_* 聚合)
need_avx512=yes/no
  ↓  (configure.ac AC_COMPILE_IFELSE 编译测试)
can_compile_avx512=yes/no
  ↓  失败则 AC_MSG_ERROR 终止 configure
  ↓  通过则 AC_DEFINE HAVE_AVX512
  ↓  (Makefile.am 条件编译)
if WITH_REAL_AVX512_BLOCK2_KERNEL → 编译 .c 文件到 libelpa.la
  ↓  使用全局 CFLAGS（无 -mavx512f）
编译失败：_mm512_fmadd_pd 等内联需要 -mavx512f
```

### AVX2 机器上的 3 个阻断点

| # | 阻断点 | 位置 | 原因 |
|---|--------|------|------|
| 1 | Spack 传 `--disable-avx512-kernels` | `package.py` | `avx512 not in spec.target` |
| 2 | configure AVX512 编译测试失败 | `configure.ac` | 本机 CFLAGS 不含 `-mavx512f`，`AC_COMPILE_IFELSE` 失败 → `AC_MSG_ERROR` 终止 |
| 3 | AVX512 kernel .c 文件编译失败 | `Makefile.am` | 全局 CFLAGS 不含 `-mavx512f`，GCC 报 `_mm512_fmadd_pd` 未定义 |

### 为什么不能用更简单的方案

| 方案 | 问题 |
|------|------|
| `spack install elpa target=x86_64:v4` | 全局代码需要 AVX512，在 AVX2 机上 SIGILL 崩溃 |
| 全局加 `-mavx512f` 到 CFLAGS | 主库代码（ELPA、MPI 等）全部按 AVX512 编译，AVX2 上无法运行 |
| 不 patch，只在 AVX512 机上编译 | CI/开发机通常没有 AVX512，构建流程受限 |
| 交叉编译 | ELPA 使用 `AC_COMPILE_IFELSE`（仅编译不运行），理论上可行但不解决 Spack 端阻断 |

---

## ELPA：技术方案

核心思路：**per-object CFLAGS**——只有 AVX512 kernel 的 `.lo` 目标加 `-mavx512f`，其余代码保持构建机默认 flags。

具体修改 4 个点：

| Patch | 文件 | 修改内容 |
|-------|------|---------|
| **B** | `configure.ac` | AVX512 编译测试 `AC_MSG_ERROR` → `AC_MSG_WARN` + `can_compile_avx512=yes` |
| **C** | `configure.ac` | 在 `AC_DEFINE HAVE_AVX512` 后插入 `AVX512_CFLAGS` 定义（含编译器检测） |
| **B2** | `configure.ac` | Xeon/Xeon Phi 子测试 `AC_MSG_ERROR` → `AC_MSG_WARN` + `can_compile_avx512_xeon=yes` |
| **D** | `Makefile.am` ×5 处 | 每个 AVX512 kernel 源文件后追加 per-object `AM_CFLAGS += $(AVX512_CFLAGS)` |

Spack 侧：
| Patch | 文件 | 修改内容 |
|-------|------|---------|
| **A** | `package.py` | 新增 `+force_all_x86_kernel` variant，为 True 时强制 `--enable-avx512-kernels` + 应用源码 patch |

---

## ELPA：Patch 详细说明

### Patch B：configure.ac — AVX512 编译测试绕过

**位置**：`configure.ac`，`if test x"${need_avx512}" = x"yes"; then` 块内

**原始代码**：
```m4
  AC_MSG_RESULT([${can_compile_avx512}])
  if test x"$can_compile_avx512" != x"yes"; then
    AC_MSG_ERROR([Could not compile a test program with AVX512, adjust the C compiler or CFLAGS. Possibly (some of) the flags " $SIMD_FLAGS " solve this issue])
  fi
  AC_DEFINE([HAVE_AVX512],[1],[AVX512 is supported on this CPU])
```

**Patched**：
```m4
  AC_MSG_RESULT([${can_compile_avx512}])
  if test x"$can_compile_avx512" != x"yes"; then
    AC_MSG_WARN([Could not compile a test program with AVX512, but forcing AVX512 support enabled as requested])
    can_compile_avx512=yes
  fi
  AC_DEFINE([HAVE_AVX512],[1],[AVX512 is supported on this CPU])
```

**原理**：AVX512 编译测试使用全局 CFLAGS（不含 `-mavx512f`），必然失败。改为 WARN 并强制 `can_compile_avx512=yes`，让 configure 继续执行。

### Patch C：configure.ac — AVX512 per-object CFLAGS 定义

**位置**：紧接 `AC_DEFINE([HAVE_AVX512],...)` 之后、`if test x"$can_compile_avx512" = x"yes"` 之前

**新增代码**：
```m4
  # Per-object CFLAGS for AVX512 kernel files only
  # Compiler-specific flags: GCC/Clang use -mavx512f syntax, Intel uses -xCORE-AVX512
  if test x"$GCC" = x"yes" || test x"$ac_cv_c_compiler_gnu" = x"yes"; then
    AVX512_CFLAGS="-mavx512f -mavx512cd -mavx512vl -mavx512bw -mavx512dq"
  else
    case "$CC" in
      *icc*|*icx*|*ifort*|*ifx*)
        AVX512_CFLAGS="-xCORE-AVX512"
        ;;
      *)
        # Fallback: try GCC-style flags
        AVX512_CFLAGS="-mavx512f -mavx512cd -mavx512vl -mavx512bw -mavx512dq"
        ;;
    esac
  fi
  AC_SUBST([AVX512_CFLAGS])
```

**编译器兼容性**：
- **GCC / Clang**：`-mavx512f -mavx512cd -mavx512vl -mavx512bw -mavx512dq`
- **Intel 编译器**（icc/icx/ifort/ifx）：`-xCORE-AVX512`（一条指令启用全部 AVX512 子集）
- **其他**：回退到 GCC 语法
- 通过 autoconf 内置变量 `$GCC` 和 `$CC` 名称检测区分编译器

### Patch B2：configure.ac — Xeon/Xeon Phi 子测试

**位置**：AVX512 Xeon/Xeon Phi 检测块末尾

**原始代码**：
```m4
      else
        AC_MSG_ERROR([Oho! We can neither compile AVX512 intrinsics for Xeon nor Xeon Phi. This should not happen!])
      fi
```

**Patched**：
```m4
      else
        AC_MSG_WARN([Cannot compile AVX512 intrinsics for Xeon nor Xeon Phi. Defaulting to Xeon.])
        can_compile_avx512_xeon=yes
      fi
```

**原理**：Patch B 让 configure 通过了主 AVX512 测试，但后续的 Xeon vs Xeon Phi 子测试同样因为全局 CFLAGS 不含 AVX512 flags 而编译失败，会进入 `else` 分支触发 `AC_MSG_ERROR`。改为 WARN 并默认 Xeon 模式。

**安全说明**：ELPA 所有 SIMD 探测均使用 `AC_COMPILE_IFELSE`（仅编译不运行）。唯一的 `AC_RUN_IFELSE` 用于 MPI 线程检测，与 AVX512 无关。

### Patch D：Makefile.am — per-object CFLAGS

**位置**：5 处 AVX512 kernel SOURCES 声明之后、对应 SVE kernel 声明之前

**模式**（以 `real_avx512_block2` 为例）：

原始 Makefile.am：
```makefile
if WITH_REAL_AVX512_BLOCK2_KERNEL
  libelpa@SUFFIX@_private_la_SOURCES += src/elpa2/kernels/real_avx512_2hv_double_precision.c
if WANT_SINGLE_PRECISION_REAL
  libelpa@SUFFIX@_private_la_SOURCES += src/elpa2/kernels/real_avx512_2hv_single_precision.c
endif
endif

if WITH_REAL_SVE128_BLOCK2_KERNEL    ← 下一个 kernel block
```

Patched（在 `endif` 和 SVE block 之间插入）：
```makefile
if WITH_REAL_AVX512_BLOCK2_KERNEL
  libelpa@SUFFIX@_private_la_SOURCES += ...
endif
endif

if WITH_REAL_AVX512_BLOCK2_KERNEL
src/elpa2/kernels/real_avx512_2hv_double_precision.lo: AM_CFLAGS += $(AVX512_CFLAGS)
if WANT_SINGLE_PRECISION_REAL
src/elpa2/kernels/real_avx512_2hv_single_precision.lo: AM_CFLAGS += $(AVX512_CFLAGS)
endif
endif

if WITH_REAL_SVE128_BLOCK2_KERNEL    ← 下一个 kernel block
```

**原理**：GNU Make 的 [target-specific variable](https://www.gnu.org/software/make/manual/make.html#Target_002dspecific) 语法。`file.lo: AM_CFLAGS += $(AVX512_CFLAGS)` 只在编译这些 `.lo` 目标时追加 AVX512 flags，不影响其他文件的编译。`$(AVX512_CFLAGS)` 由 Patch C 通过 `AC_SUBST` 从 configure.ac 导出到 Makefile。

**5 组 per-object CFLAGS**：

| 组 | 条件宏 | 受影响的 .lo 文件 |
|----|--------|-------------------|
| D1 | `WITH_REAL_AVX512_BLOCK2_KERNEL` | `real_avx512_2hv_{double,single}_precision.lo` |
| D2 | `WITH_REAL_AVX512_BLOCK4_KERNEL` | `real_avx512_4hv_{double,single}_precision.lo` |
| D3 | `WITH_REAL_AVX512_BLOCK6_KERNEL` | `real_avx512_6hv_{double,single}_precision.lo` |
| D4 | `WITH_COMPLEX_AVX512_BLOCK1_KERNEL` | `complex_avx512_1hv_{double,single}_precision.lo` |
| D5 | `WITH_COMPLEX_AVX512_BLOCK2_KERNEL` | `complex_avx512_2hv_{double,single}_precision.lo` |

---

## ELPA：Spack 集成

### 新增 variant

在 `repos/spack_repo/builtin/packages/elpa/package.py` 中添加：

```python
variant(
    "force_all_x86_kernel",
    default=False,
    description="Force-build all x86 SIMD kernels (AVX512 etc.) even if not "
    "supported by the build host. Enables runtime autotune selection on "
    "AVX512-capable targets.",
)
```

### Patch 声明

```python
patch("force_all_x86_kernel.patch", when="+force_all_x86_kernel")
```

patch 文件需放在 `repos/spack_repo/builtin/packages/elpa/` 目录下。

### 修改 `configure_args`

```python
kernels = "-kernels" if spec.satisfies("@2023.11:") else ""

simd_features = ["sse", "avx", "avx2", "sve128", "sve256", "sve512"]
x86_force_features = ["avx512"]

if spec.satisfies("+force_all_x86_kernel"):
    for feature in simd_features:
        msg = "--enable-{0}" if feature in spec.target else "--disable-{0}"
        options.append(msg.format(feature + kernels))
    for feature in x86_force_features:
        options.append("--enable-{0}".format(feature + kernels))
else:
    all_simd_features = simd_features + x86_force_features
    for feature in all_simd_features:
        msg = "--enable-{0}" if feature in spec.target else "--disable-{0}"
        options.append(msg.format(feature + kernels))
```

### 使用

```bash
# 在 AVX2 机器上构建带 AVX512 kernel 的 ELPA
spack install elpa +force_all_x86_kernel

# 用于 CP2K
spack install cp2k +elpa ^elpa+force_all_x86_kernel
```

---

## ELPA：跨版本兼容性

### 行号漂移

ELPA 的 `configure.ac` 在不同版本之间行数变化极大：

| 版本 | Patch B 目标行 | Patch B2 目标行 | configure.ac 总行数 |
|------|---------------|----------------|-------------------|
| 2021.05.001 | L1529 | L1585 | ~2100 |
| 2022.11.001 | L1624 | L1681 | ~2200 |
| 2023.11.001 | L2394 | L2451 | ~3100 |
| 2024.05.001 | L2396 | L2453 | ~3100 |
| 2025.06.001 | L2395 | L2452 | ~3100 |
| 2026.02.001 | L2427 | L2484 | ~3150 |

### 但上下文文本完全一致

经过逐版本比对，patch 目标区域的**文字内容**（包括 AVX512 编译测试代码、`AC_MSG_ERROR` 消息、`AC_DEFINE` 声明、Xeon/Xeon Phi 检测逻辑）在所有版本中**一字不差**。

Makefile.am 中 5 个 `WITH_*_AVX512_BLOCK*_KERNEL` 宏和对应 SVE 宏的结构在所有版本中也完全一致。

### Spack 如何应用 patch

Spack 的 `patch()` 函数（`spack.patch.apply_patch()`）底层调用：
```
patch -s -p 1 -i <patchfile> -d <working_dir>
```

- `-s`：静默模式
- `-p 1`：strip 1 层路径前缀（`a/configure.ac` → `configure.ac`）
- **没有传 `--fuzz`**：使用 GNU patch 默认 fuzz=2

GNU patch 的行为：
1. 以 patch 中标注的行号为初始猜测位置
2. 如果行号处的上下文不匹配，**向前向后扫描整个文件**寻找匹配
3. 找到唯一匹配后应用（报告 offset 偏移量）
4. 默认 fuzz=2 允许忽略上下文前/后各 2 行的微小差异

### Dry-run 测试结果

patch 文件从 ELPA 2021.05.002 源码生成，在 3 个版本上测试：

| 版本 | configure.ac | Makefile.am | 结果 |
|------|-------------|-------------|------|
| **2021.05** | ✅ 精确匹配 | ✅ 全部精确匹配 | PASS |
| **2023.11** | ✅ offset 865 行 | ✅ offset 113 行 | PASS |
| **2026.02** | ✅ 自动匹配 | ✅ 3 hunks offset 180 行 | PASS |

---

## ELPA：使用方法

### 方法 1：手动应用 patch

```bash
cd elpa-source-directory
patch -p1 < force_all_x86_kernel.patch
./autogen.sh   # 需要重新生成 configure（需要 autoconf + automake + libtool）
./configure --enable-avx512-kernels ...
make
```

### 方法 2：通过 Spack（推荐）

1. 将 `force_all_x86_kernel.patch` 复制到 `repos/spack_repo/builtin/packages/elpa/`
2. 按上面 [Spack 集成](#spack-集成) 章节修改 `package.py`
3. 安装：

```bash
spack install elpa +force_all_x86_kernel
```

### 方法 3：直接构建（不用 Spack）

```bash
# 1. 获取 ELPA 源码
git clone https://gitlab.mpcdf.mpg.de/elpa/elpa.git
cd elpa

# 2. 应用 patch
patch -p1 < /path/to/force_all_x86_kernel.patch

# 3. 重新生成 configure（需要 autoconf + automake + libtool）
./autogen.sh

# 4. configure（确保 --enable-avx512-kernels）
./configure --enable-avx512-kernels CC=gcc FC=gfortran ...

# 5. 编译
make -j$(nproc)

# 6. 检查 AVX512 kernel 是否被编译
nm src/.libs/libelpa*.a | grep avx512 | head -5
```

---

## ELPA：风险与注意事项

1. **编译失败风险**：GCC 在 AVX2 机器上编译 AVX512 intrinsics 需要 `-mavx512f` 支持。GCC 6+ 支持此选项（即使 CPU 不支持 AVX512）。如果你的 GCC 太旧（< 6），编译可能失败。

2. **运行时不加速**：这个 patch 不改变 ELPA 的 autotune 行为。autotune 会在运行时检测 CPU 是否支持 AVX512，如果支持则自动使用 AVX512 kernel。在 AVX2 机器上运行时，即使编译了 AVX512 kernel，autotune 也不会选择它（因为 CPU 不支持），所以不会产生运行错误。

3. **Intel 编译器**：Intel 编译器使用 `-xCORE-AVX512` 而非 GCC 的 `-mavx512f`。Patch C 已处理此兼容性。

4. **新 ELPA 版本**：如果 ELPA 未来版本重构了 AVX512 代码结构（如移动文件、重命名宏），patch 可能需要更新。但截至 2026.02.001，所有版本的 AVX512 代码结构完全一致。

5. **Spack sha256**：Spack 的 `patch()` 要求 patch 文件的 sha256 与 concretize 记录中的一致。修改 patch 文件后需要 `spack concretize -f` 重新求解。

---

## 文件清单

```
force_all_x86_kernel/
├── FORCE_ALL_X86_KERNELS_PATCH.md    ← 本文件：完整技术文档
└── force_all_x86_kernel.patch        ← ELPA 源码 patch（统一 diff 格式）
```

Spack `package.py` 的修改不在此文件夹中，需要手动修改 Spack 仓库中的 `repos/spack_repo/builtin/packages/elpa/package.py`。

---

# Part II — FFTW

## FFTW：为什么不需要 patch

FFTW（版本 3.3.10）的 AVX512 架构和 ELPA **完全不同**，天然支持「在 AVX2 机器上编译 AVX512 kernel」的场景。

**关键区别**：

| 特性 | ELPA | FFTW |
|------|------|------|
| AVX512 代码编译 | 和主代码混在同一个 `.la` | **独立的 `.la` 库**（如 `libdft_avx512_codelets.la`） |
| AVX512 CFLAGS | 全局 CFLAGS，无 per-object | **天然 per-library** — 每个 SIMD 子目录的 `Makefile.am` 第 1 行就是 `AM_CFLAGS = $(AVX512_CFLAGS)` |
| configure 编译测试 | `AC_COMPILE_IFELSE` 测试真实 AVX512 intrinsics → 失败 | `AX_CHECK_COMPILER_FLAGS` **只测编译器是否接受 `-mavx512f`** → 不失败 |
| 运行时 dispatch | autotune | **CPUID 运行时检测**（`have_simd_avx512()`），条件注册 solver |

**结论**：FFTW 不需要任何源码 patch。唯一需要做的是让 Spack 传 `--enable-avx512`（而非默认的 `--disable-avx512`）。

---

## FFTW：AVX512 架构分析

### configure.ac — 编译器 flag 检测（非运行时检测）

FFTW 的 AVX512 检测使用 `AX_CHECK_COMPILER_FLAGS`：

```m4
# configure.ac L371 (gcc 分支)
if test "$have_avx512" = "yes" -a "x$AVX512_CFLAGS" = x; then
    AX_CHECK_COMPILER_FLAGS(-mavx512f, [AVX512_CFLAGS="-mavx512f"],
        [AC_MSG_ERROR([Need a version of gcc with -mavx512f])])
fi
```

`AX_CHECK_COMPILER_FLAGS` 的实现（`m4/ax_check_compiler_flags.m4`）：

```
AC_COMPILE_IFELSE([AC_LANG_PROGRAM()])
```

它编译一个**空程序**（`AC_LANG_PROGRAM()` 无 body），只测试编译器是否接受 `-mavx512f` 这个 flag。**不需要 CPU 支持 AVX512**，GCC 6+ 在任何 x86_64 CPU 上都接受此 flag。

对比 ELPA 的 `AC_COMPILE_IFELSE`，它编译了真实的 AVX512 intrinsics 代码（`_mm512_fmadd_pd` 等），需要 `-mavx512f` 在全局 CFLAGS 中才能通过——这就是 ELPA 在 AVX2 机器上失败的原因。

### Makefile.am — 天然 per-library CFLAGS

每个 SIMD 子目录有独立的 `Makefile.am`，天然隔离 CFLAGS：

```makefile
# dft/simd/avx512/Makefile.am
AM_CFLAGS = $(AVX512_CFLAGS)       ← 仅 AVX512 codelets 使用
SIMD_HEADER = simd-support/simd-avx512.h

if HAVE_AVX512
noinst_LTLIBRARIES = libdft_avx512_codelets.la
libdft_avx512_codelets_la_SOURCES = $(BUILT_SOURCES)
endif
```

```makefile
# dft/simd/avx2/Makefile.am
AM_CFLAGS = $(AVX2_CFLAGS)         ← AVX2 codelets 使用自己的 flags
```

主库代码（`kernel/`、`dft/` 主目录等）使用默认的全局 CFLAGS，**不受 `AVX512_CFLAGS` 影响**。

### AVX512_CFLAGS 的导出

```m4
# configure.ac L470
AC_SUBST(AVX512_CFLAGS)
```

`AC_SUBST` 将 `AVX512_CFLAGS` 导出到 `Makefile` 中，每个子目录的 `AM_CFLAGS = $(AVX512_CFLAGS)` 引用它。

---

## FFTW：运行时 dispatch 机制

FFTW 在运行时通过 **CPUID** 检测 CPU 是否支持 AVX512，只有在 AVX512 机器上才注册 AVX512 solver。

### `simd-support/avx512.c` — CPUID 检测

```c
int X(have_simd_avx512)(void)
{
     static int init = 0, res;
     int max_stdfn, eax, ebx, ecx, edx;

     if (!init) {
          cpuid_all(0,0,&eax,&ebx,&ecx,&edx);
          max_stdfn = eax;
          if (max_stdfn >= 0x1) {
               /* have OSXSAVE? (implies XGETBV exists) */
               cpuid_all(0x1, 0, &eax, &ebx, &ecx, &edx);
               if ((ecx & 0x08000000) == 0x08000000) {
                    /* CPUID leaf 7, bit 16 = AVX512F */
                    cpuid_all(7,0,&eax,&ebx,&ecx,&edx);
                    if (ebx & (1 << 16)) {
                         /* OS support for XMM, YMM, ZMM via XGETBV */
                         int zmm_ymm_xmm = (7 << 5) | (1 << 2) | (1 << 1);
                         res = ((xgetbv_eax(0) & zmm_ymm_xmm) == zmm_ymm_xmm);
                    }
               }
          }
          init = 1;
     }
     return res;
}
```

检测流程：
1. CPUID leaf 0 → 获取最大标准功能号
2. CPUID leaf 1, ECX bit 27 → 检查 OSXSAVE（XGETBV 指令是否可用）
3. CPUID leaf 7, EBX bit 16 → 检查 AVX512F 支持
4. XGETBV → 检查 OS 是否保存 ZMM/YMM/XMM 寄存器状态
5. 全部通过才返回 `res=1`

**在 AVX2 机器上**：步骤 3 会失败（EBX bit 16 = 0），函数返回 `res=0`（初始化为 0）。

### `dft/conf.c` / `rdft/conf.c` — 条件注册 solver

```c
void X(dft_conf_standard)(planner *p)
{
     ...
#if HAVE_AVX512
     if (X(have_simd_avx512)())          ← 运行时 CPUID 检测
          X(solvtab_exec)(X(solvtab_dft_avx512), p);   ← 只在 AVX512 CPU 上注册
#endif
     ...
}
```

**安全保证**：
- `#if HAVE_AVX512` — 编译期条件（`--enable-avx512` 时定义），决定 AVX512 codelets 和检测代码是否编译进库
- `if (X(have_simd_avx512)())` — **运行时检测**，决定 AVX512 solver 是否被注册到 planner
- 在 AVX2 机器上，`have_simd_avx512()` 返回 0，AVX512 solver **永远不会被注册**，库正常使用 AVX2/AVX/SSE2 路径
- 在 AVX512 机器上，`have_simd_avx512()` 返回 1，AVX512 solver 被注册并自动选择

---

## FFTW：Spack 集成

Spack 的 `fftw/package.py` 中，AVX512 的控制逻辑和 ELPA 类似：

```python
# package.py 中的 simd_options 生成
simd_features = ["sse2", "avx", "avx2", "avx512", "avx-128-fma", "kcvi", "vsx", "asimd"]

for feature in simd_features:
    msg = "--enable-{0}" if feature in spec.target else "disable-{0}"
    simd_options.append(msg.format(feature_opt))
```

在 AVX2 机器上，`"avx512" not in spec.target` → `--disable-avx512`。

### 方案：添加 `+force_avx512` variant

修改 `repos/spack_repo/builtin/packages/fftw/package.py`：

#### 1. 添加 variant

在现有 variant 定义区域添加：

```python
variant(
    "force_avx512",
    default=False,
    description="Force enable AVX512 SIMD kernels even if not supported by "
    "the build host. AVX512 codelets are compiled with per-library "
    "AVX512_CFLAGS and dispatch is via runtime CPUID detection.",
)
```

#### 2. 修改 configure() 中 simd_options 生成

在 `simd_options` 列表生成之后添加：

```python
# Force enable avx512 if variant is set
if spec.satisfies("+force_avx512"):
    simd_options = [
        opt.replace("--disable-avx512", "--enable-avx512")
        for opt in simd_options
    ]
```

或者更直接，在 `for feature in simd_features` 循环中修改条件：

```python
for feature in simd_features:
    if feature == "avx512" and spec.satisfies("+force_avx512"):
        msg = "--enable-{0}"   # 强制 enable
    else:
        msg = "--enable-{0}" if feature in spec.target else "--disable-{0}"
    feature_opt = feature
    if feature == "asimd":
        feature_opt = "neon"
    simd_options.append(msg.format(feature_opt))
```

#### 3. 使用

```bash
# 在 AVX2 机器上构建带 AVX512 kernel 的 FFTW
spack install fftw +force_avx512

# 用于 CP2K
spack install cp2k +elpa ^fftw+force_avx512 ^elpa+force_all_x86_kernel
```

### 为什么 FFTW 不需要源码 patch

| 阻断点（ELPA 需要 patch 的） | FFTW 的状态 |
|----------------------------|------------|
| Spack 传 `--disable-avx512` | ✅ 同样存在，但只需改 `package.py` |
| configure 编译测试失败 | ❌ **不存在** — `AX_CHECK_COMPILER_FLAGS` 只测编译器 flag 支持，不测 intrinsics |
| kernel 代码编译失败 | ❌ **不存在** — 天然 per-library `AM_CFLAGS = $(AVX512_CFLAGS)` |

---

## 附录：ELPA vs FFTW AVX512 对比总结

| | ELPA | FFTW |
|---|------|------|
| **需要源码 patch？** | ✅ 需要（configure.ac + Makefile.am） | ❌ **不需要** |
| **需要改 Spack package.py？** | ✅ 需要（加 variant + patch 声明） | ✅ 需要（加 variant） |
| **per-object CFLAGS** | 需 patch Makefile.am 添加 | **天然支持**（每 SIMD 子目录独立 `AM_CFLAGS`） |
| **configure 编译测试** | `AC_COMPILE_IFELSE`（真实 intrinsics）→ AVX2 上失败 | `AX_CHECK_COMPILER_FLAGS`（空程序）→ AVX2 上成功 |
| **运行时 dispatch** | autotune 机制 | CPUID 检测（`have_simd_avx512()`） |
| **dispatch 安全性** | autotune 检测 CPU 类型 | CPUID + XGETBV 双重检测 |
| **在 AVX2 机上运行** | ✅ 安全（走 AVX2 kernel） | ✅ 安全（AVX512 solver 不注册） |
| **在 AVX512 机上运行** | ✅ 自动加速（autotune 选 AVX512） | ✅ 自动加速（CPUID 注册 AVX512 solver） |



ELPA Result: (使用 elpa2_print_kernels_openmp 验证)

root@shaojiehe-pc:/opt/spack/linux-x86_64_v3/elpa-2025.01.001-vnsczcaiqsnsktbwa2okemtskatxw5cj/bin# grep -E 'model name|flags' /proc/cpuinfo | head -n 2
model name      : Intel(R) Core(TM) Ultra 7 265K
flags           : fpu vme de pse tsc msr pae mce cx8 apic sep mtrr pge mca cmov pat pse36 clflush mmx fxsr sse sse2 ss ht syscall nx pdpe1gb rdtscp lm constant_tsc rep_good nopl xtopology tsc_reliable nonstop_tsc cpuid tsc_known_freq pni pclmulqdq vmx ssse3 fma cx16 pcid sse4_1 sse4_2 x2apic movbe popcnt tsc_deadline_timer aes xsave avx f16c rdrand hypervisor lahf_lm abm 3dnowprefetch ssbd ibrs ibpb stibp ibrs_enhanced tpr_shadow ept vpid ept_ad fsgsbase tsc_adjust bmi1 avx2 smep bmi2 erms invpcid rdseed adx smap clflushopt clwb sha_ni xsaveopt xsavec xgetbv1 xsaves avx_vnni vnmi umip waitpkg gfni vaes vpclmulqdq rdpid movdiri movdir64b fsrm md_clear serialize flush_l1d arch_capabilities

root@shaojiehe-pc:/opt/spack/linux-x86_64_v3/elpa-2025.01.001-vnsczcaiqsnsktbwa2okemtskatxw5cj/bin# ./elpa2_print_kernels_openmp 
 This program will give information on the ELPA2 kernels, 
 which are available with this library and it will give 
 information if (and how) the kernels can be choosen at 
 runtime

  ELPA supports threads: yes

 Information on ELPA2 real case: 
 =============================== 
  choice via environment variable: yes
  environment variable name      : ELPA_DEFAULT_real_kernel

  Available real kernels are: 

   ELPA_2STAGE_REAL_GENERIC
   ELPA_2STAGE_REAL_GENERIC_SIMPLE
   ELPA_2STAGE_REAL_SSE_ASSEMBLY
   ELPA_2STAGE_REAL_SSE_BLOCK2
   ELPA_2STAGE_REAL_SSE_BLOCK4
   ELPA_2STAGE_REAL_SSE_BLOCK6
   ELPA_2STAGE_REAL_AVX_BLOCK2
   ELPA_2STAGE_REAL_AVX_BLOCK4
   ELPA_2STAGE_REAL_AVX_BLOCK6
   ELPA_2STAGE_REAL_AVX2_BLOCK2
   ELPA_2STAGE_REAL_AVX2_BLOCK4
   ELPA_2STAGE_REAL_AVX2_BLOCK6
   ELPA_2STAGE_REAL_AVX512_BLOCK2
   ELPA_2STAGE_REAL_AVX512_BLOCK4
   ELPA_2STAGE_REAL_AVX512_BLOCK6
   ELPA_2STAGE_REAL_GENERIC_SIMPLE_BLOCK4
   ELPA_2STAGE_REAL_GENERIC_SIMPLE_BLOCK6


 Information on ELPA2 complex case: 
 =============================== 
  choice via environment variable: yes
  environment variable name      : ELPA_DEFAULT_complex_kernel

  Available complex kernels are: 

   ELPA_2STAGE_COMPLEX_GENERIC
   ELPA_2STAGE_COMPLEX_GENERIC_SIMPLE
   ELPA_2STAGE_COMPLEX_SSE_ASSEMBLY
   ELPA_2STAGE_COMPLEX_SSE_BLOCK1
   ELPA_2STAGE_COMPLEX_SSE_BLOCK2
   ELPA_2STAGE_COMPLEX_AVX_BLOCK1
   ELPA_2STAGE_COMPLEX_AVX_BLOCK2
   ELPA_2STAGE_COMPLEX_AVX2_BLOCK1
   ELPA_2STAGE_COMPLEX_AVX2_BLOCK2
   ELPA_2STAGE_COMPLEX_AVX512_BLOCK1
   ELPA_2STAGE_COMPLEX_AVX512_BLOCK2

ITS WORKKKKKKING!!!


fftw avx512 codelet:
root@shaojiehe-pc:/opt/spack/linux-x86_64_v3/fftw-3.3.10-65egq7iups7c6ewxpso22e3kow3j7pn7/lib# nm -D libfftw3.so | grep avx512
00000000002f5250 T fftw_codelet_hc2cbdftv_10_avx512
00000000002f5a20 T fftw_codelet_hc2cbdftv_12_avx512
00000000002f6510 T fftw_codelet_hc2cbdftv_16_avx512
00000000002f8ca0 T fftw_codelet_hc2cbdftv_20_avx512
00000000002f3d30 T fftw_codelet_hc2cbdftv_2_avx512
00000000002f7e50 T fftw_codelet_hc2cbdftv_32_avx512
00000000002f41c0 T fftw_codelet_hc2cbdftv_4_avx512
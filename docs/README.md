# 文档总览

HPC-Container-Factory 的完整文档索引。顶层入口：[../README.md](../README.md)、[../QUICKSTART.md](../QUICKSTART.md)。

## 核心文档

| 文档 | 说明 |
|------|------|
| [快速开始](QUICK_START.md) | 5 步完成构建（Dockerfile → 镜像 → SIF） |
| [CLI 用法](GENERATE_CLI.md) | `generate.py` 全部子命令与参数 |
| [离线资源](ASSETS_GUIDE.md) | bootstrap + mirror 容器化构建流程 |
| [SIF 构建](BUILD_SIF.md) | Apptainer SIF 转换、MOTD 技术方案 |
| [模板矩阵](TEMPLATE_MATRIX.md) | 环境 ↔ 模板映射表 |
| [新建环境](ADD_NEW_ENV.md) | 8 步添加新 Spack 环境 |
| [已知问题](KNOWN_ISSUES.md) | 当前 issue 跟踪 |

## 架构

### 项目结构

```
.
├── generate.py              # 统一 CLI 入口
├── activate.sh              # 激活开发环境
├── requirements.txt         # Python 依赖 (jinja2, pyyaml)
├── configs/versions.yaml    # 全局配置
├── spack-envs/              # 每个环境自包含
│   └── <env>/
│       ├── Dockerfile.j2    # 镜像模板
│       ├── cp2k.def.j2      # (可选) SIF 定义模板
│       └── spack-env-file/
│           ├── env.yaml     # Single source of truth
│           ├── spack.yaml
│           └── streamline.sh
├── scripts/                 # 构建、mirror、激活脚本
├── templates/               # Legacy 模板回退
├── assets/                  # 离线资源
├── artifacts/               # 构建产物
├── tools/                   # 本地工具 (apptainer)
└── legacy/                  # 归档
```

### 每个环境自包含

`spack-envs/<env>/` 包含构建所需的一切：

```
spack-envs/<env>/
  ├── Dockerfile.j2       ← 最终镜像模板
  ├── cp2k.def.j2         ← (可选) SIF 定义模板
  └── spack-env-file/
      ├── env.yaml        ← Single source of truth
      ├── spack.yaml      ← Spack 包定义
      ├── spack.lock      ← concretize 产出
      ├── streamline.sh   ← mirror pipeline 入口（~15 行，逻辑在 spack-common.sh）
      └── repos/          ← (可选) 自定义 Spack repo
```

### Mirror 构建三层架构

```
scripts/build-mirror-in-container.sh    调度器（宿主机）
    ↓ podman run ... bash streamline.sh
spack-envs/<env>/spack-env-file/streamline.sh   Per-env 入口（~15 行）
    ↓ source spack-common.sh
scripts/spack-common.sh    通用函数库（所有环境共享）
```

**设计原则**：
- `containers/Dockerfile.mirror-builder` 是通用 Spack-only 镜像，不含系统包或 pipeline 逻辑
- 系统包在运行时由 `streamline.sh` 从 `env.yaml` 读取后安装
- 每个 env 的差异完全由 `env.yaml` 驱动

## 当前环境

| `--app-version` | 说明 | 自动镜像名 |
|------|------|-----------|
| `cp2k-opensource-2025.2` | CP2K 2025.2 开源 BLAS 版 | `cp2k-opensource:2025.2` |
| `cp2k-opensource-2025.2-force-avx512` | 同上 + AVX512 强制 kernel | `cp2k-opensource:2025.2-force-avx512` |
| `cp2k-rocm-2026.1-gfx942` | CP2K 2026.1 ROCm GPU 版 (gfx942) | `cp2k-rocm:2026.1-gfx942` |

## Build Notes 与专题文档（开发参考）

以下文档为构建过程记录，不做常规更新：

- [`BUILD_NOTE/`](BUILD_NOTE/) — CP2K 各版本构建日志
- [`cp2k/`](cp2k/) — CP2K 特定文档与 InfinityHub 方案
- [`force_all_x86_kernel/`](force_all_x86_kernel/) — AVX512 强制编译 patch

## 归档

历史路线（VASP、CP2K MKL）已迁移至 `legacy/` 目录。
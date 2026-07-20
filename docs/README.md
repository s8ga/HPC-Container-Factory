# 文档总览

HPC-Container-Factory 的完整文档索引。顶层入口：[../README.md](../README.md)、[../QUICKSTART.md](../QUICKSTART.md)。

## 核心文档

| 文档 | 说明 |
|------|------|
| [快速开始](QUICK_START.md) | 5 步完成构建（Dockerfile → 镜像 → SIF） |
| [CLI 用法](GENERATE_CLI.md) | `hpc_cf` 全部子命令与参数 |
| [离线资源](ASSETS_GUIDE.md) | bootstrap + mirror 容器化构建流程 |
| [SIF 构建](BUILD_SIF.md) | Apptainer SIF 转换、MOTD 技术方案 |
| [模板矩阵](TEMPLATE_MATRIX.md) | 环境 ↔ 模板映射表 |
| [新建环境](ADD_NEW_ENV.md) | 8 步添加新 Spack 环境 |
| [已知问题](KNOWN_ISSUES.md) | 当前 issue 跟踪 |
| [发布说明](releases/README.md) | 版本证据、限制与发布记录索引 |

## 架构

### 项目结构

```
.
├── hpc_cf/                  # Python 包 (python -m hpc_cf)
│   ├── __main__.py          # 入口
│   ├── cli.py               # argparse + 调度
│   ├── assets.py            # assets 工作流
│   ├── container.py         # Podman 容器管理
│   ├── spack_ops.py         # Spack 操作
│   ├── template.py          # Jinja2 渲染
│   ├── sif.py               # SIF 构建 + apptainer
│   ├── env.py               # env.yaml 解析
│   └── config.py            # 路径常量
├── activate.sh              # 激活开发环境
├── requirements.txt         # Python 依赖 (jinja2, pyyaml)
├── spack-envs/              # 每个环境自包含
│   └── <env>/
│       ├── Dockerfile.j2    # 镜像模板
│       ├── cp2k.def.j2      # (可选) SIF 定义模板
│       └── spack-env-file/
│           ├── env.yaml     # Single source of truth
│           ├── spack.yaml
│           └── spack.lock   # concretize 产出
├── scripts/                 # 运行时脚本 (apptainer 激活、MOTD 等)
├── containers/              # Dockerfile.mirror-builder
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
      └── repos/          ← (可选) 自定义 Spack repo
```

### Mirror 构建架构

```
hpc_cf/assets.py + hpc_cf/cli.py      工作流调度（宿主机）
    ↓ Container.exec() / run_ephemeral()
hpc_cf/container.py                    容器生命周期管理
    ↓ bash -lc <script>
hpc_cf/spack_ops.py                    Spack 操作函数库（所有环境共享）
```

**设计原则**：
- `containers/Dockerfile.mirror-builder` 是通用 Spack-only 镜像，不含系统包或 pipeline 逻辑
- 系统包在运行时由 `hpc_cf/spack_ops.py` 从 `env.yaml` 读取后安装
- 每个 env 的差异完全由 `env.yaml` 驱动

## 当前环境

| `--app-version` | Spack | 自动镜像名 |
|------|------|-----------|
| `abacus_opensource-3.9.0.27-force-avx512` | 1.2.0 | `abacus_opensource:3.9.0.27-force-avx512` |
| `abacus_opensource-3.10.1-force-avx512` | 1.2.0 | `abacus_opensource:3.10.1-force-avx512` |
| `cp2k_mkl-2025.2-experimental` | 1.1.0 | `cp2k_mkl:2025.2-experimental` |
| `cp2k_opensource-2025.2` | 1.1.0 | `cp2k_opensource:2025.2` |
| `cp2k_opensource-2025.2-force-avx512` | 1.1.0 | `cp2k_opensource:2025.2-force-avx512` |
| `cp2k_opensource-2026.1-force-avx512` | 1.1.1 | `cp2k_opensource:2026.1-force-avx512` |
| `cp2k_opensource-2026.2-force-avx512` | 1.2.0 | `cp2k_opensource:2026.2-force-avx512` |
| `cp2k_rocm-2026.1-gfx942` | 1.1.0 | `cp2k_rocm:2026.1-gfx942` |
| `vasp_mkl-6.6.0-avx2` | 1.1.1 | `vasp_mkl:6.6.0-avx2` |
| `vasp_mkl-6.6.0-avx512` | 1.1.1 | `vasp_mkl:6.6.0-avx512` |

运行 `python -m hpc_cf dockerfile --app-version` 可获取实时发现结果。

## Build Notes 与专题文档（开发参考）

以下文档为构建过程记录，不做常规更新：

- [`BUILD_NOTE/`](BUILD_NOTE/) — CP2K 各版本构建日志
- [`cp2k/`](cp2k/) — CP2K 特定文档与 InfinityHub 方案
- [`force_all_x86_kernel/`](force_all_x86_kernel/) — AVX512 强制编译 patch

## 归档

归档内容位于 `legacy/`。当前可选环境始终以 CLI 发现结果为准。

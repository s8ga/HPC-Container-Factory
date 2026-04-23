# HPC-Container-Factory

面向 HPC 软件栈的容器构建工厂。通过 Jinja2 模板 + Spack 环境定义，一键生成多阶段 Dockerfile、构建容器镜像、转换为 Apptainer SIF。

## 核心能力

| 能力 | 命令 |
|------|------|
| 生成 Dockerfile | `python generate.py dockerfile --app-version <env>` |
| 构建容器镜像 | `python generate.py build --app-version <env>` |
| 转换为 SIF | `python generate.py build-sif --app-version <env>` |
| 准备离线资源 | `python generate.py assets --env <env>` |
| 打包 Apptainer | `python generate.py pack-apptainer` |

## 当前环境

| 环境 | 说明 | 自动镜像名 |
|------|------|-----------|
| `cp2k-opensource-2025.2` | CP2K 2025.2 开源 BLAS 版 | `cp2k-opensource:2025.2` |
| `cp2k-opensource-2025.2-force-avx512` | 同上 + 强制 AVX512 kernel | `cp2k-opensource:2025.2-force-avx512` |
| `cp2k-rocm-2026.1-gfx942` | CP2K 2026.1 ROCm GPU 版 (gfx942) | `cp2k-rocm:2026.1-gfx942` |

## ROCm 镜像构建参考

`cp2k-rocm-2026.1-gfx942` 使用 AMD InfinityHub CI 的 ROCm 构建流。构建器镜像为 `rocm/dev-ubuntu-24.04:7.2.1-complete`，运行时镜像为 `rocm/dev-ubuntu-24.04:7.2.1`。

上游参考和本地适配记录见 `spack-envs/cp2k-rocm-2026.1-gfx942/README_SOURCE.md`，主要包括：

- 上游来源：`https://github.com/amd/InfinityHub-CI`，路径 `cp2k/docker/cp2k_environment`
- 保持 ROCm 7.2.1 兼容性
- 通过 Spack externals 发现 ROCm / HIP 组件，而不是强制下载 HIP 软件包
- 使用本地 `cp2k` package.py 扩展 ROCm/DLA-Future 支持

## 工具要求

### 必需

| 工具 | 版本 | 用途 |
|------|------|------|
| Python | ≥ 3.10 | 运行 `generate.py` |
| Podman 或 Docker | 任意 | 容器构建、mirror 构建 |
| Bash | ≥ 4.0 | 脚本运行 |

### Python 依赖

```bash
pip install -r requirements.txt
# 或
uv pip install -r requirements.txt
```

依赖列表：`jinja2`、`markupsafe`、`pyyaml`

### 可选（按功能）

| 工具 | 用途 | 安装 |
|------|------|------|
| Apptainer / Singularity | SIF 构建、SIF 运行 | `python generate.py build-sif --install-apptainer-only` |
| makeself | 打包 Apptainer 自解压包 | `sudo apt install makeself` |
| gzip | pack-apptainer 压缩（默认 gzip，系统自带） | 系统自带 |
| `curl`, `rpm2cpio`, `cpio` | Apptainer 非特权安装 | `sudo apt install curl rpm2cpio cpio` |

## 快速开始

详见 **[QUICKSTART.md](QUICKSTART.md)**，30 秒上手。

## 文档

完整文档在 [`docs/`](docs/)：

- [文档总览](docs/README.md) — 架构与文档导航
- [快速开始](docs/QUICK_START.md) — 5 步完成构建
- [CLI 用法](docs/GENERATE_CLI.md) — `generate.py` 全部命令
- [离线资源指南](docs/ASSETS_GUIDE.md) — bootstrap + mirror 流程
- [SIF 构建](docs/BUILD_SIF.md) — Apptainer SIF 转换与 MOTD
- [模板矩阵](docs/TEMPLATE_MATRIX.md) — 环境 ↔ 模板映射
- [新建环境](docs/ADD_NEW_ENV.md) — 8 步添加新环境
- [已知问题](docs/KNOWN_ISSUES.md) — 当前 issue 跟踪

### Build Notes（不动）

构建日志记录，仅供开发参考：

- [`docs/BUILD_NOTE/`](docs/BUILD_NOTE/) — CP2K 各版本构建过程
- [`docs/cp2k/`](docs/cp2k/) — CP2K 特定文档与 InfinityHub 方案
- [`docs/force_all_x86_kernel/`](docs/force_all_x86_kernel/) — AVX512 强制编译 patch

## 项目结构

```
.
├── generate.py              # 统一 CLI 入口
├── activate.sh              # 激活开发环境 (venv + apptainer)
├── requirements.txt         # Python 依赖
├── spack-envs/              # 每个环境自包含
│   ├── cp2k-opensource-2025.2/
│   │   ├── Dockerfile.j2    # 镜像模板
│   │   ├── cp2k.def.j2      # (可选) SIF 定义模板
│   │   └── spack-env-file/
│   │       ├── env.yaml     # Single source of truth
│   │       ├── spack.yaml   # Spack 包定义
│   │       └── streamline.sh
│   └── ...
├── scripts/                 # 构建、mirror、激活脚本
├── templates/               # Legacy 模板 (回退)
├── assets/                  # 离线资源 (bootstrap + mirror)
├── artifacts/               # 构建产物 (SIF, .run, Dockerfile)
├── tools/                   # 本地工具 (apptainer)
├── legacy/                  # 归档 (VASP, MKL)
└── docs/                    # 文档
```

## License

MIT

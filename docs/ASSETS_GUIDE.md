# assets 与离线资源指南

assets 是本项目的核心，负责把构建中最耗时、最依赖网络的部分前置到本地。

## 目录职责

典型关键子目录：
- assets/spack-v1.1.0.tar.gz: Spack 源压缩包
- assets/bootstrap: Spack bootstrap 元数据与缓存
- assets/spack-mirror: Spack 源码镜像
- assets/spack-repo: 自定义 Spack 仓库
- assets/static: 静态构建产物（项目自定义 .so）

## 容器化缓存流程（推荐）

推荐使用 `generate.py assets` 作为统一入口来准备离线资源；底层仍调用 `scripts/build-mirror-in-container.sh` 与 `scripts/prepare-bootstrap-cache.sh`，避免主机环境污染。

### 前置条件

- Podman rootless 已安装
- 网络可访问 APT 源和 GitHub
- 已在 `spack-envs/<env-name>/` 下准备好 `spack.yaml`

### 快速使用

```bash
# 一键完整流程（推荐）
python generate.py assets --env cp2k-opensource-2025.2

# 分步执行
python generate.py assets --create-container
python generate.py assets --prepare-bootstrap
python generate.py assets --env cp2k-opensource-2025.2 --download-mirror
python generate.py assets --env cp2k-opensource-2025.2 --verify-mirror
python generate.py assets --env cp2k-opensource-2025.2 --status
```

也可以直接调用底层脚本：

```bash
./scripts/build-mirror-in-container.sh image
./scripts/build-mirror-in-container.sh create-container
./scripts/prepare-bootstrap-cache.sh --create-container --use-container
./scripts/build-mirror-in-container.sh -e cp2k-opensource-2025.2 mirror
./scripts/build-mirror-in-container.sh -e cp2k-opensource-2025.2 verify
```

也可使用 `-e` 的长选项形式：

```bash
./scripts/build-mirror-in-container.sh --env cp2k-opensource-2025.2 mirror
```

### `-e` / `--env` 参数

所有环境相关命令（`mirror`、`verify`、`all`、`status`）都需要通过 `-e <name>` 指定目标环境。环境名对应 `spack-envs/` 下的子目录名。

如果不想每次传 `-e`，也可设置环境变量：

```bash
export ENV_NAME=cp2k-opensource-2025.2
./scripts/build-mirror-in-container.sh mirror
```

### 子命令说明

| 子命令 | 需要 `-e` | 说明 |
|--------|-----------|------|
| `image` | 否 | 构建 `hpc-mirror-builder` 容器镜像 |
| `create-container` | 否 | 创建或启动 reusable mirror worker container |
| `bootstrap` | 否 | 在宿主机运行 `spack bootstrap mirror --binary-packages` |
| `concretize` | **是** | 在容器中重新 concretize，生成/更新 `spack.lock` |
| `mirror` | **是** | 使用已有 `spack.lock` 运行 `spack mirror create`；如无 lock 自动切换为 `all` 模式 |
| `verify` | **是** | 两层验证：Spack 重跑 mirror create + 结构/符号链接检查 |
| `all` | **是** | 依次 concretize → mirror → verify |
| `status` | **是** | 显示镜像、bootstrap、mirror、环境、streamline 的当前状态 |

### 三层架构

容器化构建系统由三个层级组成：

```
┌─────────────────────────────────────────────────┐
│  scripts/build-mirror-in-container.sh           │
│  调度器 — 宿主机上运行，只管容器生命周期           │
│  职责: podman build/run, 参数解析, 宿主端检查     │
└───────────────┬─────────────────────────────────┘
                │ podman run ... bash streamline.sh
                ▼
┌─────────────────────────────────────────────────┐
│  spack-envs/<env>/streamline.sh                 │
│  Per-env 流水线 — 容器内执行                     │
│  职责: 配置声明 + 环境特有步骤                    │
│  只需修改文件头部的 CONFIG SECTION 即可适配新环境  │
└───────────────┬─────────────────────────────────┘
                │ source spack-common.sh
                ▼
┌─────────────────────────────────────────────────┐
│  scripts/spack-common.sh                        │
│  通用函数库 — 所有环境共享                        │
│  提供: spack_bootstrap(), install_system_pkgs(), │
│        mirror_create(), mirror_verify()          │
└─────────────────────────────────────────────────┘
```

**设计原则**：
- `Dockerfile.mirror-builder` 只是一个干净执行器（debian:trixie + spack），不包含任何 pipeline 逻辑
- 每个 spack 环境通过 `streamline.sh` 的 CONFIG SECTION 声明自己的需求
- 通用操作（bootstrap、mirror create、stats 解析）全部在 `spack-common.sh` 中复用

### Per-environment 配置（streamline.sh）

每个 spack 环境目录下需要一个 `streamline.sh`。新建环境时，复制现有 `streamline.sh` 并只修改 CONFIG SECTION：

```
spack-envs/
├── cp2k-opensource-2025.2/
│   ├── spack.yaml
│   ├── spack.lock          ← concretize 自动生成
│   └── streamline.sh       ← per-env 配置驱动流水线
└── cp2k-rocm-2026.1-gfx942/
    ├── spack.yaml
    └── streamline.sh       ← 只改配置区即可
```

**CONFIG SECTION 示例**：

```bash
# ╔══════════════════════════════════════════════════════════════════════╗
# ║  CONFIG SECTION — 只改这里                                          ║
# ╚══════════════════════════════════════════════════════════════════════╝

# Spack 环境名
SPACK_ENV_NAME="cp2k-env"

# 包管理器 + 安装参数（支持 apt-get / dnf / yum）
PKG_MANAGER="apt-get"
INSTALL_ARGS="install -y --no-install-recommends"

# 系统包列表（完整声明，不自动检测）
SYSTEM_PKGS="
    bash build-essential ca-certificates curl
    gfortran cmake python3-dev ...
"

# 自定义 Spack 仓库（空数组 = 不注册）
# 格式: "GIT_URL|BRANCH|SPARSE_PATH|NAMESPACE"
CUSTOM_REPOS=(
    "https://github.com/cp2k/cp2k.git|support/v2025.2|tools/spack/cp2k_dev_repo|cp2k_dev_repo"
)
```

**适配新环境的步骤**：

1. 复制现有 `streamline.sh` 到新环境目录
2. 修改 `SPACK_ENV_NAME`
3. 修改 `PKG_MANAGER` / `INSTALL_ARGS`（如需要）
4. 修改 `SYSTEM_PKGS`（声明该环境需要的系统包）
5. 修改 `CUSTOM_REPOS`（如需要自定义 Spack 仓库，留空则跳过）
6. 下方的通用逻辑不需要任何修改

**执行模式**：

| 模式 | 触发命令 | 执行步骤 |
|------|----------|----------|
| `concretize` | `... concretize` | install system pkgs → spack bootstrap → register repos → compiler/external find → concretize → 写回 lock |
| `mirror` | `... mirror` | spack bootstrap → register repos → mirror create |
| `all` | `... all` | concretize 全部步骤 → mirror create |
| `verify` | `... verify` | spack bootstrap → register repos → mirror verify |

### 旧的 mirror-create.sh Hook（已废弃）

> ⚠️ `mirror-create.sh` hook 已被 `streamline.sh` 的 `CUSTOM_REPOS` 配置取代。
> 新环境请使用 `streamline.sh`，不再需要单独的 hook 脚本。
> 现有的 `mirror-create.sh` 文件保留但不再被调用。

### 工作原理

- 容器镜像基于 `debian:trixie-slim`，预装 Spack 1.1.0（通用镜像，不含项目特定仓库）
- 通过 `podman run --userns=keep-id -v $PWD:/work:Z` 挂载项目目录
- 所有 Spack 命令在容器内执行，产物直接写入宿主的 `assets/` 目录
- 首次运行 `mirror` 时会在容器内 concretize 并将 `spack.lock` 回写到 `spack-envs/<env-name>/`
- Hook 脚本在 concretize 之前自动注入执行

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MIRROR_BUILDER_IMAGE` | `hpc-mirror-builder` | 容器镜像名 |
| `PODMAN_CMD` | `podman` | 容器运行时可执行文件 |
| `ENV_NAME` | （空） | 环境名（可替代 `-e` 参数） |
| `MIRROR_DIR` | `assets/spack-mirror` | 覆盖默认 mirror 输出路径 |
| `EXTRA_PODMAN_OPTS` | （空） | 额外 podman run 选项 |

## 主机直跑方式（不推荐）

> ⚠️ 主机直跑会 source Spack 的 `setup-env.sh`，污染 PATH/PYTHONPATH/MANPATH。建议使用上方的容器化流程。

脚本：../scripts/init_assets_v2.py

可用子命令：
- --status
- --spack
- --bootstrap
- --mirror
- --static
- --add-package <spec>
- --verify-mirror

## 特殊包处理

编译器类包（如 nvhpc、cuda）可能不会被标准 mirror 流程完整收录，项目通过 ../scripts/package_handlers.py 做补偿抓取。

自定义仓库位于：
- ../assets/spack-repo/spack_repo/custom

其中包含：
- custom nvhpc package

## 注意事项

1. assets 体积很大，属于离线构建能力的一部分，不建议随意清空。
2. VASP/CP2K-MKL 相关资产已迁移至 legacy 归档目录，不再参与活跃流程。
3. 运行前建议先执行 `./scripts/build-mirror-in-container.sh -e <env-name> status` 检查资源状态。
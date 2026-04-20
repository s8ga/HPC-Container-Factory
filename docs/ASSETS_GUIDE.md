# assets 与离线资源指南

assets 负责把构建中最耗时、最依赖网络的部分前置到本地。

## 目录结构

```
assets/
  ├── spack-v1.1.0.tar.gz         ← Spack 源码包
  ├── bootstrap/                  ← Spack bootstrap 元数据与缓存
  └── spack-mirror/               ← Spack 源码镜像（所有依赖的源码 tarball）
```

## 容器化缓存流程（推荐）

统一使用 `generate.py assets` 入口，底层调用 `scripts/build-mirror-in-container.sh` 与 `scripts/prepare-bootstrap-cache.sh`。

### 前置条件

- Podman rootless 已安装
- 网络可访问 APT 源和 GitHub
- 已在 `spack-envs/<env>/spack-env-file/` 下准备好 `spack.yaml`

### 使用

```bash
# 一键完整流程
python generate.py assets --env cp2k-opensource-2025.2

# 分步
python generate.py assets --create-container
python generate.py assets --prepare-bootstrap
python generate.py assets --env cp2k-opensource-2025.2 --download-mirror
python generate.py assets --env cp2k-opensource-2025.2 --verify-mirror
python generate.py assets --env cp2k-opensource-2025.2 --status
```

也可直接调用底层脚本：

```bash
./scripts/build-mirror-in-container.sh image
./scripts/build-mirror-in-container.sh -e cp2k-opensource-2025.2 mirror
./scripts/build-mirror-in-container.sh -e cp2k-opensource-2025.2 verify
./scripts/build-mirror-in-container.sh -e cp2k-opensource-2025.2 status
```

### 三层架构

```
┌─────────────────────────────────────────────────┐
│  scripts/build-mirror-in-container.sh           │
│  调度器 — 宿主机上运行，只管容器生命周期           │
└───────────────┬─────────────────────────────────┘
                │ podman run ... bash streamline.sh
                ▼
┌─────────────────────────────────────────────────┐
│  spack-envs/<env>/spack-env-file/streamline.sh  │
│  Per-env 入口 — ~15 行，只设路径，委托通用函数     │
└───────────────┬─────────────────────────────────┘
                │ source spack-common.sh
                ▼
┌─────────────────────────────────────────────────┐
│  scripts/spack-common.sh                        │
│  通用函数库 — 所有环境共享                        │
│  提供: streamline_parse_env(),                   │
│        step_install_system_pkgs(),               │
│        step_register_repos(), step_find(),       │
│        step_concretize(), mirror_create(),       │
│        mirror_verify()                           │
└─────────────────────────────────────────────────┘
```

**设计原则**：
- `containers/Dockerfile.mirror-builder` 是通用 Spack-only 镜像，不含系统包或 pipeline 逻辑
- 系统包在运行时由 `streamline.sh` 从 `env.yaml` 读取后安装
- 每个 env 的差异完全由 `env.yaml` 驱动，`streamline.sh` 内容相同

### 子命令说明

| 子命令 | 需要 `-e` | 说明 |
|--------|-----------|------|
| `image` | 否 | 构建 `hpc-mirror-builder` 容器镜像 |
| `create-container` | 否 | 创建或启动 reusable mirror worker container |
| `concretize` | **是** | 在容器中重新 concretize，生成/更新 `spack.lock` |
| `mirror` | **是** | 使用已有 `spack.lock` 下载源码 mirror |
| `verify` | **是** | 校验 mirror 完整性 |
| `all` | **是** | concretize → mirror → verify |
| `status` | **是** | 显示镜像、bootstrap、mirror、环境的状态 |

### HOME 隔离

容器运行时设置了 `HOME=/tmp/home`，Spack 用户配置写入容器内部，容器销毁时自动清理。避免跨 env 的 `repos.yaml` / `packages.yaml` 污染。

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MIRROR_BUILDER_IMAGE` | `hpc-mirror-builder` | 容器镜像名 |
| `PODMAN_CMD` | `podman` | 容器运行时 |
| `ENV_NAME` | （空） | 环境名（可替代 `-e`） |
| `MIRROR_DIR` | `assets/spack-mirror` | mirror 输出路径 |
| `EXTRA_PODMAN_OPTS` | （空） | 额外 podman run 选项 |
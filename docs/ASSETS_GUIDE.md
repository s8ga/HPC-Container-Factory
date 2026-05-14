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

统一使用 `python -m hpc_cf assets` 入口，底层由 `hpc_cf/` Python 包驱动。

### 前置条件

- Podman rootless 已安装
- 网络可访问 APT 源和 GitHub
- 已在 `spack-envs/<env>/spack-env-file/` 下准备好 `spack.yaml`

### 使用

```bash
# 一键完整流程
python -m hpc_cf assets --env cp2k_opensource-2025.2

# 分步
python -m hpc_cf assets --create-container
python -m hpc_cf assets --prepare-bootstrap
python -m hpc_cf assets --env cp2k_opensource-2025.2 --download-mirror
python -m hpc_cf assets --env cp2k_opensource-2025.2 --verify-mirror
python -m hpc_cf assets --env cp2k_opensource-2025.2 --status
```

```bash
# 也可通过 CLI 参数直接调用
python -m hpc_cf assets --create-container
python -m hpc_cf assets --env cp2k_opensource-2025.2 --download-mirror
python -m hpc_cf assets --env cp2k_opensource-2025.2 --verify-mirror
python -m hpc_cf assets --env cp2k_opensource-2025.2 --status
```

### 模块架构

```
┌─────────────────────────────────────────────────┐
│  hpc_cf/cli.py + hpc_cf/assets.py               │
│  CLI 调度 + 工作流编排                           │
└───────────────┬─────────────────────────────────┘
                │ Container.exec() / run_ephemeral()
                ▼
┌─────────────────────────────────────────────────┐
│  hpc_cf/container.py                            │
│  容器生命周期管理 (Podman)                       │
└───────────────┬─────────────────────────────────┘
                │ bash -lc <script>
                ▼
┌─────────────────────────────────────────────────┐
│  hpc_cf/spack_ops.py                            │
│  Spack 操作函数库 — 所有环境共享                 │
│  提供: bootstrap_mirror(),                       │
│        install_system_pkgs(),                    │
│        register_repos(), compiler_find(),        │
│        concretize(), mirror_create(),            │
│        mirror_verify()                           │
└─────────────────────────────────────────────────┘
```

**设计原则**：
- `containers/Dockerfile.mirror-builder` 是通用 Spack-only 镜像，不含系统包或 pipeline 逻辑
- 系统包在运行时由 `hpc_cf/spack_ops.py` 从 `env.yaml` 读取后安装
- 每个 env 的差异完全由 `env.yaml` 驱动，代码路径统一

### 子命令/动作标志说明

通过 `python -m hpc_cf assets` 使用，支持以下标志组合：

| 标志 | 需要 `--env` | 说明 |
|------|-------------|------|
| （无标志） | **是** | 一键完整流程：构建镜像 → 创建容器 → bootstrap → mirror → verify |
| `--create-container` | 否 | 构建镜像并创建/启动 reusable mirror worker container |
| `--prepare-bootstrap` | 否 | 生成 Spack bootstrap mirror |
| `--download-mirror` | **是** | 下载源码 mirror |
| `--verify-mirror` | **是** | 校验 mirror 完整性 |
| `--status` | **是** | 显示镜像、bootstrap、mirror、环境的状态 |

### HOME 隔离

容器运行时设置了 `HOME=/tmp/home`，Spack 用户配置写入容器内部，容器销毁时自动清理。避免跨 env 的 `repos.yaml` / `packages.yaml` 污染。

### CLI 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--mirror-image` | `hpc-mirror-builder` | 容器镜像名 |
| `--podman-cmd` | `podman` | 容器运行时 |
| `--container-name` | `hpc-mirror-builder-work` | reusable 容器名 |
| `--podman-opt` | （空） | 额外 podman 选项（可重复） |
| `--skip-image-build` | false | 不自动构建容器镜像 |
| `--force-bootstrap` | false | 强制重新生成 bootstrap |
| `--skip-create-container` | false | 默认流程中跳过创建容器 |
| `--skip-verify` | false | 默认流程中跳过验证 |
| `EXTRA_PODMAN_OPTS` | （空） | 额外 podman run 选项 |
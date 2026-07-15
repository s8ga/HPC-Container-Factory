# assets 与离线资源指南

assets 负责把构建中最耗时、最依赖网络的部分前置到本地。

## Lock 两阶段契约

| 阶段 | 职责 |
|------|------|
| **assets** | 产出 / 更新 `spack.lock`（mirror 下载默认要求已有非空 lock；首次或缺 lock 时加 `--allow-concretize` 走 concretize+mirror） |
| **build / dockerfile** | **只读消费** lock；`BUILD_INPUT` 要求非空 lock；镜像内缺 lock 默认 `exit 1`，逃生口为 `--allow-reconcretize` |

禁止在 `run_mirror` 缺 lock 时静默升到 `run_all_pipeline`。

`SpackEnvironmentPlan` 对 assets 侧 repo/builtin/mirror 步骤是权威输入；image 侧
自定义 repo 仍由 per-env Dockerfile + `template_vars` 负责（`spack_image_repos`
partial 尚未接线）。`ProjectLayout` / `SharedMirrorStore` 由服务层使用，CLI 不暴露
自定义布局开关。

## 目录结构

```
assets/
  ├── spack-v1.1.1.tar.gz         ← Spack 源码包（版本随 env.yaml）
  ├── bootstrap-1.1.1/            ← Spack bootstrap 元数据与缓存
  └── spack-mirror/               ← 共享累积式源码镜像（所有 env 共用）
      └── .hpc_cf/                ← 编排元数据（不改变包树布局）
          ├── mirror.lock         ← 进程级写锁（fcntl）
          └── runs/<run-id>/      ← 每次下载的日志 + manifest.json
```

## 容器化缓存流程（推荐）

统一使用 `python -m hpc_cf assets` 入口。CLI 组装 `AssetsRequest`，
`AssetsService` / `hpc_cf.assets` 编排域逻辑。

### 前置条件

- Podman rootless 已安装
- 网络可访问 APT 源和 GitHub
- 已在 `spack-envs/<env>/spack-env-file/` 下准备好 `env.yaml` + `spack.yaml`
- `assets/spack-v<ver>.tar.gz` 已就位（`assets` profile 会校验）

### 使用

```bash
# 一键完整流程（会先跑 assets 校验 profile）
python -m hpc_cf assets --env cp2k_opensource-2025.2

# 分步
python -m hpc_cf assets --create-container
python -m hpc_cf assets --prepare-bootstrap
python -m hpc_cf assets --env cp2k_opensource-2025.2 --download-mirror
python -m hpc_cf assets --env cp2k_opensource-2025.2 --verify-mirror
python -m hpc_cf assets --env cp2k_opensource-2025.2 --status
```

### 模块架构

```
┌──────────────────────────────────────────────────────────┐
│  hpc_cf/cli.py → AssetsRequest → AssetsService            │
│  （CLI 不把 argparse.Namespace 传给 assets.py）             │
└────────────────────────┬─────────────────────────────────┘
                         │ RunnerPort.exec / run_ephemeral
                         ▼
┌──────────────────────────────────────────────────────────┐
│  hpc_cf/container.py (Podman RunnerPort)                  │
│  hpc_cf/execution.py (ProjectLayout, SharedMirrorStore)   │
└────────────────────────┬─────────────────────────────────┘
                         │ bash -lc <script>
                         ▼
┌──────────────────────────────────────────────────────────┐
│  hpc_cf/spack_ops.py  ← 消费 SpackEnvironmentPlan          │
│  bootstrap / repos / concretize / mirror create+verify    │
└──────────────────────────────────────────────────────────┘
```

**设计原则**：
- `containers/Dockerfile.mirror-builder` 是通用 Spack-only 镜像
- 系统包在运行时由 `spack_ops` 从 `EnvironmentSpec` 读取后安装
- 共享 `assets/spack-mirror` 保持累积布局；并发写通过 `SharedMirrorStore.exclusive_write` 串行化（**writers-only** flock：不与只读 bind-mount 读者互斥；等锁时约每 30s 打日志；优先本地盘，NFS flock 可能不可靠）
- 每次成功 mirror 会在 `.hpc_cf/runs/<id>/manifest.json` 记录 env、spack 版本、lock hash、统计
- **assets 产 lock，build 只读消费**：`--download-mirror` 缺 `spack.lock` 默认失败；初次生成用 `--allow-concretize`。镜像构建缺 lock 默认 `exit 1`，逃生口是 `build`/`dockerfile` 的 `--allow-reconcretize`

### 校验 profile（按动作选择）

`AssetsService` 在调用域逻辑前做**唯一**一次 preflight（`run_assets` 不再重复校验）。

| 命令 / 标志 | Profile | 是否要求大体积资产 |
|-------------|---------|-------------------|
| `dockerfile` / `validate --profile config` | config | 否 |
| `build` / `validate`（默认） | build-input | 是（tarball + manual_packages） |
| `assets --status` | **config** | 否（缺大资产不因此失败） |
| `assets --prepare-bootstrap` | assets | 是（bootstrap 输入） |
| `assets --download-mirror` | assets | 是（tarball；缺输入必失败） |
| `assets --verify-mirror` | assets | 是（lock + mirror） |
| `assets`（默认一键流程） | assets | 是 |

```bash
python -m hpc_cf validate --app-version <env> --profile assets --format json
python -m hpc_cf assets --env <env> --status          # config only
python -m hpc_cf assets --env <env> --download-mirror # assets inputs required
```

Mirror 注册 scope（`spack_mirror_scope`，默认 `site`）与自定义 repo 的
`repo_scope` **解耦**——image 侧 `repo_scope: env` 不会泄漏到
`spack mirror add --scope`。verify 事务在同一 mirror lock 下完成：
container verify → host symlink → 原子写 manifest；失败时写入
`status=failed` 摘要，不留下成功态 manifest。

### 子命令/动作标志说明

| 标志 | 需要 `--env` | 说明 |
|------|-------------|------|
| （无标志） | **是** | 一键完整流程：构建镜像 → 创建容器 → bootstrap → mirror → verify |
| `--create-container` | 否 | 构建镜像并创建/启动 reusable mirror worker container |
| `--prepare-bootstrap` | 否 | 生成 Spack bootstrap mirror（失败必须传播，不吞错） |
| `--download-mirror` | **是** | 下载源码 mirror（持锁 + 写 manifest；缺 lock 需加 `--allow-concretize`） |
| `--allow-concretize` | 配合 download / 一键流程 | 缺 `spack.lock` 时显式允许 concretize+mirror |
| `--verify-mirror` | **是** | 校验 mirror 完整性（与 download 同属 assets profile） |
| `--status` | **是** | 显示镜像、bootstrap、mirror、环境状态（config profile） |

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

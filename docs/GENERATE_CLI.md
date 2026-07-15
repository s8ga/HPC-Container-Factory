# HPC Container Factory CLI 使用说明

入口: `python -m hpc_cf`

架构边界：`cli.py`（argparse）→ `BuildRequest` / `AssetsRequest` →
`BuildService` / `AssetsService`。领域模块不接收 `argparse.Namespace`。

解析结果统一为 `ResolvedBuildInput`（配置目录与渲染模板路径可分离）：

```text
ResolvedBuildInput
├── environment_spec
├── environment_dir      # env.yaml / spack.yaml 所在目录
├── render_template      # 实际渲染的 Dockerfile.j2（可为共享模板）
└── compatibility_mode   # 无相邻 env.yaml 的 legacy 模板
```

## 命令总览

```bash
python -m hpc_cf <command> [options]
```

| 子命令 | 用途 |
|--------|------|
| `dockerfile` | 只生成 Dockerfile（config/template 校验） |
| `build` | 生成 Dockerfile 并构建 OCI 镜像（podman/docker） |
| `validate` | 静态预检；可用 `--profile` 选择深度 |
| `build-sif` | 从 OCI 镜像构建 Apptainer SIF |
| `pack-apptainer` | 打包本地 apptainer 为 makeself 自解压包 |
| `assets` | 准备离线资源（bootstrap + source mirror） |

## dockerfile

```bash
python -m hpc_cf dockerfile \
  --app-version <env-name> \
  --output Dockerfile
```

| 参数 | 说明 |
|------|------|
| `--app-version <name>` | 环境名（对应 `spack-envs/<name>/`）。不传值列出可用环境；`--env` 为别名 |
| `--template <path>` | 显式模板路径（覆盖自动选择；须存在） |
| `--output <path>` | 输出 Dockerfile 路径 |
| `--mirror` / `--no-mirror` | 启用 / 禁用离线 mirror 上下文 |
| `--build-only` | 只渲染 builder 阶段（模板支持时） |
| `--allow-reconcretize` | 允许在无非空 `spack.lock` 时渲染/构建（默认 fail-closed） |

`method: no_spack` 且无 per-env `Dockerfile.j2` 时，自动使用共享
`templates/Dockerfile.nospack.j2`。

> **ProjectLayout**：服务层可注入布局供测试使用；**CLI 不提供**更换项目根 /
> assets 路径的开关。运维始终使用仓库默认目录树。

## build

```bash
python -m hpc_cf build \
  --app-version <env-name> \
  --engine podman \
  --network-host
```

| 参数 | 说明 |
|------|------|
| `--engine <engine>` | **仅** `podman` / `docker`。Apptainer SIF 请用 `build-sif` |
| `--image <name>` | 输出镜像名（默认由环境目录名推断） |
| `--tag <tag>` | 输出镜像 tag（默认由环境目录名推断） |
| `--network-host` | 构建时加 `--network host` |
| `--build-arg KEY=VAL` | 传递 `--build-arg`（可重复） |
| `--build-opt OPT` | 额外 build 选项（可重复） |
| `--allow-reconcretize` | 缺 `spack.lock` 时允许镜像内 reconcretize（默认拒绝） |

命名规则（未传 `--image`/`--tag`）：从 `spack-envs/<name>/` 目录名按
`<app>_<variant>-<version>[-suffix]` 约定推断；`env.yaml` 中
`images.output_name` / `images.output_tag` 可覆盖。

## validate

```bash
python -m hpc_cf validate --app-version <env-name>
python -m hpc_cf validate --env <env-name> --profile config --format json
```

| 参数 | 说明 |
|------|------|
| `--app-version` / `--env` | 环境名。不传值列出可用环境 |
| `--template <path>` | 显式模板/目录；**不存在则失败**（含 StrictUndefined 渲染探测） |
| `--profile` | `config` / `template`（同 config）、`build-input`（默认）、`assets` |
| `--format` | `text`（默认）或 `json`（解析错误也保证合法 JSON findings） |

### Validation profile 与命令的对应关系

| 动作 | Profile | 大体积资产 |
|------|---------|-----------|
| `dockerfile` | config | 否 |
| `build` | build-input | 是（tarball + manual_packages 等） |
| `validate`（默认） | build-input | 是 |
| `validate --profile config` | config | 否 |
| `assets --status` | config | 否 |
| `assets --prepare-bootstrap` | assets | 是（bootstrap 输入） |
| `assets --download-mirror` | assets | 是 |
| `assets --verify-mirror` | assets | 是（lock + mirror） |

`EnvironmentSpec`（`schema_version: 1`）对未知键、非法类型、未知
`phases` / `repo_scope` **fail-closed**；`template_vars` 保持开放 mapping。
Jinja 渲染使用 `StrictUndefined`——模板引用未声明变量即失败。

## build-sif

将本地 OCI 镜像转换为 Apptainer SIF。相对 `--output` 路径在启动子进程前
解析为绝对路径（相对进程 cwd）。

```bash
python -m hpc_cf build-sif --app-version <env-name>
python -m hpc_cf build-sif --docker-image <name> --docker-tag <tag>
python -m hpc_cf build-sif --install-apptainer-only
```

| 参数 | 说明 |
|------|------|
| `--app-version <name>` | 环境名，自动推断镜像名/tag |
| `--docker-image` / `--docker-tag` | 显式 OCI 镜像 |
| `-o, --output <path>` | 输出 SIF（默认 `artifacts/<image>_<tag>.sif`） |
| `--install-apptainer-only` | 仅安装 apptainer |

详见 [BUILD_SIF.md](BUILD_SIF.md)。

## pack-apptainer

将本地 apptainer 打包为 makeself 自解压包。

```bash
python -m hpc_cf pack-apptainer
python -m hpc_cf pack-apptainer -o /path/to/apptainer.run --no-sha256
```

## assets

统一入口：`python -m hpc_cf assets`。CLI 组装 `AssetsRequest`，
`AssetsService` / `hpc_cf.assets` 编排域逻辑（**无 argparse**）。

```bash
# 一键完整流程
python -m hpc_cf assets --env <env-name>

# 分步
python -m hpc_cf assets --create-container
python -m hpc_cf assets --prepare-bootstrap
python -m hpc_cf assets --env <env-name> --download-mirror
python -m hpc_cf assets --env <env-name> --verify-mirror
python -m hpc_cf assets --env <env-name> --status
```

详述与校验 profile 见 [ASSETS_GUIDE.md](ASSETS_GUIDE.md)。

## 自动发现机制

`resolve_build_input` / `select_template` 顺序：

1. `spack-envs/<app-version>/Dockerfile.j2`（优先）
2. 若有 env.yaml 且 method 声明共享模板（如 no_spack）→ `templates/<default>`
3. `spack-envs/<app>_<app-version>/Dockerfile.j2`（拼接尝试）
4. `templates/Dockerfile-<...>.j2`（legacy；无 env.yaml → compatibility mode + warning）

```bash
python -m hpc_cf dockerfile --app-version <env-name>
```

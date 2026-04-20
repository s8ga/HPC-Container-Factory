# generate.py CLI 使用说明

入口: `generate.py`

## 命令总览

```bash
python generate.py <command> [options]
```

| 子命令 | 用途 |
|--------|------|
| `dockerfile` | 只生成 Dockerfile |
| `build` | 生成 Dockerfile 并构建镜像 |
| `build-sif` | 从 OCI 镜像构建 Apptainer SIF |
| `pack-apptainer` | 打包本地 apptainer 为 makeself 自解压包 |
| `assets` | 准备离线资源 (bootstrap + source mirror) |

## dockerfile

```bash
python generate.py dockerfile \
  --app-version cp2k-opensource-2025.2 \
  --output Dockerfile
```

| 参数 | 说明 |
|------|------|
| `--app-version <name>` | 环境名（对应 `spack-envs/<name>/`）。不传值列出可用环境 |
| `--template <path>` | 显式模板路径（覆盖 `--app-version` 自动选择） |
| `--output <path>` | 输出 Dockerfile 路径 |
| `--mirror` | 模板上下文启用离线 mirror |
| `--no-mirror` | 禁用 mirror |
| `--build-only` | 只渲染 builder 阶段 |

## build

```bash
python generate.py build \
  --app-version cp2k-rocm-2026.1-gfx942 \
  --engine podman \
  --network-host
```

| 参数 | 说明 |
|------|------|
| `--engine <engine>` | `podman` / `docker` / `apptainer` |
| `--image <name>` | 输出镜像名（默认自动推断） |
| `--tag <tag>` | 输出镜像 tag（默认自动推断） |
| `--network-host` | 构建时加 `--network host` |
| `--build-arg KEY=VAL` | 传递 `--build-arg`（可重复） |
| `--build-opt OPT` | 额外 build 选项（可重复） |

自动命名规则（未传 `--image`/`--tag` 时生效）：

| 环境类型 | 镜像名 | tag 示例 |
|----------|--------|---------|
| `cp2k-opensource-*` | `cp2k-opensource` | `2025.2`、`2025.2-force-avx512` |
| `cp2k-rocm-*` | `cp2k-rocm` | `2026.1-gfx942` |

## build-sif

将本地 OCI 镜像转换为 Apptainer SIF，支持交互式 MOTD。

```bash
# 从已构建的 OCI 镜像构建 SIF
python generate.py build-sif --app-version cp2k-opensource-2025.2-force-avx512

# 显式指定镜像
python generate.py build-sif --docker-image cp2k-opensource --docker-tag 2025.2

# 仅安装 apptainer
python generate.py build-sif --install-apptainer-only
```

| 参数 | 说明 |
|------|------|
| `--app-version <name>` | 环境名，自动推断镜像名/tag。不传值列出可用环境 |
| `--docker-image <name>` | 显式指定 OCI 镜像名 |
| `--docker-tag <tag>` | 显式指定 OCI 镜像 tag |
| `-o, --output <path>` | 输出 SIF 路径（默认 `artifacts/<image>_<tag>.sif`） |
| `--install-apptainer-only` | 仅安装 apptainer，不构建 SIF |

详细说明见 [BUILD_SIF.md](BUILD_SIF.md)。

## pack-apptainer

将本地 apptainer 打包为 makeself 自解压包（gzip 压缩，最大兼容性），便于分发到目标机器。

```bash
# 打包（默认最大压缩）
python generate.py pack-apptainer

# 指定输出路径
python generate.py pack-apptainer -o /path/to/apptainer.run

# 跳过 SHA256 校验（更快）
python generate.py pack-apptainer --no-sha256
```

| 参数 | 说明 |
|------|------|
| `-o, --output <path>` | 输出 `.run` 文件路径（默认 `artifacts/apptainer-<ver>-<arch>.run`） |
| `--no-sha256` | 跳过 SHA256 校验（打包更快） |

**目标机器上使用：**

```bash
mkdir ~/apptainer && cd ~/apptainer
bash apptainer-1.4.5-3.el8-x86_64.run     # 解压到当前目录
source apptainer-bundle/activate-apptainer.sh  # 激活
apptainer shell /path/to/image.sif        # 使用
```

## assets

```bash
# 一键完整流程
python generate.py assets --env cp2k-opensource-2025.2

# 分步执行
python generate.py assets --create-container
python generate.py assets --prepare-bootstrap
python generate.py assets --env cp2k-opensource-2025.2 --download-mirror
python generate.py assets --env cp2k-opensource-2025.2 --verify-mirror
python generate.py assets --env cp2k-opensource-2025.2 --status
```

| 参数 | 说明 |
|------|------|
| `--env <name>` | 环境名。不传值列出可用环境 |
| `--mirror-image <name>` | mirror builder 镜像名 |
| `--container-name <name>` | worker container 名称 |
| `--skip-image-build` | 跳过自动构建 mirror builder 镜像 |
| `--force-bootstrap` | 强制重建 bootstrap |
| `--podman-opt OPT` | 额外 podman 选项（可重复） |

## 自动发现机制

`generate.py` 通过以下顺序查找模板：

1. `spack-envs/<app-version>/Dockerfile.j2`（新布局，优先）
2. `spack-envs/<app>-<app-version>/Dockerfile.j2`（拼接尝试）
3. `templates/Dockerfile-<app>-<app-version>.j2`（legacy 回退）

`--app-version` 直接传 `spack-envs/` 下的目录名即可：

```bash
python generate.py dockerfile --app-version cp2k-opensource-2025.2
python generate.py dockerfile --app-version cp2k-opensource-2025.2-force-avx512
python generate.py dockerfile --app-version cp2k-rocm-2026.1-gfx942
```

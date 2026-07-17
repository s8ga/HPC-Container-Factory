# Quick Start

以 `cp2k_opensource-2026.2-force-avx512` 为例完成校验、资源准备、镜像和 SIF 构建。

## 1. 安装依赖

```bash
uv venv venv
uv pip install -r requirements.txt --python ./venv/bin/python
source ./activate.sh
podman info
```

普通 OCI 镜像构建可选择 Podman 或 Docker；`assets`/mirror 工作流当前依赖
Podman。SIF 构建还需要 Apptainer 或 Singularity。

## 2. 准备对应版本的 Spack 源码

每个环境使用 `spack-env-file/env.yaml` 中声明的版本。2026.2 环境使用
Spack 1.2.0：

```bash
mkdir -p assets
curl -fSL -o assets/spack-v1.2.0.tar.gz \
  https://github.com/spack/spack/releases/download/v1.2.0/spack-1.2.0.tar.gz
```

仓库中还存在使用 Spack 1.1.0 和 1.1.1 的环境；切换环境时应准备对应的
`assets/spack-v<version>.tar.gz`。

## 3. 校验并准备 assets

```bash
# 仅检查配置与模板，不要求大体积构建输入
python -m hpc_cf validate \
  --app-version cp2k_opensource-2026.2-force-avx512 \
  --profile config

# 首次准备；若 lock 缺失，显式允许 assets concretize
python -m hpc_cf assets \
  --env cp2k_opensource-2026.2-force-avx512 \
  --allow-concretize

# 检查完整 build-input
python -m hpc_cf validate \
  --app-version cp2k_opensource-2026.2-force-avx512
```

`assets` 会访问包源、Git 仓库和系统软件源。共享 source mirror 可减少后续下载，
但不代表所有构建步骤在任何环境下都完全离线。

## 4. 渲染并构建 OCI 镜像

```bash
python -m hpc_cf dockerfile \
  --app-version cp2k_opensource-2026.2-force-avx512 \
  --output Dockerfile

python -m hpc_cf build \
  --app-version cp2k_opensource-2026.2-force-avx512 \
  --engine podman \
  --network-host
```

`dockerfile` 和 `build` 都执行 `build-input` 校验并默认要求非空
`spack.lock`。只有在明确接受镜像内重新 concretize 时才使用
`--allow-reconcretize`。

## 5. 构建并冒烟检查 SIF

```bash
python -m hpc_cf build-sif \
  --app-version cp2k_opensource-2026.2-force-avx512

apptainer exec \
  artifacts/cp2k_opensource_2026.2-force-avx512.sif \
  cp2k.psmp --version
```

需要非交互确认本地 Apptainer 安装时可加 `--yes`。完整说明见
[docs/BUILD_SIF.md](docs/BUILD_SIF.md)。

## 查看可用环境

```bash
python -m hpc_cf dockerfile --app-version
```

CLI 当前发现 9 个环境，不会自动选择默认环境；需要显式传入环境名。

## 更多文档

- [完整 CLI 参考](docs/GENERATE_CLI.md)
- [assets 与 mirror](docs/ASSETS_GUIDE.md)
- [SIF 构建](docs/BUILD_SIF.md)
- [文档总览](docs/README.md)

# 快速开始

以下示例使用 Spack 1.2.0 的
`cp2k_opensource-2026.2-force-avx512`。顶层精简入口见
[../QUICKSTART.md](../QUICKSTART.md)。

## 1. 准备运行环境

```bash
uv venv venv
uv pip install -r requirements.txt --python ./venv/bin/python
source ./activate.sh
podman info
```

普通 `build` 支持 `--engine podman` 或 `--engine docker`；`assets` 工作流当前
使用 Podman。

## 2. 准备 Spack 1.2.0 与 assets

```bash
mkdir -p assets
curl -fSL -o assets/spack-v1.2.0.tar.gz \
  https://github.com/spack/spack/releases/download/v1.2.0/spack-1.2.0.tar.gz

python -m hpc_cf validate \
  --app-version cp2k_opensource-2026.2-force-avx512 \
  --profile config

python -m hpc_cf assets \
  --env cp2k_opensource-2026.2-force-avx512 \
  --allow-concretize
```

`--allow-concretize` 是缺少 `spack.lock` 时由 assets 生成 lock 的显式授权。
mirror 可以减少重复下载，但不要据此假设整个后续构建完全离线。

## 3. 校验并生成 Dockerfile

```bash
python -m hpc_cf validate \
  --app-version cp2k_opensource-2026.2-force-avx512

python -m hpc_cf dockerfile \
  --app-version cp2k_opensource-2026.2-force-avx512 \
  --output Dockerfile
```

`dockerfile` 与 `build` 都执行 `build-input` 校验；它们默认只读消费非空
`spack.lock`。`validate --profile config` 才是不要求完整构建资产的浅层检查。

## 4. 构建 OCI 镜像

```bash
python -m hpc_cf build \
  --app-version cp2k_opensource-2026.2-force-avx512 \
  --engine podman \
  --network-host
```

如明确接受镜像内重新 concretize，可为 `dockerfile` 或 `build` 加
`--allow-reconcretize`；常规发布构建应先由 assets 生成 lock。

## 5. 构建并检查 SIF

```bash
python -m hpc_cf build-sif \
  --app-version cp2k_opensource-2026.2-force-avx512

apptainer exec \
  artifacts/cp2k_opensource_2026.2-force-avx512.sif \
  cp2k.psmp --version
```

详见 [BUILD_SIF.md](BUILD_SIF.md)。

## 查看所有环境

```bash
python -m hpc_cf dockerfile --app-version
```

环境不会被默认选择；请显式传 `--app-version` 或对应命令的 `--env` 别名。

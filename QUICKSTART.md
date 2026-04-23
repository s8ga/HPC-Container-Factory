# Quick Start

30 秒完成从零到可运行的 SIF 容器镜像。

## 前置条件

```bash
# 1. Python 依赖
pip install -r requirements.txt

# 2. 需要 Podman 或 Docker
podman info >/dev/null 2>&1 || docker info >/dev/null 2>&1
```

## 六步流程

### Step 0 — 准备 Spack 源码（首次 clone 后必须）

```bash
# 下载 Spack v1.1.0 release tarball
mkdir -p assets
curl -fSL -o assets/spack-v1.1.0.tar.gz \
  https://github.com/spack/spack/releases/download/v1.1.0/spack-1.1.0.tar.gz

# 解压出 spack-src/（bootstrap 阶段需要）
tar -xzf assets/spack-v1.1.0.tar.gz -C assets/
mv assets/spack-1.1.0 assets/spack-src
```

> `assets/` 被 `.gitignore` 排除，每次从 GitHub 新 clone 后都需要执行此步骤。
> `spack-v1.1.0.tar.gz` 用于容器镜像构建（`Dockerfile.mirror-builder` COPY），
> `spack-src/` 用于宿主机上的 bootstrap 缓存生成。

### Step 1 — 激活环境

```bash
source ./activate.sh
```

> 自动激活 Python venv（如存在）并将本地 apptainer 加入 PATH。

### Step 2 — 准备离线资源（首次或更新依赖时）

**前置：确保 Step 0 已完成（`assets/spack-v1.1.0.tar.gz` 和 `assets/spack-src/` 存在）。**

```bash
python generate.py assets --env cp2k-opensource-2025.2
```

> 一键完成：构建 mirror builder 容器 → 下载 Spack bootstrap → 下载源码 mirror → 校验。
> 产出在 `assets/` 目录，支持完全离线构建。
> 详见 [离线资源指南](docs/ASSETS_GUIDE.md)。

### Step 3 — 构建容器镜像

```bash
python generate.py build --app-version cp2k-opensource-2025.2 --network-host
```

> 自动推断镜像名 `cp2k-opensource:2025.2`，使用 Podman/Docker 构建多阶段镜像。

### Step 4 — 转换为 SIF

```bash
python generate.py build-sif --app-version cp2k-opensource-2025.2
```

> 首次运行会提示安装 apptainer（非特权安装到 `tools/apptainer/`）。
> 产出：`artifacts/cp2k-opensource_2025.2.sif`

### Step 5 — 运行

```bash
apptainer shell artifacts/cp2k-opensource_2025.2.sif
```

> 进入容器后自动显示 MOTD（硬件信息、环境提示）。

## 可选：打包 Apptainer 分发到目标机器

```bash
python generate.py pack-apptainer
```

> 生成 `artifacts/apptainer-<version>-x86_64.run` 自解压包。

目标机器上：

```bash
mkdir ~/apptainer && cd ~/apptainer
bash apptainer-*.run                    # 解压到当前目录
source activate-apptainer.sh            # 激活
apptainer shell /path/to/image.sif      # 使用
```

## 所有可用环境

```bash
python generate.py build --app-version    # 列出可用环境
```

| `--app-version` | 说明 |
|---|---|
| `cp2k-opensource-2025.2` | CP2K 2025.2 开源 BLAS |
| `cp2k-opensource-2025.2-force-avx512` | 同上 + AVX512 强制 kernel |
| `cp2k-rocm-2026.1-gfx942` | CP2K 2026.1 ROCm GPU (gfx942) |

## 更多文档

- 完整 CLI 参考：[docs/GENERATE_CLI.md](docs/GENERATE_CLI.md)
- SIF 构建详解：[docs/BUILD_SIF.md](docs/BUILD_SIF.md)
- 架构总览：[docs/README.md](docs/README.md)

# 快速开始

5 步完成从 Dockerfile 到可运行的 SIF 容器。更精简的版本见顶层 [../QUICKSTART.md](../QUICKSTART.md)。

## 1. 环境准备

```bash
# 安装 Python 依赖
pip install -r requirements.txt

# 激活开发环境（自动加入本地 apptainer 到 PATH）
source ./activate.sh
```

## 2. 准备离线资源（首次）

```bash
# 一键完整流程：构建 builder → 下载 bootstrap → 下载 mirror → 校验
python -m hpc_cf assets --env cp2k_opensource-2025.2
```

> 此步需要网络。完成后 `assets/` 目录包含所有构建所需资源，后续构建可完全离线。
> 详见 [ASSETS_GUIDE.md](ASSETS_GUIDE.md)。

## 3. 构建容器镜像

```bash
# 默认环境（cp2k_opensource-2025.2）
python -m hpc_cf build --app-version cp2k_opensource-2025.2 --network-host

# ROCm GPU 版
python -m hpc_cf build --app-version cp2k_rocm-2026.1-gfx942 --network-host

# force-avx512 变体
python -m hpc_cf build --app-version cp2k_opensource-2025.2-force-avx512 --network-host
```

自动镜像命名：
- opensource → `cp2k_opensource:<version>`
- rocm → `cp2k_rocm:<version>-<gpu>`

## 4. 构建 SIF（Apptainer）

```bash
# 从本地 OCI 镜像构建 SIF
python -m hpc_cf build-sif --app-version cp2k_opensource-2025.2-force-avx512

# 仅安装 apptainer（不构建 SIF）
python -m hpc_cf build-sif --install-apptainer-only
```

详细说明见 [BUILD_SIF.md](BUILD_SIF.md)。

## 5. 打包 Apptainer（可选）

将本地 apptainer 打包为自解压包，分发到目标机器：

```bash
python -m hpc_cf pack-apptainer
# 产出: artifacts/apptainer-<version>-x86_64.run
```

目标机器上：

```bash
mkdir ~/apptainer && cd ~/apptainer
bash apptainer-*.run                    # 解压到当前目录
source activate-apptainer.sh            # 激活
apptainer shell /path/to/image.sif      # 使用
```

## 只生成 Dockerfile（不构建）

```bash
# 指定环境 + 输出路径
python -m hpc_cf dockerfile --app-version cp2k_opensource-2025.2 --output Dockerfile

# 列出所有可用环境
python -m hpc_cf dockerfile --app-version
```

## 查看可用环境

任何需要 `--app-version` 的命令，不传值即可列出：

```bash
python -m hpc_cf build --app-version
python -m hpc_cf build-sif --app-version
python -m hpc_cf assets --env
```
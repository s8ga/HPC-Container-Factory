# 快速开始

本页给出当前仓库下可以直接执行的最短路径。

## 1. 环境准备

在仓库根目录执行：

```bash
cd /home/shaojiehe/HPC-Container-Factory
source ./activate.sh
```

若虚拟环境不存在，可先安装依赖：

```bash
pip install pyyaml jinja2
```

## 2. 生成 Dockerfile（推荐）

当前默认参数已经对齐活跃模板，可直接生成：

```bash
python generate.py --output Dockerfile --dry-run
```

如果你希望显式指定模板，也可以：

```bash
python generate.py --template templates/Dockerfile-cp2k-opensource-2025.2.j2 --output Dockerfile --dry-run
```

## 3. 构建镜像

Docker：

```bash
docker build -f Dockerfile -t hpc-cp2k:latest .
```

Podman：

```bash
podman build -f Dockerfile -t hpc-cp2k:latest .
```

## 4. 准备离线缓存（容器化，推荐）

推荐统一入口（最简）：

```bash
python generate.py assets --env cp2k-opensource-2025.2
```

该命令会自动执行：

1. 构建 mirror builder 镜像
2. 创建/启动 mirror worker container
3. 准备 bootstrap cache
4. 重新 concretize（生成 spack.lock）
5. 下载 source mirror
6. 校验 mirror 完整性

如需分步执行：

```bash
python generate.py assets --create-container
python generate.py assets --prepare-bootstrap
python generate.py assets --env cp2k-opensource-2025.2 --concretize
python generate.py assets --env cp2k-opensource-2025.2 --download-mirror
python generate.py assets --env cp2k-opensource-2025.2 --verify-mirror
python generate.py assets --env cp2k-opensource-2025.2 --status
```

也可以直接调用底层脚本：

```bash
./scripts/build-mirror-in-container.sh image
./scripts/build-mirror-in-container.sh create-container
./scripts/prepare-bootstrap-cache.sh --create-container --use-container
./scripts/build-mirror-in-container.sh -e cp2k-opensource-2025.2 concretize
./scripts/build-mirror-in-container.sh -e cp2k-opensource-2025.2 mirror
./scripts/build-mirror-in-container.sh -e cp2k-opensource-2025.2 verify
```

> 每个 spack 环境通过 `spack-envs/<env>/streamline.sh` 的 CONFIG SECTION 声明自己的需求
> （包管理器、系统包列表、自定义 Spack 仓库等）。
> 详见 [assets 与离线资源指南](./ASSETS_GUIDE.md)。

## 5. 可选：离线镜像模式

如果本地已准备 assets/spack-mirror，可在生成时打开镜像选项：

```bash
python generate.py \
  --mirror \
  --output Dockerfile \
  --dry-run
```
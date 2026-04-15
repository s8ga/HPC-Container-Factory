# generate.py 使用说明

入口文件: ../generate.py

## 设计目标

新版 CLI 统一了三类工作:

- 生成 Dockerfile
- 构建镜像
- 准备离线资源 (bootstrap + source mirror + mirror worker container)

## 命令总览

```bash
python generate.py <command> [options]
```

可用 command:

- dockerfile: 只生成 Dockerfile
- build: 生成并构建镜像
- assets: 准备离线资源与 mirror 容器

## 1) dockerfile

```bash
python generate.py dockerfile \
  --app cp2k \
  --app-version rocm-2026.1-gfx942 \
  --template templates/Dockerfile-cp2k-rocm-2026.1-gfx942.j2 \
  --output Dockerfile.cp2k-rocm-2026.1-gfx942
```

常用参数:

- --template: 显式模板路径
- --app / --app-version: 自动选模板时使用
- --output: 输出 Dockerfile 路径
- --mirror: 模板上下文启用离线 mirror 标志
- --build-only: 模板支持时只渲染 builder 阶段

## 2) build

```bash
python generate.py build \
  --app-version rocm-2026.1-gfx942 \
  --template templates/Dockerfile-cp2k-rocm-2026.1-gfx942.j2 \
  --output Dockerfile.cp2k-rocm-2026.1-gfx942 \
  --engine podman \
  --image hpc-cp2k-rocm-infinityhub \
  --tag 2026.1 \
  --network-host
```

常用参数:

- --engine: podman / docker / apptainer
- --image / --tag: 目标镜像名
- --network-host: build 时加 --network host

默认命名规则（未传 --image/--tag 时自动生效）:

- opensource: `cp2k-opensource:<版本号>`，例如 `cp2k-opensource:2025.2`
- rocm: `cp2k-rocm:<版本号>-<gpu架构>`，例如 `cp2k-rocm:2026.1-gfx942`

## 3) assets (推荐)

### 一键完整流程（最简）

```bash
python generate.py assets --env cp2k-rocm-2026.1-gfx942
```

默认会执行:

1. 构建 mirror builder 镜像
2. 创建或启动 reusable mirror worker container
3. 准备 bootstrap cache
4. 下载 source mirror
5. 校验 mirror 完整性

### 分步执行

```bash
# 只创建 mirror worker container
python generate.py assets --create-container

# 只准备 bootstrap
python generate.py assets --prepare-bootstrap

# 只下载 mirror
python generate.py assets --env cp2k-rocm-2026.1-gfx942 --download-mirror

# 只校验 mirror
python generate.py assets --env cp2k-rocm-2026.1-gfx942 --verify-mirror

# 查看状态
python generate.py assets --env cp2k-rocm-2026.1-gfx942 --status
```

常用参数:

- --env: 对应 spack-envs/<env-name>
- --mirror-image: mirror builder 镜像名
- --container-name: worker container 名称
- --skip-image-build: 跳过自动构建 mirror builder 镜像
- --force-bootstrap: 强制重建 bootstrap 目录

## 兼容旧参数

旧命令仍可用（兼容模式）:

```bash
python generate.py --output Dockerfile --dry-run
python generate.py --build --image hpc-cp2k --tag latest
```

建议后续逐步迁移到子命令形式，阅读和自动化都更清晰。

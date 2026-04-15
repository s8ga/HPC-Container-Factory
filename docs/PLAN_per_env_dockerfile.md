# Plan: Per-Env 自包含 Dockerfile 架构重构

> 状态：**待审查**
> 日期：2026-04-16
> 目标：每个 spack-env 目录自包含 `Dockerfile.j2`，`streamline.sh` 的 CONFIG SECTION 作为 single-point-of-truth

---

## 1. 设计原则

```
1 个 env 目录 = 构建该 env 需要的一切

spack-envs/<env-name>/
  ├── spack.yaml          ← 构建什么（包定义）
  ├── streamline.sh       ← 怎么准备（mirror pipeline CONFIG）
  ├── Dockerfile.j2       ← 怎么构建（最终镜像模板，Jinja2 参数化）
  ├── spack.lock          ← concretize 产出
  └── repos/              ← (可选) 自定义 spack repo
```

**Single-Point-of-Truth**：`streamline.sh` 的 CONFIG SECTION 是唯一需要修改的地方。
`Dockerfile.j2` 中的 `{{ builder_base_image }}`、`{{ runtime_base_image }}` 等变量由 `generate.py` 从同一份 CONFIG 读取并注入。

---

## 2. 目标目录结构

```
spack-envs/
  cp2k-opensource-2025.2/
    spack.yaml
    spack.lock
    streamline.sh             ← CONFIG: BASE_IMAGE, PKG_MANAGER, SYSTEM_PKGS, CUSTOM_REPOS
    Dockerfile.j2             ← 自包含模板（从 templates/ 搬入）
    mirror-create.sh          ← (可选保留，已由 CUSTOM_REPOS 取代)

  cp2k-rocm-2026.1-gfx942/
    spack.yaml
    spack.lock
    streamline.sh             ← CONFIG: BASE_IMAGE="rocm/dev-ubuntu-24.04:7.2.1-complete"
    Dockerfile.j2             ← 自包含模板，FROM rocm 镜像
    repos/

containers/
  Dockerfile.mirror-builder   ← 参数化：ARG BASE_IMAGE，从 streamline.sh 读取

templates/                    ← 标记为 legacy，generate.py 兼容回退
  Dockerfile-base.j2          ← (legacy)
  Dockerfile-cp2k-opensource-2025.2.j2  ← (legacy, 搬入 env 后可删除)

configs/
  versions.yaml               ← 精简：删除 images 段，仅保留 spack version 等全局配置

scripts/
  build-mirror-in-container.sh  ← 从 streamline.sh 读取 BASE_IMAGE
  spack-common.sh               ← 不变

generate.py                   ← template 搜索优先 spack-envs/<env>/Dockerfile.j2
```

---

## 3. 分步实施计划

### Step 1: `streamline.sh` 增加 `BASE_IMAGE` 字段

**改动文件**：`spack-envs/cp2k-opensource-2025.2/streamline.sh`

在 CONFIG SECTION 中新增：

```bash
# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  CONFIG SECTION — Modify ONLY this section for new environments         ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

# Container base images (single source of truth)
# Used by:  Dockerfile.mirror-builder (concretize/mirror) + Dockerfile.j2 (final build)
BASE_IMAGE="debian:trixie"
RUNTIME_BASE_IMAGE="debian:trixie-slim"

# ... 其余 CONFIG 不变 ...
```

**影响范围**：仅 1 个文件，纯新增 2 行。

---

### Step 2: `Dockerfile.mirror-builder` 参数化

**改动文件**：`containers/Dockerfile.mirror-builder`

变更点：
- `FROM debian:trixie` → `ARG BASE_IMAGE=debian:trixie` + `FROM ${BASE_IMAGE}`
- APT mirror 的 `sed` 命令改为条件判断，非 Debian 系发行版跳过

```dockerfile
ARG BASE_IMAGE=debian:trixie
FROM ${BASE_IMAGE}

LABEL maintainer="s8ga" \
      description="Spack mirror builder — generates bootstrap and source mirrors in isolation"

# Configure APT mirrors (only for Debian-based images)
RUN if command -v apt-get >/dev/null 2>&1; then \
        sed -i 's|deb.debian.org|mirrors.ustc.edu.cn|g' /etc/apt/sources.list.d/*.sources 2>/dev/null || true && \
        sed -i 's|deb.debian.org|mirrors.ustc.edu.cn|g' /etc/apt/sources.list 2>/dev/null || true && \
        sed -i 's|security.debian.org|mirrors.ustc.edu.cn/debian-security|g' /etc/apt/sources.list 2>/dev/null || true; \
    fi

# 其余不变（通用 spack 安装逻辑）
# 注意：这里只装最小依赖（spack + curl + git + python）
# 完整的 SYSTEM_PKGS 由 streamline.sh 在容器内安装
```

同时精简系统包列表，只保留 spack bootstrap 所需的最小集：
- `bash`, `python3`, `git`, `curl`, `ca-certificates`, `environment-modules`
- 其余（`gfortran`, `cmake`, `build-essential` 等）由 `streamline.sh` 的 `SYSTEM_PKGS` 安装

**影响范围**：1 个文件。

---

### Step 3: `build-mirror-in-container.sh` 从 streamline.sh 读取 BASE_IMAGE

**改动文件**：`scripts/build-mirror-in-container.sh`

变更点：
- `cmd_image()` 从 `streamline.sh` 提取 `BASE_IMAGE`
- image tag 带上 env 名，避免不同 base image 冲突

```bash
cmd_image() {
    # 从 streamline.sh 提取 BASE_IMAGE
    local base_image
    base_image=$(grep '^BASE_IMAGE=' "${SPACK_ENV_DIR}/streamline.sh" 2>/dev/null \
                 | head -1 | sed 's/BASE_IMAGE="//;s/"//')
    base_image="${base_image:-debian:trixie}"

    local image_tag="${MIRROR_BUILDER_IMAGE}"

    info "Building mirror-builder image: ${image_tag}"
    info "Base image: ${base_image}"
    info "Dockerfile: ${DOCKERFILE}"

    ${PODMAN_CMD} build \
        --network=host \
        --build-arg BASE_IMAGE="${base_image}" \
        -t "${image_tag}" \
        -f "${DOCKERFILE}" \
        "${PROJECT_ROOT}"
}
```

同时更新 `cmd_create_container()` 中的镜像引用。

**影响范围**：1 个文件。

---

### Step 4: 将现有 template 搬入 env 目录

**操作**：

```bash
# opensource
cp templates/Dockerfile-cp2k-opensource-2025.2.j2 \
   spack-envs/cp2k-opensource-2025.2/Dockerfile.j2

# rocm
cp templates/Dockerfile-cp2k-rocm-2026.1-gfx942.j2 \
   spack-envs/cp2k-rocm-2026.1-gfx942/Dockerfile.j2
```

然后修改搬入的 `Dockerfile.j2`：

**对 opensource 的 Dockerfile.j2**：
- 去掉 `{% include "Dockerfile-base.j2" %}`
- 将 `Dockerfile-base.j2` 的内容直接内联到该文件中
- `FROM {{ builder_base_image }}` 中的变量由 generate.py 从 streamline.sh 注入

**对 rocm 的 Dockerfile.j2**：
- 已经是自包含的，不需要内联
- `ARG BUILDER_IMAGE="docker.io/rocm/dev-ubuntu-24.04:7.2.1-complete"` 改为使用 Jinja2 变量
  `FROM {{ builder_base_image }} AS builder`

**影响范围**：2 个新文件 + 手动调整内容。

---

### Step 5: `generate.py` 适配新 template 位置

**改动文件**：`generate.py`

变更点：

1. **`select_template()` 搜索路径优先 env 目录**：

```python
def select_template(app, app_version, explicit_template):
    if explicit_template:
        if not explicit_template.exists():
            raise FileNotFoundError(f"Specified template not found: {explicit_template}")
        return explicit_template

    # 优先: spack-envs/<env>/Dockerfile.j2
    # 尝试把 app_version 映射到 env 目录名
    env_candidates = _app_version_to_env_dirs(app, app_version)
    for env_dir in env_candidates:
        env_template = PROJECT_ROOT / "spack-envs" / env_dir / "Dockerfile.j2"
        if env_template.exists():
            return env_template

    # 回退: templates/Dockerfile-<app>-<version>.j2 (legacy 兼容)
    template_name = f"Dockerfile-{app}-{app_version}.j2"
    legacy_path = TEMPLATES_DIR / template_name
    if legacy_path.exists():
        return legacy_path

    raise FileNotFoundError(...)

def _app_version_to_env_dirs(app, app_version):
    """Map --app-version to possible env directory names."""
    # e.g. "opensource-2025.2" → ["cp2k-opensource-2025.2", "cp2k-2025.2-opensource"]
    # e.g. "rocm-2026.1-gfx942" → ["cp2k-rocm-2026.1-gfx942"]
    return [
        f"{app}-{app_version}",
    ]
```

2. **`build_context()` 从 streamline.sh 读取 base image**：

```python
def build_context(config, *, use_mirror, build_only, app_version, template_path):
    # 尝试从 env 的 streamline.sh 读取 BASE_IMAGE
    env_dir = template_path.parent if template_path else None
    builder_base, runtime_base = read_base_images_from_streamline(env_dir)

    # fallback 到 versions.yaml
    images = config.get("images", {})
    builder_base = builder_base or images.get("builder_base", "debian:trixie")
    runtime_base = runtime_base or images.get("runtime_base", "debian:trixie-slim")

    context = {
        "builder_base_image": builder_base,
        "runtime_base_image": runtime_base,
        ...
    }

def read_base_images_from_streamline(env_dir):
    """Parse BASE_IMAGE and RUNTIME_BASE_IMAGE from streamline.sh in env dir."""
    if not env_dir:
        return None, None
    streamline = env_dir / "streamline.sh"
    if not streamline.exists():
        return None, None
    # 简单 grep 提取
    builder = runtime = None
    for line in streamline.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("BASE_IMAGE="):
            builder = stripped.split("=", 1)[1].strip('"').strip("'")
        elif stripped.startswith("RUNTIME_BASE_IMAGE="):
            runtime = stripped.split("=", 1)[1].strip('"').strip("'")
    return builder, runtime
```

3. **`_extract_available_versions()` 同时扫描 env 目录**：

```python
def _extract_available_versions():
    versions = []
    # 扫描 spack-envs/*/Dockerfile.j2
    for env_dir in sorted((PROJECT_ROOT / "spack-envs").iterdir()):
        if env_dir.is_dir() and (env_dir / "Dockerfile.j2").exists():
            versions.append(env_dir.name)
    # 同时保留 legacy templates/ 扫描
    ...
    return versions
```

**影响范围**：1 个文件。

---

### Step 6: 精简 `configs/versions.yaml`

**改动文件**：`configs/versions.yaml`

删除 `images` 段（base image 现在从 streamline.sh 读取），保留 spack 全局配置：

```yaml
# HPC Container Build Configuration
# Base images: 已迁移到各 env 的 streamline.sh CONFIG SECTION

# Spack configuration
spack:
  version: "1.1.0"
  use_mirror: true
  mirror_path: "/opt/spack-mirror"

# Build settings
build:
  jobs: 4
  verbose: true
  cache: true
```

**影响范围**：1 个文件。

---

### Step 7: 为 rocm env 创建 `streamline.sh`

**新文件**：`spack-envs/cp2k-rocm-2026.1-gfx942/streamline.sh`

```bash
#!/bin/bash
set -euo pipefail

# ╔══ CONFIG ═══╗
BASE_IMAGE="rocm/dev-ubuntu-24.04:7.2.1-complete"
RUNTIME_BASE_IMAGE="rocm/dev-ubuntu-24.04:7.2.1"
SPACK_ENV_NAME="cp2k-env"
PKG_MANAGER="apt-get"
INSTALL_ARGS="install -y --no-install-recommends"
SYSTEM_PKGS="
    bash build-essential ca-certificates curl
    environment-modules gfortran git openssh-client pkg-config
    unzip vim wget rsync nano cmake file automake bzip2
    xxd xz-utils zstd ninja-build patch pkgconf
    libncurses-dev libssh-dev libssl-dev libtool-bin
    lsb-release python3 python3-dev python3-pip python3-venv
    zlib1g-dev
"
CUSTOM_REPOS=(
    "https://github.com/cp2k/cp2k.git|v2026.1|tools/spack/cp2k_dev_repo|cp2k_dev_repo"
)
# ╚══ END CONFIG ═══╝

source /work/scripts/spack-common.sh
# ... generic pipeline logic (与 opensource 相同) ...
```

**影响范围**：1 个新文件。

---

## 4. 改动汇总

| Step | 文件 | 改动类型 | 风险 |
|------|------|----------|------|
| 1 | `spack-envs/cp2k-opensource-2025.2/streamline.sh` | 新增 2 行 CONFIG | 🟢 低 |
| 2 | `containers/Dockerfile.mirror-builder` | 参数化 FROM | 🟡 中 |
| 3 | `scripts/build-mirror-in-container.sh` | 读取 BASE_IMAGE | 🟡 中 |
| 4 | `spack-envs/*/Dockerfile.j2` | 新建（从 templates/ 搬入+内联） | 🟡 中 |
| 5 | `generate.py` | template 搜索路径 + context 读取 | 🟡 中 |
| 6 | `configs/versions.yaml` | 删除 images 段 | 🟢 低 |
| 7 | `spack-envs/cp2k-rocm-2026.1-gfx942/streamline.sh` | 新建 | 🟢 低 |

---

## 5. 不变的部分

- `scripts/spack-common.sh` — 不变
- `spack-envs/*/spack.yaml` — 不变
- `spack-envs/*/spack.lock` — 不变
- `assets/` — 不变
- `templates/` — 保留作为 legacy 回退，不删除

---

## 6. 验证计划

每个 Step 完成后验证：

```
Step 1-3 完成后：
  ./scripts/build-mirror-in-container.sh -e cp2k-opensource-2025.2 concretize
  → 验证 concretize 仍正常工作

Step 4-5 完成后：
  python generate.py dockerfile --app-version opensource-2025.2
  → 验证 Dockerfile 生成正确，FROM 行使用了 streamline.sh 中的 BASE_IMAGE

Step 6 完成后：
  python generate.py dockerfile --app-version opensource-2025.2
  → 验证不再依赖 versions.yaml 的 images 段

全部完成后：
  ./scripts/build-mirror-in-container.sh -e cp2k-rocm-2026.1-gfx942 concretize
  → 验证 rocm env 使用 rocm base image 构建容器
```

---

## 7. 未来拉起新 env 的流程

```bash
# 1. 创建目录
mkdir -p spack-envs/cp2k-cuda-2027.1

# 2. 写 spack.yaml

# 3. 拷贝最近的 streamline.sh，只改 CONFIG
cp spack-envs/cp2k-opensource-2025.2/streamline.sh spack-envs/cp2k-cuda-2027.1/
# 改 BASE_IMAGE="nvidia/cuda:12.x-devel-ubuntu24.04"
# 改 SYSTEM_PKGS / CUSTOM_REPOS

# 4. 拷贝最近的 Dockerfile.j2，只改构建步骤
cp spack-envs/cp2k-opensource-2025.2/Dockerfile.j2 spack-envs/cp2k-cuda-2027.1/
# 改 FROM / 构建逻辑

# 5. 执行
./scripts/build-mirror-in-container.sh -e cp2k-cuda-2027.1 concretize
python generate.py build --app-version cuda-2027.1

# 完成！没有改过 spack-envs/ 之外的任何文件。
```

---

## 8. 开放问题（需确认）

| # | 问题 | 备注 |
|---|------|------|
| Q1 | `templates/` 旧文件是否在本次删除，还是标记 deprecated 后保留？ | 建议保留作为回退 |
| Q2 | `Dockerfile-base.j2` 的 `{% include %}` 模式彻底废弃？ | 是，每个 Dockerfile.j2 自包含 |
| Q3 | Step 的执行顺序是否按 1→7，还是可以并行某些？ | 1→2→3 串行，4/5/6 可并行，7 独立 |

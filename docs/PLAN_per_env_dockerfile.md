# Plan: Per-Env 自包含 Dockerfile 架构重构 (v3)

> 状态：**✅ 已实施并验证 (2026-04-16)**
> 日期：2026-04-16
> 目标：每个 spack-env 目录自包含 `Dockerfile.j2`，env 内的 `env.yaml` 作为 single-point-of-truth

---

## 1. 设计原则

```
1 个 env 目录 = 构建该 env 需要的一切

spack-envs/<env-name>/
  ├── env.yaml            ← Single source of truth (base images, pkg manager, system pkgs, ...)
  ├── spack.yaml          ← 构建什么（spack 包定义）
  ├── spack.lock          ← concretize 产出
  ├── streamline.sh       ← mirror pipeline（读取 env.yaml，运行时装系统包）
  ├── Dockerfile.j2       ← 最终镜像模板（{{ var }} 由 generate.py 从 env.yaml 注入）
  └── repos/              ← (可选) 自定义 spack repo
```

**核心策略：通用镜像 + 运行时配置**

| 组件 | 职责 | 变化频率 |
|------|------|----------|
| `Dockerfile.mirror-builder` | 通用 Spack-only 镜像（不含系统包） | 极低（仅随 Spack 版本更新） |
| `env.yaml` | 每个 env 的完整配置 | 中（新增 env 时创建） |
| `streamline.sh` | 运行时读取 env.yaml，装系统包，执行 pipeline | 低（改为通用后几乎不变） |

**为什么用 YAML 而不是把配置全放 streamline.sh？**

| YAML 配置文件 | streamline.sh bash CONFIG |
|--------------|---------------------------|
| `generate.py` 可直接 `yaml.safe_load()` | 需要 grep/sed 解析 bash 变量 |
| 结构化，支持嵌套（images, mirror_builder, template_vars） | 扁平，扩展需加变量 |
| `Dockerfile.j2` 和 `streamline.sh` 共享同一份配置 | 分散在 bash 和 yaml 两处 |
| 新增配置项只改 yaml，不动代码 | 新增变量需要改所有消费者 |

**为什么系统包在运行时安装而不是 build-time？**

| 构建时安装 (`--build-arg`) | 运行时安装（streamline.sh） |
|---------------------------|---------------------------|
| 切换 env 必须重建镜像 | **一个镜像所有 env 共用** |
| `cmd_image()` 依赖 env.yaml | `cmd_image()` 独立，一键 build |
| 缓存策略复杂（base + pkgs 两层） | 缓存极简：Spack 不变就命中 |
| — | 每次 run 多 ~30s（reusable container 可缓解） |

---

## 2. `env.yaml` 格式定义

每个 `spack-envs/<env>/env.yaml`：

```yaml
# spack-envs/cp2k-opensource-2025.2/env.yaml
# This file is the single source of truth for all build configuration.

# ── Container Images ──────────────────────────────────────────────────────
# Used by: Dockerfile.j2 ({{ builder_base_image }}, {{ runtime_base_image }})
#          Dockerfile.mirror-builder (ARG BASE_IMAGE)
images:
  builder: debian:trixie
  runtime: debian:trixie-slim

# ── Mirror Builder ────────────────────────────────────────────────────────
# Controls system package installation at runtime (inside streamline.sh).
# The mirror-builder image is a generic Spack-only container.
# These packages are installed by step_install_system_pkgs() on each run.
mirror_builder:
  # System packages to install in the mirror-builder container.
  # This REPLACES the hardcoded package list in Dockerfile.mirror-builder.
  system_pkgs:
    - bash
    - build-essential
    - ca-certificates
    - curl
    - environment-modules
    - gfortran
    - git
    - openssh-client
    - pkg-config
    - unzip
    - wget
    - cmake
    - file
    - automake
    - bzip2
    - xxd
    - xz-utils
    - zstd
    - ninja-build
    - patch
    - patchelf
    - pkgconf
    - libncurses-dev
    - libssh-dev
    - libssl-dev
    - libtool-bin
    - python3
    - python3-dev
    - python3-pip
    - python3-venv
    - zlib1g-dev
  # Shell oneliner to configure package mirrors — runs as-is via eval.
  # No distro detection needed. Write whatever works for your base image.
  pkg_mirror_setup: "sed -i 's|deb.debian.org|mirrors.ustc.edu.cn|g; s|security.debian.org|mirrors.ustc.edu.cn/debian-security|g' /etc/apt/sources.list.d/*.sources /etc/apt/sources.list 2>/dev/null || true"
  # Shell command to install packages (system_pkgs are appended automatically)
  pkg_install_cmd: "apt-get update -qq && apt-get install -y --no-install-recommends"

# ── Spack Environment ─────────────────────────────────────────────────────
# Used by: streamline.sh
spack:
  env_name: cp2k-env
  # Custom repos to clone and register
  custom_repos:
    - url: https://github.com/cp2k/cp2k.git
      branch: support/v2025.2
      sparse_path: tools/spack/cp2k_dev_repo
      namespace: cp2k_dev_repo

# ── Template Variables ────────────────────────────────────────────────────
# Additional variables passed to Dockerfile.j2 rendering.
# These become {{ key }} in the Jinja2 template.
template_vars: {}
#  amdgpu_targets: gfx942
#  cp2k_branch: v2026.1
```

**rocm 的 env.yaml 示例**：

```yaml
# spack-envs/cp2k-rocm-2026.1-gfx942/env.yaml
images:
  builder: rocm/dev-ubuntu-24.04:7.2.1-complete
  runtime: rocm/dev-ubuntu-24.04:7.2.1

mirror_builder:
  system_pkgs:
    - bash
    - build-essential
    - ca-certificates
    - curl
    - environment-modules
    - gfortran
    - git
    - openssh-client
    - openssh-server
    - pkg-config
    - unzip
    - vim
    - wget
    - rsync
    - nano
    - cmake
    - file
    - automake
    - bzip2
    - xxd
    - xz-utils
    - zstd
    - ninja-build
    - patch
    - pkgconf
    - libncurses-dev
    - libssh-dev
    - libssl-dev
    - libtool-bin
    - lsb-release
    - python3
    - python3-dev
    - python3-pip
    - python3-venv
    - zlib1g-dev
  # Shell oneliner for Ubuntu mirror setup
  pkg_mirror_setup: "sed -i 's|//archive.ubuntu.com|//mirrors.ustc.edu.cn|g; s|//security.ubuntu.com|//mirrors.ustc.edu.cn|g' /etc/apt/sources.list.d/*.sources /etc/apt/sources.list 2>/dev/null || true"
  pkg_install_cmd: "apt-get update -qq && apt-get install -y --no-install-recommends"

spack:
  env_name: cp2k-env
  custom_repos:
    - url: https://github.com/cp2k/cp2k.git
      branch: v2026.1
      sparse_path: tools/spack/cp2k_dev_repo
      namespace: cp2k_dev_repo

template_vars:
  amdgpu_targets: gfx942
  cp2k_branch: v2026.1
  ucx_branch: v1.19.0
  ucc_branch: v1.5.1
  ompi_branch: v5.0.8
```

---

## 3. 数据流

```
env.yaml (single source of truth)
  │
  ├──→ generate.py ──→ build_context() ──→ Dockerfile.j2 渲染
  │     读取: images.builder, images.runtime, template_vars.*
  │
  ├──→ build-mirror-in-container.sh ──→ podman build (通用镜像)
  │     读取: images.builder → podman run 时通过 BASE_IMAGE 选择
  │
  └──→ streamline.sh ──→ spack pipeline (concretize/mirror)
        运行时读取: mirror_builder.system_pkgs, mirror_builder.pkg_mirror_setup,
                    mirror_builder.pkg_install_cmd, spack.env_name, spack.custom_repos
```

**一份配置文件，三个消费者，零重复。**
**一个通用镜像，所有 env 共用，永远不需要重建。**

---

## 4. 分步实施计划

### Step 1: 创建 `env.yaml` 文件

**新文件**：

- `spack-envs/cp2k-opensource-2025.2/env.yaml`
- `spack-envs/cp2k-rocm-2026.1-gfx942/env.yaml`

内容如上面第 2 节所示。

**从现有代码提取**：
- `images.builder` / `images.runtime` ← 来自 `configs/versions.yaml` 的 `images` 段 + rocm template 的硬编码
- `mirror_builder.system_pkgs` ← 来自 `containers/Dockerfile.mirror-builder` 的 apt-get 列表
- `spack.*` ← 来自 `streamline.sh` 的 CONFIG SECTION

**影响范围**：2 个新文件。

---

### Step 2: `Dockerfile.mirror-builder` → 通用 Spack-only 镜像

**改动文件**：`containers/Dockerfile.mirror-builder`

变为**通用镜像**：只装 Spack，不含任何系统包或 APT mirror 配置。所有 env 共用同一个镜像。

```dockerfile
ARG BASE_IMAGE=debian:trixie
FROM ${BASE_IMAGE}

LABEL maintainer="s8ga" \
      description="Spack mirror builder — generic Spack-only image (shared by all envs)"

# ── Spack installation (rarely changes → maximum cache hit) ───────────────

COPY assets/spack-v1.1.0.tar.gz /tmp/spack.tar.gz

ENV SPACK_ROOT=/opt/spack
ENV PATH="${SPACK_ROOT}/bin:${PATH}"

RUN mkdir -p ${SPACK_ROOT} && \
    tar -axf /tmp/spack.tar.gz --strip-components=1 -C ${SPACK_ROOT} && \
    rm /tmp/spack.tar.gz && \
    . ${SPACK_ROOT}/share/spack/setup-env.sh && \
    spack --version && \
    chmod -R a+w ${SPACK_ROOT}/var/spack && \
    chmod a+w ${SPACK_ROOT}/__spack_path_placeholder__ 2>/dev/null || true

RUN chmod -R a+w ${SPACK_ROOT}

# Pre-clone the spack-packages repo to avoid re-cloning on every container run.
RUN . ${SPACK_ROOT}/share/spack/setup-env.sh && \
    spack info zlib >/dev/null 2>&1 && \
    echo "✅ spack-packages repo pre-cloned"

RUN mkdir -p /work
WORKDIR /work

RUN printf '#!/bin/bash\n\
. /opt/spack/share/spack/setup-env.sh\n\
exec "$@"\n' > /opt/entrypoint.sh && chmod +x /opt/entrypoint.sh

ENTRYPOINT ["/opt/entrypoint.sh"]
```

**关键变化**：
- 删除全部系统包安装（`apt-get install` 等）
- 删除 APT mirror 配置（移到 `streamline.sh` 运行时）
- 删除 `ARG SYSTEM_PKGS` / `ARG APT_MIRROR` build-arg
- 保留 `ARG BASE_IMAGE`（不同 env 可用不同 base image）
- **一个镜像所有 env 共用**，只要 Spack 版本不变就命中缓存

**影响范围**：1 个文件。

---

### Step 3: `build-mirror-in-container.sh` 简化

**改动文件**：`scripts/build-mirror-in-container.sh`

`cmd_image()` 不再需要读 env.yaml 或传 `--build-arg`——镜像与 env 无关：

```bash
cmd_image() {
    info "Building generic mirror-builder image: ${MIRROR_BUILDER_IMAGE}"
    info "Dockerfile: ${DOCKERFILE}"
    info "Context: ${PROJECT_ROOT}"
    echo ""

    if [[ ! -f "${DOCKERFILE}" ]]; then
        error "Dockerfile not found: ${DOCKERFILE}"
        return 1
    fi

    ${PODMAN_CMD} build \
        --network=host \
        -t "${MIRROR_BUILDER_IMAGE}" \
        -f "${DOCKERFILE}" \
        "${PROJECT_ROOT}"

    ok "Image built: ${MIRROR_BUILDER_IMAGE}"
    ${PODMAN_CMD} images "${MIRROR_BUILDER_IMAGE}" --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}\t{{.CreatedAt}}"
}
```

**`run_in_container()` 也不需要改**——它只 `podman run` 通用镜像，env 差异完全由 `streamline.sh` 运行时处理。

**`cmd_concretize()` / `cmd_mirror()` 中的 `run_in_container` 调用也不变**：

```bash
run_in_container "ENV_NAME=${ENV_NAME} MIRROR_DIR=/work/assets/spack-mirror bash /work/spack-envs/${ENV_NAME}/streamline.sh concretize"
```

`streamline.sh` 在容器内从 `env.yaml` 读取配置，自行安装系统包。

**关键变化**：
- `cmd_image()` 完全不依赖 env——任何人 `./scripts/build-mirror-in-container.sh image` 一键 build
- 不再有 `_read_env_yaml()` / `--build-arg SYSTEM_PKGS` / `--build-arg APT_MIRROR`
- 所有 env 配置通过 `streamline.sh` 运行时消费 `env.yaml`

**影响范围**：1 个文件（简化，`cmd_image()` 删除 ~30 行，`_read_env_yaml()` 不再需要）。

---

### Step 4: `streamline.sh` → env.yaml 消费者 + 运行时系统包安装

**改动文件**：`spack-envs/cp2k-opensource-2025.2/streamline.sh`

`streamline.sh` 不再维护自己的 CONFIG SECTION，改为从 `env.yaml` 读取，并在运行时安装系统包：

```bash
#!/bin/bash
# ============================================================================
# streamline.sh — Generic per-env pipeline
#
# Runs INSIDE the mirror-builder container.
# /work is bind-mounted to the project root.
#
# All configuration is read from spack-envs/<env>/env.yaml.
# To adapt for a new environment, modify env.yaml — not this file.
# ============================================================================

set -euo pipefail

ENV_NAME="${ENV_NAME:?ENV_NAME not set}"
MIRROR_DIR="${MIRROR_DIR:-/work/assets/spack-mirror}"
ENV_DIR="/work/spack-envs/${ENV_NAME}"
MODE="${1:?Usage: streamline.sh <concretize|mirror|all|verify>}"

# ── Read configuration from env.yaml ──────────────────────────────────────
ENV_YAML="${ENV_DIR}/env.yaml"
if [[ ! -f "${ENV_YAML}" ]]; then
    echo "[ERROR] env.yaml not found: ${ENV_YAML}" >&2
    exit 1
fi

# Single Python call: parse env.yaml → export all needed bash variables
eval "$(python3 -c "
import yaml, sys
with open('${ENV_YAML}') as f:
    d = yaml.safe_load(f)

# spack section
spack = d.get('spack', {})
print(f\"SPACK_ENV_NAME='{spack.get('env_name', 'cp2k-env')}'\")

# mirror_builder section
mb = d.get('mirror_builder', {})
pkgs = mb.get('system_pkgs', [])
print(f\"SYSTEM_PKGS='{' '.join(pkgs)}'\")
# pkg_mirror_setup and pkg_install_cmd are raw shell strings — eval'd directly
import shlex
pkg_mirror_setup = mb.get('pkg_mirror_setup', '')
pkg_cmd          = mb.get('pkg_install_cmd', '')
print(f"PKG_MIRROR_SETUP={shlex.quote(pkg_mirror_setup)}")
print(f\"PKG_INSTALL_CMD={shlex.quote(pkg_cmd)}\")

# custom repos
repos = spack.get('custom_repos', [])
for i, r in enumerate(repos):
    url = r['url']
    branch = r['branch']
    sparse = r.get('sparse_path', '')
    ns = r.get('namespace', '')
    print(f\"CUSTOM_REPO_${i}='{url}|${branch}|${sparse}|${ns}'\")
print(f\"CUSTOM_REPO_COUNT=${len(repos)}\")
")"

# Reconstruct CUSTOM_REPOS array from indexed variables
CUSTOM_REPOS=()
for ((i=0; i<${CUSTOM_REPO_COUNT:-0}; i++)); do
    CUSTOM_REPOS+=("$(eval echo \"\${CUSTOM_REPO_${i}}\")")
done

# ── Source common utilities ────────────────────────────────────────────────
source /work/scripts/spack-common.sh

# ============================================================================
# Step: Configure mirrors + Install system packages (runtime)
# ============================================================================
step_install_system_pkgs() {
    # Configure package mirrors — just eval the oneliner from env.yaml
    if [[ -n "${PKG_MIRROR_SETUP:-}" ]]; then
        _sc_info "Configuring package mirrors..."
        eval "${PKG_MIRROR_SETUP}"
    fi

    # Install system packages — just eval the command + pkg list from env.yaml
    if [[ -n "${SYSTEM_PKGS:-}" && -n "${PKG_INSTALL_CMD:-}" ]]; then
        _sc_info "Installing system packages..."
        eval "${PKG_INSTALL_CMD} ${SYSTEM_PKGS}"
        _sc_ok "System packages installed"
    else
        _sc_info "No system packages declared — skipping"
    fi
}

# ============================================================================
# Step: Register custom Spack repos
# ============================================================================
step_register_repos() {
    if [[ ${#CUSTOM_REPOS[@]} -eq 0 ]]; then
        _sc_info "No custom repos configured — skipping"
        return 0
    fi

    for repo_entry in "${CUSTOM_REPOS[@]}"; do
        IFS='|' read -r git_url branch sparse_path namespace <<< "${repo_entry}"
        # ... (same sparse clone + register logic as current)
    done
}

# ... step_find(), step_concretize() — unchanged ...

# ============================================================================
# Main dispatch
# ============================================================================
case "${MODE}" in
    concretize)
        step_install_system_pkgs   # ← runtime: apt-get install from env.yaml
        spack_bootstrap
        step_register_repos
        step_find
        step_concretize
        ;;
    mirror)
        spack_bootstrap
        step_register_repos
        mirror_create "${ENV_DIR}" "${MIRROR_DIR}"
        ;;
    all)
        step_install_system_pkgs   # ← runtime: apt-get install from env.yaml
        spack_bootstrap
        step_register_repos
        step_find
        step_concretize
        mirror_create "${ENV_DIR}" "${MIRROR_DIR}"
        ;;
    verify)
        spack_bootstrap
        step_register_repos
        mirror_verify "${ENV_DIR}" "${MIRROR_DIR}"
        ;;
esac
```

**关键变化**：
- 删除整个 CONFIG SECTION — 配置完全来自 `env.yaml`
- **单次 `python3` 调用** 解析 env.yaml → 导出所有 bash 变量（避免多次 `$(python3 ...)` 的启动开销）
- `step_install_system_pkgs()` 极简：`eval "${PKG_MIRROR_SETUP}"` + `eval "${PKG_INSTALL_CMD} ${SYSTEM_PKGS}"` — 零条件逻辑
- 镜像配置和包安装命令都是 **YAML 中的字符串**，shell 直接 eval — 通用化设计
- `CUSTOM_REPOS` 数组从 `CUSTOM_REPO_*` 索引变量重建
- `step_register_repos()` 逻辑不变，只是数据源从硬编码数组变为 env.yaml

**影响范围**：1 个文件（重构，功能不变）。

---

### Step 5: 将现有 template 搬入 env 目录

**操作**：

```bash
# opensource
cp templates/Dockerfile-cp2k-opensource-2025.2.j2 \
   spack-envs/cp2k-opensource-2025.2/Dockerfile.j2

# rocm
cp templates/Dockerfile-cp2k-rocm-2026.1-gfx942.j2 \
   spack-envs/cp2k-rocm-2026.1-gfx942/Dockerfile.j2
```

**对 opensource 的 Dockerfile.j2**：
- 去掉 `{% include "Dockerfile-base.j2" %}`
- 将 `Dockerfile-base.j2` 的内容直接内联
- `FROM {{ builder_base_image }}` 不变（generate.py 从 env.yaml 注入）

**对 rocm 的 Dockerfile.j2**：
- 已是自包含的
- 将硬编码的 `ARG BUILDER_IMAGE="docker.io/rocm/..."` 改为 `FROM {{ builder_base_image }}`

**影响范围**：2 个新文件。

---

### Step 6: `generate.py` 适配新架构

**改动文件**：`generate.py`

变更点：

#### 6a. `select_template()` 搜索优先 env 目录

```python
def select_template(app, app_version, explicit_template):
    if explicit_template:
        if not explicit_template.exists():
            raise FileNotFoundError(...)
        return explicit_template

    # 优先: spack-envs/<app>-<app-version>/Dockerfile.j2
    env_dir = PROJECT_ROOT / "spack-envs" / f"{app}-{app_version}"
    env_template = env_dir / "Dockerfile.j2"
    if env_template.exists():
        return env_template

    # 回退: templates/Dockerfile-<app>-<app-version>.j2 (legacy)
    template_name = f"Dockerfile-{app}-{app_version}.j2"
    legacy_path = TEMPLATES_DIR / template_name
    if legacy_path.exists():
        return legacy_path

    raise FileNotFoundError(...)
```

#### 6b. `build_context()` 从 env.yaml 读取

```python
def load_env_yaml(template_path):
    """Load env.yaml from the same directory as the template (if inside spack-envs/)."""
    if not template_path:
        return {}
    env_yaml = template_path.parent / "env.yaml"
    if not env_yaml.exists():
        return {}
    with env_yaml.open() as f:
        return yaml.safe_load(f)

def build_context(config, *, use_mirror, build_only, app_version, template_path):
    env_config = load_env_yaml(template_path)

    # env.yaml images 段优先，versions.yaml images 段 fallback
    env_images = env_config.get("images", {})
    cfg_images = config.get("images", {})
    builder_base = env_images.get("builder", cfg_images.get("builder_base", "debian:trixie"))
    runtime_base = env_images.get("runtime", cfg_images.get("runtime_base", "debian:trixie-slim"))
    default_image_name, default_image_tag = infer_image_defaults(app_version, template_path)

    context = {
        "timestamp": datetime.now().isoformat(),
        "generated_with": "HPC Dockerfile Generator",
        "builder_base_image": builder_base,
        "runtime_base_image": runtime_base,
        "use_mirror": use_mirror,
        "build_only": build_only,
        "default_image_name": default_image_name,
        "default_image_tag": default_image_tag,
        # 注入 env.yaml 中的 template_vars 作为顶层变量
        **env_config.get("template_vars", {}),
        # 注入全局 config
        **config,
    }
    return context
```

#### 6c. `_extract_available_versions()` 扫描 env 目录

```python
def _extract_available_versions():
    versions = []
    seen = set()

    # 扫描 spack-envs/*/Dockerfile.j2 (新布局)
    spack_envs = PROJECT_ROOT / "spack-envs"
    if spack_envs.exists():
        for env_dir in sorted(spack_envs.iterdir()):
            if env_dir.is_dir() and (env_dir / "Dockerfile.j2").exists():
                name = env_dir.name
                if name not in seen:
                    versions.append(name)
                    seen.add(name)

    # 扫描 templates/ (legacy 布局)
    for f in sorted(TEMPLATES_DIR.glob("Dockerfile-*.j2")):
        if f.name == "Dockerfile-base.j2":
            continue
        stem = f.name[len("Dockerfile-"):-len(".j2")]
        if stem not in seen:
            versions.append(stem)
            seen.add(stem)

    return versions
```

**影响范围**：1 个文件。

---

### Step 7: 精简 `configs/versions.yaml`

**改动文件**：`configs/versions.yaml`

删除 `images` 段和 `applications` 段（已迁移到各 env 的 `env.yaml`），仅保留全局配置：

```yaml
# HPC Container Build Configuration
# Per-env configuration has moved to spack-envs/<env>/env.yaml
# This file only contains global (cross-env) settings.

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

## 5. 改动汇总

| Step | 文件 | 改动类型 | 风险 |
|------|------|----------|------|
| 1 | `spack-envs/*/env.yaml` | **新建** (2 个文件) | 🟢 低 |
| 2 | `containers/Dockerfile.mirror-builder` | 精简为通用 Spack-only 镜像 | 🟢 低 |
| 3 | `scripts/build-mirror-in-container.sh` | 简化 `cmd_image()`（不传 `--build-arg`） | 🟢 低 |
| 4 | `spack-envs/*/streamline.sh` | 重构：从 env.yaml 读取 + 运行时装包 | 🟡 中 |
| 5 | `spack-envs/*/Dockerfile.j2` | **新建** (从 templates/ 搬入+内联) | 🟡 中 |
| 6 | `generate.py` | template 搜索 + context 读取 | 🟡 中 |
| 7 | `configs/versions.yaml` | 精简 | 🟢 低 |

---

## 6. 不变的部分

- `scripts/spack-common.sh` — 不变
- `spack-envs/*/spack.yaml` — 不变
- `spack-envs/*/spack.lock` — 不变
- `assets/` — 不变
- `templates/` — 保留作为 legacy 回退

---

## 7. 验证计划

```
Step 1-2 完成后（通用镜像 + env.yaml）：
  ./scripts/build-mirror-in-container.sh image
  → 验证通用镜像一键 build，不依赖任何 env
  → 检查镜像大小：应比当前小（无系统包）

Step 1-4 完成后（streamline 运行时消费 env.yaml）：
  ./scripts/build-mirror-in-container.sh -e cp2k-opensource-2025.2 concretize
  → 验证容器内 streamline.sh 正确读取 env.yaml
  → 验证运行时 apt-get install 系统包成功
  → 验证 concretize 正常产出 spack.lock

Step 5-6 完成后（template 搬迁 + generate.py）：
  python generate.py dockerfile --app-version cp2k-opensource-2025.2
  → 验证 Dockerfile 生成正确，FROM 行来自 env.yaml
  → 验证 template_vars 正确注入

全部完成后（rocm env）：
  ./scripts/build-mirror-in-container.sh -e cp2k-rocm-2026.1-gfx942 concretize
  → 验证使用同一通用镜像（无需重建）
  → 验证 rocm 系统包在运行时正确安装
```

---

## 8. 未来拉起新 env 的流程

```bash
# 1. 创建目录
mkdir -p spack-envs/cp2k-cuda-2027.1

# 2. 写 spack.yaml（spack 包定义）

# 3. 写 env.yaml（唯一配置文件）
#    → 拷贝最近的 env.yaml，改 images / mirror_builder.system_pkgs / spack.custom_repos / template_vars
cp spack-envs/cp2k-opensource-2025.2/env.yaml spack-envs/cp2k-cuda-2027.1/
# 改 images.builder: "nvidia/cuda:12.x-devel-ubuntu24.04"
# 改 mirror_builder.system_pkgs: 加 cuda 相关包
# 加 template_vars: cuda_version: "12.x"

# 4. 写 Dockerfile.j2（最终构建模板）
#    → 拷贝最近的 Dockerfile.j2，改构建步骤
cp spack-envs/cp2k-opensource-2025.2/Dockerfile.j2 spack-envs/cp2k-cuda-2027.1/

# 5. 执行
./scripts/build-mirror-in-container.sh -e cp2k-cuda-2027.1 concretize
python generate.py build --app-version cp2k-cuda-2027.1

# 完成！没有改过 spack-envs/ 之外的任何文件。
```

---

## 9. 与 v1/v2 计划的关键差异

| 项目 | v1 计划 | v2 计划 | v3 计划（本版） |
|------|---------|---------|----------------|
| 配置位置 | `streamline.sh` bash CONFIG | `env.yaml` | `env.yaml` |
| `generate.py` 读取 | grep bash 变量 | `yaml.safe_load()` | `yaml.safe_load()` |
| mirror-builder 镜像 | 硬编码系统包 | `--build-arg SYSTEM_PKGS` | **通用 Spack-only，无系统包** |
| 系统包安装时机 | 构建时 | 构建时 | **运行时（streamline.sh）** |
| 切换 env 是否重建镜像 | — | 是 | **否** |
| Dockerfile.j2 变量来源 | `configs/versions.yaml` | `env.yaml` images 段 | `env.yaml` images 段 |
| 新增 env 改几处 | 改 streamline.sh CONFIG | 改 env.yaml | 改 env.yaml |

---

## 10. 开放问题（需确认）

| # | 问题 | 建议 |
|---|------|------|
| Q1 | `streamline.sh` 是否完全删除 CONFIG SECTION？ | 建议完全删除，强制使用 env.yaml |
| Q2 | `pkg_mirror_setup` / `pkg_install_cmd` 用 eval 安全吗？ | YAML 由项目维护者控制（非用户输入），安全性可接受 |
| Q3 | 通用镜像不含系统包，reusable container 场景下每次都要重装？ | reusable container 只装一次（容器状态持久化）；`--rm` 模式每次装 ~30s |
| Q4 | `templates/` 旧文件何时删除？ | 建议 v3 稳定后再删，期间 generate.py 兼容回退 |
| Q5 | 通用镜像是否需要 `ARG BASE_IMAGE`？ | 保留，允许非 debian base（如 rocm），但默认 `debian:trixie` |

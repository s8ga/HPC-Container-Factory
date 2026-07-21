# 新建 Spack 环境指南

本文档说明如何在 HPC-Container-Factory 中创建一个新的 Spack 环境并集成到构建系统中。

---

## 8 步流程

### Step 1: 复制现有环境

从最接近的现有环境复制，减少遗漏：

```bash
# 例：基于 opensource 环境创建新变体
cp -r spack-envs/cp2k_opensource-2025.2 spack-envs/<new-env-name>
```

> **命名规则**：目录名即 `--app-version` 的值。`hpc_cf` 直接用这个名字查找模板和推断镜像名/tag。
>
> 推荐格式：`<app>_<variant>-<version>[-<suffix>]`（应用名与变体之间使用下划线）
> 例：`cp2k_opensource-2025.2-force-avx512`

### Step 2: 修改 `spack-env-file/env.yaml`

`env.yaml` 是 single source of truth，由 `EnvironmentSpec` 解析（`schema_version: 1`）。
未知顶层键、非法类型、未知 `phases` / `repo_scope` 会 **fail-closed**；
缺失 `schema_version` 时兼容按 v1 读取并警告迁移。修改以下段落：

```yaml
schema_version: 1
method: spack                  # 或 no_spack

# ── Container Images ──
images:
  # Prefer tag@sha256:… pins for reproducible FROM (floating tags drift).
  # Example authority pins: cp2k_opensource-2026.2-force-avx512.
  # Refresh: podman pull <tag> && podman image inspect <tag> --format '{{.Digest}}'
  builder: debian:trixie        # 构建阶段基础镜像（可写 debian:trixie@sha256:…）
  runtime: debian:trixie-slim   # 运行阶段基础镜像

# ── Mirror Builder ──
mirror_builder:
  system_pkgs: [...]            # 容器内需要的系统包
  pkg_mirror_setup: "..."       # APT 源配置（shell oneliner）
  pkg_install_cmd: "..."        # 包安装命令

# ── Spack Environment ──
spack:
  version: "1.1.1"
  env_name: cp2k-env            # 注入模板 {{ spack_env_name }}，勿在 Dockerfile 硬编码
  custom_repos: [...]           # 自定义 Spack 仓库（可设 phases: assets|image|both）

# ── Template Variables ──
template_vars: {}               # 注入 Dockerfile.j2（StrictUndefined：缺变量即失败）
```

**关键点**：
- `images.builder` / `images.runtime` → `{{ builder_base_image }}` / `{{ runtime_base_image }}`
- **基础镜像 digest pin**：浮动 tag 会随上游滚动；权威环境应写成
  `debian:trixie@sha256:<digest>`（CP2K opensource force-avx512 的 2026.1 / 2026.2 已示范）。
  刷新：`podman pull debian:trixie && podman image inspect debian:trixie --format '{{.Digest}}'`
- `spack.env_name` → `SpackEnvironmentPlan` → 模板与 assets 共用
- `mirror_builder.system_pkgs` → 容器运行时安装（不是 bake 进镜像）
- `template_vars` → 如 `cp2k_branch`、`amdgpu_targets`
- `custom_repos`：
  - **git**: 有 `url` → sparse clone + register
  - **local**: 有 `path` → 直接 register（相对 `spack-env-file/`）
  - **phases**: `both`（默认）/ `assets` / `image`（例如仅镜像构建用的 AVX512 override）
  - 若同一 git repo 的 branch/url 也出现在 `template_vars`（如 `cp2k_branch`），
    **短期双写必须同步**；ABACUS opensource 与 CP2K force-avx512（2026.1 /
    2026.2）的 image 注册已走 `spack_image_repos` partial
    （`custom_repos[].image_path`）；其他应用仍可能手写 `spack repo add`
  - CP2K force-avx512：sparse clone 仍用 `force_avx512_repo_path`；
    `custom_repos[].image_path` 必须等于
    `/opt/s8ga-spack-packages/{{ force_avx512_repo_path }}`（由
    `scripts/check-dual-write.py` 守卫，避免 `image_path` 死配置）

### Step 3: 修改 `spack-env-file/spack.yaml`

修改 Spack 包定义：版本号、variant、编译器约束、外部包声明等。

### Step 4: 修改 `Dockerfile.j2`

Dockerfile.j2 在 `spack-envs/<env>/Dockerfile.j2`。公共 Spack 步骤应通过
`templates/partials/` include，不要复制粘贴完整 bootstrap 块。通常需要修改：

- 顶部注释中的路径引用
- 应用层构建 / regtest / ROCm 等 per-env 特有步骤
- 若引入了新的 `template_vars`，在模板中使用 `{{ var_name }}`

**禁止**在模板中硬编码 `spack env create cp2k-env`（或其它固定名）——始终使用
`{{ spack_env_name }}`（由 plan 注入）。

如果新环境与源环境的构建流程完全一致，可以只改 `env.yaml` / `spack.yaml`。

### Step 5: 删除 `spack.lock`（随后由 assets 显式重新生成）

**spack.lock 不可复用**——它是根据源环境的 `spack.yaml` + 编译器信息 + 平台信息求解的具体依赖图，直接复用会导致安装失败。

两阶段契约：

1. **assets** 负责 concretize 并写出 `spack.lock`（以及共享 mirror）
2. **build / dockerfile** 默认**只读消费**已有 lock；缺 lock 时镜像构建 fail-closed（`exit 1`），除非显式 `--allow-reconcretize`

```bash
rm spack-envs/<new-env-name>/spack-env-file/spack.lock
```

### Step 6: 清理 `repos/` （如有自定义包）

如果新环境需要自定义 Spack 包（patch、variant 等），将修改放在 `repos/` 目录下：

```
spack-env-file/
  └── repos/
      └── packages/
          ├── elpa/
          │   ├── package.py          ← 自定义 package.py
          │   └── some.patch          ← 源码 patch
          └── fftw/
              └── package.py
```

并在 `env.yaml` 的 `custom_repos` 中注册（local 类型）。

如果不需要自定义包，可以删除 `repos/` 或保留空目录。

### Step 7: 恢复原环境（如果是从现有环境复制的）

确保原环境没有被修改：

```bash
git diff spack-envs/<original-env>/
```

确认只有新目录下的文件有变更。

### Step 8: Concretize + 构建验证

```bash
# 激活环境
source activate.sh

# config 校验不要求大体积资产，也不要求 lock
python -m hpc_cf validate --app-version <new-env-name> --profile config

# concretize + 下载 mirror；缺 lock 时必须显式 --allow-concretize
python -m hpc_cf assets --env <new-env-name> --create-container
python -m hpc_cf assets --env <new-env-name> --download-mirror --allow-concretize

# build-input 校验、dockerfile 和 build 都要求完整构建输入及非空 lock
python -m hpc_cf validate --app-version <new-env-name> --format text
python -m hpc_cf dockerfile --app-version <new-env-name> --output /tmp/test.Dockerfile
python -m hpc_cf build --app-version <new-env-name>
```

如果只是临时调试并明确接受镜像内重新 concretize，可给 `dockerfile` 或
`build` 加 `--allow-reconcretize`；这不是常规 lock 生成流程。

---

## hpc_cf 自动发现机制

### 模板查找顺序

`select_template(app_version, explicit_template=None, *, app="")`:

1. 如果传了 `--template` → 直接使用
2. `spack-envs/<app-version>/Dockerfile.j2` → **优先**（通常传完整目录名）
3. `spack-envs/<app>_<app-version>/Dockerfile.j2` → 仅当显式传了 legacy `app` 时
4. `templates/Dockerfile-<app>-<app-version>.j2` → legacy 回退

`--app-version` / `--env` 直接传 `spack-envs/` 下的目录名即可。

### 镜像名 / Tag 推断

`resolve_output_image_tag(template_path)` — 自动从目录名推导，可选 env.yaml 覆盖：

**默认行为（约定推导）**：从目录名按 `-` 拆分，第一个以数字开头的段作为版本边界。

| 目录名 | 镜像名 | tag |
|--------|--------|-----|
| `cp2k_opensource-2025.2` | `cp2k-opensource` | `2025.2` |
| `cp2k_opensource-2025.2-force-avx512` | `cp2k-opensource` | `2025.2-force-avx512` |
| `cp2k_mkl-2025.2-experimental` | `cp2k-mkl` | `2025.2-experimental` |
| `cp2k_rocm-2026.1-gfx942` | `cp2k-rocm` | `2026.1-gfx942` |

新增变体时无需修改任何代码，只要目录名遵循
`<app>_<variant>-<version>[-<suffix>]` 约定即可自动工作。CLI 不提供
`--app` 参数；环境名通过 `--app-version`（或支持时的 `--env` 别名）传入。

**env.yaml 覆盖**（可选）：如果需要自定义镜像名或 tag，在 `env.yaml` 中添加：

```yaml
images:
  builder: debian:trixie
  runtime: debian:trixie-slim
  output_name: my-custom-image    # 可选，覆盖自动推导
  output_tag: custom-tag          # 可选，覆盖自动推导
```

### env.yaml 加载

优先使用 `load_environment_spec` / `EnvironmentSpec`（`schema_version: 1`）。

查找顺序（`find_env_yaml`）：

1. `spack-envs/<env>/spack-env-file/env.yaml` → **优先**
2. `spack-envs/<env>/env.yaml` → 回退

遗留的 `load_env_yaml()` 仍返回 dict，但会打弃用日志。

### Assets 发现

`_extract_available_versions()`:

基于 `list_available_envs()`（有 `env.yaml` 的 `spack-envs/` 目录），并追加 legacy
`templates/Dockerfile-*.j2` 的 stem。与仅扫描 `Dockerfile.j2` 不同：包含无 per-env
Dockerfile 的 `no_spack` 环境。

---

## 完整派生示例：force-avx512

以下是从 `cp2k_opensource-2025.2` 派生 `cp2k_opensource-2025.2-force-avx512` 的实际操作步骤。

### 1. 复制环境

```bash
cp -r spack-envs/cp2k_opensource-2025.2 spack-envs/cp2k_opensource-2025.2-force-avx512
```

### 2. 修改 `spack-env-file/spack.yaml`

在 `elpa` 和 `fftw` 的 require 中添加 AVX512 variant：

```yaml
  elpa:
    - +force_all_x86_kernel
  fftw:
    - +force_avx512
```

### 3. 添加自定义 Spack 包

在 `repos/packages/` 下添加修改后的 `package.py` 和 patch 文件：

```
spack-env-file/repos/packages/
  ├── elpa/
  │   ├── package.py                ← 添加了 +force_all_x86_kernel variant
  │   └── force_all_x86_kernel.patch
  └── fftw/
      └── package.py                ← 添加了 +force_avx512 variant
```

### 4. 在 `env.yaml` 中注册 local repo

```yaml
spack:
  custom_repos:
    - url: https://github.com/cp2k/cp2k.git
      branch: support/v2025.2
      sparse_path: tools/spack/cp2k_dev_repo
      namespace: cp2k_dev_repo
    - path: repos           # ← local repo，注册在 git repo 之后，优先级更高
      namespace: cp2k-env
```

`hpc_cf assets` 会先获取这些 repo，再创建 named environment、更新 pinned
builtin，最后按列表顺序将 custom repos 注册到该 environment scope。这样后注册的
local repo 才能稳定覆盖 git repo 和 `repos.builtin.commit` 指定的 builtin。
Dockerfile 中的手工 `spack repo add` 必须使用相同的 environment scope。

### 5. 删除 spack.lock

```bash
rm spack-envs/cp2k_opensource-2025.2-force-avx512/spack-env-file/spack.lock
```

### 6. 验证

```bash
source activate.sh
python -m hpc_cf validate \
  --app-version cp2k_opensource-2025.2-force-avx512 \
  --profile config
python -m hpc_cf assets \
  --env cp2k_opensource-2025.2-force-avx512 \
  --allow-concretize
python -m hpc_cf dockerfile --app-version cp2k_opensource-2025.2-force-avx512 --output /tmp/test.Dockerfile
# 默认镜像名/tag: cp2k_opensource:2025.2-force-avx512
```

---

## 注意事项

| 注意项 | 说明 |
|--------|------|
| **spack.lock 不可复用** | lock 包含具体平台/编译器约束，必须重新 concretize |
| **env.yaml 驱动** | 差异完全由 `env.yaml` 驱动，代码路径统一 |
| **Dockerfile.j2 路径引用** | 如果 Dockerfile 中硬编码了环境路径，需要同步修改 |
| **custom repo 优先级** | custom repos 必须在 `repo update builtin` 后注册到 environment scope；同一 scope 内后注册的 local repo 优先，可覆盖 pinned builtin 和 git repo |
| **patch sha256** | Spack `patch()` 会在 concretize 时记录 patch 文件的 sha256。修改 patch 后需要 `spack concretize -f` |
| **容器 HOME 隔离** | 容器运行时 `HOME=/tmp/home`，Spack 用户配置不会跨 env 污染 |
| **通用镜像** | `hpc-mirror-builder` 是所有 env 共用的通用 Spack-only 镜像，新环境不需要单独构建 mirror builder |

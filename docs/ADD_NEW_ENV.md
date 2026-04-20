# 新建 Spack 环境指南

本文档说明如何在 HPC-Container-Factory 中创建一个新的 Spack 环境并集成到构建系统中。

---

## 8 步流程

### Step 1: 复制现有环境

从最接近的现有环境复制，减少遗漏：

```bash
# 例：基于 opensource 环境创建新变体
cp -r spack-envs/cp2k-opensource-2025.2 spack-envs/<new-env-name>
```

> **命名规则**：目录名即 `--app-version` 的值。`generate.py` 直接用这个名字查找模板和推断镜像名/tag。
>
> 推荐格式：`<app>-<variant>-<version>[-<suffix>]`
> 例：`cp2k-opensource-2025.2-force-avx512`

### Step 2: 修改 `spack-env-file/env.yaml`

`env.yaml` 是 single source of truth，修改以下段落：

```yaml
# ── Container Images ──
images:
  builder: debian:trixie        # 构建阶段基础镜像
  runtime: debian:trixie-slim   # 运行阶段基础镜像

# ── Mirror Builder ──
mirror_builder:
  system_pkgs: [...]            # 容器内需要的系统包
  pkg_mirror_setup: "..."       # APT 源配置（shell oneliner）
  pkg_install_cmd: "..."        # 包安装命令

# ── Spack Environment ──
spack:
  env_name: cp2k-env            # Spack 环境名（通常不需要改）
  custom_repos: [...]           # 自定义 Spack 仓库

# ── Template Variables ──
template_vars: {}               # 注入 Dockerfile.j2 的额外变量
```

**关键点**：
- `images.builder` 和 `images.runtime` → 传入 `Dockerfile.j2` 的 `{{ builder_base_image }}` / `{{ runtime_base_image }}`
- `mirror_builder.system_pkgs` → 容器运行时安装的系统包（不是 bake 进镜像）
- `template_vars` → 传给 Jinja2 的自定义变量（如 `amdgpu_targets: gfx942`）
- `custom_repos` 支持两种类型：
  - **git**: 有 `url` 字段 → sparse clone + register
  - **local**: 有 `path` 字段 → 直接 register（path 相对于 `spack-env-file/`，注册优先级高于 git repo）

### Step 3: 修改 `spack-env-file/spack.yaml`

修改 Spack 包定义：版本号、variant、编译器约束、外部包声明等。

### Step 4: 修改 `Dockerfile.j2`

Dockerfile.j2 在 `spack-envs/<env>/Dockerfile.j2`。通常需要修改：

- 顶部的注释中的路径引用
- 如果引入了新的 `template_vars`，在模板中使用 `{{ var_name }}`

如果新环境与源环境的构建流程完全一致，可以不修改 `Dockerfile.j2`。

### Step 5: 删除 `spack.lock`

**spack.lock 不可复用**——它是根据源环境的 `spack.yaml` + 编译器信息 + 平台信息求解的具体依赖图，直接复用会导致安装失败。

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

# 查看新环境是否被自动发现
python generate.py assets --env

# concretize + 下载 mirror（在容器内）
python generate.py assets --env <new-env-name> --create-container
python generate.py assets --env <new-env-name> --download-mirror

# 生成 Dockerfile 验证
python generate.py dockerfile --app-version <new-env-name> --output /tmp/test.Dockerfile
```

---

## generate.py 自动发现机制

### 模板查找顺序

`select_template(app, app_version, explicit_template)`:

1. 如果传了 `--template` → 直接使用
2. `spack-envs/<app-version>/Dockerfile.j2` → **优先**
3. `spack-envs/<app>-<app-version>/Dockerfile.j2` → 拼接尝试
4. `templates/Dockerfile-<app>-<app-version>.j2` → legacy 回退

`--app-version` 直接传 `spack-envs/` 下的目录名即可。

### 镜像名 / Tag 推断

`infer_image_defaults(app_version, template_path)`:

从目录名解析（去掉 `cp2k-opensource-` 或 `cp2k-rocm-` 前缀）：

| 目录名 | 镜像名 | tag |
|--------|--------|-----|
| `cp2k-opensource-2025.2` | `cp2k-opensource` | `2025.2` |
| `cp2k-opensource-2025.2-force-avx512` | `cp2k-opensource` | `2025.2-force-avx512` |
| `cp2k-rocm-2026.1-gfx942` | `cp2k-rocm` | `2026.1-gfx942` |

对于 ROCm 环境，如果模板中包含 GPU arch 信息，会自动检测并用于 tag。

### env.yaml 加载

`load_env_yaml(template_path)`:

1. `spack-envs/<env>/spack-env-file/env.yaml` → **优先**（当前布局）
2. `spack-envs/<env>/env.yaml` → 回退

### Assets 发现

`_extract_available_versions()`:

扫描 `spack-envs/*/Dockerfile.j2`，所有包含 `Dockerfile.j2` 的目录名即为可用环境。

---

## 完整派生示例：force-avx512

以下是从 `cp2k-opensource-2025.2` 派生 `cp2k-opensource-2025.2-force-avx512` 的实际操作步骤。

### 1. 复制环境

```bash
cp -r spack-envs/cp2k-opensource-2025.2 spack-envs/cp2k-opensource-2025.2-force-avx512
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

### 5. 删除 spack.lock

```bash
rm spack-envs/cp2k-opensource-2025.2-force-avx512/spack-env-file/spack.lock
```

### 6. 验证

```bash
source activate.sh
python generate.py dockerfile --app-version cp2k-opensource-2025.2-force-avx512 --output /tmp/test.Dockerfile
# 确认输出: cp2k-opensource:2025.2-force-avx512
```

---

## 注意事项

| 注意项 | 说明 |
|--------|------|
| **spack.lock 不可复用** | lock 包含具体平台/编译器约束，必须重新 concretize |
| **streamline.sh 不需修改** | 当前所有 `streamline.sh` 内容相同（~15 行入口），差异由 `env.yaml` 驱动 |
| **Dockerfile.j2 路径引用** | 如果 Dockerfile 中硬编码了环境路径，需要同步修改 |
| **local repo 优先级** | `env.yaml` 中 `custom_repos` 的 local repo 注册在 git repo 之后，优先级更高（可覆盖 builtin 和 git repo 的 package.py） |
| **patch sha256** | Spack `patch()` 会在 concretize 时记录 patch 文件的 sha256。修改 patch 后需要 `spack concretize -f` |
| **容器 HOME 隔离** | 容器运行时 `HOME=/tmp/home`，Spack 用户配置不会跨 env 污染 |
| **通用镜像** | `hpc-mirror-builder` 是所有 env 共用的通用 Spack-only 镜像，新环境不需要单独构建 mirror builder |

# PLAN: Streamline.sh 统一 + HOME 隔离 + External 策略优化

**日期**: 2026-04-16
**目标**: 
1. 隔离容器 HOME，根治跨 env 污染（repos.yaml / packages.yaml 持久化问题）
2. 修改 external 包策略：mirror builder 只做 `compiler find`，不做 `external find`
3. 将两个 env 的 `streamline.sh` 统一为 config-driven 入口（~15行），所有逻辑移入 `scripts/spack-common.sh`

---

## 前置分析

### 污染根因

```
podman run --rm --userns=keep-id -v $PWD:/work:Z hpc-mirror-builder
                                          ↑
容器内 HOME=/work（bind mount 到宿主机项目根目录）
                                          │
spack 用户配置写入 ~/.spack/ = /work/.spack/
  ├── repos.yaml      ← spack repo add（跨 env 累积）
  ├── packages.yaml   ← spack external find（跨 env 累积）
  └── bootstrap.yaml  ← spack bootstrap add
                                          │
容器退出 (--rm) 但 /work/.spack/*.yaml 持久化在宿主机！
                                          │
下一个 env 的容器 → 读到上一个 env 的残留配置 → concretize 失败
```

**三层问题叠加**：
1. **HOME 泄漏**：`--userns=keep-id` 让 `HOME=/work`（bind mount），spack 写入持久化
2. **awk bug**：`step_clean_stale_repos()` 用 `awk '{print $1}'` 取 `[+]` 而非 namespace → 清理从未生效（已修复为 `$2`）
3. **external find 污染**：`spack external find --all --not-buildable` 写入 `packages.yaml` 的 `buildable:false` 在 env 间冲突

### 锁文件分析（opensource cp2k-2025.2）

从 `spack.lock` 的 113 个 concrete_specs 中：

| 类型 | 数量 | 说明 |
|------|------|------|
| External（系统包） | 15 | `gcc`, `cmake`, `python`, `binutils`, `perl`, `autoconf`, `automake`, `libtool`, `m4`, `gmake`, `ninja`, `pkgconf`, `git`, `zlib`, `glibc` |
| Spack 构建 | 98 | 所有 HPC 库（openmpi, openblas, cp2k, sirius, hdf5, ...） |

**关键发现**：
- 15 个 external 包中只有 `glibc` 不可替代（系统 C 库）
- 其余 14 个完全可以由 Spack 构建，用系统版只是为了加速
- mirror builder 的职责是**下载源码**，不是编译。它根本不需要知道系统有哪些包

### 正确的分层设计

```
mirror builder 容器（只下载源码）
  ├── HOME=/tmp/home              ← 隔离！不写入 bind mount
  ├── spack compiler find         → 只发现编译器（concretize 需要架构信息）
  ├── 不运行 external find        → 所有包都从源码构建（mirror 下载全部源码）
  ├── spack concretize            → 生成完整依赖图
  └── spack mirror create         → 下载所有包源码（包括 cmake/python 等）

build 容器（实际编译）
  ├── spack compiler find         → 发现实际编译器
  ├── spack external find --all   → 发现 base image 的系统包（ROCm GPU 库等）
  ├── spack install               → 优先用系统包加速，fallback 到源码
  └── spack gc -y                 → 清理不需要的
```

**为什么 mirror builder 不需要 external find**：
- `buildable:false` 的包（如 ROCm GPU 库）已在 `spack.yaml` 中声明 → concretize 知道不需要源码
- 通用工具（cmake/python/binutils）的源码很小（~几十MB）→ 全部下载无性能损失
- 去掉 external find 后 → 无 `packages.yaml` 污染 → 彻底消除跨 env 冲突

### ROCm 必须的 external 包（Spack 无法构建的 GPU 库）

从 `spack.yaml` 中已有的声明，在 **build 容器**（Dockerfile.j2）中通过 `spack external find` 发现：
```
hip, hipblas, hipfft, rocfft, rocblas, rocsolver, hsa-rocr-dev, llvm-amdgpu
```
这些是 AMD 预编译闭源库，**必须保持 `buildable:false`**（在 spack.yaml 中声明）。

### 两个 streamline.sh 差异

```bash
diff spack-envs/cp2k-opensource-2025.2/streamline.sh spack-envs/cp2k-rocm-2026.1-gfx942/streamline.sh
```
**结论**：逻辑 100% 一致，唯一差异是 debug logging 级别。

---

## 执行步骤

### Step 1: 隔离容器 HOME（根治污染）

**文件**: `scripts/build-mirror-in-container.sh`

**改动**: 在 `run_in_container()` 中添加 `-e HOME=/tmp/home`：

```bash
# 当前
run_in_container() {
    local cmd="$1"
    warn_proxy_requirement_if_needed
    ${PODMAN_CMD} run --rm \
        ${EXTRA_PODMAN_OPTS} \
        --network=host \
        --userns=keep-id \
        -v "${PROJECT_ROOT}:/work:Z" \
        "${MIRROR_BUILDER_IMAGE}" \
        bash -c "${cmd}"
}

# 修改后
run_in_container() {
    local cmd="$1"
    warn_proxy_requirement_if_needed
    ${PODMAN_CMD} run --rm \
        ${EXTRA_PODMAN_OPTS} \
        --network=host \
        --userns=keep-id \
        -e HOME=/tmp/home \
        -v "${PROJECT_ROOT}:/work:Z" \
        "${MIRROR_BUILDER_IMAGE}" \
        bash -c "mkdir -p /tmp/home && ${cmd}"
}
```

**原因**: 
- spack 用户配置写入 `$HOME/.spack/` = `/tmp/home/.spack/`（容器内部，销毁时自动清理）
- bind mount 的 `/work/.spack/` 不再被写入 → 零污染
- **同时清理**宿主机的 `/work/.spack/repos.yaml` 和 `/work/.spack/packages.yaml`（历史残留）

**测试**:
```bash
# 1. 清理宿主机残留
echo "packages: {}" > .spack/packages.yaml
echo "repos: {}" > .spack/repos.yaml

# 2. 运行后验证 /work/.spack/ 未被修改
python generate.py assets --env cp2k-opensource-2025.2
cat .spack/repos.yaml     # 预期：repos: {}
cat .spack/packages.yaml  # 预期：packages: {}
```

---

### Step 2: 修改 external find 策略

**文件**: 当前在两个 `streamline.sh` 的 `step_find()` 中（Step 4 统一后变为 `scripts/spack-common.sh`）

**改动**:
```bash
# 旧版（问题源）
step_find() {
    spack compiler find
    spack external find --all --not-buildable   # ← 污染 packages.yaml
}

# 新版（修复）
step_find() {
    spack compiler find
    # 不运行 external find — mirror builder 只需要编译器信息
    # external 发现由 build 容器在 install 时做（Dockerfile.j2 中的 spack external find）
}
```

**原因**: 
- mirror builder 的唯一工作是 `concretize + mirror create`（下载源码）
- `spack.yaml` 中的 `buildable:false` 声明（如 ROCm GPU 库）在 concretize 时已生效 → 这些包不会被加入 mirror
- 通用工具的源码很小，全部下载无性能损失
- 去掉 external find → 无 `packages.yaml` 污染

**测试**: 同 Step 1

---

### Step 3: 扩充 `scripts/spack-common.sh` + 精简 `streamline.sh`

#### 3a: 扩充 `scripts/spack-common.sh`

**文件**: `scripts/spack-common.sh`

**改动**:

1. **删除**旧版 `install_system_pkgs()` 函数（使用 `PKG_MANAGER`/`INSTALL_ARGS` 旧接口，无调用方）
2. **新增** `streamline_parse_env()` — 从 `env.yaml` 解析配置到 bash 变量
3. **新增**以下 step 函数（从 `streamline.sh` 搬入）：
   - `step_install_system_pkgs()` — 系统包安装
   - `step_clean_stale_repos()` — 清理残留 repo 注册（已修复 `awk '{print $2}'`）
   - `step_register_repos()` — 自定义 repo 注册
   - `step_find()` — 编译器发现（**无 external find**）
   - `step_concretize()` — concretize 并导出 spack.lock
4. **新增** `streamline_dispatch()` — main case/esac 调度逻辑

**函数依赖的变量**（由 `streamline_parse_env()` 设置）：

| 变量 | 来源 | 使用者 |
|------|------|--------|
| `SPACK_ENV_NAME` | env.yaml → spack.env_name | `step_concretize` |
| `SYSTEM_PKGS` | env.yaml → mirror_builder.system_pkgs | `step_install_system_pkgs` |
| `PKG_MIRROR_SETUP` | env.yaml → mirror_builder.pkg_mirror_setup | `step_install_system_pkgs` |
| `PKG_INSTALL_CMD` | env.yaml → mirror_builder.pkg_install_cmd | `step_install_system_pkgs` |
| `CUSTOM_REPOS[]` | env.yaml → spack.custom_repos | `step_register_repos` |

**测试**:
```bash
bash -n scripts/spack-common.sh
```

#### 3b: 精简两个 `streamline.sh`

**文件**: 
- `spack-envs/cp2k-opensource-2025.2/streamline.sh`
- `spack-envs/cp2k-rocm-2026.1-gfx942/streamline.sh`

**改动**: 两个文件内容**完全相同**，替换为 ~15 行：

```bash
#!/bin/bash
# streamline.sh — per-env pipeline entrypoint
# All logic is in scripts/spack-common.sh. This file only sets env-specific paths.
set -euo pipefail

ENV_NAME="${ENV_NAME:?ENV_NAME not set}"
MIRROR_DIR="${MIRROR_DIR:-/work/assets/spack-mirror}"
ENV_DIR="/work/spack-envs/${ENV_NAME}"
MODE="${1:?Usage: streamline.sh <concretize|mirror|all|verify>}"

source /work/scripts/spack-common.sh
streamline_parse_env
streamline_dispatch "${MODE}"
```

**原因**: 消除重复代码，所有 env 差异由 `env.yaml` 驱动

**测试**:
```bash
bash -n spack-envs/cp2k-opensource-2025.2/streamline.sh
bash -n spack-envs/cp2k-rocm-2026.1-gfx942/streamline.sh
```

---

### Step 4: 端到端测试

**操作**:
```bash
# 1. 清理宿主机持久化状态
echo "packages: {}" > .spack/packages.yaml
echo "repos: {}" > .spack/repos.yaml

# 2. Opensource 端到端测试
python generate.py assets --env cp2k-opensource-2025.2
# 预期：concretize 成功，spack.lock 生成

# 3. 验证 HOME 隔离生效
cat .spack/repos.yaml     # 预期：repos: {}（未被修改）
cat .spack/packages.yaml  # 预期：packages: {}（未被修改）

# 4. ROCm 端到端测试
python generate.py assets --env cp2k-rocm-2026.1-gfx942
# 预期：concretize 成功，spack.lock 生成

# 5. 交叉验证：连续跑两个 env，确认无交叉污染
python generate.py assets --env cp2k-opensource-2025.2
python generate.py assets --env cp2k-rocm-2026.1-gfx942
# 预期：两次都成功

# 6. 验证 ROCm spack.yaml 中的 buildable:false 仍然生效
python3 -c "
import json
with open('spack-envs/cp2k-rocm-2026.1-gfx942/spack.lock') as f:
    lock = json.load(f)
for h, s in lock['concrete_specs'].items():
    if s.get('external') and 'rocm' in s.get('external',{}).get('path',''):
        print(f'{s[\"name\"]}@{s[\"version\"]} external (ROCm GPU lib)')
"
```

---

## 不做的事情

- ❌ 不修改 `env.yaml` 结构
- ❌ 不修改 `spack.yaml` 中的 ROCm `buildable:false` 声明（GPU 库必须 external）
- ❌ 不修改 `generate.py`（入口不变）
- ❌ 不修改 `containers/Dockerfile.mirror-builder`（HOME 隔离在 run 时处理）
- ❌ 不删除 `.spack/cache/` 和 `.spack/package_repos/`（可重建但有加速作用）

## 变更文件清单

| 文件 | 改动类型 | 说明 |
|------|----------|------|
| `scripts/build-mirror-in-container.sh` | 修改 | `run_in_container()` 加 `-e HOME=/tmp/home` |
| `scripts/spack-common.sh` | 重写 | 吸收所有 step 函数 + dispatch |
| `spack-envs/cp2k-opensource-2025.2/streamline.sh` | 重写 | 精简为 ~15 行入口 |
| `spack-envs/cp2k-rocm-2026.1-gfx942/streamline.sh` | 重写 | 精简为 ~15 行入口 |
| `.spack/repos.yaml` | 清理 | 重置为 `repos: {}` |
| `.spack/packages.yaml` | 清理 | 重置为 `packages: {}` |

## 风险点

| 风险 | 缓解 |
|------|------|
| HOME 隔离后 bootstrap cache 每次重建 | 可接受——bootstrap cache 很小（~几 MB），且避免了更严重的污染问题 |
| mirror 体积稍大（多了 cmake/python 等源码） | 这些源码总共 ~100MB，远小于 HPC 库源码（~2GB+） |
| 去掉 external find 后 concretize 可能失败 | `spack.yaml` 中的 ROCm `buildable:false` 仍生效；通用工具从源码构建不影响 |

# Apptainer SIF 构建

## 概述

`generate.py build-sif` 将本地构建的 OCI 镜像（Docker/Podman）转换为 Apptainer SIF 文件，同时注入交互式 MOTD 显示功能。

```bash
source ./activate.sh
python generate.py build-sif --app-version cp2k-opensource-2025.2-force-avx512
```

产物输出到 `artifacts/`：

| 文件 | 说明 |
|------|------|
| `artifacts/<image>_<tag>.sif` | SIF 镜像（SquashFS 压缩，~350 MB） |
| `artifacts/<image>_<tag>.tar` | OCI tar 中间产物（可复用，跳过重复导出） |
| `artifacts/<image>_<tag>.def` | 渲染后的 Apptainer 定义文件（调试参考） |

## 命令选项

```bash
python generate.py build-sif [options]
```

| 参数 | 说明 |
|------|------|
| `--app-version <name>` | 环境名，自动推断镜像名/tag。不传值列出可用环境 |
| `--docker-image <name>` | 显式指定 OCI 镜像名 |
| `--docker-tag <tag>` | 显式指定 OCI 镜像 tag |
| `-o, --output <path>` | 输出 SIF 路径（默认 `artifacts/<image>_<tag>.sif`） |
| `--install-apptainer-only` | 仅安装 apptainer，不构建 SIF |

## 构建流程

```
OCI 镜像 (podman/docker)
    ↓  podman save / docker save
OCI tar (artifacts/*.tar)
    ↓  渲染 cp2k.def.j2 模板
Apptainer 定义文件 (.def)
    ↓  apptainer build
SIF 镜像 (artifacts/*.sif)
```

1. **导出 OCI tar**：使用 `podman save` 或 `docker save` 将本地镜像导出为 tar
2. **渲染定义文件**：查找 `spack-envs/<env>/cp2k.def.j2` 模板，渲染为 `.def` 文件
3. **构建 SIF**：`apptainer build --force` 从 docker-archive 构建 SIF

## Apptainer 安装

`build-sif` 首次运行时自动检测 apptainer。若未安装：

1. 检查系统依赖（`curl`、`rpm2cpio`、`cpio`），缺少时报错并提示安装命令
2. 从 GitHub 下载最新 `install-unprivileged.sh`
3. 提示用户确认后，安装到 `tools/apptainer/`（非特权安装）

手动安装：
```bash
python generate.py build-sif --install-apptainer-only
```

系统依赖（Ubuntu/Debian）：
```bash
sudo apt-get install -y curl rpm2cpio cpio
```

`activate.sh` 会自动将 `tools/apptainer/bin` 加入 `PATH`。

---

## MOTD（交互式 Shell 欢迎信息）

### 效果

`apptainer shell` 进入容器时自动显示：

```
 -----------------------------------------------------------------------
 ⬡  cp2k-opensource | Version: 2025.2-force-avx512
 -----------------------------------------------------------------------
  Built At  : 2026-04-20T10:12:17.876652

  HARDWARE CHECK:
  CPU Model : Intel(R) Core(TM) Ultra 7 265K (20 cores)
  Memory    : 6/49 GiB (Used/Total)
  SIMD Stat : [OK — AVX-512 detected]

  ENVIRONMENT:
  Data Dir  : /opt/spack-view/share/cp2k/data (Basis sets, Potentials)
  Executable: cp2k.psmp (MPI + OpenMP Hybrid)

  HINT:
  To optimize performance, set:
  export OMP_NUM_THREADS=1 (or your preferred threads per rank)
  Use mpirun -x OMP_NUM_THREADS=1 to set it across all ranks.
 -----------------------------------------------------------------------
  Type 'cp2k.psmp --version' for more details.
 -----------------------------------------------------------------------

  In CP2K we trust.
```

MOTD **仅在 `apptainer shell` 时显示**，`apptainer exec` 和 `apptainer run` 不会触发。

### Docker/Podman MOTD

Docker/Podman 通过 ENTRYPOINT + CMD 机制显示 MOTD：
- `podman run -it img` → 显示 MOTD
- `podman run img cp2k.psmp` → 不显示 MOTD

### 技术细节：SINGULARITY_SHELL 方案

Apptainer 的 `shell` 命令内部使用 `bash --norc` 启动 shell，这会跳过：
- `/etc/bash.bashrc`
- `~/.bashrc`
- `BASH_ENV` 环境变量
- 所有其他 bash 初始化文件

因此，将 MOTD 注入到 bash.bashrc、BASH_ENV 或 `~/.bashrc` **均无效**。

同样，`/.singularity.d/env/99-motd.sh` 虽然会在容器启动时被 source，但它发生在 bash 启动**之前**，其输出会被 apptainer 的 shell action 吞掉，用户看不到。

#### 正确方案：SINGULARITY_SHELL 环境变量

Apptainer 的 shell action 脚本 (`/.singularity.d/actions/shell`) 在启动 bash 之前会检查 `SINGULARITY_SHELL`：

```sh
# /.singularity.d/actions/shell (简化)
for script in /.singularity.d/env/*.sh; do
    . "$script"           # ← SINGULARITY_SHELL 在这里被 source
done

if test -n "$SINGULARITY_SHELL" -a -x "$SINGULARITY_SHELL"; then
    exec $SINGULARITY_SHELL "$@"    # ← 用自定义 shell 替代 bash --norc
elif test -x /bin/bash; then
    exec /bin/bash --norc "$@"      # ← 默认行为（跳过所有 rc 文件）
fi
```

利用这个机制，我们：

1. **`%environment` 中设置**：`export SINGULARITY_SHELL=/usr/local/bin/hpc-shell-wrapper.sh`
2. **`%post` 中创建 wrapper**：

```bash
#!/bin/bash
# /usr/local/bin/hpc-shell-wrapper.sh
if [ "${APPTAINER_COMMAND:-}" = "shell" ]; then
    /usr/local/bin/hpc-motd.sh 2>/dev/null || true
fi
exec /bin/bash --norc "$@"
```

#### 为什么安全

- `SINGULARITY_SHELL` **只被 shell action 读取**，`exec` 和 `run` action 完全忽略
- wrapper 最终 `exec /bin/bash --norc`，恢复正常的 Apptainer shell 体验
- 不加载用户的 `~/.bashrc`（没有副作用）

#### 已验证不可行的方案

| 方案 | 原因 |
|------|------|
| `/etc/bash.bashrc` 注入 | `bash --norc` 跳过 |
| `BASH_ENV` 环境变量 | `bash --norc` 跳过 |
| `~/.bashrc` 注入 | `bash --norc` 跳过 |
| `/.singularity.d/env/99-motd.sh` | source 发生在 bash 之前，输出不可见 |
| Docker ENTRYPOINT | Apptainer 不保留 ENTRYPOINT |
| `SINGULARITY_SHELL=/bin/bash`（直接） | 会加载用户 `~/.bashrc`，有副作用 |

### 为新环境添加 SIF 构建

在 `spack-envs/<env-name>/` 下创建 `cp2k.def.j2`，最少只需要：

```
Bootstrap: docker-archive
From: {{ docker_tar_filename }}

%environment
    export SINGULARITY_SHELL=/usr/local/bin/hpc-shell-wrapper.sh

%post
    cat > /usr/local/bin/hpc-shell-wrapper.sh <<'WRAPPER_EOF'
#!/bin/bash
if [ "${APPTAINER_COMMAND:-}" = "shell" ]; then
    /usr/local/bin/hpc-motd.sh 2>/dev/null || true
fi
exec /bin/bash --norc "$@"
WRAPPER_EOF
    chmod 755 /usr/local/bin/hpc-shell-wrapper.sh
```

如果容器内已有 `/usr/local/bin/hpc-motd.sh`（通过 Dockerfile COPY），则无需额外操作。若没有 `cp2k.def.j2`，`build-sif` 会自动生成包含上述内容的 fallback def 文件。

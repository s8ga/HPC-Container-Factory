# Apptainer SIF 构建

`build-sif` 将本地 Podman/Docker OCI 镜像导出为 OCI archive，再调用
Apptainer（或 Singularity）构建 SIF。

## 通用用法

按环境名推断 OCI 镜像名和 tag：

```bash
python -m hpc_cf build-sif --app-version <env-name>
```

显式指定 OCI 镜像：

```bash
python -m hpc_cf build-sif \
  --docker-image <image-name> \
  --docker-tag <tag> \
  --output /path/to/image.sif
```

CP2K 2026.2 示例：

```bash
python -m hpc_cf build-sif \
  --app-version cp2k_opensource-2026.2-force-avx512
```

主要选项：

| 参数 | 说明 |
|------|------|
| `--app-version`, `--env` | 环境名；用于推断 OCI 镜像名和 tag |
| `--docker-image` | 显式 OCI 镜像名 |
| `--docker-tag` | 显式 OCI 镜像 tag |
| `-o`, `--output` | SIF 输出路径 |
| `--mksquashfs-args <args>` | 传给 Apptainer 的 SquashFS 参数 |
| `--install-apptainer-only` | 仅安装本地 Apptainer |
| `--yes`, `-y` | 安装时无需交互确认 |

例如自定义压缩参数：

```bash
python -m hpc_cf build-sif \
  --app-version cp2k_opensource-2026.2-force-avx512 \
  --mksquashfs-args "-comp zstd -Xcompression-level 15 -b 1M"
```

非交互安装 Apptainer：

```bash
python -m hpc_cf build-sif --install-apptainer-only --yes
```

## Definition 选择与 fallback

指定 `--app-version` 时，程序在 `spack-envs/<env-name>/` 中查找
`*.def.j2`，按文件名排序并选择第一个：

- 找到 definition：渲染该模板，再由 Apptainer 构建。环境模板可在此配置
  shell wrapper、MOTD 或其他 Apptainer 专用行为。
- 未找到 definition：不生成通用 wrapper，而是直接执行
  `apptainer build ... docker-archive://<archive>`。

因此 definition 文件名并不固定为 `cp2k.def.j2`。同一环境不应放置多个含义
不明确的 `*.def.j2`。

OCI archive 导出时按顺序检测 Podman、Docker；至少需要其中一个能读取待转换
镜像。相对 `--output` 路径按调用命令时的工作目录解析。

## Smoke 命令

通用检查：

```bash
apptainer inspect /path/to/image.sif
apptainer exec /path/to/image.sif /bin/sh -c 'true'
```

CP2K 检查：

```bash
apptainer exec /path/to/cp2k.sif cp2k.psmp --version
apptainer exec /path/to/cp2k.sif \
  /bin/sh -c 'test -x /opt/spack-view/bin/cp2k.psmp'
```

交互式 shell：

```bash
apptainer shell /path/to/image.sif
```

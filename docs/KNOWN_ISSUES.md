# 当前已知问题

当前版本暂无阻断式已知问题。

## 归档

VASP 与 CP2K MKL 路线已归档到 `legacy/`。

## 已解决：Apptainer Shell MOTD

Apptainer `shell` 命令使用 `bash --norc` 启动，导致所有 bash 初始化文件（`/etc/bash.bashrc`、`~/.bashrc`、`BASH_ENV`）均被跳过。

**解决方案**：使用 `SINGULARITY_SHELL` 环境变量指向一个 wrapper 脚本，该脚本在 `exec bash --norc` 之前显示 MOTD。
详见 [BUILD_SIF.md](BUILD_SIF.md) 中的技术细节章节。
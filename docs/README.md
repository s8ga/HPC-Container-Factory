# HPC-Container-Factory 文档总览

本目录文档已按当前仓库代码状态重建，目标是：
- 只保留可验证的信息
- 明确哪些路径可直接使用
- 显式记录当前已知不一致点

## 项目定位

HPC-Container-Factory 是一个面向 HPC 软件栈的容器构建工厂，核心能力是：
- 用 Jinja2 模板生成多阶段 Dockerfile
- 用 Spack 环境定义依赖
- 用本地 assets 提供离线构建支持（bootstrap、mirror、源码与工具链）
- 通用化的容器化 mirror 构建系统，支持任意 `spack-envs/` 下的环境
- 当前活跃路线为 CP2K opensource

历史路线（VASP、CP2K MKL）已迁移至 legacy 目录归档。

关键入口：
- 生成与构建入口: ../generate.py
- 资产初始化入口: ../scripts/init_assets_v2.py
- 容器化 mirror 构建: ../scripts/build-mirror-in-container.sh

构建系统三层架构：
- 调度器: `scripts/build-mirror-in-container.sh` — 宿主机上运行，管理容器生命周期
- 通用函数库: `scripts/spack-common.sh` — spack bootstrap / mirror create / mirror verify
- Per-env 流水线: `spack-envs/<env>/streamline.sh` — 配置驱动，声明包管理器、系统包、自定义仓库

## 文档导航

- 快速开始: ./QUICK_START.md
- generate.py 参数与用法: ./GENERATE_CLI.md
- assets 与离线资源说明: ./ASSETS_GUIDE.md
- 模板与环境映射矩阵: ./TEMPLATE_MATRIX.md
- 当前已知问题与规避方式: ./KNOWN_ISSUES.md

## 当前推荐路径

当前最稳定、可直接走通的是 CP2K opensource 模板的显式生成流程：

1. 激活虚拟环境
2. 显式指定模板生成 Dockerfile
3. 用 docker 或 podman 构建

示例见 ./QUICK_START.md。

## 说明

本次文档清理后，历史阶段性报告、重复说明和与当前代码不一致的文档均已移除。
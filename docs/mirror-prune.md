# Source-mirror git 缓存增长与 prune 流程

> 本页从 AGENTS.md 的运维 doctrine 迁移而来（2026-11 fragility 修复程序,
> 审计发现 2.12:关键运维知识只活在面向 agent 的指南里）。AGENTS.md 保留一行指针。

## 为什么 `_source-cache/git/` 会无限增长

`spack mirror create` 是 fill-only:每次带新 pin 的运行都会往镜像的
`_source-cache/git/` 追加完整 repo bundle。单个 py-torch 级别的 pin 就是一次
约 4 GiB 的全仓库 bundle,不做 prune 时共享镜像树只会越滚越大。

## 入口与安全机制

mirror + verify 之后,assets 工作流可以把 git 侧裁剪到**本次运行 lock 成员**
为止:

```bash
# 先看 dry-run(默认;只报告,不删除)
python -m hpc_cf assets --env <env> --download-mirror --verify-mirror --prune-mirror

# 确认后真正删除
python -m hpc_cf assets --env <env> ... --prune-mirror --prune-mirror-apply
```

nightly pipeline 同时传两者。删除永远需要显式 `--prune-mirror-apply`。

## 身份来源:fetch-specs.json

prune 的身份依据是容器侧 `spack python` 导出的 `.hpc_cf/fetch-specs.json`
(由 `hpc_cf.mirror_prune` 消费):每个 concrete spec 映射到其 fetcher
(git URL + commit)。注意 spack 1.2 的一个坑:`fetcher.git` 是 git **可执行文件**,
URL 在 `.url` 字段。

## Planner 规则

- **pinned-commit repo**:只保留其 pin 的 commit。
- **branch-floating repo**:保留本次运行实际 fetch 的 blob(`fresh_after`
  时间戳判定)。seed 恢复出来的文件 mtime 是 epoch-0,所以"新鲜度"只在
  单次运行内部有效;停滞时执行 single-survivor 规则。
- **匹配不到任何 spec 的 repo**(如 py-torch 子模块缓存):整体保留。
- 每包层的 symlink 修复到与保留对象一致。

## 归档侧按校验和裁剪

archive 侧的 blob 文件名**就是**源码校验和。允许集来自 sidecar 的
recipe version-dict sha256/md5(lock 节点不带源码校验和)、patch 校验和、
resource 校验和;空允许集 = 保留整个 archive 侧。
sidecar 为空或损坏时**硬拒绝**(不猜)。

## 共享树的特别警告

共享 LOCAL mirror 树同时承载多个 env;本地 prune 会删掉其他 env 仍需要的
源码。**永远先审 dry-run 输出再 apply。**

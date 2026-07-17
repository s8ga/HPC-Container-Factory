# 当前已知问题

## CP2K 2026.2：SPGLIB 混合并行敏感性

测试 `QS/regtest-kp-1-spg/c_geo_opt_kpsym_spglib.inp` 对特定 MPI/OpenMP 分解敏感。补充测试中：

- `1x1`、`1x2`、`2x1`、`2x2`、`1x4`、`4x1`、`2x4`、`8x1`、`1x8`、`3x2` 通过。
- `4x2` 连续 5/5 次失败，residual 为 0.282 至 0.489；`2x3` 和 `4x3` 也失败。
- 关闭 keepalive 未解决 `4x2`；提高 cutoffs 至 400/60 后仍在 3 次中失败 1 次。

建议避免未经目标输入验证的混合分解。只有测试名和 residual/abort 特征精确匹配、且 targeted retest 通过时，才可归类为此已知间歇性问题；其他失败仍是 release blocker。

## CP2K 2026.2：TDDFPT tolerance-sensitive 变化

`QS/regtest-tddfpt-4/test09.inp` 的 `TDDFPT_Check_Osc_Strength` 曾出现一次相对差 `4.38e-5`，略高于当前 `4e-5` tolerance。之后 isolated、concurrent 和 keepalive 复测均通过；8 种已测并行组合各自通过 46/46 checks，相对误差为 `5.12e-6` 至 `1.99e-5`。

这不是当前发布中的失败项，但后续结果必须按测试名、matcher 和 tolerance 判断，不能把任意 TDDFPT 偏差归入已知问题。

完整边界、上游引用和原始结果见：

- [CP2K 2026.2 发布说明](releases/CP2K_2026.2.md)
- [CP2K regtest 日志](../spack-envs/cp2k_opensource-2026.2-force-avx512/cp2k-regtest.log)
# 默认扫描完整研究目录

日期：2026-10-18，Asia/Shanghai。修改基线：`3dc2444`。

## 原因与修改

旧 `Pipeline.scan()` 遍历 `effective_repositories()`，该方法基于 `config.repositories` 的八个贡献目标。因此普通 `scan`、正式 `tick` 都只扫八个；另一个 `catalog_sweep.py` 入口虽然能扫完整目录，却没有改变正式入口的默认行为。

现将扫描目标统一为完整的、未过期的已核验研究目录，当前实际解析为 32 个。`peer_repositories` 只限制研究同行选择，不再缩小扫描范围。研究资格仍由原目录规则判断，贡献白名单和维护者/发布门禁继续单独执行。

没有目录的旧配置保留原扫描方式；已有目录却没有合格目标时，明确要求重新核验，不默默回到八个。单独 `scan` 不再先执行贡献看板健康检查，`tick` 仍会在后续贡献阶段使用原有门禁。

新增 `runs/scan-summary.json`，列出完整计划、逐仓库结果及总数，逐仓库原子保存。失败保留 unknown；预算中断后未访问的项保留 not_scanned，不表示没有问题。计划完成与全部成功是两个不同口径。

## 验证

新增四项回归在修复前实际失败：默认完整范围、tick 完整范围、目录仓库失败保留、空目录不得退回八个。修复后另覆盖独立扫描无需贡献看板权限，以及预算中断保留所有未访问目标。原 peer 子集的默认研究行为仍保留。

完整回归：`python -X utf8 -m unittest discover -v`，184 项通过，137.627 秒，无失败或跳过。日志：`runs/catalog-default-scan-tests.log`。四项失败证据：`runs/catalog-default-scan-red.log`。

## 正式入口实网扫描

使用修改后的 `python -X utf8 contribute.py scan`，运行 ID `f2a807a846684c0d979a44786ace509c`，北京时间 14:01:14–14:11:49，634.578 秒，退出码 0，运行状态 success；开头实际输出 `scan scope: verified_catalog (32 repositories)`。日志：`runs/catalog-default-scan-live-20261018.log`，逐仓库规范记录：`runs/scan-summary.json`。

最终统计：

| 项目 | 实际结果 |
| --- | ---: |
| 计划仓库 | 32 |
| 扫描成功 | 32 |
| unknown / not_scanned | 0 / 0 |
| 开放 Issue | 2,631 |
| 开放 PR | 4,798 |
| 新增或更新本地任务 | 66 |
| 模型入口调用 | 0 |

`completed=true`，所有仓库逐项标为 scanned；每仓库最多 100 条 Issue、200 条 PR，不宣称全部历史均被读取。最终 SQLite `integrity_check=ok`，进程锁为 0。本次验证仅扫描 Issue / PR、更新本地队列，不发布上游内容；tick 的相同默认范围通过回归测试验证，没有为本次扫描验收另行启动自动贡献。

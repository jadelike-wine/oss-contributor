# 贡献转化工程验证记录

本轮从 `ab5ed20` 的干净工作区开始。仅本地提交，未授权远端推送、Issue、PR 或评论；配置中的自动化开关不作为发布授权。当前源码默认禁止外部写入。

## 重新核验的基线

SQLite backup API 的一致性快照为 `runs/conversion-20261031/baseline.db`；原始统计见同目录 `baseline.json`。统计自 2026-10-19 起、有结束记录的 33 轮，而非原线索的 32 轮：总耗时 93,087.142 秒（25.858 小时）。健康、扫描及研究阶段按每轮时间区间并集计算为 77,836.874 秒（83.617%），不重复累计嵌套阶段。代码实现阶段 6 次尝试，只有 2 个不同任务。

原配置 `max_tasks_per_tick=6` 与代码中单任务实现限制同时存在；PLAN 在模型请求前就占据实现身份。每轮先研究后交付，失败规划仍可阻塞唯一身份。扫描没有跨轮成功缓存。历史中发现 3 组同仓库同 Issue 的重复任务、92 组跨周同主题调查。

Mermaid #7953 历史发布先因 Windows Corepack 钩子路径错误中断，随后暂存新增文件使旧指纹变化。保留的原工作区仍只有原三项暂存改动，`git write-tree` 仍为批准时的 `8743aea7e65b6e36e6a077216f79a58d8d713ce6`。新补丁在另一个工作区准备，未清除原任务的三次失败。

Agno #10887 实网仍开放且只有 bug 标签；当前官方指南明确要求 ready 标签。旧 `maintainer_approved=true` 来自快速通道，不能作为真人方向确认。新代码同时执行当前指南提取的硬要求与配置中的 ready 要求，旧布尔值没有人类评论收据就不授权实现。规则和查询证据保存在 `policy-live-evidence.json`。

## 旧失败与新通过

| 缺陷 | 可重复验证 | 证据 |
| --- | --- | --- |
| 暂存／提交改变补丁身份，提交或推送响应丢失后不能可靠恢复 | 真实临时 Git、临时 bare remote；冻结旧 fingerprint/publish_pr 与同组测试对照；真正内容变化仍拒绝 | `delivery-baseline-tests.log`：同组 7 项有 3 项失败/错误；`delivery-integration-tests.log`：7/7；额外 pending 对账测试及最终集成另列 |
| 配额 6 实际只能推进 1 个，失败规划阻塞其余任务 | 同一历史全队列、同样七个真实历史任务、阶段正文替换的调度探针：1 → 6；明确属于模拟调度 | `history-replay-final/replay-report.json`，`conversion-scheduling-before-20261031.log` |
| 交付／研究游标混用、缓存中断丢失、跨周与 Issue 重复 | 独立持久游标、缓存预填、规范身份／别名保留、固定主题复用、语义复核引用原任务 | `scheduling-final.log`，`test_conversion_scheduling.py`，`semantic-duplicate-after.log` |
| 基础设施错误消耗候选质量次数 | 网络、限流、证书、服务、格式与质量/政策分开；三次上限、冷却与同轮服务熔断 | `history-replay/runtime-before-verified.log`，`runtime-after.log`，`schema-classification-before.log` / `schema-classification-after.log` |
| bug／超时伪造维护者批准，仓库 ready 规则失效 | 五项旧入口回归失败；新版硬规则及真人评论门禁通过 | `policy-before.log`，`targeted-after.log`，`test_conversion_policy.py` |
| 发布默认权限与 CI 等待批准混淆 | 默认禁止写入；只读旧 CI 收据可复用；写操作须显式授权；精确 head 的 CI、真人回应、正式批准与合并分开 | `external-write-policy-tests.log`，`final-targeted.log`，原 `test_ci_status.py` |

控制器原始全套为 194 项通过（207.674 秒）。最后一次完整运行为 274 项通过（323.502 秒，`full-tests-frozen.log`），覆盖发布草稿和工具发现回归。之后只有新 worktree 的 LF 检出小修正及一处审查措辞调整；相关 checkout/pipeline/delivery/runtime 46 项通过（42.293 秒，`checkout-related-after.log`）。真实临时 Git 的原检出命令在同四项测试中有两项失败，新版四项通过，既有文件、索引和配置字节不变，显式 `.gitattributes eol=crlf` 仍生效。不能把重叠专项相加当作独立测试总数。

工具发现另有旧 `ab5ed20` 同四项两项失败的证据（`runtime-tool-discovery-before.log`）；本机 `pnpm --version` 实际以 Node + Corepack JS 入口执行，在禁用 Corepack 网络时输出 10.30.3、exit 0（`runtime-tools-live-execution.json`）。结构化模型格式与扫描冷却问题也有定向失败/通过证据。所有临时 Git/bare remote 和模型/GitHub mock 测试属于本地模拟，不能称为真实上游提交。

一次旧实现对照脚本存在隔离错误，误调用不存在的测试仓库 fork 接口并返回 404，没有创建资源。该次日志 `delivery-baseline-isolation-error.log` 不计为缺陷证据。正式旧/新对照已在 `subprocess.Popen` 层阻止一切真实 gh 命令及非临时本地远端推送，防护本身有测试。

## 历史兼容与生产维护

真实快照上的迁移与恢复均执行两遍。全部 20,031 个任务、事件、automation、research_candidates 和 pr_observations 的整行哈希相同，所有旧表行身份保留，完整性正常。只增加 3 个别名引用，10 条遗留运行标记为 interrupted；中断实际停止时间未知，时间边界与耗时只保留最后检查点，不把等待恢复的多日时间算作运行。

再次核验没有本项目控制器进程且锁为空后，额外保存 `production-before-maintenance.db`，在互斥 maintenance 锁下把相同迁移及恢复应用到生产库。`production-maintenance.json` 证明历史哈希未变、完整性为 ok、第二次恢复为空；没有修改任务阶段或清空质量次数，没有发布。原 Mermaid 工作区、SQLite 历史和日志保留。

## 有界真实网络验证

`validate_live.py` 从完整生产队列建立隔离副本，不调用实现或发布入口。它实际访问 GitHub、执行当前扫描/缓存/队列排序，并每轮实时检查 Mermaid 与 Agno 的状态和规则。统计范围始终是全部 94 个研究仓库，每轮最多实际刷新 12 个仓库，剩余项目保留 not_scanned；有效缓存也不伪装成当轮新请求。

首批三轮发现并保留了健康规则缓存缺少元数据的集成错误：缺失信息保持 unknown，未当作成功。修复后从同一完整队列副本续跑三轮：

| 轮次 | 全部任务（前 → 后） | 成功新扫描 | 有效缓存 | unknown | 尚未扫描 | 秒 |
| --- | --- | --- | --- | --- | --- | --- |
| 4 | 20,199 → 20,207 | 12 | 17 | 0 | 65 | 319.781 |
| 5 | 20,207 → 20,209 | 12 | 29 | 0 | 53 | 211.859 |
| 6 | 20,209 → 20,210 | 11 | 41 | 1 | 41 | 207.344 |

第 6 轮 Tabler 的 GitHub GraphQL 请求遇到 EOF；保留为 unknown。随后仅对这个失败项做一次有界实网重试，成功取得 39 个开放 Issue 和 57 个开放 PR。完整范围仍为 94，未把单项补跑计为全目录新扫描。全部原始逐仓库记录与运行 ID 在 `live-validation/results.json`，日志分别为 `live-validation.log`、`live-validation-resumed.log` 与 `live-failure-retry.log`。

实网规则还拒绝了 Goose（缺 read:project）、Mistral Vibe（规则不接受 PR）和已迁移的 Lexical，均未扩大目录或绕过规则。

这些轮次的实际实现数与验证补丁数为 0；它们验证的是网络采集、恢复、规则及调度观察。真实实现样本由下节单独报告。不能用有界每轮 12 个新请求的耗时，与旧完整扫描 94 个仓库直接相除后宣称吞吐提高；缓存命中、保留完整范围和逐轮推进才是这里的证据。

前七轮的固定切片、同输入调度/扫描刷新派发/故障分类比较见 [同口径比较](conversion-comparison-2026-10-31.md)。该对账识别到第二轮 11 项错误在中间版本丢失检查点，虽仍在完整范围内，却误列为 not_scanned；不能用第七轮 unknown=0 宣称全部恢复。

保留原始轮次证据和扫描摘要备份后，从 round-2.json 恢复这 11 项到隔离验证副本。第 8 轮对 11 项全部实网重试成功：完整范围仍为 94，11 scanned、53 cached、30 not_scanned；全部任务 20,210 → 20,225，371.500 秒，完整性 ok。未开启外写、未执行实现；Mermaid 仍开放，Agno 仍因缺 ready 暂停。恢复收据为 `scan-failure-restoration.json`，结果为 `live-validation/round-8.json` 和 `live-restored-failures.log`。此前发现的采集故障均有成功恢复证据；剩余 30 个是尚未采集项目，不能宣称本次已新扫描全目录。

## 真实候选与上游结果

Mermaid #7953 在 develop `370c4af57f43cb0bda05f4a9fe2d99c912ba16dd` 的隔离工作区重新准备。2026-10-31 18:12:55（上海时间）再次只读核验：该分支 SHA 未变，Issue 开放，四组包含开放/关闭 PR 的竞争搜索均为零且非 incomplete；当前贡献规则没有 Agno 的 ready 门槛。历史补丁的额外 getter 调用由新测试揭示，已修正为复用原第二次调用；旧批准没有复用。

最终补丁严格五个文件、196 行新增/1 行删除，SHA-256 为 `3ff7f7b3ed96ce4bd4b0d5fe3d1f8373386726e9aea25c1c6d94611017d34c88`。本轮实际构建的当前 SHA 基线产物用最终测试重放，明确出现两处不可达表达式的 AssertionError，非超时或基础设施失败。补丁构建与同一聚焦检查 11 项通过；最终 `pnpm test --maxWorkers=2` 包含全仓 ESLint、Jison、Prettier 及 Vitest，367 文件通过/1 跳过，7,573 测试通过/17 跳过/2 todo，无失败。外层总耗时 552.937 秒，Vitest 487.66 秒，嵌套/并行子阶段时间不相加。

可审阅材料为 `mermaid-7953.patch`、`mermaid-pr-draft.md`、`mermaid-verification-spec.json`、`mermaid-ready-manifest.json` 与 `mermaid-review.json`。根验收再次核对 patch 哈希及 `git apply --check --reverse`；原历史 worktree 的暂存树仍为 `8743aea7e65b6e36e6a077216f79a58d8d713ce6`，未清空其失败次数或自动发布。

最终审查为 `approved=true`，清单状态 `ready_for_authorized_submission`。清单 SHA-256 为 `2df67fb4284d5a7e9b43813ac4a9c15f9fa2b32e1b2953894555cb3304649f59`，绑定补丁、PR 草稿、验证配方、审查及五个源码文件的哈希。2026-10-31 18:20:20 的最终机器核验全部通过，见 `final-evidence-check.json`；生产库再次只读核验也确认全部历史内容相同、完整性 ok、锁为空，见 `final-database-check.json`。

第一次完整项目测试出现 3 文件失败：20 个文档断言及两个脚本加载错误；同一 SHA 的无补丁基线重复了相同失败。实际原因是 Windows 自动 CRLF 检出：文档测试使用 LF 正则，Vite 7.3.7 的 hashbang 正则也不识别 CRLF，导致两个脚本加载失败。仅将 34 个未改 Markdown 和两个未改 mjs 恢复为精确 HEAD 的 LF 字节，逐项保留备份和哈希，未改变五个补丁文件，也未增加 Git diff；这三文件 110 项随后全部通过。证据为 `mermaid-platform-baseline.log`、`mermaid-platform-lf.log`、`mermaid-validation-checkout-lf.json`。全范围复跑保留完整测试，没有排除失败项。当前 Node 22.23.1 满足项目 engines，pnpm 10.30.3 符合锁定版本；未声称在 CI 的 Node 24.16.0 或 Firefox DevTools 上执行过。

没有真实上游新 Issue、PR、评论或推送，也没有任何新的维护者接受或合并可报告。模型/同模型子代理的不同调用均不是独立模型审查。

本轮系统工程验收完成，已验证内容身份与发布恢复、独立额度与公平轮转、增量扫描/去重、错误冷却/中断恢复、仓库规则和接受状态区分。公开发布仍依照用户的会话授权边界，候选目前是可审阅待提交。上游要求的 build-docs、lint、unit-test、e2e-required 远端 CI 尚未运行；正式发布前须再实时核验规则、Issue、竞争实现与目标分支，并遵守验证证据时效。

后续持续观察：每个实际进入实现的不同任务、基线红／补丁绿且完整检查通过的补丁数、待发布时延、网络/模型/证据/补丁/政策故障分布、每仓库研究轮转间隔、重复研究比例，以及精确 PR head 的 CI、真人反馈、正式批准、合并与关闭。样本不足，尚不能量化生产贡献吞吐或合并率改善。

## 本地提交

- `d8daf7c`：内容身份、可恢复交付、调度与政策证据原语。
- `372e0cb`：控制器集成、交付优先、独立额度、规则门禁、错误恢复与针对性测试。
- `8890c53`：结构化模型输出错误不扣候选质量次数。
- `70a1641`：扫描故障冷却，保留完整覆盖检查点。
- `2809087`：无发布授权时保存草稿，避免重复处理无变化反馈。
- `38ddce7`：Windows Corepack 发现回归、与审查独立性一致的文案。
- `31cecd5`：可重复的全历史审计、回放及有界实网验证工具。
- `bcf6d7e`：新隔离 worktree 保留上游行尾，防止宿主 CRLF 制造验证假失败。

操作和恢复说明见 [恢复说明](conversion-recovery-2026-10-31.md)；完整历史回放口径见 [回放记录](conversion-history-replay-2026-10-31.md)。运行证据和数据库含本机历史，因此不提交到 Git；它们保留在本工作区的 runs 目录中。

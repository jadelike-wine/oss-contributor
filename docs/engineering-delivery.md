# Cross-Harness 工程交付记录

验收日期：2026-10-17（用户时区 Asia/Shanghai）。本文件区分实网读取、本地真实测试和模拟发布，不将模拟结果计为上游接受。

## 审计事实与兼容性

- 起始工作区干净，分支 `main`，基线提交 `3c42abb`。没有覆盖用户修改，没有提交或推送仓库。
- 改动前 `python -X utf8 -m unittest discover -v`：96 项全部通过，78.065 秒。此前 README 的 89 项不是当前真实测试数量。
- 原研究每次只选择一个 peer、最多四个文件；目标引文读取工作区。现已改为有界多 peer 与目标基线文件读取。
- 原实现模型来自配置，Hermes 审查模型写死 `tokenking/devin/swe-2`；原生入口均支持指定模型。本次没有切换真实模型。
- 原模型调用没有调用次数、每日运行或费用预算；已有 events、状态迁移、模型输出和验证日志，缺少运行级/阶段级统计。
- 原超时分别为普通命令 180、Git 准备 600、模型/测试 1500、专用远端 CI 轮询 1800 秒；不是所有命令统一为 1500 秒。
- 已审计贡献政策、CI 观察、远端收据验证、进程锁、状态恢复、查重、发布意图与回复丢失恢复。它们继续作为发布前置条件。

## 文件与作用

| 文件 | 实际变化 |
| --- | --- |
| `research.py` | 多 peer 选择、固定 SHA、目标/同行有界采集、失败保留 unknown、checkpoint 续跑、候选和矩阵接入 |
| `feature_matrix.py`（新增） | 十四类功能分类、四种状态及精确证据校验、稳定 ID、队列/优先级、SQLite upsert 和 JSON 导出 |
| `repository_catalog.py`（新增） | 候选逐仓核验、固定版本样本、研究准入、临时失败隔离、显式退役优先、规范地址记录 |
| `repository-catalog.json`（新增） | 真实 GitHub 读取产生的 52 条核验记录，含来源、SHA、摘要/哈希及准入原因 |
| `telemetry.py`（新增） | 五张附加表、运行/阶段计时、入口调用预留、原生 token、预算熔断、只读报告和不同统计口径 |
| `autonomous.py` | 批量只读研究、功能队列附加门禁、每轮一个实际实现任务、三候选上限、审查模型配置/身份、阶段观测 |
| `contribute.py` | 安全重复迁移、新 CLI、原子 JSON 保存、研究/贡献权限分离、按用途超时及预算截断 |
| `repository_policy.py` | 修复组织共享贡献指南的官方 profile 404 回退，仅信任同组织 `.github` |
| `verify_ci.py` | 保留专用精确补丁收据逻辑，轮询上限纳入配置及剩余运行预算 |
| `config.json` | 新研究、预算、超时、目录和 review_model 配置；保留原八仓库与退役/Goose 规则 |
| `test_feature_matrix.py`、`test_repository_catalog.py`、`test_cross_harness_flow.py`、`test_telemetry.py`（新增） | 矩阵、目录、跨 Harness 闭环、观测和预算验证 |
| `test_ci.py`、`test_duplicates.py`、`test_repository_policy.py`、`test_research.py` | 补齐模块复制、真实 SQLite 观测依赖、显式空配置及研究调度隔离；未放宽安全断言 |
| `README.md`、本记录及 `examples/*.fixture.json` | 实际用法、数据关系、迁移、限制和执行生成的模拟示例 |

`ci_status.py`、`hermes_tick.py` 继续复用，未重复实现另一套 CI/调度机制。没有新增 Python 第三方依赖。

## 跨 Harness 闭环验收

已用临时 Git 仓库完成：目标 + 三个不同 peer → 精确引文与固定 SHA 矩阵 → 维护者确认队列 → 模拟维护者认可 → 真实基线失败 → 真实补丁通过和项目检查 → 模拟审查/发布 → 同一 PR 恢复。Git push 仅指向临时本地 bare 仓库。

矩阵示例与运行报告由该集成测试实际生成，均明确标记 `fixture_only`；没有把 peer 夹具或模拟 PR 当成真实上游成果。

实网只读 smoke 使用现有 mini-swe-agent checkout，模型调用预算为零：

| 对象 | 固定 SHA |
| --- | --- |
| SWE-agent/mini-swe-agent | `04d809ceab9df28f9adaed044884180159172930` |
| google-gemini/gemini-cli | `ed96faedfd5571f055c44f2c9509d62321d1138d` |
| QwenLM/qwen-code | `6ad896c20b974f9a6f5b5c0a8683f290ca8aa28d` |
| anomalyco/opencode | `8c173a8f3805d8e749d44311054b07e37a1b2ce1` |

首次读取遇到 GitHub EOF，两个成功 peer 的材料保留；续跑复用它们，仅重试失败部分并补齐反馈。累计 26 次请求，目标 12 个文件、同行各 4 个文件，最终上下文 211,427 字符；0 次模型调用、0 次发布。首轮 52.578 秒、续跑 27.516 秒，最后复核缓存未新增请求。这证明实网采集与恢复，不代表真实模型已完成一项上游功能设计。

## 仓库核验结果

32 个 `research_only`、0 个新增 `active`、10 个 `paused`、8 个 `rejected`、2 个 `retired`。研究资格不授予 Issue/PR/推送权限；原八仓库仍由既有白名单及实时贡献门禁控制。

未准入/暂停/退役的逐项结果：

| 仓库 | 状态 | 核验原因 |
| --- | --- | --- |
| Kilo-Org/kilocode | paused | Verification failed: TimeoutExpired |
| anthropics/claude-code | rejected | Open-source license not established by GitHub license metadata; manual license review required |
| zed-industries/zed | rejected | Open-source license not established by GitHub license metadata; manual license review required |
| github/copilot-cli | paused | Verification failed: RuntimeError HTTP 404 |
| plandex-ai/plandex | paused | No repository push within 180 days; maintenance review required |
| TabbyML/tabby | rejected | Open-source license not established by GitHub license metadata; manual license review required |
| bytedance/trae-agent | paused | No repository push within 180 days; maintenance review required |
| Pythagora-io/gpt-pilot | paused | Official maintenance/migration notice: This repository is no longer actively maintained |
| modelcontextprotocol/servers | rejected | Open-source license not established by GitHub license metadata; manual license review required |
| e2b-dev/desktop | rejected | Usable source, test and documentation samples not all verified |
| OpenInterpreter/open-interpreter | paused | Repository moved to openinterpreter/openinterpreter; explicit allowlist update required |
| microsoft/autogen | paused | Official maintenance/migration notice: This project has been archived |
| vercel/ai | rejected | Open-source license not established by GitHub license metadata; manual license review required |
| langchain-ai/langchain | paused | Verification failed: RuntimeError |
| BerriAI/litellm | rejected | Open-source license not established by GitHub license metadata; manual license review required |
| mastra-ai/mastra | rejected | Open-source license not established by GitHub license metadata; manual license review required |
| FlowiseAI/Flowise | paused | Repository is archived, disabled or has issues disabled |
| instructor-ai/instructor | paused | Repository moved to 567-labs/instructor; explicit allowlist update required |
| continuedev/continue | retired | Official README: no longer actively maintained; read-only for all users |
| SWE-agent/SWE-agent | retired | Official README: development moved to mini-swe-agent, which supersedes SWE-agent |

部分知名开源项目有混合或非标准许可证，GitHub 返回 NOASSERTION；这里的 rejected 表示当前保守准入器尚未确认，不是判定整个项目闭源。Kilo 的最后复核超时，继续保留 paused，没有使用旧成功记录伪装成当轮通过。

## CLI 与配置

```powershell
python -X utf8 contribute.py catalog --verify
python -X utf8 contribute.py research-batch
python -X utf8 contribute.py candidates
python -X utf8 contribute.py matrix <任务ID或feature_id>
python -X utf8 contribute.py metrics --limit 10
```

配置细节见 README。默认每轮研究 3 个候选，每候选 3 个 peer（最多 5）；每轮 16、单任务累计 40、UTC 日 80 次模型入口尝试；单轮 5400、单任务本轮活跃时间 3600、UTC 日已记录时间 21600 秒。单任务时间不错误计入其他候选的研究时间。

金额预算当前不执行，可靠账单费用不可获取；报告明确显示 null 和 `cost_caps_enforced=false`。原生 token 可用时记录，并标明完整/部分/不可获取覆盖率；CLI 尝试与内部模型步骤分开统计。

## SQLite 与历史数据

真实数据库两次迁移后，tasks=1025、events=99、automation=8、scheduler=1、locks=0；五张原表逐行内容哈希前后一致，完整性检查为 ok。新增 research_candidates/runs/stages/model_calls/pr_observations，不清空、重建或重置原数据。

本地证据：`runs/upgrade-migration.json`；SQLite backup API 的备份为 `state/tasks-pre-cross-harness.db`。两者均未加入 Git。运行观测与旧任务状态分开保存，政策暂停不改写旧 automation 证据。

## 验证口径与限制

- 本次没有运行生产模型进行真实贡献，也没有产生新的真实上游 Issue、PR、推送或合并。已有真实 PR 是历史记录，仍标为待观察。
- 默认审查仍是同一模型、不同通道；不同模型配置只在模拟适配器测试中验证参数传递，未声称另一个模型在本机实际可用。
- 有界样本可能遗漏实现；not_found 仅限已读范围，失败为 unknown。测试目录内的 Markdown、配置和初始化文件不再冒充可执行测试证据。
- 复杂许可证、自然语言贡献政策、语义去重和最终维护者接受仍需要后续证据。已有功能 Issue 的保守标签 hold 保留；主动功能开发主要经 hunt 研究与维护者确认闭环进入。
- 金额预算因缺少账单数据不可执行；入口调用次数与时间为可执行预算，CLI 内部步骤不是实时逐步熔断。
- 中断的 metrics 保留 running/未结束状态，不假造完整耗时或历史费用。没有对旧运行补造阶段统计。

## 最终完整回归

最终命令：`python -X utf8 -m unittest discover -v`。

```text
Ran 139 tests in 108.541s
OK
```

基线 96 项 + 新增 43 项 = 139 项；失败 0，跳过 0。原有安全断言未放宽。最终完整输出保存在本地 `runs/upgrade-delivery-tests.log`；针对性调试中发现的失败均已修复后重新运行全套。

测试覆盖多 peer 的数量/版本/引文/范围、四种状态、读取失败与预算中断、维护者确认与政策禁令、目录退役及共享指南、只读报告、重复迁移、调用/token 记录、阶段/任务计时、最多三项研究和单项实现。原有红绿验证、指纹、锁、发布意图和远端回复丢失恢复全部通过。

全部测试结束后再次对真实数据库的五张原表比较内容哈希，仍与迁移前一致；`PRAGMA integrity_check` 仍为 `ok`。`git diff --check` 通过。工作区保留可审查变更，未 commit、push 或 merge。

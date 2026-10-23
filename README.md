# OSS Contributor

**Cross-Harness Feature Research & Autonomous OSS Engineering**

面向 AI Coding Harness 与 Agent 基础设施的跨项目功能研究和开源贡献平台。Python 3.11+ 标准库实现，使用 Git、GitHub CLI、OpenCode 和 Hermes；研究发现、实现验证、公开发布具有独立的准入门槛。成功以可靠功能、可重复验证和上游真实接受衡量，不以运行时长或 PR 数量代替质量。

## 工作流程

```text
核验研究目录（只读） + 核验原有贡献白名单
  → 每轮最多研究 3 个候选
  → 目标基线 SHA + 3–5 个不同 peer 的固定 SHA
  → 有界代码/测试/文档采集 + 精确引文校验
  → 功能矩阵 + 架构迁移分析 + 定向查重 + 审查
  → READY_TO_IMPLEMENT / NEEDS_MAINTAINER_CONFIRMATION / RESEARCH_ONLY / PAUSED_POLICY
  → 原有状态机：规划 → 基线真实失败 → 实现 → 同一测试通过 + 项目检查
  → 单独审查通道 → 指纹及政策门禁 → 发布意图 → Issue/PR
  → 精确 head SHA 的 CI 观察、维护者回应、合并或关闭
```

默认实现模型与审查模型仍为 `tokenking/devin/swe-2`。当前研究通道显式配置为 Hermes，研究审查标识为 **`same_model_separate_invocation`**；OpenCode 实现与 Hermes 审查仍为 `same_model_separate_channel`，均不宣称两个独立模型。新功能必须获得维护者明确方向才进入实现；高风险候选在确认方向后仍暂停等待人工补丁审查，目前没有可绕过这一门槛的人工批准 CLI。

## 快速使用

需要已登录的 `gh`、Git，以及已配置可用模型的 OpenCode 和支持 `chat --query-file --format stream-json` 的 Hermes。凭据继续由各 CLI 管理。不要把密钥写入配置或研究资料。

```powershell
python -X utf8 -m unittest discover -v

# 只读本地诊断：不运行模型，不访问网络，不迁移数据库
python -X utf8 contribute.py metrics --limit 10
python -X utf8 contribute.py candidates
python -X utf8 contribute.py matrix <任务ID或feature_id>
python -X utf8 contribute.py catalog

# 有界 GitHub 只读核验；更新本地目录，不授予贡献权限
python -X utf8 contribute.py catalog --verify

# 默认扫描完整的已核验研究目录（当前 32 个），刷新 Issue / PR 队列
python -X utf8 contribute.py scan

# 遍历全部已核验研究仓库，扫描已有 Issue、固定源码版本并逐仓库静态分诊
# 默认不受原贡献白名单或 research-batch 每轮 3 个候选的限制
python -X utf8 catalog_sweep.py --output runs/catalog-sweep
# 网络采集和模型分析可分开运行；成功结果保留，失败结果保持 unknown
python -X utf8 catalog_sweep.py --output runs/catalog-sweep --collect-only
python -X utf8 catalog_sweep.py --output runs/catalog-sweep --analyze-only --attempts 1
# 定向补跑仍保留完整目录和逐仓库结果
python -X utf8 catalog_sweep.py --output runs/catalog-sweep --analyze-only --repo QwenLM/qwen-code

# 创建本地研究任务；目标可以来自研究目录，也可以来自原贡献白名单
python -X utf8 contribute.py hunt SWE-agent/mini-swe-agent "Compare session recovery and checkpoints"
python -X utf8 contribute.py analyze <上一步任务ID>

# 研究已有 hunt 队列，最多 3 个候选；不会调用发布或实现入口
python -X utf8 contribute.py research-batch

# 下列入口保留原有自动发布能力，生产运行前应理解上游规则
python -X utf8 contribute.py auto --dry-run
python -X utf8 contribute.py auto
python -X utf8 contribute.py tick

# 清理任务 worktree：默认只删终态(SKIPPED/CLOSED/MERGED/FAILED)任务、base-* 临时副本和孤儿 worktree
# 保留 base clone 与全部日志/遥测；--all 连同进行中任务一起删（下轮 prepare 按固定 SHA 重建）
python -X utf8 contribute.py clean-workspaces --dry-run
python -X utf8 contribute.py clean-workspaces
python -X utf8 contribute.py clean-workspaces --all
```

`auto --dry-run` 不调用模型或发布，但仍可能进行只读健康核验。`metrics/candidates/matrix/catalog`（不带 `--verify`）使用本地资料；`metrics/candidates/matrix` 通过 SQLite `mode=ro` 连接，不触发迁移。旧 `scan/status/followup/prepare/analyze/repair/verify/review/hunt/unlock` CLI 保留。

`scan` 和 `tick` 的扫描阶段默认使用完整的已核验研究目录，当前为 94 个仓库，不受 `peer_repositories` 同行子集或每轮三个模型研究候选的限制。扫描仍排除过期、暂停、拒绝、退役、归档、禁用及未授予研究权限的目录项。没有目录文件的旧配置继续使用原白名单；已有目录却没有合格项时会明确报错，提示 `catalog --verify`，不会静默退回白名单。单独 `scan` 无需核验贡献看板权限；`tick` 后续的实现和发布继续使用原有贡献及维护者门禁。

每轮 `runs/scan-summary.json` 保存计划范围、全部仓库、逐仓库 Issue / PR 数及总数。网络失败记为 `unknown`，中断后尚未访问的项保留 `not_scanned`；成功记录逐仓库保存，进程正常退出也不表示所有仓库都成功。此摘要仅记录扫描，源码采集和模型研究仍通过下述入口推进。

默认范围修复及 32 仓库正式入口的实网结果见 [完整目录默认扫描验收](docs/default-catalog-scan-2026-10-18.md)。

后续正式流程实跑、查重修复及本轮发布结果见 [2026-10-18 续跑验收](docs/live-rerun-2026-10-18.md)。查重搜索不完整时，可以依据已读结果拒绝重复候选，不能据此放行实现或发布。

`catalog_sweep.py` 默认遍历核验目录中的全部研究仓库，保存完整 `scope.json` 和 `results.json`，不会把缺失项当作无问题。每仓库最多扫描 100 个开放 Issue、200 个开放 PR，并有界读取固定 SHA 的规则、源码和测试；模型分析不是运行目标项目的测试。成功采集与分诊可恢复，失败保留 `unknown` 和诊断；原有模型预算仍生效。此入口不会自动实现或发布，静态候选的政策判断仅供后续核对，正式贡献仍须读取完整上游规则、查重、复现和验证。实际运行及上游贡献见 [2026-10-18 全目录运行记录](docs/catalog-sweep-2026-10-18.md)。

`hunt` 和只读研究入口不再先探测全部贡献仓库的看板权限，仍通过已核验研究目录准入。`research-batch` 不自行创建新任务；先 `hunt` 或使用已有每周调查任务。`auto/tick` 默认创建每周调查、批量只读研究，再推进实现。已有成功研究不会被批量入口重复调用模型。研究失败按阶段最多尝试三次，预算暂停不消耗失败次数。实现保护按实际任务身份执行，即使多个已发布 PR 同时出现修改要求，同轮也只推进一个任务的代码阶段。

## 跨 Harness 研究与证据

`research.py` 使用已核验目录的类别和有限路径清单选择相关 peer，并对同等匹配项确定性轮转。一个任务保存其 peer 集合，续跑不会悄悄换掉比较对象。可用项目不足时会保留实际数量，但少于三个 peer 的矩阵不会进入功能实现队列。

目标文件由控制器从 `git show <base_sha>:<path>` 读取，同行文件从 GitHub 的 `contents?ref=<sha>` 读取。目标适用的贡献、安全和嵌套 `AGENTS.md` 优先纳入有限上下文；随后按主题选择源码、测试、文档及可用设计/更新材料。大于 60,000 字节的文件不进入候选读取范围；每个文件再受字符截断限制。截断以外的内容不能用于引文验证。

目标采集先在固定 SHA 上进行主题内容检索，再按源码、测试和文档配额选择材料；贡献规则单独预留位置。文件名按词匹配，避免 `minisweagent` 这样的包名让所有文件误中 `agent`。配置和初始化辅助文件不再优先挤占功能源码。研究模型只接收这些有界材料：OpenCode 拒绝全部工具，Hermes 使用非空 `-t none`（空字符串会启用平台默认工具），并拒收包含工具事件的结果。研究模型不可自行读取更多文件、联网、执行命令或委派。实现/规划入口仍使用原有受限工具权限。文件采集完成后立即保存 checkpoint；预算中断会保留已完成的目标和 peer 快照。失败 peer 可以在下一次研究重试；请求失败不会变成能力缺失。

| 状态 | 程序门槛 |
| --- | --- |
| `implemented` | 必须有该项目固定 SHA 的精确源码或测试引文 |
| `partial` | 同上，并明确 `missing` 行为 |
| `not_found` | 必须给出实际读取的源码/测试路径，仅表示此检查范围内未找到 |
| `unknown` | 请求失败、证据不足或无法验证；失败 peer 不能使用其他状态 |

README 没有提到功能不构成缺失证据；README 声称支持也不能替代源码/测试证据。目标和同行引文按仓库分别验证，不能相互替代。静态测试文件标为 `execution_result: not_run`，不会冒充已执行测试。

### 记录关系

- `research-context.json`：固定版本的输入材料、读取范围、失败、选择理由与请求计数；可恢复。
- `opportunity.json`：模型候选与政策判断的审计快照、基线/指纹、审查结果和 `feature_id`。其中原始模型矩阵是输入记录，不作为可编辑的事实来源。
- SQLite `research_candidates.matrix`：经校验的当前矩阵和队列，是规范记录。
- `feature-matrix.json`：上面记录的导出视图，由控制器更新，不应手工双写。
- `duplicate-search-issue.json`：包括开放和关闭条目的定向查重证据。

矩阵包含稳定 `feature_id`（目标仓库 + `feature_key` 哈希）、名称、类别、用户场景、目标/同行 SHA、各项目状态与范围、证据、测试、差距、迁移适配、预计文件、实施/验证计划、风险、贡献路径及创建/更新时间。相同任务可继续更新；不同任务遇到同一功能标识会停止重复推进。语义相同但标识不同的候选仍依赖定向查重和审查，不声称哈希能够解决语义去重。

十四类分类可在 `feature_matrix.CATEGORIES` 扩展：上下文历史、会话恢复、工具、权限沙箱、MCP/Skills/Hooks、多 Agent、模型 Provider、规划、取消与资源回收、Git/审查/测试、错误重试、终端观测、插件配置、安全发布。

[矩阵示例](examples/feature-matrix.fixture.json) 来自真实执行的**本地集成测试夹具**：`owner/repo` 和 peer 内容均为模拟材料。它展示序列化格式，不是完成过的真实上游研究成果。

## 功能优先队列

| 队列 | 行为 |
| --- | --- |
| `READY_TO_IMPLEMENT` | 证据、政策、查重和审查通过；需要的维护者方向已确认，仍须通过原有红绿测试和发布门禁 |
| `NEEDS_MAINTAINER_CONFIRMATION` | 仅形成设计提案；政策允许时原状态机可先提交讨论，然后等待真实维护者回复 |
| `RESEARCH_ONLY` | 证据不充分、目标已有等价功能、重复或审查未通过，不可实现/发布 |
| `PAUSED_POLICY` | 明确禁止自动 Issue（`automated_issue=false`）、本人提交/审查要求（`manual_issue_submission`/`human_pr_review_required`）或高风险人工门槛，暂停自动贡献。模型仅报告 `automated_pr=false` 不再判死——该候选降级为 `NEEDS_MAINTAINER_CONFIRMATION`，经讨论 Issue 等待维护者方向，不会自动实现或发 PR |

数值优先级只在证据与政策门槛通过后产生：功能优先于 Bug，低风险略优先。价值、现有替代方案、架构成本、维护者方向、测试可行性和接受可能性的论据必须提供给审查者；没有这些依据的候选不能靠模型打高分进入实现。原有 Issue 初筛评分和功能标签的保守 hold 保留，主动功能研究由 hunt/batch 承担。

查重由控制器统一仓库限定，清除模型查询中的仓库名普通词和自带 `repo:`，避免假阴性；记录原始及实际查询。结果超过第一页时控制器自动翻页拉取（每查询最多 100 条），全部命中都经审查后才算完整搜索；超过上限或上游 `incomplete_results` 仍记为不完整，可以依据已读结果拒绝，不能据此放行。功能查重区分已有实现/竞争 PR 和开放的功能请求：前者仍拒绝，后者须经审查确认关联已读取的开放 Issue，并等待维护者方向，不重复开 Issue，也不把用户需求当作维护者批准。

队列是原有状态机的附加门禁，不替代状态机。旧任务继续按 `PLAN → REPRODUCE → ISSUE/REPAIR → PATCH_READY → AUTO_REVIEW → PUBLISH → PR_OPEN` 恢复。维护者确认不能覆盖 `automated_issue=false` 或本人审查要求；`automated_pr=false` 只能阻止直接实现/发布，不阻止讨论 Issue。

编号 Issue 的 bug 修复不等待维护者逐一批准：Issue 带 `accepted`、`help wanted`、`good first issue`、`triaged` 或 `bug` 标签时，`WAITING_MAINTAINER` 直接进 `REPAIR`。上游对明确 bug 的惯例是欢迎 PR——错过窗口让别人抢先，风险大于提交一个可能被重定向的 PR。有 revert 历史、方向不明或维护者明确要求讨论的例外，模型仍以 `wait` 决策降级。

## 仓库目录与准入

最新核验结果见 [repository-catalog.json](repository-catalog.json)：保留原 52 条记录，追加本次清单中未重复的 80 个项目，共 132 条记录。用户提供的 100 个项目全部纳入目录，按前端框架、UI 基础设施、AI 应用、Agent 与工作流四类组织。核验记录带 UTC 时间、规范名称、默认分支、推送时间、归档/禁用标志、许可证、固定 SHA、文件摘要/哈希、贡献政策材料及原因。逐项结果见 [100 项仓库扩容记录](docs/catalog-expansion-2026-10-18.md)。

| 目录状态 | 数量 | 含义 |
| --- | ---: | --- |
| `research_only` | 94 | 已核验公开源码、测试、文档及可识别开源许可证，进入默认研究和扫描范围；其中白名单内 94 项标记 `automatic_contribution` / `pr_submission: allowlisted`，允许自动贡献（仍受实时门禁约束） |
| `active` | 0 | 目录核验不绕过运行时健康/政策门禁；白名单内项目的贡献授权记录在 `automatic_contribution` |
| `paused` | 13 | 迁移旧地址、长期无推送、停止维护/归档、贡献限制等，逐条记录原因 |
| `rejected` | 23 | 有界准入无法确认开源许可或源码/测试/文档覆盖，保留待审查 |
| `retired` | 2 | `continuedev/continue`、`SWE-agent/SWE-agent`，保持显式退役 |

GitHub 对复杂/混合许可证可能返回 `NOASSERTION`。这类仓库目前不自动准入，需另行逐目录许可证审查。临时失败只暂停，不永久退役；已退役记录刷新时不会自动恢复。OpenInterpreter 和 Instructor 的原名称保留迁移状态，官方规范名称另行核验后进入研究池。

- `repositories`：贡献目标白名单，保留原 33 个目标并追加本次核验通过的项目，当前 94 个；白名单内且目录核验为 `research_only` 的仓库记录 `automatic_contribution` 与 `pr_submission: allowlisted`，允许进入自动贡献流程，但仍需当轮健康检查、每个任务的政策判断及发布门禁。
- `repository_catalog`：完整核验目录文件路径，默认 `repository-catalog.json`。
- `peer_repositories`：可选的研究池子集，仅过滤已核验目录，不能直接授权未知仓库。
- 目录超过 `research.catalog_max_age_days`（默认 30 天）不再作为研究准入依据，需 `catalog --verify`。

`catalog --verify` 按每批最多 80 个名称顺序刷新完整目录，最多四个并行只读检查，目录扩容后也不会截断或因总数超过 80 而失败。名称按大小写不敏感去重；每个项目至多 25 次 API/内容读取，每次 30 秒。部分失败不影响其余记录，逐条原子保存，刷新保留原有额外候选和退役项。GitHub 的组织共享贡献指南仅接受官方 community profile 指向同一组织 `.github` 的路径，并额外固定其 SHA。

Goose 的指定看板 `Ready`、`manual_issue_submission`、`human_pr_review_required` 原样保留。无法核验看板权限仍暂停贡献；只读研究无需自动发布权限。研究目录的认可不会修改这些规则，也不会把组织共享指南当成本项目任意路径的执行指令。

## 预算、超时与观测

新增配置具有默认值，旧配置仍可运行。保留 `timeout_seconds` 作为模型/测试超时的回退；新配置按用途分别覆盖，不再笼统声称所有命令都用同一时限。

| 配置 | 默认值/约束 |
| --- | --- |
| `research.model_channel` | `opencode` 或 `hermes`；缺省 OpenCode，当前配置 Hermes；不静默切换 |
| `research.candidates_per_run` | 3，范围 1–10 |
| `research.peers_per_candidate` | 3，范围 3–5 |
| `research.files_per_peer` | 4，范围 2–6 |
| `research.target_files` | 12，范围 3–25，包含规则文件 |
| `research.file_characters` | 12,000，最大 60,000 |
| `research.context_characters` | 320,000，最大 600,000；超出安全暂停，不静默丢弃证据 |
| `research.network_requests` | 每候选每次采集 40，最大 60；历史累计另记 |
| `research.batch_on_auto` | 当前配置 true；旧配置缺省 false，保留旧调度行为 |
| `timeouts.command_seconds` | 普通命令 180 秒 |
| `timeouts.git_seconds` | clone/fetch/worktree/push 600 秒 |
| `timeouts.model_seconds` / `test_seconds` | 当前 1,500 秒，缺省回退 `timeout_seconds` |
| `timeouts.stage_seconds` | 单阶段 2,400 秒；auto/task 等包含多个阶段的观测跨度不另加阶段截止线 |
| `timeouts.remote_ci_seconds` | 现有专用远端 CI 轮询最多 1,800 秒，再受运行预算限制 |
| `budgets.run_model_calls` | 单轮最多 16 次模型入口调用，审查计入 |
| `budgets.task_model_calls` | 单任务跨轮累计最多 40 次入口调用 |
| `budgets.daily_model_calls` | 默认 `null`，每日模型调用次数不限；设置数字可恢复上限 |
| `budgets.run_seconds` / `task_seconds` | 单轮 5,400 秒；单任务本轮 3,600 秒 |
| `budgets.daily_seconds` | UTC 日累计已记录运行时间最多 21,600 秒 |
| `budgets.run_cost_usd` / `daily_cost_usd` | 默认 null；当前无可信账单数据，金额限制不执行，依靠调用/时间限制 |
| `review_model` | `provider/model`，缺省沿用实现模型，默认没有切换供应商或模型 |

所有受管命令都会被当前阶段、任务、运行及日预算的剩余时间进一步截短。达到预算后保留阶段、研究 checkpoint 和已预留调用，下一轮从保存处继续，不消耗普通失败重试。单任务累计调用耗尽不会自动清零，需要检查原因并明确调整预算。执行耗时使用单调时钟，追踪时间使用 UTC。普通命令失败、超时、失败阶段重试、暂停/跳过/等待维护者分别记录。

每条调用记录是一次 **OpenCode/Hermes CLI 入口调用尝试**，在启动前预留，启动失败也消耗调用预算，不等于单个 HTTP 模型请求。OpenCode 一次入口内部可以有多个模型步骤；日志可取得时另记 `reported_provider_steps`。入口预算和运行时限是硬门槛，当前不提供每个内部步骤的实时费用熔断。研究入口禁用工具，不能依靠内部工具循环扩大源码读取范围。

原生日志提供 token 时统计输入（含缓存读写输入）和输出；缺失即 null。当前适配器不提供可核实的账单费用，OpenCode 的零费用占位值也不作为实际成本。未完成的调用保留 `running`，不算成功。不同审查模型只表示配置身份不同，不等于已证明其判断质量独立；不可用配置会失败关闭，不自动换模型。

`metrics` 显示最近运行、阶段耗时/结果/原因、CLI 调用和 token、候选类别/证据/多 peer 完成度/重复尝试/政策队列，以及实现、红绿验证、审查、PR、CI、维护者回应和合并的独立口径。比例带分子、分母和 scope，无样本时为 null。CI 通过仅表示观察到的检查通过，不推断未查询的分支保护要求；开放 PR 标为待观察。

[运行报告夹具](examples/metrics.fixture.json) 来自本地真实阶段计时及红绿测试，模型和 GitHub 发布接口被模拟，不是实际上游贡献。旧数据库迁移后的真实只读输出摘录为：

```json
{
  "runs": [],
  "candidate_queues": [],
  "quality_all_time": {
    "researched_unique_candidates": 0,
    "submitted_unique_prs": 1,
    "merged_prs": 0,
    "pending_observation_prs": 1
  },
  "cost_caps_enforced": false
}
```

这里的一个 PR 来自已有历史任务，不是本次升级创建；空 runs 表示旧运行没有被伪造回填。

## SQLite 升级与恢复

首次初始化 `Pipeline` 自动执行可重复的 `CREATE TABLE/INDEX IF NOT EXISTS`。新增五张表：`research_candidates`、`runs`、`stages`、`model_calls`、`pr_observations`。不删除、重建或清空 `state/tasks.db`，不改任务 ID、原有 tasks/automation/events/scheduler/locks 数据。

迁移不依赖手工操作。复制旧数据库前应停止调度；需要备份时使用 SQLite backup API，避免只复制仍处于 WAL 模式的主文件。本次对真实数据库使用 backup API 保存了本地备份，并两次迁移校验：1,025 条任务、99 条事件、8 条 automation、1 条 scheduler、0 条锁，原表内容哈希全部一致，`integrity_check=ok`。数据库和备份继续由 `.gitignore` 排除。

阶段/调用使用唯一 ID，结束时更新原记录；多次查看统计不累计计数。异常时保存已产生的观测；进程被强制杀死会留下未结束记录，已保存的部分耗时不是完整耗时。恢复依靠原有控制器/子进程 PID 锁和任务状态，不依靠指标完整性。矩阵和上下文文件使用原子替换，避免中断留下半份 JSON。旧候选和历史指标不会被猜测回填。

## 保留的安全与发布门禁

- 基线 worktree 上真实出现指定回归断言，补丁 worktree 上同一测试通过，项目检查通过。
- 验证、审查、发布的补丁指纹保持一致；旧收据不能批准新补丁。
- worktree、进程/子进程锁、有限阶段重试及 SQLite 恢复保持有效。
- Issue/PR 创建前持久化意图；远端响应丢失时先查找既有结果，不盲目重建。
- 发布文本和补丁继续检查凭据；安全候选保留本地，不能进入公开披露。
- 本人提交、本人工程审查、看板门槛及自动贡献禁令高于模型或一般维护者回复。
- 旧 PR 跟进、精确 head SHA 的 CI 观察、特殊 OpenCode Linux 验证收据继续保留。该远端 CI 配方仍只适用于原有特定任务，不是通用 CI 服务。

worktree 不是操作系统沙箱。实际测试/依赖安装会在本机执行；研究入口全部禁用模型工具，但实现阶段仍需可信的执行环境。`hermes_tick.py` 继续调用相同 `tick`；克隆仓库不复制本机调度，也不自动启动 gateway。

## 验证与边界

完整测试使用 `python -X utf8 -m unittest discover -v`。原始基线 96 项全部通过；最终 139 项（新增 43 项）全部通过，108.541 秒，无失败或跳过。新增测试覆盖多 peer 证据/矩阵、队列与维护者确认、目录准入、模型身份和 token、预算恢复、批量上限、只读统计和真实旧库迁移。测试使用临时 Git 仓库、本地 bare remote 和模拟 GitHub/模型接口，开发过程没有向真实上游创建 Issue、PR、推送或合并。

当前限制：有界路径选择可能遗漏实现，此时必须保持 scope 限定或 unknown；复杂许可证与组织规则仍需人工进一步核验；政策的自然语言语义和功能价值依赖有证据的审查；默认审查模型相同；可靠费用不可获取；CLI 调用预算不是每个模型内部步骤的硬计数器；历史运行不补造指标；统计只能在未来真实贡献得到回应/合并后反映接受率。

详细审计、执行记录与最终验收见 [工程交付记录](docs/engineering-delivery.md)。

## 真实运行验收（2026-10-18）

本轮真实 GitHub / 模型调用的发现、失败和修复记录见 [真实研究报告](docs/live-research-2026-10-18.md)。模型结构或引文校验失败时，保留原始输出并最多请求一次纠正，纠正使用相同材料、计入预算并重新校验；不补造证据。旧提示词造成的顶层 `feature_status` 可兼容读取，但冲突映射、未读取路径和不匹配引文仍拒绝。

完整扫描与自动推进的后续验收见 [流程运行与恢复报告](docs/live-pipeline-2026-10-18.md)。搜索诊断清单现在有独立大小上限，旧检查点可恢复；Windows 原子替换遇到短暂文件占用时有限重试，持续失败仍保留旧记录并报错。

规划重试会把保留的本地草稿与固定 SHA 基线分开提供，避免将自己上次生成的测试误认成上游已有覆盖。JSON 语法错误与证据/结构错误共享一次研究纠正额度，纠正仍失败时停止。

# OSS Contributor

**Cross-Harness Feature Research & Autonomous OSS Engineering**

面向 AI Coding Harness 与 Agent 基础设施的跨项目功能研究和开源贡献平台。Python 3.11+ 标准库实现，使用 Git、GitHub CLI、DeepSeek Harness CLI（兼容 OpenCode）和 Hermes；研究发现、实现验证、公开发布具有独立的准入门槛。成功以可靠功能、可重复验证和上游真实接受衡量，不以运行时长或 PR 数量代替质量。

## 工作流程

```text
核验研究目录（只读） + 核验原有贡献白名单
  → 优先推进 PR 有效反馈、待提交补丁、已复现任务和研究通过的候选
  → 有界串行：当前实现最多 8 个任务、维护最多 6 个任务、研究最多 3 个候选
  → 目标基线 SHA；跨项目功能研究另采集 3–5 个不同 peer 的固定 SHA
  → 有界代码/测试/文档采集 + 精确引文校验
  → 功能矩阵 + 架构迁移分析 + 定向查重 + 审查
  → READY_TO_IMPLEMENT / NEEDS_MAINTAINER_CONFIRMATION / RESEARCH_ONLY / PAUSED_POLICY
  → 原有状态机：规划 → 基线真实失败 → 实现 → 同一测试通过 + 项目检查
  → 单独审查通道 → 内容身份及政策门禁 → 本地待提交材料
  → 有明确发布授权（含已启用的持续调度授权）时：发布意图 → Issue/PR
  → 精确 head SHA 的 CI 观察、维护者回应、合并或关闭
```

当前研究初稿、纠正和目录分诊通过 Hermes 使用 `tokenking/deepseek-v4.1-flash`；方案、实现和反馈通过 DSH CLI 使用 `tokenking/devin/swe-2`，审查仍通过 Hermes 使用 SWE-2。研究与实现模型分别配置，新研究保存实际生成时的模型和通道；恢复旧研究不按新配置改写来源，无法确认的身份标为 `unknown`。DeepSeek 研究与 SWE-2 审查为 `different_model_same_channel`；SWE-2 实现与审查为 `same_model_separate_channel`，不宣称独立模型审查或维护者批准。新功能必须获得维护者明确方向才进入实现；高风险候选在确认方向后仍暂停等待人工补丁审查，目前没有可绕过这一门槛的人工批准 CLI。

直接调用 `contribute.py` 默认禁止远端推送、创建 Issue/PR、修改 PR 和其他 GitHub 写操作。当前项目已于 2026-11-05 获得用户的持续调度发布授权，并启用 `automation.scheduled_external_writes=true`：`hermes_tick.py` 每轮读取此项，严格为布尔值 `true` 时传入全局参数 `--allow-external-writes`；缺省或 `false` 则关闭。`automation.enabled=true` 和仓库白名单本身不授予发布权限，仓库规则、复现、验证和审查门禁继续生效。无外写授权时仍可准备本地补丁与发布材料。恢复规则见 [贡献恢复说明](docs/conversion-recovery-2026-10-31.md)，此前验收进度见 [2026-10-31 工程记录](docs/conversion-progress-2026-10-31.md)。

## 快速使用

需要已登录的 `gh`、Git、DSH CLI（可使用 DeepSeek Harness 桌面客户端捆绑版），以及支持 `chat --query-file --format stream-json` 的 Hermes。旧配置仍可选择 OpenCode。DSH 使用项目内独立的 `state/dsh/`，不修改桌面客户端的配置或会话；模型凭据通过进程环境传递，可复用已有 OpenCode 服务商配置。不要把密钥写入项目配置或研究资料。

```powershell
python -X utf8 -m unittest discover -v

# 只读本地诊断：不运行模型，不访问网络，不迁移数据库
python -X utf8 contribute.py metrics --limit 10
python -X utf8 contribute.py candidates
python -X utf8 contribute.py matrix <任务ID或feature_id>
python -X utf8 contribute.py catalog

# 有界 GitHub 只读核验；更新本地目录，不授予贡献权限
python -X utf8 contribute.py catalog --verify

# 扫描完整的已核验研究目录；允许复用有效扫描缓存
python -X utf8 contribute.py scan

# 实时重审指定历史 Issue（每次最多 10 条）；只更新本地分诊，不发布
python -X utf8 contribute.py rescan-issues TanStack/router 8736 8473 8437

# 遍历全部已核验研究仓库，扫描已有 Issue、固定源码版本并逐仓库静态分诊
# 默认不受原贡献白名单或 research-batch 候选额度的限制
python -X utf8 catalog_sweep.py --output runs/catalog-sweep
# 网络采集和模型分析可分开运行；成功结果保留，失败结果保持 unknown
python -X utf8 catalog_sweep.py --output runs/catalog-sweep --collect-only
python -X utf8 catalog_sweep.py --output runs/catalog-sweep --analyze-only --attempts 1
# 定向补跑仍保留完整目录和逐仓库结果
python -X utf8 catalog_sweep.py --output runs/catalog-sweep --analyze-only --repo QwenLM/qwen-code

# 创建本地研究任务；目标可以来自研究目录，也可以来自原贡献白名单
python -X utf8 contribute.py hunt SWE-agent/mini-swe-agent "Compare session recovery and checkpoints"
python -X utf8 contribute.py analyze <上一步任务ID>

# 研究已有 hunt 队列，当前最多 3 个候选；不会调用发布或实现入口
python -X utf8 contribute.py research-batch

# 默认禁止外部写入；auto/tick 会推进本地实现及研究
python -X utf8 contribute.py auto --dry-run
python -X utf8 contribute.py auto
python -X utf8 contribute.py tick

# 仅在已明确授权对应远端写入时使用；参数本身不能代替授权
# python -X utf8 contribute.py --allow-external-writes auto

# 清理任务 worktree：默认只删终态(SKIPPED/CLOSED/MERGED/FAILED)任务、base-* 临时副本和孤儿 worktree
# 保留 base clone 与全部日志/遥测；--all 连同进行中任务一起删（下轮 prepare 按固定 SHA 重建）
python -X utf8 contribute.py clean-workspaces --dry-run
python -X utf8 contribute.py clean-workspaces
python -X utf8 contribute.py clean-workspaces --all
```

`auto --dry-run` 不调用模型或发布，但仍可能进行只读健康核验。`metrics/candidates/matrix/catalog`（不带 `--verify`）使用本地资料；`metrics/candidates/matrix` 通过 SQLite `mode=ro` 连接，不触发迁移。旧 `scan/status/followup/prepare/analyze/repair/verify/review/hunt/unlock` CLI 保留。

`scan` 和 `tick` 保留完整的已核验研究目录范围，不受 `peer_repositories` 同行子集或模型研究候选额度的限制。`tick` 先观察已有 PR，再推进已有任务和有预留份额的研究，最后扫描，避免模型耗尽预算后漏掉 PR 跟进；各阶段仍共享同一运行预算。当前每轮最多实际请求 12 个尚未命中缓存的仓库，按持久游标续扫。单独 `scan` 不受该 12 项上限限制，仍遵守运行时间预算。两者都可复用当前配置的 6 小时成功扫描缓存；将 `scanning.cache_seconds` 设为 `0` 可关闭复用。缓存命中不延长原观察时间。

扫描仍排除过期、暂停、拒绝、退役、归档、禁用及未授予研究权限的目录项。没有目录文件的旧配置继续使用原白名单；已有目录却没有合格项时明确报错，提示 `catalog --verify`。单独 `scan` 无需核验贡献看板权限；实现和发布继续受贡献政策门禁约束。仓库健康检查另使用 `repository_checks.cache_seconds`，当前为 24 小时；Issue/PR 发布前强制实时检查目标仓库与贡献规则，缓存不能授予发布资格。

2026-11-06 恢复更新：扫描按完整的仓库和 Issue 编号识别 PR 关联。跨仓同号不算重复；普通引用、测试专用 PR 和部分覆盖保留范围复核证据，进入正常分诊，不能直接证明可以发布。同仓明确 closing 关系继续阻止重复实现。每次实际扫描仓库时，默认额外刷新最多 2 条旧 HOLD，优先处理持久的 `issue_refresh_queue`；旧 PR 不在最新 200 条时进行有界精确查询，读取不完整保留原 HOLD 和 unknown。连续失败耗尽的仓库冷却一小时后，每轮最多获得一次恢复探测机会；原错误和恢复事件保留。

已发布 PR 可以用 `register-pr` 先预览、再通过 `--apply` 绑定到维护任务。入口要求仓库、PR 编号、干净工作区、原回归基线和预期的完整 head/base SHA，并实时核验本人身份、fork 和本地内容。`--expected-base` 指 GitHub PR 元数据的 base SHA 快照；该快照可能落后于目标分支，入口另行读取真实目标分支 tip，保存为 `target_base_sha`，并在绑定前再次核验。维护合并与发布前核验均使用真实 tip，快照刷新本身不重置已有维修。`--plan` 可提供含 `spec` 的验证计划；缺少计划时只观察并报告待维护事项。注册不复用旧批准，不发布内容，也不会覆盖不同或终态任务。维护会观察合并冲突及 head/base 变化；普通合并后仍须通过基线/补丁验证和审查，不能强推或把等待 CI 批准视为代码失败。

环境准备会在每个安装步骤执行前检查工具入口，整体检查会持续运行的开发服务器命令，并提供实际 Yarn 入口；允许前一步创建虚拟环境、依赖工具或构建产物。安装失败与补丁质量次数分开记录；超时耗尽后，仅在安装方案、工具内容或相关根目录/子包依赖清单有实质变化时进行一次有界恢复，修改文件时间不算修复。研究候选只有固定源码、精确引文及实现/验证范围均相同时才复用；部分重叠保留共有与独有范围，已有 PR 的维护不受该去重影响。

部分重叠由自动调度进行有界范围复审，也可用 `review-research-overlap <任务ID>` 触发同一流程。每组双方源码范围和任务状态只保留一个完成的判断：`distinct` 把独立剩余范围交回正常规划，`reuse` 等待前一任务变化，`unknown` 等待补充材料。网络、模型格式、超时和中断不会冒充已完成判断，沿用基础设施冷却并最多尝试三次；相关源码范围或前一任务状态变化后可以重新评估。`metrics` 保留待复审原因和尝试记录。人工明确复核后，可用 `resolve-research-overlap` 按当前范围摘要和具体理由登记决定；该操作不授予上游批准或发布资格。

复审后的剩余范围及已排除共有工作会进入研究、规划和配方审核。尚未通过研究审核的旧提案缓存会归档失效，必须生成缩小后的完整提案并重新查重、审核；同一调查保持原 `feature_key`，避免旧宽范围索引继续生效。已解决的同材料范围不能被旧缓存直接改回重叠暂停，历史提案、引文和审查记录始终保留。

每轮 `runs/scan-summary.json` 保存完整计划范围、全部仓库、逐仓库 Issue / PR 数及总数。实际采集标为 `scanned`，有效复用标为 `cached`，网络失败记为 `unknown`，尚未访问的项保持 `not_scanned`。开始新轮前先保留所有有效旧检查点，中断后继续；失败项重新尝试，不能把未知当空结果。统计分别报告实际扫描和缓存数，进程正常退出也不表示全目录都已成功采集。

默认范围修复及 32 仓库正式入口的实网结果见 [完整目录默认扫描验收](docs/default-catalog-scan-2026-10-18.md)。

后续正式流程实跑、查重修复及本轮发布结果见 [2026-10-18 续跑验收](docs/live-rerun-2026-10-18.md)。查重搜索不完整时，可以依据已读结果拒绝重复候选，不能据此放行实现或发布。

`catalog_sweep.py` 默认遍历核验目录中的全部研究仓库，保存完整 `scope.json` 和 `results.json`，不会把缺失项当作无问题。每仓库最多扫描 100 个开放 Issue、200 个开放 PR，并有界读取固定 SHA 的规则、源码和测试；模型分析不是运行目标项目的测试。成功采集与分诊可恢复，失败保留 `unknown` 和诊断；原有模型预算仍生效。此入口不会自动实现或发布，静态候选的政策判断仅供后续核对，正式贡献仍须读取完整上游规则、查重、复现和验证。实际运行及上游贡献见 [2026-10-18 全目录运行记录](docs/catalog-sweep-2026-10-18.md)。

`hunt` 和只读研究入口通过已核验研究目录准入，不先探测全部贡献仓库的看板权限。`research-batch` 不自行创建新任务；`auto/tick` 优先推进已有任务，再注册或复用调查并批量研究。实现、维护和研究有独立的任务额度，同一任务跨阶段不重复占用同一类额度。研究与交付分别持久轮转，研究池包括只允许研究的仓库；已有工作优先不改写研究游标。

`auto/tick` 每完成一个研究候选，就立即检查它是否通过审查、查重并进入 `READY_TO_IMPLEMENT`，在剩余实现、调用和时间额度内继续正常交付，不等整批研究耗完预算。研究结束后，未用完的研究预留可让给之前仅因预留份额而暂停的交付；真实失败、已耗尽任务和已处理候选不会借此重复执行。派发前先检查额度，有余量才读取候选的上游政策，避免实现名额用完后继续联网检查整个队列。仓库轮转、最终发布的实时政策核验和单轮总预算不变。独立 `research-batch` 仍只研究，不实现或发布。

跨周相同仓库、相同主题的调查复用既有任务，成功研究不因新一周重新调用模型。规范仓库名与 Issue 编号相同的历史任务通过 `task_aliases` 关联，不删除历史；已存在独立 PR 的任务仍保留维护。日历变化或无关默认分支 SHA 变化不是重评依据；相关源码、政策、用户需求或竞争实现出现完整新证据后才应审阅重评，失败采集仍是 unknown。

## 跨 Harness 研究与证据

`research.py` 使用已核验目录的类别和有限路径清单选择相关 peer，并对同等匹配项确定性轮转。一个任务保存其 peer 集合，续跑不会悄悄换掉比较对象。可用项目不足时会保留实际数量，但少于三个 peer 的矩阵不会进入功能实现队列。

目标文件由控制器从 `git show <base_sha>:<path>` 读取，同行文件从 GitHub 的 `contents?ref=<sha>` 读取。目标适用的贡献、安全和嵌套 `AGENTS.md` 优先纳入有限上下文；随后按主题选择源码、测试、文档及可用设计/更新材料。大于 60,000 字节的文件不进入候选读取范围；每个文件再受字符截断限制。截断以外的内容不能用于引文验证。控制器保存 `evidence_sources` 编号，绑定仓库、固定 SHA、路径和内容摘要；模型优先返回 `{evidence_id,quote}`，展开后仍精确校验引文，未知、过期或冲突编号均拒绝。旧 `{repo,path,quote}` 格式仍受相同证据门禁约束。

目标采集先在固定 SHA 上进行主题内容检索，再按源码、测试和文档配额选择材料；贡献规则单独预留位置。文件名按词匹配，避免 `minisweagent` 这样的包名让所有文件误中 `agent`。配置和初始化辅助文件不再优先挤占功能源码。研究模型只接收这些有界材料：OpenCode 拒绝全部工具，Hermes 使用非空 `-t none`（空字符串会启用平台默认工具），并拒收包含工具事件的结果。研究模型不可自行读取更多文件、联网、执行命令或委派。实现/规划入口仍使用原有受限工具权限。文件采集完成后立即保存 checkpoint；预算中断会保留已完成的目标和 peer 快照。失败 peer 可以在下一次研究重试；请求失败不会变成能力缺失。

通用周研究在首次选取目标源码前读取原有的最多 20 条近期 Issue/PR 清单，从开放 Issue 提取具体搜索主题，优先核对其中精确匹配固定版本文件清单的源码/测试路径。PR 和关闭条目不作为这一步的需求线索；Issue 文本仅帮助选材，不是事实证据、维护者批准或可执行指令。显式研究主题、既有源码检查点、已选 peer 和材料配额保持原样。

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

## 候选队列

| 队列 | 行为 |
| --- | --- |
| `READY_TO_IMPLEMENT` | 证据、政策、查重和审查通过；需要的维护者方向已确认，仍须通过原有红绿测试和发布门禁 |
| `NEEDS_MAINTAINER_CONFIRMATION` | 仅形成设计提案；政策允许时原状态机可先提交讨论，然后等待真实维护者回复 |
| `RESEARCH_ONLY` | 证据不充分、目标已有等价功能、重复或审查未通过，不可实现/发布 |
| `PAUSED_POLICY` | 明确禁止自动 Issue（`automated_issue=false`）、本人提交/审查要求（`manual_issue_submission`/`human_pr_review_required`）或高风险人工门槛，暂停自动贡献。模型仅报告 `automated_pr=false` 不再判死——该候选降级为 `NEEDS_MAINTAINER_CONFIRMATION`，经讨论 Issue 等待维护者方向，不会自动实现或发 PR |

数值优先级只在证据与政策门槛通过后产生：可实现候选优先于待确认提案，低风险略优先，功能类别本身不再固定加分。审查要求考虑明确需求、用户价值、范围、独立复现、验证成本、架构适配、候选时效及已有维护者/外部贡献观察；缺失信息保持 unknown，不编造接受概率。原有 Issue 初筛关键词分数只是分诊线索，不能代替证据、查重和政策门禁。高价值功能研究保留在单独研究份额中。

跨项目功能迁移仍要求至少三个 peer 和可验证的功能差距。本地 bug、测试、UX、可靠性及性能修复不以“另一个项目已经实现”为必要条件，但必须具备固定 SHA 的目标源码/测试引文、明确用户场景、缺陷范围和具体验证计划；目标状态 `unknown` 或仅有文档的候选仍不能进入实现。所有候选继续通过查重、审查、上游政策与真实基线复现。旧拒绝记录不会因门槛修正而自动清零或发布。

源码自发现每轮最多占用两个研究名额：从已存在且来源已核验的本地仓库选择一个源码模块和匹配测试，完整读取适用规则，固定到缓存中的 upstream SHA。注册时不 clone/fetch；真正轮到该候选才创建隔离工作区。仓库和模块游标持久保存，以源码、测试和规则内容共同标识研究范围，避免仅因日期或无关提交变化重复研究。规则、源码或测试材料不完整时保持 unknown；该通道不凭空生成跨项目功能对比，也不把“没有收集 Issue”当作查重通过。

现有功能请求通过 `feature_requests` 独立入口进入同一实现流程。当前每轮最多评估 2 个新功能或待确认功能请求，占已有 8 个实现名额；已有 PR 和进行中的实现优先。识别 `feature`、`enhancement`、明确的类型标签或标题前缀，组件标签（如 `feature:st.App`）不会误当成功能请求。缺省关闭，当前配置已开启；可用 `candidates_per_tick=0` 暂停新候选。仓库和仓库内 Issue 均持久轮换，预览不消耗轮次。

未分配只表示候选。开工前实时核查 Issue、完整讨论、认领信号、关联 PR 和仓库规则；每种讨论最多 300 条、关联 PR 最多 10 条，达到上限或失败保留 unknown 和部分证据。需要批准、分配或认领的项目仍须满足原规则，不代发认领、不把模型判断当维护者批准。旧的单一 feature 暂停任务可重新评估，其他暂停原因及失败、关闭历史不自动清除。明确要求本人提交 PR 的仓库不进入自动功能交付。

功能计划必须列出具体的验收条件，沿用原 Issue，先证明基线缺少该行为，再实现、测试、审查。Issue 范围变化须重新规划和验证；讨论或规则变化须重新审查当前计划。实现前以及远端 fork、push 和新建 PR 前重新核验。`feature-request-context.json` 保存当前采集，`feature-request-admission.json` 保存门槛结果；eligible 只代表可继续正常流水线，不代表已认领、维护者采纳或可以跳过发布门禁。

关联 PR 不等于重复实现。控制器核对关联 PR 的目标仓库、URL、编号和 head SHA，单次审查最多 10 项，每项结论须引用 Issue 与 PR 正文；跨仓下游引用、无关引用及尚缺的范围分别记录。同仓同范围竞争继续按既有贡献策略处理，已实现功能或证据不全仍不能放行。关联 PR 审查缺少必要结构时按模型格式错误有界重试，不缓存成永久的政策暂停。

发布验证过期、基线不一致或补丁变更时，保留工作区、旧验证及发布意图，回到真实红绿测试和补丁审查。创建 PR 的响应丢失时，先只读核对保存的仓库、作者、分支及精确 SHA，恢复唯一 PR 的状态后结束本次处理；不因旧验证或新政策阻塞而重复创建。身份不符、多个匹配或读取失败仍保持待核验，不推断未创建。

模型服务耗尽重试的 PLAN/REPAIR 任务，仅在后续同渠道、同模型、同用途有近 24 小时成功调用证据时，允许每任务每阶段恢复一次。旧错误、重试记录和失败/成功调用凭据完整保留；质量尝试、单任务累计调用和每轮预算不重置。实时目标仓库检查优先于旧缓存，并保存当前规则；明确退休、白名单、Ready、本人提交和人工审查要求继续生效。

实现调用的总结只用于说明，不是发布凭据。若 OpenCode 已明确完整结束、最后文本和终止事件属于同一消息，仅总结不符合 JSON 格式，则保留原始输出和异常记录，将补丁送入正常红绿验证及审查，避免重复实现。缺失结束事件、工具调用残片、服务失败和超时仍失败；规划、查重、审查等结构化决策不适用此恢复。

查重由控制器统一仓库限定，清除模型查询中的仓库名普通词和自带 `repo:`，避免假阴性；记录原始及实际查询。结果超过第一页时控制器自动翻页拉取（每查询最多 100 条），全部命中都经审查后才算完整搜索；超过上限或上游 `incomplete_results` 仍记为不完整，可以依据已读结果拒绝，不能据此放行。功能查重区分已有实现/竞争 PR 和开放的功能请求，已有报告优先复用，不重复开 Issue；竞争实现按下述自己的贡献规则判断，不能把用户需求当作维护者批准。

### 自报与自发现问题的贡献规则

当前配置启用 `contribution_strategy` 的三条规则：

1. `prefer_own_pr_on_competition`：我们自己提交的 Issue，以及本地 `hunt` 独立发现并留有来源记录的问题，遇到其他人提交的同范围 PR 仍推进自己的实现和 PR。记录竞争 PR 的范围、链接与审查；不能仅因竞争、维护者尚未表态或预计不会采纳而跳过。普通扫描得到的他人问题不自动享有该例外；已有等价修复、无效问题、证据不足等仍须拒绝。
2. `submit_issue_and_pr_together`：上述任务在上游规则允许、范围清晰、可验证时，可以同一轮先提交 Issue，再提交通过复现、补丁测试和审查的 PR，无须额外等待接纳表态。不会为了抢先省略验证，也不把本地偏好写成 `maintainer_approved`。明确要求事先批准、指定标签、人工提交/审查，以及高风险和安全披露门禁继续生效。
3. `link_pr_in_own_issue`：自己的 PR 创建或恢复后，读取自己 Issue 的最新描述，保留原内容并追加/更新 “Our implementation” 链接区块；幂等核验，失败后在后续 PR 观察中重试。只修改当前认证账号自己提交的 Issue；复用他人的 Issue 时，保留 PR 描述里的关联，不改他人的描述。

来源依据是认证账号与 Issue 作者核验，以及持久化的本地发现记录；不会据此声称已经证明全球最早发现。旧配置缺省关闭这些偏好，当前 `config.json` 已全部开启。这些贡献偏好本身不会触发远端写入；实际发布仍须明确授权并通过 `--allow-external-writes` 门禁，当前定时入口按已授权的持续调度配置传入该参数。

队列是原有状态机的附加门禁，不替代状态机。旧任务继续按 `PLAN → REPRODUCE → ISSUE/REPAIR → PATCH_READY → AUTO_REVIEW → PUBLISH → PR_OPEN` 恢复。维护者确认不能覆盖 `automated_issue=false` 或本人审查要求；`automated_pr=false` 只能阻止直接实现/发布，不阻止讨论 Issue。

仓库明确规则高于通用分诊信号，例如 Agno 必须具有 `ready` 标签，普通 `bug` 标签不能替代。`WAITING_MAINTAINER` 不再按标签或等待天数自动认定维护者批准：仅审阅真实人类维护者的有效评论，并记录评论 ID、内容摘要和方向审查结果。普通讨论、机器人评论、旧 `maintainer_approved=true` 布尔值及超时均不是批准。贡献规则允许、需求清晰且可独立复现的小修复可以按正常门禁推进，不伪造维护者背书。

## 仓库目录与准入

最新核验结果见 [repository-catalog.json](repository-catalog.json)：2026-11-01 保留已有 132 条记录，追加 9 个应用与工具项目，共 141 条记录。此前用户提供的 100 个项目仍按前端框架、UI 基础设施、AI 应用、Agent 与工作流四类组织，历史核验见 [100 项仓库扩容记录](docs/catalog-expansion-2026-10-18.md)。核验记录带 UTC 时间、规范名称、默认分支、推送时间、归档/禁用标志、许可证、固定 SHA、文件摘要/哈希、贡献政策材料及原因；本次仅核验新增项目，旧项保留原核验时间。

| 目录状态 | 数量 | 含义 |
| --- | ---: | --- |
| `research_only` | 103 | 已核验公开源码、测试、文档及可识别开源许可证，进入默认研究和扫描范围；其中白名单内 102 项标记 `automatic_contribution` / `pr_submission: allowlisted`，允许自动贡献（仍受实时门禁约束） |
| `active` | 0 | 目录核验不绕过运行时健康/政策门禁；白名单内项目的贡献授权记录在 `automatic_contribution` |
| `paused` | 13 | 迁移旧地址、长期无推送、停止维护/归档、贡献限制等，逐条记录原因 |
| `rejected` | 23 | 有界准入无法确认开源许可或源码/测试/文档覆盖，保留待审查 |
| `retired` | 2 | `continuedev/continue`、`SWE-agent/SWE-agent`，保持显式退役 |

GitHub 对复杂/混合许可证可能返回 `NOASSERTION`。这类仓库目前不自动准入，需另行逐目录许可证审查。临时失败只暂停，不永久退役；已退役记录刷新时不会自动恢复。OpenInterpreter 和 Instructor 的原名称保留迁移状态，官方规范名称另行核验后进入研究池。

- `repositories`：贡献目标白名单，保留已有 94 个目标，本次追加 8 个，当前 102 个；白名单内且目录核验为 `research_only` 的仓库记录 `automatic_contribution` 与 `pr_submission: allowlisted`，允许进入自动贡献流程，但仍需当轮健康检查、每个任务的政策判断及发布门禁。
- `repository_catalog`：完整核验目录文件路径，默认 `repository-catalog.json`。
- `peer_repositories`：可选的研究池子集，仅过滤已核验目录，不能直接授权未知仓库。
- 目录超过 `research.catalog_max_age_days`（默认 30 天）不再作为研究准入依据，需 `catalog --verify`。

本次新增 omni-tools、GeoLibre、Puter、Matcha、xtool、VoiceStudio、openGym、impeccable、NVIDIA OpenShell。九项均进入研究目录；除 impeccable 外，其余八项进入贡献白名单。impeccable 的 [上游规则](https://github.com/pbakaus/impeccable/blob/2f97a85a4a87a80066a693bd3546b092bd300772/AGENTS.md) 要求 AI 代理获得指定维护者明确指示后才可创建 Issue/PR，因此保持仅研究。Matcha 的 [贡献指南](https://github.com/floatpane/matcha/blob/8e9ba02326f5feb30b7e083f0827893bccd3623c/CONTRIBUTING.md) 禁止直接发布 AI 生成的 Issue、评论和评审，已配置人工提交 Issue。OpenShell 的 [规则](https://github.com/NVIDIA/OpenShell/blob/42671c2dd2c02b744850e0bda72b99d5509969c1/AGENTS.md) 要求外部贡献关联带 `state:accepted` 标签的 Issue，已配置标签门禁；首次贡献的人工 Vouch Request、维护者担保及其他上游要求仍须在具体任务中核验。VoiceStudio、openGym 的 CLA 要求也须由贡献者本人处理，目录准入不代表已经签署。

GeoLibre README 的“archived on Zenodo”是用于引用的版本存档，不是停止维护；本次修正了该误判并通过实时准入复核。真正的仓库归档及停止维护声明继续阻止准入。

`catalog --verify` 按每批最多 80 个名称顺序刷新完整目录，最多四个并行只读检查，目录扩容后也不会截断或因总数超过 80 而失败。名称按大小写不敏感去重；每个项目至多 25 次 API/内容读取，每次 30 秒。部分失败不影响其余记录，逐条原子保存，刷新保留原有额外候选和退役项。GitHub 的组织共享贡献指南仅接受官方 community profile 指向同一组织 `.github` 的路径，并额外固定其 SHA。

Goose 的指定看板 `Ready`、`manual_issue_submission`、`human_pr_review_required` 原样保留。无法核验看板权限仍暂停贡献；只读研究无需自动发布权限。研究目录的认可不会修改这些规则，也不会把组织共享指南当成本项目任意路径的执行指令。

## 预算、超时与观测

新增配置具有默认值，旧配置仍可运行。保留 `timeout_seconds` 作为模型/测试超时的回退；新配置按用途分别覆盖，不再笼统声称所有命令都用同一时限。

| 配置 | 默认值/约束 |
| --- | --- |
| `automation.scheduled_external_writes` | 当前 `true`，用户已授权持续调度发布；缺省/`false` 关闭，仅由 `hermes_tick.py` 转换为全局发布参数 |
| `automation.implementation_tasks_per_tick` | 当前 8；缺省沿用 `max_tasks_per_tick`，有界串行推进不同任务 |
| `feature_requests.enabled` / `feature_requests.candidates_per_tick` | 缺省关闭、当前开启；每轮当前 2（范围 0–8），占用上述实现额度，0 暂停新功能候选 |
| `automation.maintenance_tasks_per_tick` | 当前 6；独立于实现/研究额度，PR 修复进入代码阶段后还需实现名额 |
| `automation.max_infrastructure_attempts` | 默认/当前 3；基础设施故障与质量失败分别计数 |
| `automation.infrastructure_cooldown_seconds` | 默认/当前 300 秒，指数退避，每次最多 3,600 秒；耗尽后停止自动重试 |
| `automation.verification_max_age_seconds` | 缺省 86,400 秒；发布前验证收据超时须重跑红绿检查及审查 |
| `scanning.repositories_per_tick` | 当前 12；每轮实际网络采集上限，保留完整目录和持久扫描游标 |
| `scanning.cache_seconds` / `repository_checks.cache_seconds` | 当前扫描 21,600 秒、成功仓库健康检查 86,400 秒；发布前仍实时核验目标规则；旧配置缺省 0，设 0 禁用对应缓存 |
| `implementation_channel` | `opencode` 或 `dsh`；旧配置缺省 OpenCode，当前 DSH；故障不会静默退回其他执行器 |
| `model` / `review_model` | 当前均为 `tokenking/devin/swe-2`；分别供方案实现和 Hermes 审查使用 |
| `research.model` | 当前 `tokenking/deepseek-v4.1-flash`；研究初稿、纠正和目录分诊使用；缺省沿用 `model`，不影响审查 |
| `research.model_channel` | `opencode`、`dsh` 或 `hermes`；缺省 OpenCode，当前配置 Hermes；不静默切换 |
| `dsh.desktop_install_dir` / `dsh.provider_config_path` | 可选；自动寻找已安装的桌面捆绑 CLI，默认读取用户现有 OpenCode 服务商配置，仅传递本次指定模型的凭据 |
| `dsh.base_url` / `dsh.api_key_env` | 可选的模型接口地址和环境变量名；密钥不写入项目文件 |
| `dsh.context_window` / `dsh.max_tokens` | 当前 128,000 / 32,768；DSH 模型上下文及单步输出上限，项目单轮调用/时间预算仍独立执行 |
| `research.candidates_per_run` | 缺省 3，当前 3，范围 1–10；缩小单轮研究批次，优先把已有候选交付；候选上限不是 Issue/PR 发布指标 |
| `research.source_discovery_per_run` | 缺省 0，当前 2，范围 0–2；源码自发现占上述研究额度，使用已存在的固定版本源码，不额外扩大总额度 |
| `research.reserve_model_calls` / `research.reserve_seconds` | 缺省预留 2 次调用和 600 秒给研究；从单轮既有总额度内分配，不增加总预算 |
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
| `timeouts.research_model_seconds` | 当前 240 秒；仅研究生成和结构纠错，其他规划、实现、审查超时不变；缺省沿用模型超时 |
| `timeouts.stage_seconds` | 单阶段 2,400 秒；auto/task 等包含多个阶段的观测跨度不另加阶段截止线 |
| `timeouts.remote_ci_seconds` | 现有专用远端 CI 轮询最多 1,800 秒，再受运行预算限制 |
| `budgets.run_model_calls` | 缺省 16，当前 32 次模型入口调用；本轮未增加该总预算，审查计入 |
| `budgets.task_model_calls` | 单任务跨轮累计最多 40 次入口调用 |
| `budgets.daily_model_calls` | 默认 `null`，每日模型调用次数不限；设置数字可恢复上限 |
| `budgets.run_seconds` / `task_seconds` | 单轮 5,400 秒；单任务本轮 1,800 秒，耗尽后保存进度并让其他任务继续 |
| `budgets.daily_seconds` | UTC 日累计已记录运行时间最多 21,600 秒 |
| `budgets.run_cost_usd` / `daily_cost_usd` | 默认 null；当前无可信账单数据，金额限制不执行，依靠调用/时间限制 |
| `review_model` | `provider/model`，缺省沿用实现模型，默认没有切换供应商或模型 |

所有受管命令都会被当前阶段、任务、运行及日预算的剩余时间进一步截短。达到预算后保留阶段、研究 checkpoint 和已预留调用，下一轮从保存处继续，不消耗普通失败重试。单任务累计调用耗尽不会自动清零，需要检查原因，不能通过提高预算掩盖重复失败。执行耗时使用单调时钟，追踪时间使用 UTC。

单任务时间、任务内阶段时间和单任务累计调用上限使用独立的任务暂停结果，继续调度其他任务；整轮或当日时间/调用上限仍停止整轮。以上扩展没有增加当前单轮 32 次模型调用、90 分钟或每日 6 小时的总预算。

网络、限流、证书、服务不可用、模型 JSON 格式、超时、证据不足、补丁错误和政策阻塞分别记录。前六类采用独立基础设施计数与有界冷却，不增加候选质量失败次数；证据和补丁问题仍走质量重试路径，政策暂停保持原阶段。模型通道同轮两次传输/服务故障后暂停该通道后续模型阶段，仍允许不依赖模型的本地阶段。不会关闭 TLS 校验或静默切换模型。详见 [恢复和故障处理](docs/conversion-recovery-2026-10-31.md)。

依赖解析冲突（如 npm `ERESOLVE`）单独记为 `dependency_conflict`，复现阶段转回规划以修正安装方案，不原样作网络重试。历史上被误记为网络失败的同类基线安装记录，会保留完整旧错误和次数到 `failure_reclassifications`，按一份失败方案累计原有阶段次数，再转回规划；达到原阶段上限则停止。真实服务故障、DNS 故障及终态任务不会因此清零或复活。

每条调用记录是一次 **OpenCode/Hermes CLI 入口调用尝试**，在启动前预留，启动失败也消耗调用预算，不等于单个 HTTP 模型请求。OpenCode 一次入口内部可以有多个模型步骤；日志可取得时另记 `reported_provider_steps`。入口预算和运行时限是硬门槛，当前不提供每个内部步骤的实时费用熔断。研究入口禁用工具，不能依靠内部工具循环扩大源码读取范围。

原生日志提供 token 时统计输入（含缓存读写输入）和输出；缺失即 null。当前适配器不提供可核实的账单费用，OpenCode 的零费用占位值也不作为实际成本。未完成的调用保留 `running`，不算成功。不同审查模型只表示配置身份不同，不等于已证明其判断质量独立；不可用配置会失败关闭，不自动换模型。

`metrics` 显示最近运行、阶段耗时/结果/原因、CLI 调用和 token、候选类别/证据/多 peer 完成度/重复尝试/政策队列，以及实现、红绿验证、审查、PR、CI、维护者回应和合并的独立口径。比例带分子、分母和 scope，无样本时为 null。CI 通过仅表示观察到的检查通过，不推断未查询的分支保护要求；开放 PR 标为待观察。

`metrics/status` 的 `pr_maintenance` 和每次跟进保存的 `runs/pr-maintenance.json` 持续列出已观察但没有有效任务关联的开放 PR，附精确 head、观察时间、CI 和维护者回应信号。即使 PR 没有新变化，缺口仍保留；报告不会自动接管手工发布的补丁，须先核对作者、远端版本和本地工作区身份。

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

首次初始化 `Pipeline` 自动执行兼容旧数据的增量迁移。研究和观测表继续保留，当前另增加 `task_aliases` 及 `runs.owner_pid`；不删除、重建或清空 `state/tasks.db`，不改历史任务 ID。重复任务通过别名关联，历史任务、事件、候选及发布记录均保留。

分析使用 SQLite backup API 保存一致性快照，不直接分析活动生产连接；修改生产状态前先核实控制器及子进程已停止并再次备份，避免只复制 WAL 数据库主文件。2026-10-31 的真实历史副本包含 20,031 条任务，相关历史表整行哈希保持不变，两次迁移与恢复幂等，`integrity_check=ok`；详见 [历史兼容与调度回放](docs/conversion-history-replay-2026-10-31.md)。该副本验证不表示生产库已执行恢复。数据库和备份由 `.gitignore` 排除。

阶段/调用使用唯一 ID，结束时更新原记录；多次查看统计不累计计数。被强制终止的运行可能留下 `running`，已保存耗时只是最后检查点。`recover_interrupted` 必须确认记录的 owner 和锁内父/子进程均已停止；旧版无 owner PID 的行默认不恢复，需完成进程核验后显式提供 `legacy_processes_checked=True`。恢复记录中断原因和保存的任务阶段，不推算缺失耗时，也不清除失败记录。矩阵和上下文文件通过原子替换保存。

## 保留的安全与发布门禁

- 基线 worktree 上真实出现指定回归断言，补丁 worktree 上同一测试通过，项目检查通过。
- 验证、审查、发布使用 `tree-v2:<Git tree>` 内容身份；未跟踪、暂存、已提交和重启后的等价内容保持一致，真实内容变化必须重新验证和审查。旧格式收据不能自动换算，必须重跑。
- worktree、进程/子进程锁、有限阶段重试及 SQLite 恢复保持有效。
- Issue/PR 创建前持久化意图；响应丢失后按原身份对账，未找到时最多三次成功查询、间隔 15 分钟，之后转人工核验；网络失败不等同于空结果，pending 意图不会自动清空重发。
- 提交前保存预期 tree；提交成功但响应丢失可恢复原提交。推送失败后按精确提交 SHA 核对远端分支，不强推；钩子改变内容会拒绝复用批准。
- 发布文本和补丁继续检查凭据；安全候选保留本地，不能进入公开披露。
- 本人提交、本人工程审查、看板门槛及自动贡献禁令高于模型或一般维护者回复。
- 旧 PR 跟进、精确 head SHA 的 CI 观察、特殊 OpenCode Linux 验证收据继续保留。该远端 CI 配方仍只适用于原有特定任务，不是通用 CI 服务。

worktree 不是操作系统沙箱。实际测试/依赖安装会在本机执行；研究入口全部禁用模型工具，但实现阶段仍需可信的执行环境。`hermes_tick.py` 继续调用相同 `tick`，按 `automation.scheduled_external_writes` 选择是否传入发布参数，并报告本轮权限状态；修改开关从下一次启动生效。克隆仓库不复制本机调度，也不自动启动 gateway。

## 验证与边界

完整测试使用 `python -X utf8 -m unittest discover -v`。测试分为三类报告：临时 Git/bare remote、模拟 GitHub/模型接口的回归测试；完整真实历史数据库副本的兼容与调度回放；真实网络/模型/上游源码上的有界运行和补丁验证。三类不能相互替代，模拟发布不计为上游提交，调度额度通过不计为完成修复。

早期工程记录中的 96→139 项测试、1,025 条旧库记录等是 2026-10-17 的历史结果，不是当前验收数量。2026-10-31 的检查、真实验证、剩余事项和最终测试结果以 [当前工程记录](docs/conversion-progress-2026-10-31.md) 为准；未完成项不会据本地测试或机器人回应宣称完成，也不据短样本宣称合并率提高。

当前限制：有界路径选择可能遗漏实现，此时必须保持 scope 限定或 unknown；复杂许可证与组织规则仍需人工进一步核验；政策的自然语言语义和功能价值依赖有证据的审查；默认审查模型相同；可靠费用不可获取；CLI 调用预算不是每个模型内部步骤的硬计数器；历史运行不补造指标；统计只能在未来真实贡献得到回应/合并后反映接受率。

详细审计、执行记录与最终验收见 [工程交付记录](docs/engineering-delivery.md)。

## 真实运行验收（2026-10-18）

本轮真实 GitHub / 模型调用的发现、失败和修复记录见 [真实研究报告](docs/live-research-2026-10-18.md)。模型结构或引文校验失败时，保留原始输出并最多请求一次纠正，纠正使用相同材料、计入预算并重新校验；不补造证据。旧提示词造成的顶层 `feature_status` 可兼容读取，但冲突映射、未读取路径和不匹配引文仍拒绝。

完整扫描与自动推进的后续验收见 [流程运行与恢复报告](docs/live-pipeline-2026-10-18.md)。搜索诊断清单现在有独立大小上限，旧检查点可恢复；Windows 原子替换遇到短暂文件占用时有限重试，持续失败仍保留旧记录并报错。

规划重试会把保留的本地草稿与固定 SHA 基线分开提供，避免将自己上次生成的测试误认成上游已有覆盖。JSON 语法错误与证据/结构错误共享一次研究纠正额度，纠正仍失败时停止。

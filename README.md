# OSS Contributor

Hermes + OpenCode + GitHub CLI 驱动的开源贡献流水线，覆盖 Qwen Code、Gemini CLI、OpenCode、Cline、OpenHands、Kilo Code、Goose 和 mini-SWE-agent。上游规则允许的流程自动推进；要求讨论、人工提交或本人审查的项目保留对应约束。两个模型入口均使用 tokenking 的 `devin/swe-2`，不设置模型费用上限。

## 自动流程

```text
启动核验维护状态与贡献政策
  → 扫描已有 Issue / 每周主动调查
  → 研究代码/文档/测试/更新/反馈，横向比较并核对贡献规则
  → 设计提案：查重与独立研究审查 → 创建 Issue → 等待维护者方向
  → 生成回归测试与执行配方
  → Hermes 审查测试配方
  → 运行基线测试，确认预期失败
  → Hermes 核查复现证据
  → 低风险新发现：查重 → 创建 Issue（政策要求时等待维护者）
  → 已有 Issue 或获准的新发现：修复
  → 同一回归测试在基线上失败、在补丁上通过 + 项目检查
  → Hermes 独立审查补丁
  → 查重 → 创建 fork → 提交/推送 → 创建 PR
  → 监测 CI 和维护者评审 → 自动修改、重新验证、推送更新
  → 记录上游合并或关闭
```

上游允许自动贡献时，正常流程不需要人工生成测试配方、批准审查、执行发布命令或转交评审意见。模型给出结构化决策，控制器执行命令、检查退出码和补丁指纹，再决定是否发布。上游要求的本人审查不能用模型独立审查替代。

新发现先经过只读研究、查重和独立审查。小型 Bug、测试、性能和 UX 修复只有在风险低、政策允许时才可在复现后创建 Issue 并继续修复；功能、架构或存在争议的设计先创建研究提案，等待维护者明确同意后才编写测试和实现。必须先讨论的已有问题仍会等待。上游最终是否接受、合并 PR 由维护者决定。

### 主动研究

`hunt` 登记本地任务，`analyze <hunt任务ID>` 只运行研究与分析，不发布 Issue 或 PR。`auto` / `tick` 在同一个 PLAN 阶段消费研究结果，复用原有恢复、复现、验证、审查和发布机制，无新数据库表或第三方依赖。

研究会读取目标 checkout 的代码、文档、测试、贡献及安全规则，抓取近期 Issue/PR 和更新记录，并从八仓库名单中轮换选择一个可访问的 Harness 比较官方材料。同行材料固定提交 SHA，最多选择四个代码、测试或设计文件；目标证据核对实际文件引文，不能把表面缺失当作未实现。功能提案必须有同行实现证据，同时检查目标项目的定位、替代实现和设计文档。

每个候选的背景、证据、受益用户、预期效果、实现范围、验证方式、风险、成本和接受可能性记录在 `runs/<任务ID>/opportunity.json`；上下文在 `research-context.json`，包括已关闭条目的定向查重结果在 `duplicate-search-issue.json`。近期列表只作研究线索，不能替代发布前定向查重。证据不足、查重失败或政策不明时不会发布；可利用的安全候选仅保留本地记录，遵守上游私密披露政策，不创建公开安全 Issue。`automated_pr=false` 表示政策禁止或无法确认自动实现/发布，维护者的一般回复不能绕过此禁令；设计方向未确认单独使用 `maintainer_required` 表达。

大功能提案明确标注静态证据、测试待执行，不伪称已经复现。维护者批准后回到 PLAN 编写回归测试，PR 仍须通过原有基线失败、补丁通过、独立审查和指纹门槛。旧任务和旧每周调查 ID 继续恢复；已执行到 ISSUE 的旧任务保持等待维护者的保守行为。

## 已接入的 8 个上游仓库

所有仓库均属于 AI 编程 Agent、Harness 或自主软件工程工具生态。

| 仓库 | 主要研究方向 |
| --- | --- |
| [QwenLM/qwen-code](https://github.com/QwenLM/qwen-code) | 多模型编程 Agent、工具调用 |
| [google-gemini/gemini-cli](https://github.com/google-gemini/gemini-cli) | 终端 Agent、上下文管理 |
| [anomalyco/opencode](https://github.com/anomalyco/opencode) | 通用 Coding Harness、会话与模型适配 |
| [cline/cline](https://github.com/cline/cline) | 自主编程、工具执行与权限控制 |
| [OpenHands/OpenHands](https://github.com/OpenHands/OpenHands) | 自主软件开发、真实工程任务 |
| [Kilo-Org/kilocode](https://github.com/Kilo-Org/kilocode) | Agent 工作流、多模型与开发工具集成 |
| [aaif-goose/goose](https://github.com/aaif-goose/goose) | 通用 Agent、MCP 扩展与工具执行 |
| [SWE-agent/mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) | 自动修复 GitHub Issue、测试与任务验证 |

每轮扫描核验通过的配置仓库，优先跟进已发布 PR 和等待中的 Issue。消耗研究额度的任务按有效贡献仓库轮转，仓库内部优先处理已有 PR 的修复、进行中的任务，再按评分选新任务。轮转位置保存在 `state/tasks.db`，跨进程、跨轮次延续，失败或中断的尝试也会让出下一次机会；没有候选的仓库自动跳过。`auto` 和关闭自动执行后的 `tick --analyze` 共用轮转位置，`auto --dry-run` 只预览、不占用轮次。默认每轮仍只推进一个研究任务，有候选且满足贡献政策的仓库依次获得机会，不因全局分数差异长期偏向一个仓库；这保证研究机会均衡，不保证相同 Token 消耗或 PR 数量。每个允许自动贡献且无需本人提交新 Issue 的仓库每周生成一个主动调查任务。初筛每仓库取最近 100 个开放 Issue、200 个开放 PR；发布前再做针对性搜索，发布恢复则通过直接 API 分页查找本人 Issue 和确定的 PR 分支。

`scan` 只读访问 GitHub，并按仓库提交本地任务更新；单仓库网络、权限、限流或响应格式异常会回滚该仓库任务更新，继续扫描其余仓库。标准错误和数据库 `events` 中的 `scan:owner/repo` 记录区分 `OK`、`EMPTY`、`FAILED` 和归档跳过。贡献指南优先取根目录 `CONTRIBUTING.md`，404 时通过 GitHub community profile 查找其他位置；没有指南会警告并继续，研究时仍须读取 checkout 中适用的贡献规则、`AGENTS.md` 和测试说明。快照文件不是事务的一部分，失败时应以扫描事件判断本轮是否成功。

从三个仓库增至八个后，单轮初筛上限从 300 个 Issue / 600 个 PR 增至 800 / 1,600，网络请求、快照空间和任务量相应增加；研究任务预算保持每轮一个。启动检查额外读取元数据、官方 README 和贡献指南，进程内复用结果；每个新进程重新核验，失败时不沿用上一轮的活跃结论。数据库结构、仓库内评分及去重键不变。

### 维护状态、退役与贡献政策

`scan`、`followup`、`tick`、`auto` 和诊断执行入口共享 `effective_repositories()` 的核验结果。仓库归档、禁用、Issue 关闭、标识迁移、官方 README/贡献指南明确停止维护或迁移，以及超过 `repository_checks.max_inactive_days`（默认 180 天）无推送时，暂停其自动贡献并记录原因；网络、权限、限流或响应异常也会暂停本轮处理。短暂故障可在下一进程重新核验恢复。180 天阈值是可配置的维护复核条件，不能单凭推送次数判断贡献价值。

核验依据和暂停原因保存在 `state/repositories.json` 与数据库 `events`，原文快照在 `runs/snapshots/`；`status` 显示上次核验结果而不发起网络请求。暂停以仓库级状态执行，不改写历史任务阶段、评分、重试次数或执行证据，也不自动把历史 Issue 迁移到新仓库。官方自然语言声明的识别是有限规则，仍需结合计划阶段对实际 checkout、讨论和贡献政策的检查；不会自动信任迁移目标并替换白名单。

- `continuedev/continue`：其[官方 README](https://github.com/continuedev/continue#readme) 明确停止积极维护并只读，已移出活跃名单。
- `SWE-agent/SWE-agent`：其[官方 README](https://github.com/SWE-agent/SWE-agent#readme) 说明主要开发转向 mini-swe-agent，已移出常规自动贡献名单。

两者的退出原因与来源保留在 `repository_checks.retired`；即使误加回扫描配置也不会自动恢复贡献。历史任务及数据库不删除、不重建。

[Goose 贡献指南](https://github.com/aaif-goose/goose/blob/main/CONTRIBUTING.md) 要求 Issue 在指定看板达到 `Ready` 后才能开始实现或提 PR，并要求贡献者本人提交 Issue、在发布前亲自审查代码。控制器按准确的项目 URL 核验开放 Issue 的 `Ready` 状态，不把开放状态或普通标签当作批准；新发现不会自动提交 Issue，也不生成无法自动推进的每周调查任务。由于当前流程没有本人审查记录入口，Goose PR 发布会暂停，不能用独立 AI 审查代替本人审查。核验看板权限失败时仍允许只读扫描，但不派发 Goose 自动修复任务。2026-10-17 本机 token 缺少 `read:project`，已确认会触发此暂停；程序不会修改认证或请求权限。mini-SWE-agent 的贡献指南位于 `docs/contributing.md`，发现的实际路径也会传给规划和独立审查。

2026-10-17 核验时，新名单八个仓库均可访问且未归档。OpenHands 当前 README 将本仓库定位为 Agent Canvas，核心 Python SDK 在独立仓库中；研究时须确认 Issue 对应模块。

## 过往 PR 跟进

`followup` 读取核验通过仓库中本人最近 100 条 PR 的状态；已记录但后来退役或暂停的仓库中，未结束 PR 仍会被只读观察。单仓库失败保留其上次结果并继续其余仓库。对开放 PR 分页读取普通评论、行内评论和正式评审，包括机器人反馈。报告写入 `state/prs.json` 和 `runs/pr-updates.json`，无变化时后者清空；确认已发布 PR 合并或关闭后，同步登记的任务和自动流程最终状态，保留其余执行数据。它不会向上游发消息、改代码或重新发起贡献。

`tick` / `auto` 优先处理本流水线登记的 PR：判断尚未处理的反馈，区分代码修改和描述修改；模板标题缺失由程序直接检查，不能被绿色 CI 或模型漏判掩盖。描述修改直接更新同一 PR；代码修改需重新测试、独立审查后推送原分支。外部历史 PR 会被观察，但没有本地任务、基线和测试配方的 PR 不会自动接管修改。贡献者或机器人评论不等于新 Issue 的维护者批准。

已配置的 OpenCode Linux 验证收据可设置 `auto_refresh: true`：补丁变化后，自动把精确补丁提交到私有控制仓库的独立 `ci-*` 分支，启动验证工作流，成功后更新收据。工作目录和原暂存区不变；重试会复用同一次运行。此远程配方目前专用于 OpenCode #55694，其他贡献仍使用自己的测试配方，不能视为已具备通用跨平台 CI 自动配置能力。

## 快速开始

需要 Python 3.11+、Git、已登录的 GitHub CLI、已配置 tokenking/devin/swe-2 的 OpenCode 和 Hermes。Python 无第三方依赖。Hermes 需支持 `chat --query-file --format stream-json`。已验证运行环境为 Windows。

```powershell
gh repo clone jadelike-wine/oss-contributor
cd oss-contributor
gh auth status
opencode models tokenking
python -X utf8 -m unittest -v

# 仅预览接下来会处理的阶段，不调用模型或发布
python -X utf8 contribute.py auto --dry-run

# 扫描、跟进并自动推进任务（可创建公开 Issue/PR）
python -X utf8 contribute.py tick

# 不重复扫描，直接推进已有队列
python -X utf8 contribute.py auto
```

`config.json` 的 `automation.enabled` 控制自动执行。默认每轮推进一个新任务，每个失败阶段最多尝试 3 次，单条模型/测试命令超时 20 分钟。复现失败会回到规划阶段调整配方，验证或审查失败会回到修复阶段；耗尽尝试次数记为 `FAILED`，其他任务继续。

OpenCode 从 PATH 查找，Windows npm 安装会自动解析到实际 EXE；特殊布局可在配置中指定。凭据由 CLI 管理，不存入仓库。安装依赖和运行测试是本机真实执行；worktree 隔离代码变更，但不是操作系统沙箱。

Windows 的 npm/pnpm/Corepack 会转换为已存在的 Node 脚本入口，测试配方必须把每个参数写成单独的字符串。需要 Bun 的项目应按上游 `packageManager` 安装指定版本；也可安装到本项目的 `state/tooling`（例如 `npm install --prefix state/tooling bun@1.4.2`），控制器会自动发现它。

执行器禁用目标项目的 OpenCode 运行配置，避免上游自定义工具与本机 CLI 版本冲突。模型仍必须读取并遵守 `CONTRIBUTING.md`、适用的 `AGENTS.md` 和测试规范。

## 调度

`hermes_tick.py` 调用同一个 `tick`，无需额外人工步骤。原开发机器已配置每 6 小时执行的 Hermes no-agent 任务 `oss-contributor-qwen`；名称沿用首版，读取配置后覆盖全部八个仓库。调度本身不调用模型，具体阶段按需调用 OpenCode 和 Hermes。

```powershell
hermes cron list
hermes gateway status
```

克隆不会复制本机调度任务或自动启动 gateway。新机器可直接运行仓库中的 `hermes_tick.py`；复制到 Hermes scripts 目录时需设置 `OSS_CONTRIBUTOR_ROOT`，并可用 `OSS_CONTRIBUTOR_PYTHON` 指定解释器。机器离线时不执行。无变化时保持安静，结果保存在本机。

## 可追踪与恢复

- `state/tasks.db`：任务、阶段、尝试次数、发布意图、Issue/PR 标识、进程锁。
- `runs/queue.md`：当前队列。
- `runs/<任务ID>/`：模型输入/输出、计划、测试配方、复现和验证日志、审查结论、Issue/PR 正文。
- `workspaces/`：独立任务 worktree 和用于修复前对照的基线 worktree。

核心状态为 `PLAN → REPRODUCE → ISSUE/REPAIR → PATCH_READY → AUTO_REVIEW → PUBLISH → PR_OPEN`。`WAITING_MAINTAINER` 自动复查；`SKIPPED/FAILED/CLOSED/MERGED` 为终止结果。没有等待操作者确认的审查/发布状态。

模型调用的 `.jsonl` 和 `.stderr` 在运行过程中直接写入，超时后仍保留部分输出；控制器也会即时打印阶段变更，便于判断停在模型、测试还是发布步骤。

测试子进程使用 `state/test-tmp` 作为临时目录。Windows 基线 worktree 使用短目录名，并为 Git 启用长路径支持，避免上游较长的文件名无法检出。

本次 OpenCode 实跑还提供了 `.github/workflows/verify-opencode.yml` Linux 验证入口：读取 `ci/patches/opencode-55694.json` 固定的上游提交与补丁哈希，运行包测试及根目录 `bun run check`。这是针对该贡献的验证任务，其他补丁需更新清单和补丁文件后重新运行。`verify_ci.py` 会核对 Actions 成功状态、工作流、控制器提交、上游提交和当前补丁哈希；旧 CI 结果不能用于批准新补丁。

发布前必须同时满足真实验证通过、审查批准、补丁指纹一致。创建 Issue/PR 前持久化发布意图；响应丢失时先查找远端结果，不能直接再次创建。若远端结果始终无法确定，会保留待核对状态继续自动查询，宁可不发也不重复发。反馈修复沿用同一分支和 PR。

进程锁同时记录控制器与当前子进程 PID。自动模式只回收两者都已退出的锁；子进程仍在运行时不会重复派发。耗尽重试、身份/权限错误、无法满足仓库要求等情况会留下原因，不会伪造成功。

## 诊断入口

```powershell
python -X utf8 contribute.py status
python -X utf8 contribute.py scan
python -X utf8 contribute.py followup
python -X utf8 contribute.py hunt QwenLM/qwen-code "Investigate tool cancellation"
# 使用上一条输出的任务 ID，仅运行研究分析
python -X utf8 contribute.py analyze <任务ID>
```

旧的 `analyze/repair/verify/review` 命令保留用于单阶段诊断，不是自动流程的必经人工步骤。新任务优先使用 `tick` 或 `auto`。

若本地存在 `state/windows-ca.pem`，OpenCode 会在未配置 `NODE_EXTRA_CA_CERTS` 时加载它。证书不随仓库分发，也不会关闭 TLS 校验。

## 验证覆盖与限制

2026-10-17 主动研究实跑在 mini-swe-agent `04d809ceab9df28f9adaed044884180159172930` 发现 Windows 超时后仅终止外层进程、继续等待子进程输出的问题。原始 `_run` 函数的本地有限时诊断设置 0.2 秒超时、子进程运行 3 秒，实际 3.094 秒后返回；这是单函数诊断，不能当作完整项目测试。对照 Goose 官方错误恢复文档，并检查 #826、已合并 #865、开放 #995 和开放/关闭条目后，独立审查批准了 [Issue #1000](https://github.com/SWE-agent/mini-swe-agent/issues/1000)。任务保持 `WAITING_MAINTAINER`，进程终止设计待维护者确认，本次没有提交该问题的 PR。

该次升级全部 89 项单元测试通过；新增研究证据核对、功能提案先讨论、低风险任务完整推进、自动贡献禁令、私密安全候选、旧每周 ID 复用、响应丢失恢复和 CLI 异常类型一致性覆盖。原有 549 条任务保留，新增一个研究任务，SQLite 完整性正常，未增加数据库表。

2026-10-16 真实实跑创建了 [OpenCode PR #56027](https://github.com/anomalyco/opencode/pull/56027)，修复 OpenRouter 上游错误详情被隐藏的问题。基线回归测试 24 过、2 失败，补丁后 26 过；AI 包 1,818 过、28 跳过，Linux CI 根目录检查通过。该次调通中修复了 Windows、模型输出解析、查重及发布兼容问题，并配置了针对该补丁的 Linux 验证任务；这不代表所有后续候选都已验证能无人介入完成。

测试包含真实的本地 Git 仓库、基线失败/修复通过、提交与推送，以及模拟 GitHub/模型接口的完整状态流。覆盖新 Issue 等待后恢复、审查拒绝、过期补丁拒绝、失败次数限制、发布响应丢失后的去重恢复和进程锁恢复。

`python -X utf8 -m unittest -v test_scan` 验证精确的八仓库白名单、100/200 上限、来源关联、评分和排序、竞争 PR 暂停、重复扫描、旧任务 ID/状态兼容，以及异常隔离、事务回滚、空结果、归档和贡献指南缺失/路径发现。全部回归测试仍使用 `python -X utf8 -m unittest -v`，不需要第三方 Python 包。

`python -X utf8 -m unittest -v test_repository_policy` 另外覆盖共享有效名单、维护状态变化和恢复、旧活跃缓存失效、Goose 看板权限与准确的 Ready 门槛、非根目录贡献指南、旧任务/执行证据保留，以及退役 PR 的只读跟进和最终状态记录。其他状态机测试模拟健康检查以隔离网络，不把模拟响应当作真实访问结果。

初筛评分保留原有 bug/优先级标签、工具/会话/路径关键词和复现描述加分；指派、敏感标签、功能请求和相关开放 PR 会暂停任务。后续研究重新确认 Issue 开放状态，检查讨论和代码，要求可复现的基线失败与补丁通过，并在发布前搜索包括已关闭条目在内的重复 Issue/PR。初筛尚不理解各项目的全部标签、语义相似方案或维护者接受状态，也不按近期维护活动加权（快照保留 `pushed_at`）；评分不能代替这些研究步骤。后续可先评估各仓库标签和候选质量，再按仓库可选配置做增量改进，避免直接改变现有评分。

这些测试不等于真实上游已接受贡献，也不保证所有问题都能自动解决。生产发布仍取决于实际模型、测试环境、仓库规则和 GitHub 权限。运行记录、数据库、凭据、证书和上游 checkout 不上传到本仓库。

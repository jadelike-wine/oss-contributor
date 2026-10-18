# 2026-10-18 正式流程续跑验收

本次从干净的 `main`、提交 `4f2dfd2` 开始，使用现有 GitHub 登录、模型配置、贡献白名单和预算运行。日期、时间按 Asia/Shanghai 记录。既有 ChatDev PR #689 和 OpenCode PR #56027 不计为本轮新贡献。

## 扫描范围

实际执行 `python -X utf8 contribute.py tick`，运行 ID 为 `5a030045214749c6b845948dc6cba18f`，北京时间 13:23:54–13:37:43，耗时 829.687 秒。扫描 8 个白名单仓库，读取 738 条开放 Issue、1,394 条开放 PR，更新 25 条本地任务。每仓库仍最多读取 100 条 Issue、200 条 PR；这不是全目录或全部历史扫描。

Goose 的 Ready 看板因令牌缺少 `read:project` 无法核验，继续暂停自动贡献。mini-swe-agent 的已有提案 #1000 本轮核对时没有维护者回复，不能据此批准实现。

## 查重卡点与修复

复核既有 OpenCode 研究失败材料时，发现第一条查询已返回 3 项，包含相同问题 #55378 及候选修复 PR #41898；第二条查询返回 167 项。旧逻辑在审查前直接报错，已读取的重复证据不能被用于排除候选，重试会再次停在同一位置。错误消息声称会自动细化，实际没有相应实现。

修复后仍限制两条查询、每条最多 20 项。控制器保存 `search_complete`，允许审查者依据已读结果拒绝重复候选。任何不完整搜索都不能放行，即使模型返回 approved 或建议复用某个功能 Issue，也会在采纳之前抛错。没有候选可审查时同样保持阻断。错误消息改为要求检查并细化保存的查询，不再声称自动完成。

新增回归覆盖：167 项搜索中已读重复项可被拒绝；GitHub 的 `incomplete_results`、超限和空返回均不能取得通过；功能 Issue 复用不能绕过完整性要求；后续过宽查询不能丢弃先前已读的重复证据。修复前两项回归实际报错，修复后查重专项 9 项通过。

本轮 tick 的自动阶段实际加载了修复后的模块。13:31 的 OpenCode 续跑复用已保存研究提案，两条查询仍为 3 / 167 项，审查实际读到 #46880、#37735 等同类报告和 #41898 修复，返回拒绝，任务进入 SKIPPED，流程继续推进。`duplicate-search-issue.json` 的 `search_complete=false`，没有提高结果上限，也没有新建重复 Issue。

## 第一轮候选与推进

8 次真实模型入口调用（研究、纠正、审查及规划）后，调度进程退出码 0，但运行观测为 failed；不能以进程正常退出宣称贡献成功。

| 任务 | 本轮结果 |
| --- | --- |
| `SWE-agent--mini-swe-agent--hunt-52af6cfd2dec` | 提出轨迹恢复功能，读到现有 #963，静态审查通过；政策记录不允许自动 PR，进入 PAUSED_POLICY。没有重复开 Issue，没有实施。 |
| `QwenLM--qwen-code--hunt-5b1979b3c5ba` | 首次政策引文未匹配，一次纠正后仍缺少同行实现证据，保留 PLAN 和失败材料，RESEARCH 累计尝试为 2；没有发布。 |
| `anomalyco--opencode--hunt-5b1979b3c5ba` | 修复后的查重识别已有重复报告，SKIPPED。 |
| `cline--cline--14789` | 固定基线 `a42df8c8d5fd9d9f9a9073589baffe32ea9bb2f0`，生成 Windows MCP 命令路径含空格的测试草稿，方案审查通过；`bun install --frozen-lockfile` 依赖链接 ENOENT、grpc-tools 缺少 nopt，基线安装失败，未执行回归，保留 PLAN，REPRODUCE 尝试为 1。 |

已有 OpenCode PR 和 mini-swe-agent #1000 继续观察，未取得新的维护者实施批准。Cline 草稿仅在本机忽略的任务工作区；它没有基线失败证据，不能算作已验证补丁。

## Cline 定向续跑

第一次续跑 `a38fdeccf7844e18a30f6fda50a533de`：北京时间 13:39:07–13:46:44，457.781 秒，2 次真实模型入口调用。控制器向规划补充了磁盘空间充足、Bun 支持替代链接布局和过滤安装的诊断。模型重新规划并通过方案审查，但仍返回原来的安装命令；再次安装出现 ENOENT/EEXIST 和 `husky` 缺失，任务回到 PLAN，REPRODUCE 尝试累计为 2。未执行目标回归，不能声称缺陷已复现。

第二次续跑 `2dde08359f7b442ba70de46126e2fc91`：控制器只修改安装 argv，保留 `--frozen-lockfile`，加入 `--linker hoisted --backend copyfile`，过滤到 SDK 工作区和根包。选项来自已安装 Bun 1.4.2 的 help，另核对 [安装文档](https://bun.com/docs/pm/cli/install) 和 [工作区文档](https://bun.com/docs/pm/workspaces)。重新审查测试方案实际返回 approved 后，才恢复 REPRODUCE。此处有执行代理的定向干预，不能宣称原规划模型自己修好了安装过程；两次失败计数没有重置，也没有修改清单或锁文件来绕过失败。

该次运行于北京时间 13:47:42–13:51:37，235.297 秒，1 次真实审查调用。安装实际成功：871 packages installed，90.82 秒。随后 `build:sdk` 在 `@cline/llms/src/providers/vendors/anthropic.ts:36:7` 出现 TS2322，`FlexibleSchema` / `schemaSymbol` 类型不兼容，构建返回 2；core、agents、shared、sdk 等构建成功不代表整个 SDK 构建通过。尚未在原安装布局上建立同一构建错误的对照，不能断言其上游 CI 必定失败。任务按已有三次阶段失败上限进入 FAILED；没有清零计数继续自动实现。

## 单独执行目标测试的诊断

运行 `98ca618aca5545eb84dc3a49388cba74`（北京时间 13:52:47–13:52:57，9.437 秒）仅执行原方案的目标回归，未改变 FAILED 状态，也未签发 `reproduction.json`。命令实际退出 1：目标测试 `connects a stdio MCP server whose command path contains spaces` 失败，21 项因 `-t` 过滤跳过，Vitest 报告 4.66 秒。

失败来自真实子进程：Windows 把测试目录中 `dir with space` 的可执行路径截断到 `dir`，随后 MCP 服务 `spaced-path-server` 以 code=1 退出。这与路径含空格的假设一致，建立了局部诊断复现；不是依赖找不到或测试收集失败。但 SDK 构建仍未通过，没有修复后的绿色结果、完整检查或发布审查，因此没有把这个诊断转换为可发布补丁。

Cline 工作区最终只修改 `sdk/packages/core/src/extensions/mcp/client.test.ts`，42 行测试草稿；生产代码、清单和锁文件没有修改。草稿保留在本机忽略的工作区，未向上游提交。

## 本地验证

- 修复前全量：175 项通过，129.590 秒。
- 修复后全量：178 项通过，125.382 秒，无失败或跳过。
- 日志：`runs/acceptance-rerun-20261018-baseline-tests.log`、`runs/acceptance-rerun-20261018-final-tests.log`。
- 查重红绿日志：`runs/acceptance-rerun-20261018-duplicates-red.log`、`runs/acceptance-rerun-20261018-duplicates-green.log`。

## 最终结果与证据

本轮包含一轮正式 tick、两次 Cline 定向续跑及一次独立测试诊断，合计 11 次真实模型入口调用。项目实际读取 Issue、提出候选、完成查重，并进入回归规划和真实安装/构建阶段；本轮新建上游 Issue 为 0、PR 为 0，没有验证自动发布闭环成功。旧 ChatDev PR #689 仍为 OPEN，查询时没有远端检查项；它不计为本轮新贡献或 CI 通过。

本项目的查重卡点已修复并在同一轮正式流程中实网验证，完整回归 178 项通过。最终 SQLite `integrity_check=ok`，进程锁为空。实现/测试门禁、维护者批准要求、原有预算和重试上限均保留。

除上面列出的测试日志，本机忽略目录保存：

- `runs/acceptance-rerun-20261018-tick.log`
- `runs/acceptance-rerun-20261018-cline-resume.log`
- `runs/acceptance-rerun-20261018-cline-linker-recovery.log`
- `runs/acceptance-rerun-20261018-cline-test-diagnostic.log`
- `runs/cline--cline--14789/acceptance-first-reproduce-0.log`、`acceptance-second-reproduce-0.log`、最终 `reproduce-0.log` 和 `reproduce-1.log`
- `runs/cline--cline--14789/acceptance-target-diagnostic.json`（明确标记 `diagnostic_only`、`setup_passed=false`）
- `state/tasks.db` 中上述运行 ID、阶段、调用、任务状态及错误记录。

测试夹具的模拟发布不计为上游成果。本项目代码和报告直接提交到 main；最终交付消息给出提交和推送结果。

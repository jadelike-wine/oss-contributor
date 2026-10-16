# OSS Contributor

Hermes + OpenCode + GitHub CLI 驱动的全自动开源贡献流水线，覆盖 Qwen Code、Gemini CLI 和 OpenCode。两个模型入口均使用 tokenking 的 `devin/swe-2`，不设置模型费用上限。

## 自动流程

```text
扫描已有 Issue / 每周主动调查
  → 读取贡献规则和讨论
  → 生成回归测试与执行配方
  → Hermes 审查测试配方
  → 运行基线测试，确认预期失败
  → Hermes 核查复现证据
  → 新发现：查重 → 创建 Issue → 自动等待维护者反馈
  → 已有 Issue 或获准的新发现：修复
  → 同一回归测试在基线上失败、在补丁上通过 + 项目检查
  → Hermes 独立审查补丁
  → 查重 → 创建 fork → 提交/推送 → 创建 PR
  → 监测 CI 和维护者评审 → 自动修改、重新验证、推送更新
  → 记录上游合并或关闭
```

正常流程不需要人工生成测试配方、批准审查、执行发布命令或转交评审意见。模型给出结构化决策，控制器执行命令、检查退出码和补丁指纹，再决定是否发布。

**全自动不等于绕过项目维护者。** 新发现先创建 Issue，再自动检查维护者是否同意推进；必须先讨论的已有问题也会等待。等待期间无需你操作。上游最终是否接受、合并 PR 由维护者决定，系统不会尝试代替他们合并。

## 已接入项目

| 仓库 | 自动化范围 |
| --- | --- |
| [QwenLM/qwen-code](https://github.com/QwenLM/qwen-code) | 发现、复现、修复、审查、Issue/PR 发布、CI/评审跟进 |
| [google-gemini/gemini-cli](https://github.com/google-gemini/gemini-cli) | 同上，读取该仓库的贡献规则和测试命令 |
| [anomalyco/opencode](https://github.com/anomalyco/opencode) | 同上，读取该仓库的贡献规则和测试命令 |

每轮扫描三个仓库并优先跟进已有 PR 和等待中的 Issue，再从统一评分队列推进新任务。每仓库每周生成一个主动调查任务。初筛每仓库取最近 100 个开放 Issue、200 个开放 PR；发布前再做针对性搜索，发布恢复则通过直接 API 分页查找本人 Issue 和确定的 PR 分支。

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

`hermes_tick.py` 调用同一个 `tick`，无需额外人工步骤。原开发机器已配置每 6 小时执行的 Hermes no-agent 任务 `oss-contributor-qwen`；名称沿用首版，实际覆盖全部三个仓库。调度本身不调用模型，具体阶段按需调用 OpenCode 和 Hermes。

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
```

旧的 `analyze/repair/verify/review` 命令保留用于单阶段诊断，不是自动流程的必经人工步骤。新任务优先使用 `tick` 或 `auto`。

若本地存在 `state/windows-ca.pem`，OpenCode 会在未配置 `NODE_EXTRA_CA_CERTS` 时加载它。证书不随仓库分发，也不会关闭 TLS 校验。

## 验证覆盖与限制

2026-10-16 真实实跑创建了 [OpenCode PR #56027](https://github.com/anomalyco/opencode/pull/56027)，修复 OpenRouter 上游错误详情被隐藏的问题。基线回归测试 24 过、2 失败，补丁后 26 过；AI 包 1,818 过、28 跳过，Linux CI 根目录检查通过。该次调通中修复了 Windows、模型输出解析、查重及发布兼容问题，并配置了针对该补丁的 Linux 验证任务；这不代表所有后续候选都已验证能无人介入完成。

测试包含真实的本地 Git 仓库、基线失败/修复通过、提交与推送，以及模拟 GitHub/模型接口的完整状态流。覆盖新 Issue 等待后恢复、审查拒绝、过期补丁拒绝、失败次数限制、发布响应丢失后的去重恢复和进程锁恢复。

这些测试不等于真实上游已接受贡献，也不保证所有问题都能自动解决。生产发布仍取决于实际模型、测试环境、仓库规则和 GitHub 权限。运行记录、数据库、凭据、证书和上游 checkout 不上传到本仓库。

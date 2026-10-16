# OSS Contributor

面向 Qwen Code、Gemini CLI 和 OpenCode 的贡献流水线首版。Hermes 定时调度和审查，OpenCode 分析与编辑，GitHub CLI 获取真实状态。两个模型入口均使用 tokenking 的 `devin/swe-2`。无费用上限；单次模型或测试命令最长 20 分钟，自动任务失败后不无限重试。

## 已接入项目

| 仓库 | 巡检范围 |
| --- | --- |
| [QwenLM/qwen-code](https://github.com/QwenLM/qwen-code) | Issue、开放 PR、贡献指南、本人 PR 状态 |
| [google-gemini/gemini-cli](https://github.com/google-gemini/gemini-cli) | Issue、开放 PR、贡献指南、本人 PR 状态 |
| [anomalyco/opencode](https://github.com/anomalyco/opencode) | Issue、开放 PR、贡献指南、本人 PR 状态 |

三个仓库均已加入 `config.json` 白名单，支持已有 Issue 分析及 `hunt` 主动调查。每轮扫描全部仓库，再按任务分数从统一队列选择最多一个候选分析；当前不设置仓库权重。每个任务使用所属仓库的独立 worktree 和贡献规则，测试命令按各仓库配置。

## 当前边界

自动执行：拉取 Issue/开放 PR、按规则暂缓、持久化去重、生成候选列表、每轮最多一个只读源码调查。

显式命令执行：针对已有 Issue 编辑补丁、运行人工核对过的测试配方、Hermes 独立审查。新发现必须先形成复现证据并遵循上游 Issue 讨论流程。

首版不自动发 Issue、评论、push 或 PR。发布前需要查看真实证据和维护者要求。仓库 worktree 用于任务隔离，并不是操作系统安全沙箱；执行依赖安装和测试前应检查配方。

## 运行

需要 Python 3.11+、Git、已登录的 GitHub CLI、已配置模型的 OpenCode 和 Hermes。Python 代码仅使用标准库，无需 pip 安装依赖。开发和验证环境为 Windows；其他系统尚未实际验证。

```powershell
gh repo clone jadelike-wine/oss-contributor
cd oss-contributor
gh auth status
opencode models tokenking
python -X utf8 -m unittest -v
```

模型凭据由各 CLI 自己管理，本仓库不包含 API key。新机器需要自行配置 tokenking 的 `devin/swe-2`。

在本目录 PowerShell 中运行：

```powershell
python -X utf8 contribute.py scan
python -X utf8 contribute.py status
python -X utf8 contribute.py followup
python -X utf8 contribute.py hunt QwenLM/qwen-code "Investigate tool cancellation and child process cleanup"
python -X utf8 contribute.py analyze <任务ID>
python -X utf8 contribute.py repair <已有Issue任务ID>
python -X utf8 contribute.py verify <任务ID> <验证配方.json>
python -X utf8 contribute.py review <任务ID>
python -X utf8 contribute.py tick --analyze
python -X utf8 -m unittest -v
```

查看 `runs/queue.md`。每个任务的 `runs/<任务ID>/` 下保存任务快照、模型事件、分析/修复报告、验证日志、审查结果。`state/tasks.db` 保存状态和事件；这些运行数据不会进入 Git。

`config.json` 配置仓库白名单、模型、扫描数量和执行超时。默认从 PATH 查找 OpenCode；Windows npm 安装会自动解析到同目录下的实际可执行文件。其他安装布局可将 `opencode_executable` 设为实际程序路径。

## 验证配方

配方是可信操作者审核后执行的命令，不从模型输出自动执行。每个命令都是 argv 数组，不经过 shell 字符串拼接。

```json
{
  "setup": [["node.exe", "<包管理器JS入口>", "install", "--frozen-lockfile"]],
  "regression": ["node.exe", "<测试运行器JS入口>", "run", "<回归测试路径>"],
  "checks": [["node.exe", "<包管理器JS入口>", "run", "preflight"]],
  "test_files": ["<回归测试路径>"],
  "baseline_failure_pattern": "<精确的预期断言失败文本>"
}
```

这是结构模板，替换占位符后才能运行。Windows 建议用 node.exe 加 JS 入口，避免 .cmd shell 引号问题。验证器建立基线 worktree，只复制指定测试文件，然后分别安装环境、运行同一测试、执行项目检查。环境安装失败不能算作复现；基线必须非零退出且匹配预期断言。校验前后补丁指纹必须相同。基线目录保留以便调查，需定期人工清理。

## 调度

`hermes_tick.py` 是 Hermes 的 no-agent 定时脚本入口，可在仓库中直接运行。若复制到当前 Hermes home 的 `scripts/`，需为 gateway 设置 `OSS_CONTRIBUTOR_ROOT` 为仓库绝对路径；可用 `OSS_CONTRIBUTOR_PYTHON` 指定 Python 3.11+ 解释器。调度本身不消耗模型 token，源码分析使用 `tokenking/devin/swe-2`；独立审查同样使用 Hermes 的 tokenking/devin/swe-2。

```powershell
hermes cron list
hermes gateway status
```

任务无新变化时静默。报告留在本机，不发送到聊天平台。gateway 必须运行，机器关机时不会执行。

在原开发机器上已注册每 6 小时运行的 `oss-contributor-qwen` 任务。名称沿用首版，但每次执行都会读取当前配置，现已覆盖上述三个仓库，无需重复创建定时任务。克隆仓库不会复制这个任务或自动启动服务；新机器应先验证单次运行，再按本机 Hermes 的 `cron create --help` 注册脚本。`followup` 读取本人 PR 的状态、评审决策和 CI 状态；只在变化时写入 `runs/pr-updates.json`，不会自动回复评论。

原开发机器上的 OpenCode 曾遇到证书校验错误。若本地存在 `state/windows-ca.pem`，执行时仅在未设置 NODE_EXTRA_CA_CERTS 时加载它。证书不随仓库分发；新机器应使用其自身信任的 CA，保持 TLS 校验开启。

## 状态与恢复

`DISCOVERED → ANALYZING → NEEDS_REPRODUCTION → REPAIRING → PATCH_READY → VERIFIED → AWAITING_REVIEW`

另有 `HOLD`、`BLOCKED`、`ANALYSIS_FAILED`、`VERIFICATION_FAILED`。分析完成不等于缺陷已证实，审查完成不等于批准发布。worker 锁防止并行重复任务，进程崩溃后保留锁；确认对应 PID 已停止后才能执行 `unlock <锁名> --process-stopped`。

当前扫描每仓库最近 100 个开放 Issue、最多 200 个开放 PR。引用和 closing issue 关联用于初筛，不能替代发布前的关键词、关闭 Issue 和历史 PR 检索。已有负责人的任务、需要讨论/信息、人类判断任务、安全报告、功能提案和已有引用 PR 会暂缓。

## 发布前

按当前上游 CONTRIBUTING.md 检查 Issue 讨论、重复 PR、测试要求、截图或 N/A 说明、提交格式。确认 `verification.json` 的指纹仍对应当前补丁，并阅读 `review.md`。之后通过 git 提交/推送到自己的 fork，使用 `gh pr create --repo <上游> --base <实际默认分支> --head <账号:分支> --body-file <正文文件>` 发布。此阶段由操作者明确执行。

后续应补充容器执行后端、发布去重和 PR 反馈自动闭环；当前实现不会假装这些已经自动化。

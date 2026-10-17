# 真实研究验收：会话恢复

日期：2026-10-18（Asia/Shanghai）。本报告区分实际调用、程序校验、模型审查与维护者批准；模型审查通过不等于上游接受。

## 最终验收结果

真实研究、精确引文/矩阵校验、GitHub 查重关联和最终模型审查均通过，现有请求为 #963。规范队列为 **PAUSED_POLICY**，不是 READY_TO_IMPLEMENT：模型没有确认自动贡献权限，维护者也未批准设计，因此控制器保持暂停。完整的规范矩阵、政策判断和最终审查见 [真实结果 JSON](../examples/session-recovery.live.json)，它不是模拟夹具。

最终研究、查重审查和研究审查的三个合格调用均有成功结果事件，工具事件均为零。模型均为 `tokenking/devin/swe-2`，研究/审查同属 Hermes 的不同调用，身份标识 `same_model_separate_invocation`。此前工具开启的中止调用不计入合格结果。

本项目全量 **154 项测试通过，113.423 秒，无失败或跳过**；基线为 139 项通过。目标仓库工作区保持干净，未执行上游测试、未发布任何上游消息。

结论：本样本验证了项目能够从真实源码发现有用户需求、符合已知架构方向的功能提案，并找到正确贡献入口；未证明稳定的批量成功率、自动实现资格或维护者最终接受。

## 样本与需求

任务：`SWE-agent--mini-swe-agent--hunt-84ed9801ff3a`。

目标是 **mini-swe-agent 的可选轨迹恢复 runner / cookbook 方案**：从最后一次成功保存的轨迹恢复消息和费用/调用计数，在原有环境仍可用的前提下继续运行。不得承诺恢复外部环境或恰好执行一次；未取得维护者方向前不实现。

- 真实用户需求：[mini-swe-agent #963](https://github.com/SWE-agent/mini-swe-agent/issues/963)。用户报告执行 292 步、花费约 3 美元后，在调整费用上限时崩溃，询问能否恢复。这是普通用户需求，不是维护者批准。
- 目标源码：[default.py](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/agents/default.py)。`run()` 重置消息；`serialize()/save()` 已保存轨迹和计数。缺失结论限定于实际采集范围，未宣称穷尽整个仓库。
- 贡献方向：[贡献指南](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/docs/contributing.md) 偏好独立组件、extra 目录和 cookbook，强调避免增加核心复杂度。因此“小型可选 runner / 示例”比直接扩大默认 agent 更符合已知方向；维护者仍可能只接受文档方案。
- 重要边界：保存文件可能落后于崩溃时的实际动作；不能自动假定未保存的命令未执行。墙钟时间限制是否跨恢复累计、消息格式兼容、末尾 exit/未完成工具调用如何处理，均须明确。

## 固定版本采集

实际采集 12 个目标文件、三个 peer 各 4 个文件；采集阶段 23 次 GitHub 请求，不含准备仓库、后续查重与复核请求。目标文件来自固定 Git SHA，peer 来自固定 SHA 的 GitHub contents；没有把上游测试文件当成已执行测试。

| 仓库 | SHA |
| --- | --- |
| SWE-agent/mini-swe-agent | `04d809ceab9df28f9adaed044884180159172930` |
| mistralai/mistral-vibe | `4ae5c5955da8e761318916bdb1a7f46f0b50c7d0` |
| aaif-goose/goose | `bfcc6ee6f1ee18d0c4c852a65c49b25f87ee40dc` |
| cline/cline | `a42df8c8d5fd9d9f9a9073589baffe32ea9bb2f0` |

## 实跑发现并修复的问题

1. **采集偏题。** 文件名子串匹配把包名 `minisweagent` 当作 `agent` 命中，初始化文件、`py.typed` 和动作解析文件占据上下文，贡献规则又挤掉源码。改为路径分词、固定 SHA 内容检索、源码/测试/文档配额及规则优先，并核验最终选中文件的嵌套规则。
2. **模型通道不稳定。** OpenCode 的完整研究调用两次报 `upstream stream closed before a terminal event`，小请求可用，未确定供应商内部根因。保留失败并增加显式研究通道配置；当前使用 Hermes，仍是 `tokenking/devin/swe-2`，没有静默换模型。
3. **空工具参数没有禁用工具。** Hermes 的 `-t ''` 实际启用了默认工具，曾产生额外 `read_file/search_files`。立即终止该调用并排除其结果；目标工作区无改动。改为 `-t none`，研究和原审查入口共同修复，并拒收含工具调用事件的输出。后续合格研究/审查日志须无工具事件。
4. **输出约定含糊。** 实际出现 scope/risks/validation_plan 类型不匹配、feature_status 放错层、非精确引文和未读取路径。明确类型、层级和可引用范围；保留原答复，最多一次格式/证据纠正，仍重新执行所有校验。兼容旧的顶层状态映射，但拒绝冲突映射。审查反馈另允许一次有上限的方案修订，不无限循环。
5. **查重假阴性。** 模型查询 `SWE-agent/mini-swe-agent resume saved trajectory` 把仓库名变成普通 AND 搜索词，返回零条；去掉该普通词后，同一主题命中 #963。现在由控制器统一仓库范围，保存原始及规范化查询。
6. **已有需求被误当作应丢弃的重复。** 现在区分竞争实现/PR 与开放功能请求。关联必须来自实际查到的开放 Issue；继续原讨论、等待维护者确认，不重复发布，也不把用户诉求当作批准。维护者审查会收到确切提案范围。
7. **只读入口探测无关权限。** 原先 hunt/analyze 先检查全部贡献仓库，并受到 Goose 看板权限错误干扰；现在只读研究使用目录准入，贡献发布继续执行原有检查。

## 复查方式与范围

```powershell
python -X utf8 -m unittest discover -v
python -X utf8 contribute.py matrix SWE-agent--mini-swe-agent--hunt-84ed9801ff3a
python -X utf8 contribute.py metrics --limit 10
```

本机完整材料位于 `runs/SWE-agent--mini-swe-agent--hunt-84ed9801ff3a/`，数据库位于 `state/tasks.db`，均保持 Git 忽略。`research-context.json` 是固定输入，`research-revision-feedback.json` 保留此前审查异议，`duplicate-search-issue.json` 是真实查重响应，`opportunity.json` 和 `feature-matrix.json` 是最终结果。已验证提案在网络失败后可以复用，但须重新查重和审查，旧批准不会作为失败重跑的兜底。

本轮不向目标仓库发送评论、Issue 或 PR，不实现目标功能，不声称上游测试或 CI 通过。真正的上游接受率仍须后续获得维护者回应或合并才能衡量。一次成功样本也不能证明批量成功率。

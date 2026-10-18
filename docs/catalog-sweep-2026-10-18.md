# 2026-10-18 全目录运行与上游贡献

这次按已核验研究池的完整 32 个仓库运行。之前只推进原贡献白名单的 8 个仓库、研究批次最多 3 个候选，未覆盖用户要求的全目录；向本项目自己创建 PR 也偏离了开源贡献目标。后续本项目修改直接提交 main 并推送。

## 实际执行范围

- 32/32 仓库扫描成功，32/32 保存固定 SHA 的有界源码材料。
- 共扫描 2,630 个开放 Issue 和 4,796 个开放 PR；每仓库上限分别为 100 和 200，不能声称读取了这些项目的全部历史。
- 32/32 仓库均尝试模型分诊；19 份通过结构与引用校验：11 个候选、5 个未选出候选、3 个需要更多上下文。剩余 13 个引用校验失败，明确保留 unknown。候选不是已验证缺陷，也不是发布授权。
- 全目录分诊及补跑使用 66 次模型调用，加上此前 14 次，达到现有每日 80 次预算；未重置或放宽预算。
- 这是问题扫描、固定版本源码采集和静态分诊，不是安装并运行 32 个项目的完整测试套件。

## 已提交的上游贡献

- Issue：[OpenBMB/ChatDev #688](https://github.com/OpenBMB/ChatDev/issues/688)。
- PR：[OpenBMB/ChatDev #689](https://github.com/OpenBMB/ChatDev/pull/689)，head `37b9ad3ae79a980a20d7ee75f63135b380825387`。
- 真实问题：`FunctionManager` 在同大小、同整数秒修改时间的工具源码更新后，加载时间戳缓存的旧字节码，显式 reload 仍执行旧逻辑；新 manager 也会中招。
- 基线 `a2ea4a263cebe9a7abd01ee3b5b0bf95698ed328`，Windows / Python 3.12.13，新增测试 2 失败、1 通过。修复后新增测试与已有 server reload 测试共 23 通过，差异检查通过。
- 修改直接编译当前源码字节，保留编码声明、模块元数据和函数筛选。没有删除工具缓存、修改解释器全局设置。没有运行完整 ChatDev 应用套件或模型调用。
- PR 已确认 OPEN；提交时 GitHub 没有报告检查项，不能当作远程 CI 通过。
- 先对原候选 #681 查重，发现已有 #682 后排除；另行复现缓存缺陷，搜索 FunctionManager / reload / bytecode 的开放及关闭条目，核对 README 的贡献说明后提交。未重复创建 dataclass 修复。
- 此 PR 由当前执行代理在目录分诊后继续定向调查、复现和提交；`catalog_sweep` 本身没有调用自动状态机的实现或发布入口。

## 本项目修复

- 新增可恢复的全目录入口，不再让 8 个贡献目标或 3 个研究候选的默认批量限制掩盖未运行仓库。完整 scope 与逐仓库结果始终保留。
- 裁剪 PR 的嵌套 GraphQL 数据，优先保留关联 PR，避免大型 Issue 正文通过 closing references 再次撑爆模型上下文。pydantic-ai、hermes-agent 的失败采集已补跑成功。
- 引用错误现在明确指出路径和失败引文，纠错携带原始回答，要求实际源码证据；失败回答保留，后续调用使用独立文件名。最终提示修复在 Qwen 单仓库实跑返回可校验的 needs_more_context，没有把缺失源码当作已证明缺陷。
- 采集显式读取常见 AI_POLICY 和缺少贡献指南时的 README；大于大小上限的适用规则明确列为未读，不能推断授权。实际贡献仍须单独核对完整规则及其引用文件。
- 修复只读 SQLite 连接未关闭：Windows 测试实测导致临时数据库无法删除，改为显式关闭后通过。
- 支持只采集、只分析、单仓库补跑，以及单次调用模式；保留网络失败 unknown、预算及原有发布门禁。
- 本项目新增 11 项专项测试通过；最终完整回归 `python -X utf8 -m unittest discover -v`：174 项通过。SQLite 完整性检查为 ok，运行锁为空；本项目修改直接提交并推送 main。

## 逐仓库结果

以下是静态分诊结果，不代表每个候选都经过复现或政策复核。政策标记来自模型，不能代替维护者批准或完整规则审查。

| 仓库 | 扫描 Issue / PR | 静态分诊 | 定向核对 / 后续 |
| --- | ---: | --- | --- |
| QwenLM/qwen-code | 100 / 200 | 需要补充定向源码 | 详见本地 packet、triage 及固定 SHA；未宣称目标测试通过。 |
| google-gemini/gemini-cli | 100 / 200 | unknown：引用校验失败 | 引用未通过；保留失败证据，需补充相关源码后重新分析。 |
| anomalyco/opencode | 100 / 200 | unknown：引用校验失败 | 引用未通过；保留失败证据，需补充相关源码后重新分析。 |
| cline/cline | 100 / 200 | 候选，尚未完成复现 | #14836；规则要求批准 Issue 后再提 PR。 |
| OpenHands/OpenHands | 100 / 200 | unknown：引用校验失败 | 引用未通过；保留失败证据，需补充相关源码后重新分析。 |
| aaif-goose/goose | 100 / 179 | 候选，尚未完成复现 | #12653；Ready 看板及本人提交、人工审查要求保留。 |
| SWE-agent/mini-swe-agent | 38 / 61 | unknown：引用校验失败 | 引用未通过；保留失败证据，需补充相关源码后重新分析。 |
| openai/codex | 100 / 165 | 所读范围未选出候选 | 所读贡献指南不接受外部代码 PR。 |
| Aider-AI/aider | 100 / 200 | 所读范围未选出候选 | 详见本地 packet、triage 及固定 SHA；未宣称目标测试通过。 |
| microsoft/vscode | 100 / 200 | 所读范围未选出候选 | 详见本地 packet、triage 及固定 SHA；未宣称目标测试通过。 |
| mistralai/mistral-vibe | 100 / 200 | 候选，尚未完成复现 | #1075；所读贡献指南暂不接受代码贡献。 |
| langchain-ai/deepagents | 100 / 40 | unknown：引用校验失败 | 引用未通过；保留失败证据，需补充相关源码后重新分析。 |
| langchain-ai/langgraph | 100 / 200 | 需要补充定向源码 | #8960 已有多份修复、要求 Issue 分配，不重复提交。 |
| modelcontextprotocol/python-sdk | 100 / 105 | unknown：引用校验失败 | 引用未通过；保留失败证据，需补充相关源码后重新分析。 |
| browser-use/browser-use | 100 / 200 | 候选，尚未完成复现 | #5633；定向查重发现多份现有修复 PR，排除重复。 |
| pydantic/pydantic-ai | 100 / 200 | unknown：引用校验失败 | 引用未通过；保留失败证据，需补充相关源码后重新分析。 |
| crewAIInc/crewAI | 100 / 200 | 所读范围未选出候选 | 详见本地 packet、triage 及固定 SHA；未宣称目标测试通过。 |
| microsoft/semantic-kernel | 100 / 200 | unknown：引用校验失败 | 引用未通过；保留失败证据，需补充相关源码后重新分析。 |
| huggingface/smolagents | 100 / 200 | 候选，尚未完成复现 | #2833；所读指南要求 status:accepted 及人工承担责任。 |
| mcp-use/mcp-use | 32 / 65 | 需要补充定向源码 | 详见本地 packet、triage 及固定 SHA；未宣称目标测试通过。 |
| NousResearch/hermes-agent | 100 / 200 | unknown：引用校验失败 | 引用未通过；保留失败证据，需补充相关源码后重新分析。 |
| run-llama/llama_index | 100 / 200 | 候选，尚未完成复现 | 详见本地 packet、triage 及固定 SHA；未宣称目标测试通过。 |
| ag2ai/ag2 | 24 / 56 | unknown：引用校验失败 | 引用未通过；保留失败证据，需补充相关源码后重新分析。 |
| OpenBMB/ChatDev | 54 / 28 | 候选，尚未完成复现 | 初选 #681 已有关闭的修复 #682；继续调查另一个缓存问题，提交 #688 / PR #689。 |
| julep-ai/julep | 3 / 1 | 候选，尚未完成复现 | 详见本地 packet、triage 及固定 SHA；未宣称目标测试通过。 |
| agno-agi/agno | 100 / 200 | unknown：引用校验失败 | 引用未通过；保留失败证据，需补充相关源码后重新分析。 |
| langflow-ai/langflow | 100 / 200 | unknown：引用校验失败 | 引用未通过；保留失败证据，需补充相关源码后重新分析。 |
| e2b-dev/code-interpreter | 15 / 9 | 所读范围未选出候选 | 详见本地 packet、triage 及固定 SHA；未宣称目标测试通过。 |
| cloudwego/eino | 100 / 124 | 候选，尚未完成复现 | #1254 已有关闭的修复 #1255，未重复提交。 |
| stanfordnlp/dspy | 100 / 200 | 候选，尚未完成复现 | #10100；规则明确禁止全自主代理提交 PR。 |
| openinterpreter/openinterpreter | 9 / 6 | unknown：引用校验失败 | 引用未通过；保留失败证据，需补充相关源码后重新分析。 |
| 567-labs/instructor | 55 / 157 | 候选，尚未完成复现 | 详见本地 packet、triage 及固定 SHA；未宣称目标测试通过。 |

## 可复核材料

本地忽略目录 `runs/catalog-sweep-20261018/` 保存 scope、32 份扫描记录、packet、triage / invalid / error、运行日志及 `upstream-contribution.json`。上游补丁及测试已提交到公开 PR，源代码也保留在本地 `workspaces/OpenBMB--ChatDev--reload`。本报告只记录可验证结果，不把 unknown 记成无问题。

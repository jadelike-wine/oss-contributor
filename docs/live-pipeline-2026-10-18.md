# 真实运行验收：扫描、自动推进与检查点恢复

日期：2026-10-18（Asia/Shanghai）。这是本机真实 GitHub、OpenCode 和 Hermes 调用的记录；不把本地模拟测试、历史 PR 或控制器自己的修复 PR 算作新上游贡献。

## 第一轮完整 tick

执行 `python -X utf8 contribute.py tick`，运行 ID 为 `a2cac50e61104085bbd1d912d76ea3d0`，北京时间 10:47:27–10:59:07，耗时 699.938 秒，进程退出码 0。

扫描 8 个白名单仓库，读取 738 条开放 Issue、1,394 条开放 PR，更新 17 条本地任务。扫描上限仍是每仓库 100 条 Issue / 200 条 PR，不表示遍历了全部上游条目。Goose 的 Ready 看板因令牌缺少 `read:project` 无法核验，继续暂停自动贡献。

本轮发生 5 次模型 CLI 入口调用：4 次 Hermes 研究/纠正、1 次 OpenCode 规划。研究与发布结果如下：

| 任务 | 实际结果 |
| --- | --- |
| `OpenHands--OpenHands--hunt-14401f07af77` | 已固定基线 `852132d38dbc15e03d6825bbacbeb971c72575be`；采集时出现 `PermissionError`，原日志只有异常类型。随后单独恢复采集成功，未声称已完成模型研究。 |
| `SWE-agent--mini-swe-agent--hunt-14401f07af77` | 模型识别到安全类别候选；控制器保留本地材料并置为 SKIPPED，没有公开发布。 |
| `QwenLM--qwen-code--hunt-5b1979b3c5ba` | 初始证据校验失败；允许的一次纠正返回无效 JSON，记录失败，没有实施或公开发布。 |
| `google-gemini--gemini-cli--29325` | 真正进入规划阶段；读取讨论后判定报告涉及另一个产品 `agy`，没有建立本仓库缺陷，因此 SKIPPED。 |
| 既有 OpenCode PR #56027 | 观察既有工作；不是本轮新创建的 PR。 |

本轮没有新建上游 Issue 或 PR，没有取得新上游红绿测试或 CI 通过证据。退出码 0 表示调度器完成本轮，不等于每个研究候选成功；阶段记录保留失败。

## 修复与真实恢复证据

### 搜索命中路径挤爆研究上下文

历史两轮 tick 均因 `research_context_characters_exhausted` 退出。实际检查点显示，`target.content_search.matched_paths` 保存了数千条路径，OpenCode 样本仅搜索元数据就占约 200,020 字符。这个诊断清单不属于被读取的源码，却阻止了后续阶段。

修复后，文件选择仍使用完整命中集合，保存和发给模型的诊断清单最多 100 条、约 12,000 字符，同时记录 `matched_count` 和 `matched_paths_truncated`。恢复旧检查点时同样整理该清单；不改变任何固定 SHA、源码片段、政策材料或同行身份，不提高上下文上限。

真实调用 `collect()` 恢复结果：

| 检查点 | 修复前字符 | 修复后字符 | 实际匹配数 | 源码/政策/同行材料哈希 | 网络请求 |
| --- | ---: | ---: | ---: | --- | --- |
| `anomalyco--opencode--hunt-5b1979b3c5ba` | 395,635 | 199,208 | 3,904 | 相同 | 没有新增 |
| `cline--cline--hunt-14401f07af77` | 375,758 | 230,923 | 2,629 | 相同 | 没有新增 |

两个检查点均恢复为 `complete=true`。数值按 `len(json.dumps(packet, ensure_ascii=False))` 计算，与程序限额口径一致。

### Windows 检查点原子保存

首轮 OpenHands 采集出现暂时性 `PermissionError`，恢复调用成功。由于旧事件未保存详情，不能断言那一次异常必定发生于文件替换。

另以真实 Windows 读取句柄复现检查点被占用：关闭读取句柄后，改进后的 `save()` 能成功恢复。对 Windows 错误码 5/32/33，原子替换最多尝试 5 次，合计等待至多 0.75 秒；复用同一份完整临时文件，持续失败仍抛错并保留旧 JSON。其他错误不重试。研究失败事件现在保留异常详情及任务级 `research_error`，便于定位下一次失败。

### JSON 语法失败跳过纠正额度

第二轮恢复后的 OpenCode 研究实际进入模型，返回结果在字符 14,097 处无法解析。新增错误详情保留了位置。检查发现 `research_answer()` 位于原有纠正逻辑的 `try` 之外，导致首次 JSON 语法失败直接退出，无法使用 README 已说明的一次格式纠正额度。

修复把 JSON 语法错误接入同一个纠正额度，保存原始响应，不在本地猜测或修改 JSON。纠正调用仍使用相同材料、禁用工具、计入预算，并重新执行证据、矩阵和工作区检查。第二次解析失败仍报错，不存在第三次调用。

## 本地验证

原始基线：154 项通过，119.451 秒。修复后全量 **161 项通过，124.178 秒，无失败或跳过**。新增回归覆盖大命中清单、旧检查点无需重新读取、真实 Windows 文件占用、短暂占用重试、持续拒绝访问时保留旧文件，以及 JSON 语法失败共享一次纠正额度。最终测试日志为 `runs/acceptance-verified-tests-20261018.log`，运行材料与数据库保持 Git 忽略。

## 本机证据入口

- `runs/acceptance-live-tick-20261018.log`：第一轮正式执行。
- `runs/acceptance-checkpoint-recovery-20261018.log`：两份真实检查点恢复及材料哈希对比。
- `runs/acceptance-openhands-resume-20261018.log`：OpenHands 采集恢复。
- `runs/acceptance-live-auto-retry-20261018.log`：修复后的新进程继续自动推进。
- `state/tasks.db`：规范任务、阶段、模型入口调用及队列；可用 `metrics/candidates/matrix` 只读查看。

这些日志不是全部成功的声明。模型格式失败、证据失败、政策暂停和维护者等待应按各自真实状态读取。

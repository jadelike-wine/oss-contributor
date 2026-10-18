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

## 修复后新进程继续运行

执行 `python -X utf8 contribute.py auto`，运行 ID 为 `a11abac7be80437cba2fed6c002b775e`，北京时间 11:00:03–11:14:02，耗时 838.719 秒。调度器进程退出码 0，但运行指标为 `failed`，不能把它当作贡献成功。

- OpenCode 的原超限检查点成功进入真实 Hermes 模型调用，不再被上下文清单卡住；返回 JSON 语法失败，触发了本报告记录的第三项修复。
- Cline 的原超限检查点完成研究和一次纠正；候选属于安全类别，保留本地材料后 SKIPPED。
- OpenHands 从前次 `PermissionError` 恢复，成功完成真实研究；候选同属安全类别，保留本地材料后 SKIPPED。
- 已有 OpenCode PR #56027 继续观察，没有被计作新贡献。
- Kilo #14924 进入规划，实际生成 `packages/opencode/test/kilocode/session-compaction-cap.test.ts` 的回归候选。模型检查命令采用 `bun --cwd … run typecheck`，被既有命令验证器拒绝，因为这一顺序可能只打印帮助并以 0 退出。任务保留在 PLAN，未执行测试、未发布补丁，也不声称假设已被复现。

这一轮同样是 5 次模型 CLI 入口调用。读取/编辑工具调用不是这项计数，也不代表执行了 shell 测试。

随后用修复后的新进程定向执行 `analyze anomalyco--opencode--hunt-5b1979b3c5ba`。首次响应再次是无效 JSON，控制器实际发起了唯一一次纠正；纠正成功并通过证据和矩阵校验，继续到查重。第一条定向查询命中 3 条，第二条 `empty assistant step no output no error` 命中 167 条，超过既有 20 条完整审查上限，进程退出码 1，保留 `research_pending` 和搜索证据，没有公开发布。这验证了 JSON 恢复通路，没有证明查重或整个研究通过。过宽查询的后续自动细化仍是当前限制，不能把报错文本中的“refine automatically on retry”当作已实现的能力。

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

### 规划重试把本地草稿误当成上游基线

Kilo #14924 的第一次规划留下了尚未验证的回归测试。针对 Bun 命令错误重试时，模型称“固定基线已有 `does not restart a rate-limited compaction on the next turn`”，据此 SKIPPED。控制器外复核 `git show 2a1903f29186e25adec0bace14f6ee589095f7eb:packages/opencode/test/kilocode/session-compaction-cap.test.ts` 发现基线没有这条测试；工作区 diff 则显示本轮新增了它。该次跳过理由有明确事实错误，不能作为已修复证据。

修复后的规划输入带 `planning_baseline`：列明保留的本地改动、每个路径在固定 SHA 是否存在，以及对应不可变原文。最多 15 个改动路径、单文件 60,000 字节、总上下文 120,000 字符；超大文件明确标为截断，不读取整个大 blob，不把省略内容当作不存在。模型明确收到“本地草稿不是上游已有覆盖”的规则，同时提供正确 Bun 参数示例。保留所有已有改动，不通过 reset 删除草稿或用户文件。

回归使用真实临时 Git 仓库：先让无效方案留下新测试，再修改已有测试，重试输入必须证明新测试在基线不存在，并提供已有测试的原文，而不是修改后的内容。另测超大基线文件不被读取。对实际 Kilo 任务保存原跳过理由和固定 SHA，记录审计事件，保留已有重试计数后重新进入 PLAN；这只纠正输入和错误引用，不批准实现或发布。

## 本地验证

原始基线：154 项通过，119.451 秒。所有修复后全量 **163 项通过，124.884 秒，无失败或跳过**。新增回归覆盖大命中清单、旧检查点无需重新读取、真实 Windows 文件占用、短暂占用重试、持续拒绝访问时保留旧文件、JSON 语法失败共享一次纠正额度，以及规划重试区分本地草稿和不可变基线。最终测试日志为 `runs/acceptance-final-baseline-tests-20261018.log`，运行材料与数据库保持 Git 忽略。

## 本机证据入口

- `runs/acceptance-live-tick-20261018.log`：第一轮正式执行。
- `runs/acceptance-checkpoint-recovery-20261018.log`：两份真实检查点恢复及材料哈希对比。
- `runs/acceptance-openhands-resume-20261018.log`：OpenHands 采集恢复。
- `runs/acceptance-live-auto-retry-20261018.log`：修复后的新进程继续自动推进。
- `state/tasks.db`：规范任务、阶段、模型入口调用及队列；可用 `metrics/candidates/matrix` 只读查看。

这些日志不是全部成功的声明。模型格式失败、证据失败、政策暂停和维护者等待应按各自真实状态读取。

# 100 项仓库扩容记录（2026-10-18）

本次清单共 100 个项目，其中 20 个名称已存在，实际追加 80 条。目录从 52 条增至 132 条；正式 `scan` / `tick` 的默认扫描范围从 32 个增至 94 个。原记录顺序保留，新增记录追加在后。

清单内：77 个核验通过，4 个暂停，17 个待人工准入审查，2 个保留退役。清单全部保存，不把未通过核验的项目当作可自动贡献目标。

贡献目标配置从 33 个增至 94 个。只追加通过本次核验的项目；维护者确认、任务政策、Goose 指定看板和本人审查要求继续执行。

## 目录状态

| 状态 | 扩容前 | 扩容后 |
| --- | ---: | ---: |
| `research_only` | 32 | 94 |
| `paused` | 10 | 13 |
| `rejected` | 8 | 23 |
| `retired` | 2 | 2 |

## 四类清单

| 分类 | 请求项目 | 核验通过 | 暂停 | 待人工审查 | 退役 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 前端框架与工程工具 | 20 | 19 | 0 | 1 | 0 |
| 组件库与 UI 基础设施 | 35 | 27 | 3 | 5 | 0 |
| AI 前端与 AI 应用 | 20 | 13 | 1 | 6 | 0 |
| AI Harness、Coding Agent 与自动化工作流 | 25 | 18 | 0 | 5 | 2 |

## 核验与验证

真实 GitHub 只读核验覆盖这 100 个请求名称；两项显式退役复用原记录。每个核验通过的项目均保存固定 SHA 的源码、可执行测试、文档和许可元数据。Preact 首次网络读取失败，单独重试成功后合并，没有把失败记录当作缺失。

`catalog --verify` 改为每批最多 80 个、最多四个并发，顺序处理完整目录；按大小写不敏感去重，保留已有记录、额外候选及显式退役。

正式入口范围通过 `Pipeline.scan_repositories()` 在真实目录和配置上解析并保存到 `runs/catalog-expansion-summary.json`。这次执行了准入核验，没有对所有仓库重新运行 Issue 扫描或目标项目的测试套件。回归结果见下方验收记录。

证据：`repository-catalog.json`（逐仓 SHA、材料、状态、原因）、`runs/catalog-expansion-20261018.log`（100 项核验日志）、`runs/catalog-expansion-preact-retry.json`（重试结果）、`runs/catalog-expansion-summary.json`（范围与计数）。

## 逐项结果

| # | 分类 | 仓库 | 状态 | 原因 |
| ---: | --- | --- | --- | --- |
| 1 | 前端框架与工程工具 | [react/react](https://github.com/react/react) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 2 | 前端框架与工程工具 | [vuejs/core](https://github.com/vuejs/core) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 3 | 前端框架与工程工具 | [sveltejs/svelte](https://github.com/sveltejs/svelte) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 4 | 前端框架与工程工具 | [angular/angular](https://github.com/angular/angular) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 5 | 前端框架与工程工具 | [solidjs/solid](https://github.com/solidjs/solid) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 6 | 前端框架与工程工具 | [preactjs/preact](https://github.com/preactjs/preact) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 7 | 前端框架与工程工具 | [vitejs/vite](https://github.com/vitejs/vite) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 8 | 前端框架与工程工具 | [vitest-dev/vitest](https://github.com/vitest-dev/vitest) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 9 | 前端框架与工程工具 | [withastro/astro](https://github.com/withastro/astro) | `rejected` | Open-source license not established by GitHub license metadata; manual license review required |
| 10 | 前端框架与工程工具 | [nuxt/nuxt](https://github.com/nuxt/nuxt) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 11 | 前端框架与工程工具 | [vercel/next.js](https://github.com/vercel/next.js) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 12 | 前端框架与工程工具 | [TanStack/query](https://github.com/TanStack/query) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 13 | 前端框架与工程工具 | [TanStack/router](https://github.com/TanStack/router) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 14 | 前端框架与工程工具 | [TanStack/table](https://github.com/TanStack/table) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 15 | 前端框架与工程工具 | [pmndrs/zustand](https://github.com/pmndrs/zustand) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 16 | 前端框架与工程工具 | [pmndrs/jotai](https://github.com/pmndrs/jotai) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 17 | 前端框架与工程工具 | [reduxjs/redux-toolkit](https://github.com/reduxjs/redux-toolkit) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 18 | 前端框架与工程工具 | [react-hook-form/react-hook-form](https://github.com/react-hook-form/react-hook-form) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 19 | 前端框架与工程工具 | [colinhacks/zod](https://github.com/colinhacks/zod) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 20 | 前端框架与工程工具 | [vueuse/vueuse](https://github.com/vueuse/vueuse) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 21 | 组件库与 UI 基础设施 | [shadcn-ui/ui](https://github.com/shadcn-ui/ui) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 22 | 组件库与 UI 基础设施 | [radix-ui/primitives](https://github.com/radix-ui/primitives) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 23 | 组件库与 UI 基础设施 | [mui/material-ui](https://github.com/mui/material-ui) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 24 | 组件库与 UI 基础设施 | [ant-design/ant-design](https://github.com/ant-design/ant-design) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 25 | 组件库与 UI 基础设施 | [mantinedev/mantine](https://github.com/mantinedev/mantine) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 26 | 组件库与 UI 基础设施 | [chakra-ui/chakra-ui](https://github.com/chakra-ui/chakra-ui) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 27 | 组件库与 UI 基础设施 | [heroui-inc/heroui](https://github.com/heroui-inc/heroui) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 28 | 组件库与 UI 基础设施 | [tailwindlabs/headlessui](https://github.com/tailwindlabs/headlessui) | `paused` | No repository push within 180 days; maintenance review required |
| 29 | 组件库与 UI 基础设施 | [tailwindlabs/tailwindcss](https://github.com/tailwindlabs/tailwindcss) | `paused` | Official maintenance/migration notice: We do not accept contributions |
| 30 | 组件库与 UI 基础设施 | [saadeghi/daisyui](https://github.com/saadeghi/daisyui) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 31 | 组件库与 UI 基础设施 | [lucide-icons/lucide](https://github.com/lucide-icons/lucide) | `rejected` | Open-source license not established by GitHub license metadata; manual license review required |
| 32 | 组件库与 UI 基础设施 | [tabler/tabler](https://github.com/tabler/tabler) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 33 | 组件库与 UI 基础设施 | [phosphor-icons/core](https://github.com/phosphor-icons/core) | `paused` | No repository push within 180 days; maintenance review required |
| 34 | 组件库与 UI 基础设施 | [element-plus/element-plus](https://github.com/element-plus/element-plus) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 35 | 组件库与 UI 基础设施 | [vuetifyjs/vuetify](https://github.com/vuetifyjs/vuetify) | `rejected` | Open-source license not established by GitHub license metadata; manual license review required |
| 36 | 组件库与 UI 基础设施 | [vueComponent/ant-design-vue](https://github.com/vueComponent/ant-design-vue) | `rejected` | Open-source license not established by GitHub license metadata; manual license review required |
| 37 | 组件库与 UI 基础设施 | [primefaces/primereact](https://github.com/primefaces/primereact) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 38 | 组件库与 UI 基础设施 | [primefaces/primevue](https://github.com/primefaces/primevue) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 39 | 组件库与 UI 基础设施 | [tusen-ai/naive-ui](https://github.com/tusen-ai/naive-ui) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 40 | 组件库与 UI 基础设施 | [arco-design/arco-design](https://github.com/arco-design/arco-design) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 41 | 组件库与 UI 基础设施 | [DouyinFE/semi-design](https://github.com/DouyinFE/semi-design) | `rejected` | Open-source license not established by GitHub license metadata; manual license review required |
| 42 | 组件库与 UI 基础设施 | [Tencent/tdesign-react](https://github.com/Tencent/tdesign-react) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 43 | 组件库与 UI 基础设施 | [Tencent/tdesign-vue-next](https://github.com/Tencent/tdesign-vue-next) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 44 | 组件库与 UI 基础设施 | [recharts/recharts](https://github.com/recharts/recharts) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 45 | 组件库与 UI 基础设施 | [apache/echarts](https://github.com/apache/echarts) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 46 | 组件库与 UI 基础设施 | [xyflow/xyflow](https://github.com/xyflow/xyflow) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 47 | 组件库与 UI 基础设施 | [ueberdosis/tiptap](https://github.com/ueberdosis/tiptap) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 48 | 组件库与 UI 基础设施 | [facebook/lexical](https://github.com/facebook/lexical) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 49 | 组件库与 UI 基础设施 | [udecode/plate](https://github.com/udecode/plate) | `rejected` | Open-source license not established by GitHub license metadata; manual license review required |
| 50 | 组件库与 UI 基础设施 | [mermaid-js/mermaid](https://github.com/mermaid-js/mermaid) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 51 | 组件库与 UI 基础设施 | [clauderic/dnd-kit](https://github.com/clauderic/dnd-kit) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 52 | 组件库与 UI 基础设施 | [fullcalendar/fullcalendar](https://github.com/fullcalendar/fullcalendar) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 53 | 组件库与 UI 基础设施 | [marmelab/react-admin](https://github.com/marmelab/react-admin) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 54 | 组件库与 UI 基础设施 | [refinedev/refine](https://github.com/refinedev/refine) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 55 | 组件库与 UI 基础设施 | [carbon-design-system/carbon](https://github.com/carbon-design-system/carbon) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 56 | AI 前端与 AI 应用 | [vercel/ai](https://github.com/vercel/ai) | `rejected` | Open-source license not established by GitHub license metadata; manual license review required |
| 57 | AI 前端与 AI 应用 | [CopilotKit/CopilotKit](https://github.com/CopilotKit/CopilotKit) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 58 | AI 前端与 AI 应用 | [assistant-ui/assistant-ui](https://github.com/assistant-ui/assistant-ui) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 59 | AI 前端与 AI 应用 | [vercel/ai-elements](https://github.com/vercel/ai-elements) | `rejected` | Open-source license not established by GitHub license metadata; manual license review required |
| 60 | AI 前端与 AI 应用 | [ag-ui-protocol/ag-ui](https://github.com/ag-ui-protocol/ag-ui) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 61 | AI 前端与 AI 应用 | [gradio-app/gradio](https://github.com/gradio-app/gradio) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 62 | AI 前端与 AI 应用 | [streamlit/streamlit](https://github.com/streamlit/streamlit) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 63 | AI 前端与 AI 应用 | [open-webui/open-webui](https://github.com/open-webui/open-webui) | `rejected` | Open-source license not established by GitHub license metadata; manual license review required |
| 64 | AI 前端与 AI 应用 | [lobehub/lobehub](https://github.com/lobehub/lobehub) | `rejected` | Open-source license not established by GitHub license metadata; manual license review required |
| 65 | AI 前端与 AI 应用 | [LibreChat-AI/LibreChat](https://github.com/LibreChat-AI/LibreChat) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 66 | AI 前端与 AI 应用 | [Mintplex-Labs/anything-llm](https://github.com/Mintplex-Labs/anything-llm) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 67 | AI 前端与 AI 应用 | [langgenius/dify](https://github.com/langgenius/dify) | `rejected` | Open-source license not established by GitHub license metadata; manual license review required |
| 68 | AI 前端与 AI 应用 | [huggingface/chat-ui](https://github.com/huggingface/chat-ui) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 69 | AI 前端与 AI 应用 | [langflow-ai/langflow](https://github.com/langflow-ai/langflow) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 70 | AI 前端与 AI 应用 | [onlook-dev/onlook](https://github.com/onlook-dev/onlook) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 71 | AI 前端与 AI 应用 | [dyad-sh/dyad](https://github.com/dyad-sh/dyad) | `rejected` | Open-source license not established by GitHub license metadata; manual license review required |
| 72 | AI 前端与 AI 应用 | [stackblitz/bolt.new](https://github.com/stackblitz/bolt.new) | `paused` | Repository is archived, disabled or has issues disabled |
| 73 | AI 前端与 AI 应用 | [Comfy-Org/ComfyUI](https://github.com/Comfy-Org/ComfyUI) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 74 | AI 前端与 AI 应用 | [invoke-ai/InvokeAI](https://github.com/invoke-ai/InvokeAI) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 75 | AI 前端与 AI 应用 | [CherryHQ/cherry-studio](https://github.com/CherryHQ/cherry-studio) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 76 | AI Harness、Coding Agent 与自动化工作流 | [anomalyco/opencode](https://github.com/anomalyco/opencode) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 77 | AI Harness、Coding Agent 与自动化工作流 | [openai/codex](https://github.com/openai/codex) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 78 | AI Harness、Coding Agent 与自动化工作流 | [google-gemini/gemini-cli](https://github.com/google-gemini/gemini-cli) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 79 | AI Harness、Coding Agent 与自动化工作流 | [QwenLM/qwen-code](https://github.com/QwenLM/qwen-code) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 80 | AI Harness、Coding Agent 与自动化工作流 | [cline/cline](https://github.com/cline/cline) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 81 | AI Harness、Coding Agent 与自动化工作流 | [Kilo-Org/kilocode](https://github.com/Kilo-Org/kilocode) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 82 | AI Harness、Coding Agent 与自动化工作流 | [continuedev/continue](https://github.com/continuedev/continue) | `retired` | Official README: no longer actively maintained; read-only for all users |
| 83 | AI Harness、Coding Agent 与自动化工作流 | [Aider-AI/aider](https://github.com/Aider-AI/aider) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 84 | AI Harness、Coding Agent 与自动化工作流 | [earendil-works/pi](https://github.com/earendil-works/pi) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 85 | AI Harness、Coding Agent 与自动化工作流 | [OpenHands/OpenHands](https://github.com/OpenHands/OpenHands) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 86 | AI Harness、Coding Agent 与自动化工作流 | [SWE-agent/SWE-agent](https://github.com/SWE-agent/SWE-agent) | `retired` | Official README: development moved to mini-swe-agent, which supersedes SWE-agent |
| 87 | AI Harness、Coding Agent 与自动化工作流 | [aaif-goose/goose](https://github.com/aaif-goose/goose) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 88 | AI Harness、Coding Agent 与自动化工作流 | [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 89 | AI Harness、Coding Agent 与自动化工作流 | [mastra-ai/mastra](https://github.com/mastra-ai/mastra) | `rejected` | Open-source license not established by GitHub license metadata; manual license review required |
| 90 | AI Harness、Coding Agent 与自动化工作流 | [pydantic/pydantic-ai](https://github.com/pydantic/pydantic-ai) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 91 | AI Harness、Coding Agent 与自动化工作流 | [crewAIInc/crewAI](https://github.com/crewAIInc/crewAI) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 92 | AI Harness、Coding Agent 与自动化工作流 | [agno-agi/agno](https://github.com/agno-agi/agno) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 93 | AI Harness、Coding Agent 与自动化工作流 | [microsoft/semantic-kernel](https://github.com/microsoft/semantic-kernel) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 94 | AI Harness、Coding Agent 与自动化工作流 | [modelcontextprotocol/typescript-sdk](https://github.com/modelcontextprotocol/typescript-sdk) | `rejected` | Open-source license not established by GitHub license metadata; manual license review required |
| 95 | AI Harness、Coding Agent 与自动化工作流 | [ComposioHQ/composio](https://github.com/ComposioHQ/composio) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 96 | AI Harness、Coding Agent 与自动化工作流 | [browser-use/browser-use](https://github.com/browser-use/browser-use) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 97 | AI Harness、Coding Agent 与自动化工作流 | [triggerdotdev/trigger.dev](https://github.com/triggerdotdev/trigger.dev) | `research_only` | 固定 SHA 的源码、测试、文档及许可证核验通过 |
| 98 | AI Harness、Coding Agent 与自动化工作流 | [inngest/inngest](https://github.com/inngest/inngest) | `rejected` | Open-source license not established by GitHub license metadata; manual license review required |
| 99 | AI Harness、Coding Agent 与自动化工作流 | [n8n-io/n8n](https://github.com/n8n-io/n8n) | `rejected` | Open-source license not established by GitHub license metadata; manual license review required |
| 100 | AI Harness、Coding Agent 与自动化工作流 | [activepieces/activepieces](https://github.com/activepieces/activepieces) | `rejected` | Open-source license not established by GitHub license metadata; manual license review required |

## 最终验收

最终配置下完整回归：`python -X utf8 -m unittest discover -v`，189 项通过，191.845 秒，无失败或跳过。日志：`runs/catalog-expansion-final-tests.log`。覆盖超过 80 项的默认刷新、大小写去重、已保存和退役记录保留、100 项清单完整入库及扩容后的正式扫描范围。

真实目录校验：100 个请求名称全部存在、132 条名称唯一、原 52 条记录顺序保留、两项退役记录原样保留，正式扫描范围及目标配置均为 94 个。配置除 `repositories` 外全部字段保持一致。`git diff --check` 通过。

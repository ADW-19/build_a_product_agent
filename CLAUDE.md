# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目本质（README 概览之外）

这是一个**纯文档项目**——没有源码、没有依赖、没有构建/测试命令，内容是一份面向开发者的中文技术手册，主题是"如何从零构建生产级 AI Agent 后端"。所有内容在 `docs/` 下，全部为简体中文 Markdown。

唯一的"可执行部分"是 `scripts/check_snippets.py`（文档代码块静态校验闸门），它校验的是文档里的代码块，不是项目源码。因此本项目的"开发任务"几乎都是**写/改 Markdown 文章**，而非写代码。代码块只是文章内容的一部分（示例代码）。

- 作者：Andy Yanqi Wang (ADW-19)，上海
- 许可：MIT
- 提交信息使用中文（如 `增加AI Infra章节`）
- 英文版：根目录和 `docs/01-首页/` 下有 `README-en.md` 落地页；正文英文版暂未发布（TBD）。新增正文无需同步英文。

## 目录与编号规范

`docs/` 下共 4 个一级目录，按"第 N 章"连续编号展开（一章一个二级目录）：

```
build_a_product_agent/
├── scripts/
│   └── check_snippets.py            # 文档代码块校验闸门（语法 / YAML / 语言标注 / 已弃用 API）
└── docs/
    ├── 01-首页/                     # 落地页 README（中英各一份）
    ├── 02-生产级开发-通用知识/       # 第1~4章
    │   ├── 第1章：技术选型/          # 技术栈 / 中间件 / 协议与架构模式 / 运维架构
    │   ├── 第2章：开发基本要求/      # 开发习惯（.env、Redis、async、日志、异常处理、类型注解）
    │   ├── 第3章：模块开发/          # 对话接口 / 记忆 / 工具 / 工作流 / RAG
    │   └── 第4章：Agent通路/         # 单Agent / Multi-Agent(A2A)
    ├── 03-生产级测试-系统测试/       # 第5章：系统测试
    └── 04-生产级AI Infra-基座与运维/ # 第6章：AI Infra基础知识
```

命名规则：
- 一级目录：`NN-中文主题名`（01-首页、02-生产级开发-通用知识…）
- 章目录：`第N章：主题名`
- 文章文件：`NN-标题.md`，章内序号从 01 开始
- **章内 H1 编号在每个章目录内重置**：每个章目录下的文章各自用 `# 第一章/第二章/…`，与全局章号无关（例如"第3章：模块开发"内的文件标题是 `# 第一章：对话接口`、`# 第二章：长期记忆与短期记忆`）。不要想当然地把 H1 的"第N章"当作全局章号。
- 文件系统目录名（含冒号 `：`、空格、括号）是真实路径，编写/引用时原样保留，不要改写。

## 文章写作规范（新增/修改文章时必须遵循）

每一篇独立成文，遵循统一模板与叙事线 `工业界标准 → 为什么课堂不教 → 你应该怎么写`：

1. **开头**：H1 `# 第N章：主题名`，紧接一行 `> **核心论点**：…` 引言块（用一两句话概括本章最核心的判断）。
2. **正文**：小节标题 `## 1.1 小节标题`、`## 1.2`…（章内小节号从 1.1 递增）。
3. **叙事结构**：每个主题按 `问题场景（学生/课堂典型错误代码）→ 本质原因 → 正确做法（带完整可运行代码）→ 对比表格 → 一句话总结` 展开。先给错误示范再给正确做法是强约定。
4. **表达手段**：大量使用对比表格（错误做法 vs 正确做法、维度对比）、ASCII 示意图（时序/架构图）、错误代码 vs 正确代码并置。每条关键结论通常以 `**一句话…：**` 加粗短句收束。
5. **示例代码**：均为 Python 3.13+。Web 层用 FastAPI（async def 路由），Agent 编排用 LangGraph（`StateGraph`/`astream_events`/checkpointer），工具层用 LangChain（`@tool`），数据校验用 Pydantic v2，Redis 一律用 `redis.asyncio`，LLM 客户端用 `AsyncOpenAI`。
   - **基线（校对于 2026-09）**：Python 3.14（3.13 亦可）、FastAPI 0.141、LangGraph 1.2、LangChain 1.4、Pydantic 2.13、pymilvus 3.x。写法是"基线快照"而非版本下限——只写 `≥` 会让人误以为 API 也是旧的。
   - **禁止的已弃用写法**（`scripts/check_snippets.py` 会拦）：`set_entry_point`/`set_finish_point`、`langgraph.prebuilt.create_react_agent`、`from langchain.chains|retrievers|indexes`、`MemorySaver`、`checkpointer=RedisSaver.from_conn_string(...)`（它是 `@contextmanager`）、`await httpx.post(...)`、`await tool.invoke(...)`、pymilvus 的 `connections.connect`/`utility.has_collection`、A2A 的 `tasks/send` 与 `/.well-known/agent.json`、vLLM 的 `/health_generate`。
   - **模型 ID 与单价只作占位**：模型生命周期很短（`gpt-4o` 已于 2026-02 从 ChatGPT 退役、Azure 侧 2026-10-01 退役）。新增示例请用当前在役模型，不要引入已退役的模型 ID；价格一律标注取价日期。
6. **收尾**：章末通常有 `常见踩坑清单`（表格：坑/现象/原因/解法）和 `本章小结`（表格：要点/核心原则/一句话记住）。新文章建议沿用。
7. **全章示例代码假设统一的项目骨架**：`core/`（config、llm、cache、database、agent、logger、middleware 单例封装）+ `routes/` + `services/` + `models/` + `tools/` + `main.py`。各章代码示例互相引用这个结构（如 `from core.llm import call_llm_with_retry`），写新文章时保持该骨架一致，不要引入与已有章节冲突的目录约定。

## 全书契约（改代码示例时必须守住，否则跨章会自相矛盾）

各章示例共享同一套骨架，因此下面几条是**全书面约定**，不要各写一套：

| 契约 | 约定 |
|------|------|
| Redis DB 编号 | `0` 业务缓存 / `1` 会话与 checkpointer / `2` 长期记忆 / `3` 其它。同一份数据必须固定同一 DB 号 |
| 会话历史归属 | 会话消息由 checkpointer 按 `thread_id=session_id` 持有，是**唯一真相**；不要再另搞一套 Redis 消息列表，否则两套真相必然对不上 |
| 追加型通道 | `Annotated[..., add]`/`add_messages` 只放**增量**；要放"重拼的全量"就用覆盖语义的普通字段（详见第4章 1.1.4） |
| 消息配对 | 任何裁剪/截断都必须保护 `assistant(tool_calls)` 与 `tool` 结果的配对，否则上游 API 直接 400 |
| 路由函数 | 只读 State 做 if/else，不在路由里调 LLM、不现场推算进度；判断结果写进 State |
| 结构化输出 | `with_structured_output` 默认走 strict Structured Outputs，可选字段写 `X \| None = Field(default=None)`，不用非 null 默认值或 `default_factory` |

## 常用操作

- **预览/审阅一篇或多篇文章**：直接 `Read` 对应 `.md` 文件。
- **新增一章**：在 `docs/` 下建 `NN-主题` 目录，内含 `第N章：主题名/` 子目录，文章按上述规范编写，并在根 `README.md`（及 `README-en.md`）的目录结构与内容概览表中同步登记。
- **提交前必做**：`pip install pyyaml && python scripts/check_snippets.py`，**ERROR 必须为 0**。ERROR = 示例本身有问题（语法错误、YAML 非法、语言标注错、命中已弃用 API）；WARN = 引用了 `core/` 骨架里跨文件的符号，需人工确认是否真的该存在。校验器只扫代码、不扫注释，所以"❌ 错误示范"可以写在注释里。
- **版本基线复核**：每半年对一次 `README.md` 的技术栈基线表与 `CLAUDE.md` 的基线行，改完把"校对日期"一并更新。生态半年就会前移一代，只维护版本号不维护 API 现状会持续失真。
- **git**：提交信息用中文。

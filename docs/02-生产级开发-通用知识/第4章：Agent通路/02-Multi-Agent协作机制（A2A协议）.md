# 第二章：Multi-Agent 协作机制（A2A 协议）

> **核心论点**：单 Agent 能解决 80% 的问题，剩下 20% 需要多 Agent 协作——任务太复杂、专业领域太多、单一 prompt 无法兼顾多种能力。但多 Agent 不是"多启几个进程就行"，真正的挑战在于 Agent 之间的能力发现、任务委托、状态同步和异常协调。A2A 协议把这些标准化了。本章从架构模式到代码实现，完整拆解多 Agent 协作的工程方法。

> **背景**：A2A（Agent2Agent，Agent 到 Agent）是 Google 发起、2025-06-23 宣布交由 Linux 基金会（Linux Foundation）托管的开放协议，目标是让不同厂商、不同技术栈的 Agent 之间能够互相发现能力、委托任务、同步状态。它和 MCP（Model Context Protocol，解决 **Agent ↔ 工具** 的连接）不同——A2A 解决的是 **Agent ↔ Agent** 的互操作问题，可以理解为"Agent 世界的 HTTP"。

> **规范版本基线（校对日期 2026-09-18）**：本章以 **A2A 规范 1.0.0** 为事实基线——`a2a-protocol.org` 首页标注 "Latest Released Version 1.0.0"，Agent Card 路径为 `/.well-known/agent-card.json`，JSON-RPC 绑定的方法名是 `SendMessage`/`GetTask`/`CancelTask` 这类 PascalCase 抽象操作名。命名与字段的演进路线是：**2025-04 初版草案 → v0.2.0（2025-05）→ v0.3.0（2025-07-30）→ 1.0.0**。凡与本章不一致的旧写法（`/.well-known/agent.json`、`tasks/send`、`sessionId`、Agent Card 里的 `rateLimits`）都只存在于草案或旧版本，照抄会与当前规范不兼容；正文在对应位置标注了版本差异。

---

## 2.1 为什么需要多 Agent

### 2.1.1 单 Agent 的天花板（回顾）

上一章结尾总结了单 Agent 的四个瓶颈，这里做一次更具体的对比：

```
单 Agent 处理 "帮我写一份 Q2 销售分析报告并生成 PPT"：

  同一个 LLM 需要：
  ├── 理解任务意图
  ├── 查询数据库获取 Q2 销售数据
  ├── 对数据做统计分析（同比、环比、Top10）
  ├── 检索报告模板和写作规范
  ├── 撰写分析文本
  └── 生成 PPT 格式文件

  问题：
  - System prompt 要同时包含"数据分析师"和"PPT 设计师"两种人格，互相稀释
  - 工具集混合了数据库工具、统计分析工具、文档工具，选错概率高
  - 上下文被数据表格、模板、规范塞满，LLM 注意力崩溃
```

```
多 Agent 处理同样的任务：

  主管 Agent（理解意图、分配任务、汇总结果）
      │
      ├──▶ 数据 Agent（专职查数据库 + 统计分析）
      │    工具：SQL 查询、统计计算
      │    System prompt："你是数据分析师..."
      │
      ├──▶ 文档 Agent（专职知识检索 + 写作）
      │    工具：RAG 检索、文档模板
      │    System prompt："你是报告撰写专家..."
      │
      └──▶ 设计 Agent（专职生成 PPT）
           工具：PPT 生成 API
           System prompt："你是演示文稿设计师..."
```

每个 Agent 只做一件事，能力边界清晰，prompt 精准，工具集最小化。

### 2.1.2 多 Agent 的收益量化

| 维度 | 单 Agent | 多 Agent |
|------|---------|---------|
| 单 Agent 工具数 | 15~20 个 | 3~5 个（每个 Agent） |
| System prompt 长度 | 800+ token（兼顾多种人格） | 200 token（单一角色） |
| 工具选择准确率 | 随工具数增加而降低 | 高（工具集小且专） |
| 任务隔离性 | 一个步骤崩溃可能影响全局 | 子 Agent 崩溃不影响其他 |
| 可维护性 | 改一个 prompt 可能影响所有场景 | 改一个 Agent 不影响其他 |
| 并行能力 | 依赖 LangGraph 的并行边 | 天然的进程/服务级并行 |

---

## 2.2 三种多 Agent 架构模式

### 2.2.1 模式一：Supervisor（主管-执行）

最常用的模式。一个主管 Agent 负责理解任务、分配给合适的子 Agent、汇总结果。

```
                    ┌──────────────┐
                    │  主管 Agent   │
                    │  (Supervisor) │
                    └──┬───┬───┬──┘
                       │   │   │
            ┌──────────┘   │   └──────────┐
            ▼              ▼              ▼
      ┌──────────┐ ┌──────────┐ ┌──────────┐
      │ 数据 Agent│ │ 文档 Agent│ │ 代码 Agent│
      └──────────┘ └──────────┘ └──────────┘
```

**适用场景：** 任务可以明确拆分为多个子任务，每个子任务由不同专业领域的 Agent 完成。总管负责协调，不需要参与具体执行。

**优点：** 结构清晰，容易理解和调试；主管 Agent 的逻辑简单（基本就是路由 + 汇总）。

**缺点：** 主管是单点——主管挂了，整个流程就断了；不适合 Agent 之间需要大量交互的场景。

### 2.2.2 模式二：Peer-to-Peer（对等协作）

没有主管。Agent 之间平等通信，自主协商分工。

```
      ┌──────────┐     ┌──────────┐
      │ 数据 Agent│◄───►│ 文档 Agent│
      └─────┬────┘     └─────┬────┘
            │                │
            └───────┬────────┘
                    │
              ┌──────────┐
              │ 代码 Agent│
              └──────────┘
```

**适用场景：** 任务边界不清晰，需要 Agent 之间来回协商。如"这个 SQL 查询结果有些异常，你帮我查一下最近的数据库变更日志"。

**优点：** 灵活，适合复杂协作；无单点故障。

**缺点：** 实现复杂（需要协商协议、冲突解决）；调试困难（消息链路长）。

### 2.2.3 模式三：Hierarchical（层级委托）

Supervisor 的递归版本。每个子 Agent 本身也可以是一个 Supervisor，把任务再拆分给更细粒度的 Agent。

```
                    ┌──────────────┐
                    │  总裁 Agent   │
                    └──┬───────┬──┘
                       │       │
              ┌────────┘       └────────┐
              ▼                         ▼
      ┌──────────────┐          ┌──────────────┐
      │ 业务分析主管  │          │ 技术执行主管   │
      └──┬───────┬──┘          └──┬───────┬──┘
         │       │               │       │
    ┌────┘       └────┐     ┌────┘       └────┐
    ▼                 ▼     ▼                 ▼
┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐
│市场  │ │竞品  │ │PPT   │ │前端  │ │后端  │
│分析  │ │分析  │ │生成  │ │Agent │ │Agent │
└──────┘ └──────┘ └──────┘ └──────┘ └──────┘
```

**适用场景：** 大型复杂项目（如"开发一个完整的电商平台"），需要多层任务拆解。

**本项目推荐：Supervisor 模式为主。** 理由：
- 结构简单，适合学生理解和实现
- 主管 Agent 的逻辑可以很简单（分类 + 路由）
- 调试时只需追踪 2 层（主管 → 子 Agent），不像层级模式 N 层

---

## 2.3 A2A 协议的核心概念

> **核心模型：Client 与 Agent 双角色。** A2A 的每一次交互都分为两方：**Client**（发起方，负责发请求、收结果）与 **Agent**（执行方，负责完成任务）。这个角色不是固定的——主管 Agent 对下面的子 Agent 来说是 Client，但如果它自己需要调用别的 Agent 的能力，它又会变成 Client。换言之，任何 Agent 都可以随时以 Client 身份去请求另一个 Agent。在本章的 Supervisor 架构里：**主管 Agent（编排方）= Client，数据/文档等子 Agent = 远端 Agent（执行方）**。

### 2.3.1 A2A 解决什么问题

在没有 A2A 之前，多 Agent 通信基本靠"硬编码 HTTP 调用"——Agent A 知道 Agent B 的 URL、知道 B 能干什么、知道 B 的输入输出格式——全是写死的。

A2A 把以下四件事标准化了：

| 标准化内容 | 没有 A2A 的做法 | A2A 的做法 |
|-----------|---------------|-----------|
| 能力发现 | Agent A 代码里硬编码了"Agent B 可以查订单" | Agent A 读取 Agent B 的 Agent Card，动态发现其能力 |
| 任务管理 | 一次 HTTP 请求 = 一个操作，没有任务追踪 | 每个任务有唯一 ID + 状态机 + 可取消 |
| 结果传递 | 靠约定 JSON 格式，换个 Agent 可能不兼容 | 统一的 Artifact 格式 + Part 类型（文本/文件/数据） |
| 流式推送 | 各做各的 SSE，格式不统一 | 统一的事件流（TaskStatusUpdateEvent） |

### 2.3.2 Agent Card：Agent 的"名片"

每个 Agent 在 `/.well-known/agent-card.json` 路径暴露自己的 Agent Card。其他 Agent 读这个文件就知道它能干什么（路径在 v0.3.0 从 `agent.json` 改为 `agent-card.json`，旧路径只作带 `Deprecation` 头的过渡兼容，新代码一律用新路径）：

```json
{
  "name": "数据分析 Agent",
  "description": "查询数据库、执行统计分析、生成数据报告",
  "supportedInterfaces": [
    {
      "url": "http://data-agent:8001/",
      "protocolBinding": "JSONRPC",
      "protocolVersion": "1.0"
    }
  ],
  "version": "1.0.0",
  "capabilities": {
    "streaming": true,
    "pushNotifications": false
  },
  "defaultInputModes": ["text"],
  "defaultOutputModes": ["text"],
  "skills": [
    {
      "id": "sql_query",
      "name": "SQL 查询",
      "description": "执行 SQL 查询并返回结构化数据。支持 PostgreSQL 和 MySQL。",
      "tags": ["database", "query", "sql"],
      "examples": ["SELECT * FROM orders WHERE status = 'pending'"]
    },
    {
      "id": "data_analysis",
      "name": "数据分析",
      "description": "对数据进行统计分析：均值、中位数、同比、环比、趋势预测",
      "tags": ["analysis", "statistics", "forecast"]
    }
  ],
  "securitySchemes": {
    "bearer": {"type": "http", "scheme": "bearer"}
  },
  "securityRequirements": [{"bearer": []}]
}
```

> **注意**：A2A 的 Skill 字段只有 `id`、`name`、`description`、`tags`、`examples`、`inputModes`、`outputModes`，**没有 `inputSchema`/`outputSchema`**；调用参数是放进请求的 `message`（`role` + `parts`）里传给 Agent 的，而不是在 Agent Card 里声明 JSON Schema。
>
> **Agent Card 里没有 `rateLimits` 字段。** 限流是服务端的横切策略，不属于"能力描述"，写进卡片既不会被任何 SDK 识别，也没有可依循的语义。需要限流就写在服务端配置或网关上。
>
> **认证/安全字段的形状随版本变过**：0.2.x 是 `authentication.schemes: ["bearer"]`（字符串数组）；v0.3.0 起改为 `securitySchemes`（**map**，每项必须带 `type`，如 `{"type": "http", "scheme": "bearer"}`）配合 `security`（1.0 里叫 `securityRequirements`）声明"调用需要哪些方案"。`{"schemes": [{"scheme": "bearer"}]}` 这种形状在**任何已发布版本下都不合法**——它是把 0.2.x 的字段名和 0.3.x 的嵌套结构拼错了。
>
> **服务端点也改了位置**：0.3.x 是顶层 `url` + `preferredTransport`（+ `additionalInterfaces`）；**1.0 收敛为 `supportedInterfaces` 数组**，每项是 `AgentInterface`（`url` + `protocolBinding`，如 `JSONRPC`/`HTTP+JSON`/`GRPC`），**数组第一项是首选接口**。顶层 `url` 只为兼容旧客户端而保留解析，1.0 不再输出。

### 2.3.3 Task 生命周期

A2A 中一切工作都以 Task 为单位管理：

```
                    ┌──────────────┐
                    │  SUBMITTED   │  ← 远端 Agent 收到任务
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │   WORKING    │  ← 远端 Agent 开始执行（可推送中间状态）
                    └──────┬───────┘
                           │
   ┌──────────┬────────────┼────────────┬──────────────┬──────────────┐
   │          │            │            │              │              │
┌──▼───────┐ ┌▼──────────┐ ┌▼─────────┐ ┌▼───────────┐ ┌▼───────────┐
│COMPLETED │ │INPUT_     │ │ FAILED   │ │ CANCELED   │ │ REJECTED   │
└──────────┘ │REQUIRED   │ └──────────┘ └────────────┘ └────────────┘
 （终态）     └─┬─────────┘  （终态）      （终态）      （终态：明确拒接）
               │  （AUTH_REQUIRED 同理：中断态，等调用方补认证）
               │  Client 补齐输入后【再发一次 SendMessage】
               │  （携带同一 contextId / taskId）→ 恢复
               ▼
        ┌──────────────┐
        │   WORKING    │  ← 恢复执行
        └──────┬───────┘
               │
         ┌─────▼───────┐
         │  CANCELED   │  ← Client 主动取消（终态）
         └─────────────┘
```

> **要点**：
> - **`INPUT_REQUIRED`** 是 A2A 的核心状态——Agent 在执行中向调用方（Client）索取更多输入/澄清。它**不是终态**：Client 补充信息后**再发一次 `SendMessage`**（携带服务端给的同一 `contextId`，要精确续接同一任务再带上 `taskId`），任务回到 `WORKING` 继续执行。`AUTH_REQUIRED` 是同一类"中断态"——等调用方补齐认证凭据。
> - **终态有四个**：**`COMPLETED` / `FAILED` / `CANCELED` / `REJECTED`**。`REJECTED` 是后续版本补上的，语义是 Agent **明确拒接**任务（技能不匹配、配额用尽等），与"接下来执行到一半失败"的 `FAILED` 不是一回事——只判 `FAILED` 会漏掉它。
> - **状态取值随版本变过**：2025-04 草案与 0.2.x 是小写字符串（`submitted`/`working`/`input-required`，另有 `unknown`）；**1.0 的枚举是 `TASK_STATE_*` 形式的 SCREAMING_SNAKE_CASE**（`TASK_STATE_SUBMITTED`/`TASK_STATE_WORKING`/`TASK_STATE_COMPLETED`/`TASK_STATE_FAILED`/`TASK_STATE_CANCELED`/`TASK_STATE_REJECTED`/`TASK_STATE_INPUT_REQUIRED`/`TASK_STATE_AUTH_REQUIRED`），JSON 里就写这个名字本身。
> - 拼写注意：A2A 规范用的是 **`CANCELED`（单个 L）**，不是 `CANCELLED`。

每个 Task 的核心数据结构：

```python
from enum import StrEnum
from pydantic import BaseModel, Field
from typing import Literal, Optional
from uuid import uuid4


class TaskState(StrEnum):
    """A2A Task 状态机枚举——1.0 的 JSON 取值就是这些 TASK_STATE_* 名字"""
    SUBMITTED = "TASK_STATE_SUBMITTED"
    WORKING = "TASK_STATE_WORKING"
    INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"   # 核心状态：Agent 向调用方索取更多输入/澄清（非终态）
    AUTH_REQUIRED = "TASK_STATE_AUTH_REQUIRED"     # 中断态：等调用方补认证（非终态）
    COMPLETED = "TASK_STATE_COMPLETED"             # 终态
    FAILED = "TASK_STATE_FAILED"                   # 终态
    CANCELED = "TASK_STATE_CANCELED"               # 终态（注意：规范拼写为单个 L，不是 CANCELLED）
    REJECTED = "TASK_STATE_REJECTED"               # 终态：Agent 明确拒接任务
    # 终态就是上面四个：COMPLETED / FAILED / CANCELED / REJECTED
    # （判"任务是否结束"时四个都要算上，只判 COMPLETED/FAILED 是最常见的漏判）


# ============ Part：1.0 靠 JSON 成员名区分类型 ============
# 规范要求一个 Part 恰好含 text / raw / url / data 之一，
# 没有 type / kind 判别字段（0.2.x 靠 kind，1.0 移除了它）。

class TextPart(BaseModel):
    text: str


class FilePart(BaseModel):
    """文件：内联字节用 raw（base64），外链用 url"""
    raw: Optional[str] = None
    url: Optional[str] = None


class DataPart(BaseModel):
    data: dict


Part = TextPart | FilePart | DataPart


class Message(BaseModel):
    """A2A 对话消息：role + parts（内容用 Part 承载，而不是裸字符串）"""
    role: Literal["user", "agent"]
    parts: list[Part]
    metadata: dict = Field(default_factory=dict)


class Artifact(BaseModel):
    """任务产出物——与 Message 共用 Part 类型"""
    parts: list[Part]
    metadata: dict = Field(default_factory=dict)


class TaskStatus(BaseModel):
    """Task 的状态对象——state 是对象字段，不是裸字符串"""
    state: TaskState
    message: Optional[Message] = None   # 状态说明是一个 Message（role + parts），不是裸字符串
    timestamp: Optional[str] = None     # ISO 8601 时间戳


class Task(BaseModel):
    """A2A Task 核心数据结构：{id, contextId, status, artifacts, history, metadata}"""
    id: str = Field(default_factory=lambda: str(uuid4()))
    contextId: str                                           # 服务端生成的上下文/会话 ID（camelCase）
    status: TaskStatus = Field(default_factory=lambda: TaskStatus(state=TaskState.SUBMITTED))
    artifacts: list[Artifact] = Field(default_factory=list)  # 产出物【列表】，不是单个对象
    history: list[Message] = Field(default_factory=list)     # 任务相关的对话历史
    metadata: dict = Field(default_factory=dict)             # 扩展信息（title/业务上下文等放这里）
```

> **注意**：A2A 的 Task 没有 `title`/`description`/`context`/`assigned_agent` 这些字段——任务要表达的内容放在 `history` 里的 `Message`（role + parts）中，额外的业务上下文（如"这个任务来自哪个用户""要查询哪个数据库"）放进 `metadata`。字段命名遵循 A2A 的 camelCase：`contextId` 而不是 `context_id`。
>
> **`contextId` 才是规范字段**：2025-04 草案里它叫 `sessionId`，v0.2.0 起改名 `contextId`，并且**由服务端生成**（客户端不能指定，只能在续接同一上下文时回传）。把 `sessionId` 当作"规范里的 camelCase 示例"来强调，恰好是本章旧稿最典型的"把草案当规范"。
>
> **`artifacts` 是列表**：一个任务可以有多个产出物（数据 + 图 + 报告），所以是复数数组字段，不是单个 `artifact` 对象。
>
> **`kind` 判别字段的存废**：0.2.x / 0.3.x 的 `Task`、`Message` 和各类事件都带一个 `kind` 字段（`"task"`/`"message"`/`"status-update"`/`"artifact-update"`），用来区分同一个 JSON 位置上可能出现的多种对象；**1.0 移除了 `kind`，改用 JSON 成员名本身区分**（有 `parts` 就是 Message/Artifact，有 `status` 就是 Task）。Part 同理：0.2.x 是 `{"kind": "text", "text": ...}`，1.0 就是 `{"text": ...}`。读旧 SDK 文档时看到 `kind` 不必惊讶。

---

## 2.4 用 A2A 实现 Supervisor 多 Agent 系统

### 2.4.1 架构概览

```
                         用户请求
                             │
                             ▼
                    ┌────────────────┐
                    │  FastAPI 入口   │
                    │  /v1/chat/...  │
                    └───────┬────────┘
                            │
                            ▼
      ┌──────────────────────────────────────┐
      │  主管 Agent（Orchestrator）            │
      │  = A2A 中的 Client（发起方）            │
      │  能力：理解意图 / 拆分任务 / 分配 / 汇总  │
      └──┬─────────────┬─────────────┬───────┘
         │             │             │
         │  Agent Card 里 supportedInterfaces 的 url → 子 Agent 的单个 JSON-RPC 端点
         │  方法：SendMessage、GetTask、CancelTask（JSON-RPC 2.0 over HTTP）
         │             │             │
         ▼             ▼             ▼
   ┌────────────┐ ┌────────────┐ ┌────────────┐
   │ 数据 Agent  │ │ 文档 Agent  │ │ 通用 Agent  │
   │ = 远端 Agent│ │ = 远端 Agent│ │ = 远端 Agent│
   │ 单 JSON-RPC │ │ 单 JSON-RPC│ │ 单 JSON-RPC│
   │ 端点 :8001  │ │ 端点 :8002 │ │ 端点 :8003 │
   │ Agent Card │ │ Agent Card │ │ Agent Card │
   └────────────┘ └────────────┘ └────────────┘
```

### 2.4.2 Agent Card 服务端实现

每个子 Agent 是一个独立的 FastAPI 服务，对外暴露 **Agent Card 端点**（能力发现）和 **单个 JSON-RPC 端点**（方法分发）：

```python
# agents/data_agent/main.py —— 数据分析 Agent 服务
# ============================================================
# A2A 的传输绑定是可插拔的：0.2.x 只有 JSON-RPC 2.0 over HTTP(S)，
# v0.3.0 起规范同时定义 gRPC 与 HTTP+JSON/REST（application/a2a+json、A2A-Version 头）。
# 本节用 JSON-RPC 绑定：所有方法都打到 Agent Card 里 supportedInterfaces[].url
# 指向的【单个 JSON-RPC 端点】，靠请求体里的 method 字段区分：
# SendMessage、GetTask、ListTasks、CancelTask ...
# 注意：即使换成 REST 绑定，A2A【也没有】POST /tasks、GET /tasks/{id} 这类资源路径，
# REST 形态是 POST /message:send 这样的动作式端点。
# ============================================================
import asyncio
from datetime import datetime
from uuid import uuid4

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="数据分析 Agent")

# 1.0 的状态取值（TASK_STATE_*）；终态四个，落到终态就不要再轮询/取消了
TERMINAL_STATES = (
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED",
)

# asyncio 官方文档明确警告：事件循环对 Task 只持【弱引用】，
# 必须自己保存 create_task 的返回值，否则未完成的协程可能中途被 GC 掉
_background_tasks: set[asyncio.Task] = set()


# ============ Agent Card 端点（能力发现） ============

@app.get("/.well-known/agent-card.json")   # ← 0.3.0 起的新路径，旧 agent.json 已废弃
async def get_agent_card():
    """A2A 能力发现端点——其他 Agent 通过此端点了解我能做什么"""
    return {
        "name": "数据分析 Agent",
        "description": "查询数据库、执行统计分析、生成数据报告",
        "supportedInterfaces": [
            {
                "url": "http://data-agent:8001/",   # ← JSON-RPC 端点（唯一入口）
                "protocolBinding": "JSONRPC",       # 也可声明 "HTTP+JSON" / "GRPC"
                "protocolVersion": "1.0",
            }
        ],
        "version": "1.0.0",                         # ← Agent 自己的版本号，不是协议版本
        "capabilities": {"streaming": True, "pushNotifications": False},
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "skills": [
            {
                "id": "sql_query",
                "name": "SQL 查询",
                "description": "执行 SQL 查询并返回结构化数据",
                "tags": ["database", "query"],
            },
            {
                "id": "data_analysis",
                "name": "数据分析",
                "description": "统计分析：均值、同比、环比、趋势",
                "tags": ["analysis", "statistics"],
            },
        ],
        "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
        "securityRequirements": [{"bearer": []}],
    }


# ============ 单 JSON-RPC 端点（A2A 核心） ============

# 内存任务存储（生产环境用 Redis）
tasks: dict[str, dict] = {}


class JsonRpcRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: str                        # 请求 ID（响应里原样返回）
    method: str                    # A2A 1.0 的方法名是 PascalCase 抽象操作名，如 SendMessage
    params: dict = {}


class RpcError(Exception):
    """携带 JSON-RPC 错误码的异常"""
    def __init__(self, code: int, message: str):
        self.code, self.message = code, message


def rpc_result(rpc_id: str, result: dict) -> dict:
    """JSON-RPC 2.0 成功响应：{"jsonrpc":"2.0","id":"1","result":{...}}"""
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def rpc_error(rpc_id: str, code: int, message: str) -> dict:
    """JSON-RPC 2.0 错误响应：{"jsonrpc":"2.0","id":"1","error":{...}}"""
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


@app.post("/")
async def json_rpc_endpoint(req: JsonRpcRequest):
    """
    A2A 唯一的 JSON-RPC 端点。

    主管 Agent（Client）用 HTTP POST 把
    {"jsonrpc": "2.0", "id": "1", "method": "SendMessage", "params": {...}}
    发到这里，由 method 字段分发到对应处理函数。
    """
    try:
        handlers = {
            "SendMessage": _handle_send_message,
            "GetTask": _handle_get_task,
            "ListTasks": _handle_list_tasks,
            "CancelTask": _handle_cancel_task,
            "GetExtendedAgentCard": _handle_get_extended_agent_card,
        }
        handler = handlers.get(req.method)
        if handler is None:
            raise RpcError(-32601, f"未知方法: {req.method}")  # Method not found
        result = await handler(req.params)
        return rpc_result(req.id, result)
    except RpcError as e:
        return rpc_error(req.id, e.code, e.message)


async def _handle_get_extended_agent_card(params: dict) -> dict:
    """
    GetExtendedAgentCard —— 认证后返回【扩展】Agent Card（比公开卡片多出内部技能等）。

    两件事要分清楚：
    1) 能力发现本身【不是】一个 JSON-RPC 方法，而是 GET /.well-known/agent-card.json；
       这个方法对应 0.2.0 引入的 agent/authenticatedExtendedCard，只用于"扩展卡片"。
    2) A2A 的认证走传输层头部（Authorization），不是 JSON-RPC 的 params——
       这里为教学简化省略校验，生产环境必须校验 Bearer Token。
    """
    card = await get_agent_card()
    return {
        **card,
        "skills": [
            *card["skills"],
            {
                "id": "internal_schema",
                "name": "内部表结构查询",
                "description": "仅认证调用方可见的技能",
                "tags": ["internal"],
            },
        ],
    }


async def _handle_send_message(params: dict) -> dict:
    """
    SendMessage —— 提交任务（Client 用）。

    请求体遵循规范的 SendMessageRequest：必填字段只有 message: Message（role + parts），
    可选 contextId（续接同一上下文）与 metadata（扩展点）。
    返回一个 Task 对象，内含【服务端生成】的 id 与初始状态 TASK_STATE_SUBMITTED。
    """
    message = params.get("message")
    if not isinstance(message, dict) or not message.get("parts"):
        raise RpcError(-32602, "缺少必填参数 message（role + parts）")  # Invalid params

    task_id = str(uuid4())                                 # Task ID 由服务端生成，客户端不能指定
    context_id = params.get("contextId") or str(uuid4())   # 续接同一上下文时回传，否则新开一个
    task = {
        "id": task_id,
        "contextId": context_id,
        "status": {"state": "TASK_STATE_SUBMITTED"},
        "artifacts": [],
        "history": [message],
        "metadata": params.get("metadata", {}),   # 扩展点：技能选择等私有约定放这里
        "created_at": datetime.now().isoformat(),
    }
    tasks[task_id] = task

    # 异步执行，不阻塞返回；返回值必须自己留引用，否则可能被 GC 掉
    job = asyncio.create_task(_execute_task(task_id))
    task["_job"] = job
    _background_tasks.add(job)
    job.add_done_callback(_background_tasks.discard)

    return _task_to_dict(task)


async def _handle_get_task(params: dict) -> dict:
    """GetTask —— 查询任务状态（params: {"id": task_id}）"""
    task = tasks.get(params.get("id"))
    if not task:
        raise RpcError(-32000, f"任务不存在: {params.get('id')}")
    return _task_to_dict(task)


async def _handle_list_tasks(params: dict) -> dict:
    """ListTasks —— 按 contextId 列出任务（可选参数：contextId）"""
    context_id = params.get("contextId")
    return {
        "tasks": [
            _task_to_dict(t)
            for t in tasks.values()
            if context_id is None or t["contextId"] == context_id
        ]
    }


async def _handle_cancel_task(params: dict) -> dict:
    """CancelTask —— 取消任务（params: {"id": task_id}）"""
    task = tasks.get(params.get("id"))
    if not task:
        raise RpcError(-32000, f"任务不存在: {params.get('id')}")
    if task["status"]["state"] in TERMINAL_STATES:
        raise RpcError(-32000, "任务已处于终态，无法取消")
    task["status"] = {"state": "TASK_STATE_CANCELED"}
    return _task_to_dict(task)


def _task_to_dict(task: dict) -> dict:
    """返回 A2A 规范形状的 Task 对象（只暴露协议字段，丢掉 _job/created_at 这类内部字段）"""
    return {
        "id": task["id"],
        "contextId": task["contextId"],
        "status": task["status"],
        "artifacts": task.get("artifacts", []),
        "history": task.get("history", []),
        "metadata": task.get("metadata", {}),
    }


# ============ 任务执行逻辑 ============

async def _execute_task(task_id: str):
    """后台执行任务（教学简化：仅演示状态流转）"""
    task = tasks[task_id]
    task["status"] = {"state": "TASK_STATE_WORKING"}

    try:
        # A2A 没有"调用哪个技能"的字段，技能选择是私有约定，放进 metadata 扩展点
        skill = task["metadata"].get("skill", "")
        if skill == "sql_query":
            result = await _execute_sql_query(task["metadata"], task["contextId"])
        elif skill == "data_analysis":
            result = await _execute_analysis(task["metadata"], task["contextId"])
        else:
            # 通用 LLM 处理（子 Agent 内部的 LLM 推理）
            result = await _execute_with_llm(task["metadata"], task["contextId"])

        task["status"] = {"state": "TASK_STATE_COMPLETED"}
        task["artifacts"] = [{"parts": [{"text": result}]}]

    except Exception as e:
        # status.message 的类型是 Message（role + parts），不是裸字符串
        task["status"] = {
            "state": "TASK_STATE_FAILED",
            "message": {"role": "agent", "parts": [{"text": str(e)}]},
        }


# 教学用简化实现——生产环境替换为真实数据库 / LLM 调用
async def _execute_sql_query(metadata: dict, context_id: str) -> str:
    return "查询结果：Q2 销售额 128 万，环比 +12.5%"


async def _execute_analysis(metadata: dict, context_id: str) -> str:
    return "分析完成：增长最快的品类是智能穿戴（+31%）"


async def _execute_with_llm(metadata: dict, context_id: str) -> str:
    return "通用推理完成（此处应调用子 Agent 内部的 LLM）"
```

### 2.4.3 主管 Agent 的实现

主管 Agent 的核心逻辑：解析任务 → 发现子 Agent 能力 → 分配任务 → 等待结果 → 汇总。在 A2A 中，主管 Agent 扮演 **Client** 角色，通过 JSON-RPC 调用子 Agent（远端 Agent）的单个端点。

先看一个最简单的 Client 视角调用（与 2.4.2 的服务端一一对应）：

```python
# Client 视角：一个最简单的 JSON-RPC 调用（httpx）
import asyncio
import httpx


async def main():
    # httpx.post 是【同步】顶层函数，await 它只会拿到 TypeError；
    # 要并发就得用 AsyncClient（顺带复用连接池）
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "http://data-agent:8001/",           # ← Agent Card 里 supportedInterfaces[].url
            json={
                "jsonrpc": "2.0",
                "id": "1",                       # 请求 ID，响应里原样返回
                "method": "SendMessage",         # 1.0 的 PascalCase 抽象操作名
                "params": {
                    # 必填：要表达的内容全在 message（role + parts）里
                    "message": {
                        "role": "user",
                        "parts": [{"text": "查询 Q2 销售数据"}],
                    },
                    # 可选：续接同一上下文时回传服务端给的 contextId
                    # "contextId": "b7c2...",
                    # A2A 没有"指定技能/标题"的字段，私有约定放 metadata 扩展点
                    "metadata": {"skill": "sql_query", "title": "查Q2销售"},
                },
            },
        )
        print(resp.json())


asyncio.run(main())
# → {"jsonrpc":"2.0","id":"1","result":{
#      "id":"a3f1...",                    # Task ID 由服务端生成
#      "contextId":"b7c2...",
#      "status":{"state":"TASK_STATE_SUBMITTED"},
#      "artifacts":[],"history":[...],"metadata":{...}}}
```

下面是完整的主管 Agent：

```python
# agents/orchestrator/main.py —— 主管 Agent（A2A 中的 Client）
# 模块路径记牢：2.6 的入口是 from agents.orchestrator.main import orchestrator
import asyncio
import httpx
from typing import TypedDict

from pydantic import BaseModel
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph, START, END
from langchain_openai import ChatOpenAI


# A2A 1.0 的四个终态（与 2.4.2 服务端保持一致）
TERMINAL_STATES = (
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED",
)


# ==================== State ====================

class OrchestratorState(TypedDict):
    user_query: str
    session_id: str
    subtasks: list[dict]          # 拆分后的子任务列表
    task_results: list[dict]      # 子任务执行结果
    final_response: str


# ==================== 模型 ====================

llm = ChatOpenAI(model="gpt-5.1")


class SubtaskList(BaseModel):
    """主管 LLM 输出的子任务列表"""
    reasoning: str = ""
    subtasks: list[dict]  # [{"title": "...", "description": "...", "skill": "sql_query", "agent": "data-agent"}]


# ==================== 能力注册表 ====================

# 生产环境：从各 Agent 的 /.well-known/agent-card.json 动态获取
# 开发环境：静态注册表（url 取自 Agent Card 里 supportedInterfaces[].url）
AGENT_REGISTRY = {
    "data-agent": {
        "name": "数据分析 Agent",
        "url": "http://data-agent:8001/",
        "skills": ["sql_query", "data_analysis"],
    },
    "doc-agent": {
        "name": "文档 Agent",
        "url": "http://doc-agent:8002/",
        "skills": ["search_kb", "generate_report"],
    },
    "general-agent": {
        "name": "通用 Agent",
        "url": "http://general-agent:8003/",
        "skills": ["chat", "reasoning"],
    },
}


# ==================== JSON-RPC 客户端封装 ====================

class A2AClient:
    """A2A Client 的最小封装：把 JSON-RPC 请求 POST 到子 Agent 的单个端点"""

    def __init__(self, base_url: str, client: httpx.AsyncClient):
        self.base_url = base_url.rstrip("/") + "/"
        self.client = client

    async def call(self, method: str, params: dict, rpc_id: str = "1") -> dict:
        """发送一个 JSON-RPC 2.0 请求并返回 result"""
        payload = {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}
        resp = await self.client.post(self.base_url, json=payload)
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise RuntimeError(f"JSON-RPC 错误: {body['error']}")
        return body["result"]

    async def send_message(
        self,
        message: dict,
        context_id: str = "",
        metadata: dict | None = None,
    ) -> dict:
        """SendMessage —— 提交任务。必填 message（role + parts），返回服务端生成的 Task"""
        params: dict = {"message": message}
        if context_id:
            params["contextId"] = context_id        # 续接同一上下文
        if metadata:
            params["metadata"] = metadata           # 扩展点（技能选择、标题等）
        return await self.call("SendMessage", params)

    async def get_task(self, task_id: str) -> dict:
        """GetTask —— 查询任务状态"""
        return await self.call("GetTask", {"id": task_id})


# ==================== 节点 ====================

async def node_decompose(state: OrchestratorState) -> dict:
    """
    节点①：任务分解。

    主管 LLM 分析用户请求，拆分为子任务，并决定每个子任务分配给哪个 Agent。
    """
    decompose_llm = llm.with_structured_output(SubtaskList)

    # 构建可用 Agent 的能力描述
    capabilities_desc = "\n".join(
        f"- {info['name']} (key: {key})：能力 {', '.join(info['skills'])}"
        for key, info in AGENT_REGISTRY.items()
    )

    prompt = f"""将以下用户请求拆分为 1~4 个子任务，每个子任务分配给最合适的 Agent。

## 可用 Agent
{capabilities_desc}

## 拆分规则
1. 查询数据库 → data-agent
2. 知识检索、文档生成 → doc-agent
3. 一般对话、逻辑推理 → general-agent
4. 每个子任务的 description 要具体到"查什么表""搜什么关键词"

## 用户请求
{state['user_query']}

## 输出
生成子任务列表，每个子任务包含 title、description、skill、agent 字段。"""

    result = await decompose_llm.ainvoke(prompt)

    # 为每个子任务生成唯一 ID
    subtasks = []
    for i, st in enumerate(result.subtasks):
        subtasks.append({
            "id": f"subtask-{state['session_id']}-{i+1}",
            "title": st.get("title", f"子任务{i+1}"),
            "description": st.get("description", ""),
            "skill": st.get("skill", ""),
            "agent": st.get("agent", "general-agent"),
            "status": "pending",
        })

    return {"subtasks": subtasks}


# 模块级分发器：2.4.3 的节点与 2.5.2 / 2.5.3 的批量分发共用同一个函数。
# 注意它是【模块级】的（不是嵌在某个节点里的闭包），否则后面的小节引用不到它。
async def dispatch_one(
    st: dict,
    client: httpx.AsyncClient,
    context: dict | None = None,
) -> dict:
    """
    把单个子任务作为一次 SendMessage 发给目标远端 Agent，并轮询到终态。

    - st：子任务 dict（至少含 id/title/description/skill/agent）
    - client：调用方传进来的 httpx.AsyncClient——由调用方用 async with 建好后一路传下来；
      在函数内部自己 new 一个，会让每个子任务各建一份连接池
    - context：可选上下文（user_query、dep_results 等，2.5.3 的依赖管理会用到）
    """
    context = context or {}
    agent_info = AGENT_REGISTRY.get(st["agent"])
    if not agent_info:
        return {**st, "status": "TASK_STATE_REJECTED", "error": f"未知 Agent: {st['agent']}"}

    rpc = A2AClient(agent_info["url"], client)
    try:
        # 1) 提交任务：SendMessage（JSON-RPC，不是 POST /tasks）
        #    内容放进 message.parts；技能/标题这类私有约定放进 metadata 扩展点
        task = await rpc.send_message(
            message={
                "role": "user",
                "parts": [{"text": f"{st['title']}：{st['description']}"}],
            },
            metadata={
                "skill": st.get("skill", ""),
                "title": st["title"],
                "user_query": context.get("user_query", ""),
                "dep_results": context.get("dep_results", {}),
            },
        )
        task_id = task["id"]        # ← Task ID 由服务端生成，客户端不能拿自编的 ID 去查

        # 2) 轮询任务状态：GetTask
        for _ in range(60):  # 最多等 60 秒
            await asyncio.sleep(1)
            task = await rpc.get_task(task_id)
            state_value = task["status"]["state"]   # status.state 是对象字段
            if state_value in TERMINAL_STATES:
                return {
                    "task_id": task_id,
                    "title": st["title"],
                    "agent": st["agent"],
                    "status": state_value,
                    "artifacts": task.get("artifacts", []),
                    "error": task["status"].get("message"),
                }

        return {**st, "status": "TASK_STATE_FAILED", "error": "任务超时"}

    except Exception as e:
        return {**st, "status": "TASK_STATE_FAILED", "error": str(e)}


async def node_dispatch_and_wait(state: OrchestratorState) -> dict:
    """
    节点②：以 Client 身份把任务发给各子 Agent（远端 Agent）并等待结果。

    主管是 Client、子 Agent 是远端 Agent：
    通过 Agent Card 里 supportedInterfaces[].url（单个 JSON-RPC 端点）调用 SendMessage 提交，
    然后轮询 GetTask 直到终态。
    """
    subtasks = state["subtasks"]

    # 简化实现：所有子任务并行分发（共用同一个 AsyncClient）
    async with httpx.AsyncClient(timeout=120.0) as client:
        results = await asyncio.gather(*[
            dispatch_one(st, client, context={"user_query": state["user_query"]})
            for st in subtasks
        ])

    return {"task_results": results}


async def node_assemble(state: OrchestratorState) -> dict:
    """节点③：汇总所有子任务结果，生成最终回复"""
    results = state["task_results"]

    # 分离成功和失败的结果（终态里只有 TASK_STATE_COMPLETED 算成功）
    success_results = [r for r in results if r["status"] == "TASK_STATE_COMPLETED"]
    failed_results = [r for r in results if r["status"] != "TASK_STATE_COMPLETED"]

    # 提取所有成功结果的文本（artifacts 是列表，每个 artifact 里又是 parts 列表）
    result_texts = []
    for r in success_results:
        for artifact in r.get("artifacts") or []:
            for part in artifact.get("parts", []):
                if "text" in part:          # 1.0 用 JSON 成员名判断 Part 类型
                    result_texts.append(f"## {r['title']}\n{part['text']}")

    combined = "\n\n".join(result_texts)

    # 嵌套 f-string 里复用同类引号要 Python 3.12+（PEP 701），
    # 先拼好变量，3.10 / 3.11 的读者也不会踩 SyntaxError
    failed_note = ""
    if failed_results:
        failed_titles = "、".join(r["title"] for r in failed_results)
        failed_note = f"## 注意：以下子任务失败了，不要编造相关数据：{failed_titles}"

    # 让主管 LLM 汇总
    prompt = f"""根据以下子任务的结果，回答用户的原始问题。

## 用户问题
{state['user_query']}

## 子任务结果
{combined}

{failed_note}

请生成一个完整、专业的回复。如果部分结果缺失，诚实说明。"""

    response = await llm.ainvoke([{"role": "user", "content": prompt}])

    return {"final_response": response.content}


# ==================== 构建主管 Graph ====================

def build_orchestrator():
    graph = StateGraph(OrchestratorState)

    graph.add_node("decompose", node_decompose)
    graph.add_node("dispatch", node_dispatch_and_wait)
    graph.add_node("assemble", node_assemble)

    graph.add_edge(START, "decompose")        # 入口用 add_edge(START, ...)，旧的入口 API 已弃用
    graph.add_edge("decompose", "dispatch")
    graph.add_edge("dispatch", "assemble")
    graph.add_edge("assemble", END)

    # 挂 checkpointer，ainvoke 才能带 thread_id 做会话隔离（2.6 的完整示例要用）
    return graph.compile(checkpointer=InMemorySaver())


orchestrator = build_orchestrator()
```

### 2.4.4 流式推送：A2A 的 Task Status Update 事件

A2A 的流式推送是**同一个 JSON-RPC 端点**上的另一个方法：0.2.0 起叫 `message/stream`，**1.0 的抽象操作名是 `SendStreamingMessage`**（断线后重新订阅用 `SubscribeToTask`，对应 0.2.0 的 `tasks/resubscribe`——注意流式是 `SendMessage` / `SendStreamingMessage` 成对的两个方法，不存在 `tasks/stream` 这种写法）。服务端返回 SSE（`text/event-stream`）。与 2.3.1 表格一致，每个 `data` 都是一个 JSON-RPC 2.0 响应对象，`result` 里携带 `TaskStatusUpdateEvent` 或 `TaskArtifactUpdateEvent`。子 Agent 在执行过程中可以持续推送中间结果：

```python
# agents/data_agent/main.py —— 给 2.4.2 的 JSON-RPC 端点增加流式方法 SendStreamingMessage

import asyncio
import json

from fastapi.responses import StreamingResponse


def _sse(rpc_id: str, result: dict) -> str:
    """
    把 JSON-RPC 2.0 响应对象格式化为 SSE 事件。

    注意：A2A 的流式推送里，每个 data 都是一个完整的 JSON-RPC 2.0 响应，
    result 携带 TaskStatusUpdateEvent 或 TaskArtifactUpdateEvent，
    绝不是 {"type":"status_update", ...} 这样的扁平 JSON。
    """
    payload = {"jsonrpc": "2.0", "id": rpc_id, "result": result}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _stream_task_events(rpc_id: str, params: dict):
    """
    SendStreamingMessage —— 任务事件流（A2A 的 SSE 推送）。

    事件定义（与 2.3.1 表格一致，两者是各自独立的事件对象，不要混成一个）：
    - TaskStatusUpdateEvent:   {id, status: {state, message?, timestamp?}, final}
    - TaskArtifactUpdateEvent: {id, artifact: {parts: [...]}, final}
    其中 status 是对象，status.state 才是状态字符串；
    final 标志该事件是否为终态（TASK_STATE_COMPLETED / FAILED / CANCELED / REJECTED）。
    """
    task_id = params.get("id")
    task = tasks.get(task_id)
    if not task:
        # status.message 必须是 Message（role + parts），不能塞裸字符串
        yield _sse(rpc_id, {
            "id": task_id,
            "status": {
                "state": "TASK_STATE_FAILED",
                "message": {"role": "agent", "parts": [{"text": "任务不存在"}]},
            },
            "final": True,
        })
        return

    last_state = None

    # 非终态循环推送中间状态
    while task["status"]["state"] not in TERMINAL_STATES:
        state = task["status"]["state"]
        if state != last_state:
            last_state = state
            # TaskStatusUpdateEvent：id + status 对象 + final 标志
            yield _sse(rpc_id, {"id": task_id, "status": task["status"], "final": False})

        # 中间产出 → TaskArtifactUpdateEvent（一次一个 artifact，字段是单数）
        if task.get("intermediate_artifact"):
            yield _sse(rpc_id, {"id": task_id, "artifact": task["intermediate_artifact"], "final": False})
            del task["intermediate_artifact"]

        await asyncio.sleep(0.5)

    # 终态：先推产出物事件，再推 final=true 的状态事件
    for artifact in task.get("artifacts", []):
        yield _sse(rpc_id, {"id": task_id, "artifact": artifact, "final": False})
    yield _sse(rpc_id, {"id": task_id, "status": task["status"], "final": True})
    yield "data: [DONE]\n\n"


# 在 2.4.2 的 json_rpc_endpoint 分发器里加一行（方法名不同，端点是同一个）：
#   if req.method == "SendStreamingMessage":     # 旧客户端兼容别名：message/stream
#       return StreamingResponse(
#           _stream_task_events(req.id, req.params),
#           media_type="text/event-stream",
#       )
```

主管 Agent 可以用 `SendStreamingMessage` 实时订阅子任务进度，并向用户推送进度信息：

```
用户看到：
  🔄 正在查询 Q2 销售数据...      ← 数据 Agent 状态变为 working
  ✅ Q2 销售数据已获取 (12,845 条) ← 数据 Agent 完成
  🔄 正在检索报告模板...          ← 文档 Agent 状态变为 working
  ✅ 报告模板已加载               ← 文档 Agent 完成
  📝 正在生成最终报告...          ← assemble 节点执行中
```

---

## 2.5 多 Agent 协作中的关键工程问题

### 2.5.1 Agent 能力发现：静态注册 vs 动态发现

**静态注册（本项目推荐）：**

```yaml
# 一个配置文件集中管理所有 Agent 信息
# agents/registry.yaml
agents:
  data-agent:
    url: http://data-agent:8001
    skills: [sql_query, data_analysis]
    max_concurrent_tasks: 5
  doc-agent:
    url: http://doc-agent:8002
    skills: [search_kb, generate_report]
    max_concurrent_tasks: 10
```

**动态发现（生产级）：**

```python
# 动态发现：启动时抓一遍各 Agent 的 Agent Card
import httpx

from core.logger import logger      # 手册骨架：core/ 下的单例封装


async def discover_agents() -> dict:
    """启动时自动扫描所有 Agent 的 Agent Card"""
    agent_urls = [
        "http://data-agent:8001",
        "http://doc-agent:8002",
        "http://general-agent:8003",
    ]

    registry: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=5.0) as client:
        for url in agent_urls:
            try:
                # 路径是 agent-card.json（0.3.0 起），不是老的 agent.json
                resp = await client.get(f"{url}/.well-known/agent-card.json")
                resp.raise_for_status()
                card = resp.json()
                registry[card["name"]] = {
                    # 端点从 supportedInterfaces 里取，第一项是首选接口
                    "url": card["supportedInterfaces"][0]["url"],
                    "skills": [s["id"] for s in card.get("skills", [])],
                    "skill_details": {s["id"]: s for s in card.get("skills", [])},
                }
            except Exception:
                logger.warning(f"Agent {url} 不可用，跳过")

    return registry
```

**对于本项目：静态注册足够。** 理由：Agent 种类少（3~5 个），不会频繁增减。动态发现的优势在 Agent 数量多、频繁变更的生产环境中体现。

### 2.5.2 超时与降级——子 Agent 挂了不能拖垮主管

```python
async def dispatch_with_timeout(
    st: dict,
    client: httpx.AsyncClient,
    timeout: float = 60.0,
) -> dict:
    """带超时的任务分发，超时后返回降级结果

    dispatch_one 是 2.4.3 定义的【模块级】函数，签名是
    (st, client, context=None)——这里必须把 client 一起传下去。
    """
    try:
        return await asyncio.wait_for(
            dispatch_one(st, client),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning("子任务超时 | task=%s | agent=%s", st["id"], st["agent"])
        return {
            "task_id": st["id"],
            "title": st["title"],
            "agent": st["agent"],
            "status": "TASK_STATE_FAILED",
            "error": f"任务超时（>{timeout}秒）",
            "degraded": True,  # 标记为降级结果
        }
```

主管 Agent 在汇总阶段，对降级结果的处理策略：

```python
# 在 node_assemble 中
if failed_results:
    degraded_note = "\n".join(
        f"- {r['title']}：{r.get('error', '未知错误')}" for r in failed_results
    )
    prompt += f"\n\n以下子任务未能完成，请向用户诚实说明：\n{degraded_note}"
```

### 2.5.3 子任务间的依赖管理

不是所有子任务都能并行。比如"先查数据，再用数据分析"——step 2 依赖 step 1 的结果。

```python
import asyncio

import httpx
from pydantic import BaseModel, Field


class Subtask(BaseModel):
    id: str
    title: str
    description: str
    agent: str
    skill: str
    depends_on: list[str] = Field(default_factory=list)  # 依赖的子任务 ID
    status: str = "pending"


async def dispatch_with_deps(subtasks: list[Subtask]) -> list[dict]:
    """按依赖关系分层执行：无依赖的并行，有依赖的等前置完成"""
    completed: dict[str, dict] = {}

    # 复用 2.4.3 的模块级 dispatch_one——它要的是 dict 和 AsyncClient
    async with httpx.AsyncClient(timeout=120.0) as client:
        while len(completed) < len(subtasks):
            # 找出所有前置依赖都已完成的子任务
            ready = [
                st for st in subtasks
                if st.id not in completed
                and all(dep in completed for dep in st.depends_on)
            ]

            if not ready:
                # 有循环依赖或所有剩余任务都被阻塞
                break

            # 并行执行当前批次：Pydantic 模型要先 model_dump() 成 dict，
            # 既不能用 st.dict()（v2 已废弃），也不能对模型本身下标访问
            batch_results = await asyncio.gather(*[
                dispatch_one(
                    st.model_dump(),
                    client,
                    context={
                        "dep_results": {
                            dep_id: completed[dep_id] for dep_id in st.depends_on
                        },
                    },
                )
                for st in ready
            ])

            for st, result in zip(ready, batch_results):
                completed[st.id] = result

    return list(completed.values())
```

### 2.5.4 上下文传递——子 Agent 之间需要共享什么

```
主管 Agent 分发给各子 Agent 的 context：
{
    "user_query": "帮我写一份 Q2 竞品分析报告",     ← 原始需求
    "session_id": "abc123",                         ← 内部会话标识（发给 A2A 时走 contextId / metadata）
    "user_profile": {                               ← 用户画像（长期记忆）
        "name": "张三", "role": "产品经理", "preference": "数据驱动"
    },
    "shared_knowledge": {                           ← 前置知识（RAG 结果）
        "competitor_list": ["竞品A", "竞品B", "竞品C"],
        "time_range": "2025 Q2",
    },
    "dep_results": {                                ← 前置子任务结果（dispatch_one 的返回形状）
        "subtask-1": {"status": "TASK_STATE_COMPLETED", "artifacts": [{"parts": [{"text": "..."}]}]},
    },
}
```

**原则：给子 Agent 的 context 要精不要多。** 把整个对话历史 5000 token 全丢给子 Agent，和主管 Agent 全自己干没有区别。只传递子 Agent 完成任务必需的上下文。

---

## 2.6 完整示例：多 Agent 协作写一份竞品分析报告

```python
# main.py —— 完整的多 Agent 入口
import json

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# 与 2.4.3 的文件位置 agents/orchestrator/main.py 对应
from agents.orchestrator.main import orchestrator

app = FastAPI()


class ReportRequest(BaseModel):
    """POST /v1/report/generate 的请求体"""
    query: str
    session_id: str


def _sse(event_type: str, data: dict) -> str:
    """将 type 和 data 格式化为标准 SSE 事件字符串"""
    payload = {"type": event_type, **data}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.post("/v1/report/generate")
async def generate_report(request: ReportRequest):
    """
    多 Agent 协作生成报告。
    
    内部流程：
    1. 主管 Agent 拆分任务
    2. 并行分发给数据 Agent + 文档 Agent
    3. 汇总结果
    4. 流式返回进度 + 最终结果
    """
    async def progress_stream():
        state = {
            "user_query": request.query,
            "session_id": request.session_id,
            "subtasks": [],
            "task_results": [],
            "final_response": "",
        }
        
        # 阶段一：任务拆分
        yield _sse("progress", {"stage": "decompose", "message": "正在分析任务..."})
        # thread_id 能生效的前提：build_orchestrator() 里 compile(checkpointer=InMemorySaver())
        state = await orchestrator.ainvoke(
            state,
            config={"configurable": {"thread_id": request.session_id}},
        )
        
        # 推送子任务列表
        yield _sse("plan", {"subtasks": [
            {"title": st["title"], "agent": st["agent"]} for st in state["subtasks"]
        ]})
        
        # 阶段二：分发给子 Agent（内部并行）
        yield _sse("progress", {"stage": "execute", "message": f"正在执行 {len(state['subtasks'])} 个子任务..."})
        # 子 Agent 有各自的流式端点，这里简化
        
        # 阶段三：汇总
        yield _sse("progress", {"stage": "assemble", "message": "正在生成报告..."})
        
        yield _sse("result", {"report": state["final_response"]})
        yield "data: [DONE]\n\n"
    
    return StreamingResponse(progress_stream(), media_type="text/event-stream")
```

---

## 2.7 什么时候不需要多 Agent

多 Agent 不是银弹。以下情况用单 Agent 更好：

| 场景 | 原因 |
|------|------|
| 任务步骤 < 3 个 | 多 Agent 的通信开销 > 并行收益 |
| Agent 之间存在强依赖链 | 并行不起来，多 Agent 等于分布式单 Agent |
| 团队 < 3 人 | 维护多个 Agent 的 prompt 和工具集需要人力 |
| 延迟要求 < 2 秒 | HTTP 调用链路的额外延迟可能超出预算 |
| 上下文共享需求极高 | 每个 Agent 都需要看全部上下文，等于单 Agent |

**决策框架：**
1. 先写成单 Agent（一个 StateGraph 搞定）
2. 发现 prompt 越来越长、工具越来越多、回答质量下降
3. 找出"可以独立成 Agent"的子能力（有明确的输入输出、不依赖全局上下文）
4. 把那个子能力拆出去成为一个独立的 Agent
5. 不要一开始就设计多 Agent

---

## 2.8 本章小结

| 要点 | 核心做法 | 一句话 |
|------|---------|--------|
| 多 Agent 模式 | Supervisor / Peer-to-Peer / Hierarchical | 本项目用 Supervisor：一个主管分配任务，子 Agent 各司其职 |
| A2A Agent Card | `/.well-known/agent-card.json` 暴露能力描述（端点写在 `supportedInterfaces` 里） | 每个 Agent 自描述"我能干什么、输入输出格式是什么" |
| A2A Task 生命周期 | SUBMITTED → WORKING → … → COMPLETED/FAILED/CANCELED/REJECTED（含 INPUT_REQUIRED 索取澄清） | 一切工作以 Task 为单位，有 ID、有状态机、可追踪、可取消 |
| 任务分发与汇总 | 主管 LLM 拆分 → 子 Agent 并行执行 → 主管汇总 | 互不依赖的并行（`asyncio.gather`），有依赖的串行 |
| 超时与降级 | `asyncio.wait_for` 超时兜底 + 降级结果标注 | 子 Agent 挂了不能拖垮主管，诚实告知用户"部分结果缺失" |
| 动态发现 vs 静态注册 | 本项目用静态注册表，生产环境可升级为动态发现 | 3~5 个 Agent 不需要动态发现，配置文件足够 |
| 多 Agent 适用条件 | 任务可并行拆分、各 Agent 专业领域清晰、工具集不重叠 | 单 Agent 能解决的不用多 Agent，只在 prompt/工具膨胀时拆分 |

**多 Agent 的本质是"分而治之"——把一个大而全的 Agent 拆成多个小而专的 Agent。但拆分有成本（通信延迟、上下文传递、异常协调），所以只在单 Agent 撞到天花板时才拆。A2A 协议让这个拆分有章可循。**

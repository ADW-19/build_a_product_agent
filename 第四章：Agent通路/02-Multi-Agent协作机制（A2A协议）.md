# 第二章：Multi-Agent 协作机制（A2A 协议）

> **核心论点**：单 Agent 能解决 80% 的问题，剩下 20% 需要多 Agent 协作——任务太复杂、专业领域太多、单一 prompt 无法兼顾多种能力。但多 Agent 不是"多启几个进程就行"，真正的挑战在于 Agent 之间的能力发现、任务委托、状态同步和异常协调。A2A 协议把这些标准化了。本章从架构模式到代码实现，完整拆解多 Agent 协作的工程方法。

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

每个 Agent 在 `/.well-known/agent.json` 路径暴露自己的 Agent Card。其他 Agent 读这个文件就知道它能干什么：

```json
{
  "name": "数据分析 Agent",
  "description": "查询数据库、执行统计分析、生成数据报告",
  "url": "http://data-agent:8001",
  "version": "1.0.0",
  "capabilities": {
    "streaming": true,
    "pushNotifications": false
  },
  "skills": [
    {
      "id": "sql_query",
      "name": "SQL 查询",
      "description": "执行 SQL 查询并返回结构化数据。支持 PostgreSQL 和 MySQL。",
      "tags": ["database", "query", "sql"],
      "inputSchema": {
        "type": "object",
        "properties": {
          "query": {"type": "string", "description": "SQL 查询语句"},
          "database": {"type": "string", "enum": ["postgresql", "mysql"]}
        },
        "required": ["query"]
      },
      "outputSchema": {
        "type": "object",
        "properties": {
          "columns": {"type": "array"},
          "rows": {"type": "array"},
          "row_count": {"type": "integer"}
        }
      }
    },
    {
      "id": "data_analysis",
      "name": "数据分析",
      "description": "对数据进行统计分析：均值、中位数、同比、环比、趋势预测",
      "tags": ["analysis", "statistics", "forecast"],
      "inputSchema": {
        "type": "object",
        "properties": {
          "data": {"type": "object", "description": "待分析的结构化数据"},
          "analysis_type": {"type": "string", "enum": ["summary", "trend", "compare", "forecast"]}
        }
      }
    }
  ],
  "authentication": {
    "type": "bearer_token"
  },
  "rateLimit": {
    "maxRequestsPerMinute": 60
  }
}
```

### 2.3.3 Task 生命周期

A2A 中一切工作都以 Task 为单位管理：

```
                    ┌──────────┐
                    │ submitted │  ← Agent B 收到任务
                    └────┬─────┘
                         │
                    ┌────▼─────┐
                    │  working  │  ← Agent B 开始执行（可推送中间状态）
                    └────┬─────┘
                         │
              ┌──────────┼──────────┐
              │                     │
         ┌────▼─────┐         ┌────▼─────┐
         │ completed │         │  failed   │
         └──────────┘         └────┬─────┘
                                   │
                              ┌────▼─────┐
                              │ cancelled │  ← Agent A 主动取消
                              └──────────┘
```

每个 Task 的核心数据结构：

```python
from pydantic import BaseModel, Field
from typing import Optional, Literal
from uuid import UUID, uuid4
from datetime import datetime


class TaskStatus(str):
    SUBMITTED = "submitted"
    WORKING = "working"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Artifact(BaseModel):
    """任务产出物"""
    parts: list[dict]  # 每个 part 可以是 text/image/file/data


class Task(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str                                    # 关联的会话 ID
    title: str                                         # 任务标题
    description: str                                   # 任务详细描述
    context: dict = Field(default_factory=dict)         # 上下文数据
    status: str = TaskStatus.SUBMITTED
    assigned_agent: str = ""                           # 被分配给的 Agent 名称
    artifact: Optional[Artifact] = None                 # 产出物
    error: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())
```

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
                    ┌────────────────┐
                    │  主管 Agent     │
                    │  (Orchestrator) │
                    │                │
                    │  能力：         │
                    │  1. 理解意图    │
                    │  2. 拆分任务    │
                    │  3. 分配Agent   │
                    │  4. 汇总结果    │
                    └──┬───┬───┬─────┘
                       │   │   │
           ┌───────────┘   │   └───────────┐
           ▼               ▼               ▼
    ┌────────────┐ ┌────────────┐ ┌────────────┐
    │ 数据 Agent  │ │ 文档 Agent  │ │ 通用 Agent  │
    │ :8001      │ │ :8002      │ │ :8003      │
    │            │ │            │ │            │
    │ Agent Card │ │ Agent Card │ │ Agent Card  │
    │ /.well-    │ │ /.well-    │ │ /.well-     │
    │ known/     │ │ known/     │ │ known/      │
    │ agent.json │ │ agent.json │ │ agent.json  │
    └────────────┘ └────────────┘ └────────────┘
```

### 2.4.2 Agent Card 服务端实现

每个子 Agent 是一个独立的 FastAPI 服务，对外暴露 Agent Card 和 Task 端点：

```python
# agents/data_agent/main.py —— 数据分析 Agent 服务
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from uuid import uuid4
import asyncio

app = FastAPI(title="数据分析 Agent")


# ============ Agent Card 端点 ============

@app.get("/.well-known/agent.json")
async def get_agent_card():
    """A2A 能力发现端点——其他 Agent 通过此端点了解我能做什么"""
    return {
        "name": "数据分析 Agent",
        "description": "查询数据库、执行统计分析、生成数据报告",
        "url": "http://data-agent:8001",
        "version": "1.0.0",
        "capabilities": {"streaming": True},
        "skills": [
            {
                "id": "sql_query",
                "name": "SQL 查询",
                "description": "执行 SQL 查询并返回结构化数据",
                "tags": ["database", "query"],
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "params": {"type": "object"},
                    },
                    "required": ["query"],
                },
            },
            {
                "id": "data_analysis",
                "name": "数据分析",
                "description": "统计分析：均值、同比、环比、趋势",
                "tags": ["analysis", "statistics"],
            },
        ],
    }


# ============ Task 端点（A2A 核心） ============

# 内存任务存储（生产环境用 Redis）
tasks: dict[str, dict] = {}


class TaskSendRequest(BaseModel):
    id: str
    session_id: str
    title: str
    description: str
    context: dict = {}
    skill_id: str = ""  # 要求使用的技能 ID


@app.post("/tasks")
async def create_task(request: TaskSendRequest):
    """
    A2A 任务提交端点。
    
    主管 Agent 调用此端点，向本 Agent 提交一个任务。
    返回任务 ID，后续主管可以通过 GET /tasks/{id} 查询状态。
    """
    task = {
        "id": request.id,
        "session_id": request.session_id,
        "title": request.title,
        "description": request.description,
        "context": request.context,
        "skill_id": request.skill_id,
        "status": TaskStatus.SUBMITTED,
        "artifact": None,
        "created_at": datetime.now().isoformat(),
    }
    tasks[request.id] = task

    # 异步执行任务（不阻塞返回）
    asyncio.create_task(_execute_task(request.id))

    return {"task_id": request.id, "status": "submitted"}


@app.get("/tasks/{task_id}")
async def get_task(task_id: str):
    """A2A 任务状态查询端点"""
    task = tasks.get(task_id)
    if not task:
        return JSONResponse({"error": "task not found"}, status_code=404)

    return {
        "task_id": task["id"],
        "status": task["status"],
        "artifact": task.get("artifact"),
        "error": task.get("error"),
    }


@app.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    """A2A 任务取消端点"""
    task = tasks.get(task_id)
    if not task:
        return JSONResponse({"error": "task not found"}, status_code=404)

    if task["status"] in (TaskStatus.COMPLETED, TaskStatus.FAILED):
        return {"error": "task already completed or failed"}

    task["status"] = TaskStatus.CANCELLED
    return {"task_id": task_id, "status": "cancelled"}


# ============ 任务执行逻辑 ============

async def _execute_task(task_id: str):
    """后台执行任务"""
    task = tasks[task_id]
    task["status"] = TaskStatus.WORKING

    try:
        if task["skill_id"] == "sql_query":
            result = await _execute_sql_query(task["description"], task["context"])
        elif task["skill_id"] == "data_analysis":
            result = await _execute_analysis(task["description"], task["context"])
        else:
            # 通用 LLM 处理（子 Agent 内部的 LLM 推理）
            result = await _execute_with_llm(task["description"], task["context"])

        task["status"] = TaskStatus.COMPLETED
        task["artifact"] = {"parts": [{"type": "text", "text": result}]}

    except Exception as e:
        task["status"] = TaskStatus.FAILED
        task["error"] = str(e)
```

### 2.4.3 主管 Agent 的实现

主管 Agent 的核心逻辑：解析任务 → 发现子 Agent 能力 → 分配任务 → 等待结果 → 汇总。

```python
# agents/orchestrator/main.py
import httpx
from typing import TypedDict
from pydantic import BaseModel
from langgraph.graph import StateGraph, END
from langchain_openai import ChatOpenAI


# ==================== State ====================

class OrchestratorState(TypedDict):
    user_query: str
    session_id: str
    subtasks: list[dict]          # 拆分后的子任务列表
    task_results: list[dict]      # 子任务执行结果
    final_response: str


# ==================== 模型 ====================

llm = ChatOpenAI(model="gpt-4o")


class SubtaskList(BaseModel):
    """主管 LLM 输出的子任务列表"""
    reasoning: str = ""
    subtasks: list[dict]  # [{"title": "...", "description": "...", "skill": "sql_query", "agent": "data-agent"}]


# ==================== 能力注册表 ====================

# 生产环境：从各 Agent 的 /.well-known/agent.json 动态获取
# 开发环境：静态注册表
AGENT_REGISTRY = {
    "data-agent": {
        "name": "数据分析 Agent",
        "url": "http://data-agent:8001",
        "skills": ["sql_query", "data_analysis"],
    },
    "doc-agent": {
        "name": "文档 Agent",
        "url": "http://doc-agent:8002",
        "skills": ["search_kb", "generate_report"],
    },
    "general-agent": {
        "name": "通用 Agent",
        "url": "http://general-agent:8003",
        "skills": ["chat", "reasoning"],
    },
}


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


async def node_dispatch_and_wait(state: OrchestratorState) -> dict:
    """
    节点②：分发任务给各子 Agent 并等待结果。
    
    互不依赖的子任务并行执行，有依赖的串行执行。
    """
    subtasks = state["subtasks"]
    results = []

    # 简化实现：所有子任务并行分发
    async with httpx.AsyncClient(timeout=120.0) as client:
        async def dispatch_one(st: dict) -> dict:
            agent_info = AGENT_REGISTRY.get(st["agent"])
            if not agent_info:
                return {**st, "status": "failed", "error": f"未知 Agent: {st['agent']}"}

            try:
                resp = await client.post(
                    f"{agent_info['url']}/tasks",
                    json={
                        "id": st["id"],
                        "session_id": state["session_id"],
                        "title": st["title"],
                        "description": st["description"],
                        "context": {"user_query": state["user_query"]},
                        "skill_id": st.get("skill", ""),
                    },
                )
                resp.raise_for_status()
                task_info = resp.json()

                # 轮询等待任务完成
                for _ in range(60):  # 最多等 60 秒
                    await asyncio.sleep(1)
                    status_resp = await client.get(
                        f"{agent_info['url']}/tasks/{st['id']}"
                    )
                    status_info = status_resp.json()
                    
                    if status_info["status"] in ("completed", "failed", "cancelled"):
                        return {
                            "task_id": st["id"],
                            "title": st["title"],
                            "agent": st["agent"],
                            "status": status_info["status"],
                            "result": status_info.get("artifact"),
                            "error": status_info.get("error"),
                        }

                return {**st, "status": "failed", "error": "任务超时"}

            except Exception as e:
                return {**st, "status": "failed", "error": str(e)}

        # 并行分发所有子任务
        results = await asyncio.gather(*[dispatch_one(st) for st in subtasks])

    return {"task_results": results}


async def node_assemble(state: OrchestratorState) -> dict:
    """节点③：汇总所有子任务结果，生成最终回复"""
    results = state["task_results"]
    
    # 分离成功和失败的结果
    success_results = [r for r in results if r["status"] == "completed"]
    failed_results = [r for r in results if r["status"] != "completed"]

    # 提取所有成功结果的文本
    result_texts = []
    for r in success_results:
        if r.get("result") and r["result"].get("parts"):
            for part in r["result"]["parts"]:
                if part.get("type") == "text":
                    result_texts.append(f"## {r['title']}\n{part['text']}")

    combined = "\n\n".join(result_texts)

    # 让主管 LLM 汇总
    prompt = f"""根据以下子任务的结果，回答用户的原始问题。

## 用户问题
{state['user_query']}

## 子任务结果
{combined}

{f"## 注意：以下子任务失败了，不要编造相关数据：{', '.join(r['title'] for r in failed_results)}" if failed_results else ""}

请生成一个完整、专业的回复。如果部分结果缺失，诚实说明。"""

    response = await llm.ainvoke([{"role": "user", "content": prompt}])

    return {"final_response": response.content}


# ==================== 构建主管 Graph ====================

def build_orchestrator():
    graph = StateGraph(OrchestratorState)

    graph.add_node("decompose", node_decompose)
    graph.add_node("dispatch", node_dispatch_and_wait)
    graph.add_node("assemble", node_assemble)

    graph.set_entry_point("decompose")
    graph.add_edge("decompose", "dispatch")
    graph.add_edge("dispatch", "assemble")
    graph.add_edge("assemble", END)

    return graph.compile()


orchestrator = build_orchestrator()
```

### 2.4.4 流式推送：A2A 的 Task Status Update 事件

A2A 支持 SSE 流式推送任务状态更新。子 Agent 在执行过程中可以持续推送中间结果：

```python
# agents/data_agent/main.py —— 流式任务状态端点

@app.get("/tasks/{task_id}/stream")
async def stream_task(task_id: str):
    """A2A 任务状态流——SSE 端点"""
    async def event_stream():
        task = tasks.get(task_id)
        if not task:
            yield f"data: {{\"error\": \"task not found\"}}\n\n"
            return

        last_status = None
        
        while task["status"] not in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            if task["status"] != last_status:
                last_status = task["status"]
                yield f"data: {{\"type\": \"status_update\", \"task_id\": \"{task_id}\", \"status\": \"{last_status}\"}}\n\n"
            
            # 如果有中间产出，推送
            if task.get("intermediate_artifact"):
                yield f"data: {{\"type\": \"artifact_update\", \"task_id\": \"{task_id}\", \"artifact\": {json.dumps(task['intermediate_artifact'])}}}\n\n"
                del task["intermediate_artifact"]
            
            await asyncio.sleep(0.5)

        # 最终状态
        yield f"data: {{\"type\": \"final\", \"task_id\": \"{task_id}\", \"status\": \"{task['status']}\"}}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

主管 Agent 可以用流式端点实时获取子任务进度，并向用户推送进度信息：

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

```python
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
async def discover_agents():
    """启动时自动扫描所有 Agent 的 Agent Card"""
    agent_urls = [
        "http://data-agent:8001",
        "http://doc-agent:8002",
        "http://general-agent:8003",
    ]
    
    registry = {}
    async with httpx.AsyncClient(timeout=5.0) as client:
        for url in agent_urls:
            try:
                resp = await client.get(f"{url}/.well-known/agent.json")
                card = resp.json()
                registry[card["name"]] = {
                    "url": card["url"],
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
async def dispatch_with_timeout(st: dict, timeout: float = 60.0) -> dict:
    """带超时的任务分发，超时后返回降级结果"""
    try:
        result = await asyncio.wait_for(
            dispatch_one(st),
            timeout=timeout,
        )
        return result
    except asyncio.TimeoutError:
        logger.warning("子任务超时 | task=%s | agent=%s", st["id"], st["agent"])
        return {
            "task_id": st["id"],
            "title": st["title"],
            "agent": st["agent"],
            "status": "failed",
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
        
        # 并行执行当前批次
        batch_results = await asyncio.gather(*[
            dispatch_one(st, context={
                **st.dict(),
                "dep_results": {dep_id: completed[dep_id] for dep_id in st.depends_on},
            })
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
    "session_id": "abc123",                         ← 会话标识
    "user_profile": {                               ← 用户画像（长期记忆）
        "name": "张三", "role": "产品经理", "preference": "数据驱动"
    },
    "shared_knowledge": {                           ← 前置知识（RAG 结果）
        "competitor_list": ["竞品A", "竞品B", "竞品C"],
        "time_range": "2025 Q2",
    },
    "dep_results": {                                ← 前置子任务结果
        "subtask-1": {"status": "completed", "result": {...}},
    },
}
```

**原则：给子 Agent 的 context 要精不要多。** 把整个对话历史 5000 token 全丢给子 Agent，和主管 Agent 全自己干没有区别。只传递子 Agent 完成任务必需的上下文。

---

## 2.6 完整示例：多 Agent 协作写一份竞品分析报告

```python
# main.py —— 完整的多 Agent 入口
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from agents.orchestrator import orchestrator

app = FastAPI()


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
        state = await orchestrator.ainvoke(state, config={"configurable": {"thread_id": request.session_id}})
        
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
| A2A Agent Card | `/.well-known/agent.json` 暴露能力描述 | 每个 Agent 自描述"我能干什么、输入输出格式是什么" |
| A2A Task 生命周期 | submitted → working → completed/failed/cancelled | 一切工作以 Task 为单位，有 ID、有状态、可追踪、可取消 |
| 任务分发与汇总 | 主管 LLM 拆分 → 子 Agent 并行执行 → 主管汇总 | 互不依赖的并行（`asyncio.gather`），有依赖的串行 |
| 超时与降级 | `asyncio.wait_for` 超时兜底 + 降级结果标注 | 子 Agent 挂了不能拖垮主管，诚实告知用户"部分结果缺失" |
| 动态发现 vs 静态注册 | 本项目用静态注册表，生产环境可升级为动态发现 | 3~5 个 Agent 不需要动态发现，配置文件足够 |
| 多 Agent 适用条件 | 任务可并行拆分、各 Agent 专业领域清晰、工具集不重叠 | 单 Agent 能解决的不用多 Agent，只在 prompt/工具膨胀时拆分 |

**多 Agent 的本质是"分而治之"——把一个大而全的 Agent 拆成多个小而专的 Agent。但拆分有成本（通信延迟、上下文传递、异常协调），所以只在单 Agent 撞到天花板时才拆。A2A 协议让这个拆分有章可循。**

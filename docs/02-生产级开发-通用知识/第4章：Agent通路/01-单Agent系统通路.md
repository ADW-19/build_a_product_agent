# 第一章：单 Agent 系统通路

> **核心论点**：一个生产级 Agent 不是"接到 query → 调 LLM → 回复"这么简单。它需要在一个请求周期内自主完成意图识别、记忆检索、知识库查询、工具调用、条件路由、结果校验等一系列决策。而当任务复杂到单步无法完成时，还需要"先计划再执行"以及"关键时刻让人拍板"。本章拆解单 Agent 从接收 query 到输出回复的完整决策链路，以及 human-in-the-loop 和 plan-and-execute 两种关键机制。

---

## 1.1 单 Agent 的"大脑"长什么样

### 1.1.1 一张图看懂全链路

用户发来一句话，Agent 需要依次回答四个问题，才能决定"下一步做什么"：

```
用户 query："帮我查一下上周那笔退款到账了没"
         │
         ▼
┌─────────────────────────────────────────────────────┐
│  问题一：用户想干什么？                                │
│  ┌──────────┐                                       │
│  │ 意图识别  │ → action（需要调工具）                  │
│  └──────────┘                                       │
├─────────────────────────────────────────────────────┤
│  问题二：关于这个用户，我知道什么？                      │
│  ┌──────────┐  ┌────────────┐                       │
│  │ 短期记忆  │  │ 长期记忆    │                       │
│  │ 刚才聊了啥 │  │ 用户是谁    │                       │
│  │ (Redis)  │  │ (Milvus)   │                       │
│  └──────────┘  └────────────┘                       │
├─────────────────────────────────────────────────────┤
│  问题三：需要什么外部知识？                             │
│  ┌──────────┐                                       │
│  │ RAG 检索  │ → 查文档、政策、FAQ                    │
│  │ (Milvus)  │                                       │
│  └──────────┘                                       │
├─────────────────────────────────────────────────────┤
│  问题四：需要执行什么操作？                             │
│  ┌──────────┐                                       │
│  │ 工具调用  │ → 查订单、发邮件、改数据库              │
│  │ (Tools)   │                                       │
│  └──────────┘                                       │
└─────────────────────────────────────────────────────┘
         │
         ▼
     最终回复
```

这四个问题不是串行问完再行动，而是在一个 **ReAct 循环** 中交替进行的：推理 → 行动 → 观察 → 推理 → 行动 → ... → 最终回复。

### 1.1.2 用 LangGraph 实现完整决策图

下面是一个真实的单 Agent 决策图，节点包括：意图路由、记忆加载、RAG 检索、工具执行、LLM 推理、回复生成。

```python
# core/single_agent.py
from typing import TypedDict, Annotated, Literal
from operator import add
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode
from langgraph.checkpoint.redis import RedisSaver
from langchain_openai import ChatOpenAI
from langchain_core.messages import BaseMessage

from core.intent_classifier import classify_intent, Intent
from core.session import get_history, append_message_with_limit
from core.long_memory_store import search_similar_memories
from core.rag_service import hybrid_search, rerank
from core.tool_guard import tool_guard
from tools import ALL_TOOLS, TOOLS_MAP

# ==================== State 定义 ====================

class AgentState(TypedDict):
    # 输入
    user_query: str
    session_id: str
    user_id: str

    # 对话消息（Annotated + add 实现追加）
    messages: Annotated[list[BaseMessage], add]

    # 各子系统注入的上下文
    intent: str                          # 意图分类结果
    short_term_context: list[dict]       # 短期记忆（最近对话）
    long_term_context: list[dict]        # 长期记忆（用户画像）
    rag_context: list[dict]              # RAG 检索结果

    # 工具调用控制
    available_tools: list[str]           # 当前节点可用的工具名列表

    # 最终输出
    final_response: str


# ==================== 节点实现 ====================

llm = ChatOpenAI(model="gpt-4o")


async def node_classify_intent(state: AgentState) -> dict:
    """
    节点①：意图分类
    
    判断用户想干什么，决定后续哪些子系统需要参与。
    """
    intent_result = await classify_intent(state["user_query"])

    # 根据意图决定需要启用哪些能力
    need_tools = (intent_result.intent == Intent.ACTION)
    need_rag = (intent_result.intent == Intent.QUESTION)
    # 长期记忆总是检索（成本很低，≈50ms）

    return {
        "intent": intent_result.intent,
        "available_tools": [t.name for t in ALL_TOOLS] if need_tools else [],
        "_need_rag": need_rag,
    }


async def node_load_memories(state: AgentState) -> dict:
    """
    节点②：加载记忆（短 + 长）
    
    短期记忆从 Redis 读会话历史。
    长期记忆从 Milvus 检索用户画像。
    两者并行，互不依赖。
    """
    import asyncio

    # 并行加载短期和长期记忆
    short_term, long_term = await asyncio.gather(
        get_history(state["session_id"]),
        search_similar_memories(
            user_id=state["user_id"],
            query_text=state["user_query"],
            top_k=5,
        ),
    )

    return {
        "short_term_context": short_term,
        "long_term_context": long_term,
    }


async def node_search_rag(state: AgentState) -> dict:
    """
    节点③：RAG 检索（仅在意图为 QUESTION 时执行）
    
    从 Milvus 知识库检索相关文档。
    """
    need_rag = state.get("_need_rag", False)
    if not need_rag:
        return {"rag_context": []}

    docs = await hybrid_search(state["user_query"], top_k=10)
    docs = await rerank(state["user_query"], docs, final_top_k=5)

    return {"rag_context": docs}


async def node_assemble_context(state: AgentState) -> dict:
    """
    节点④：上下文组装
    
    将系统提示、记忆、RAG 结果拼接为 messages，
    作为 LLM 推理节点的输入。
    """
    messages = []

    # 1. 系统提示词
    system_parts = ["你是用户的智能助手，回答准确、简洁。\n"]
    
    # 2. 长期记忆（用户画像）
    if state["long_term_context"]:
        facts = "\n".join(f"- {m['content']}" for m in state["long_term_context"])
        system_parts.append(f"## 关于此用户的已知信息\n{facts}\n")
    
    # 3. RAG 结果
    if state.get("rag_context"):
        docs = "\n\n".join(
            f"[{d['title']}]\n{d['content']}" for d in state["rag_context"]
        )
        system_parts.append(f"## 参考资料\n{docs}\n")
    
    # 4. 可用工具
    if state["available_tools"]:
        tools_hint = "你可以调用以下工具来完成任务：" + ", ".join(state["available_tools"])
        system_parts.append(tools_hint)

    messages.append({"role": "system", "content": "\n".join(system_parts)})

    # 5. 短期记忆（会话历史）
    for msg in state["short_term_context"]:
        messages.append(msg)

    # 6. 当前用户消息
    messages.append({"role": "user", "content": state["user_query"]})

    return {"messages": messages}


async def node_llm_reason(state: AgentState) -> dict:
    """
    节点⑤：LLM 推理
    
    核心推理节点。LLM 根据完整的上下文决定：
    - 回复文本（不需要工具时）
    - 调用工具（需要操作时）
    
    如果绑定了工具，LLM 可能返回 tool_calls。
    """
    if state["available_tools"]:
        llm_with_tools = llm.bind_tools(
            [t for t in ALL_TOOLS if t.name in state["available_tools"]]
        )
        response = await llm_with_tools.ainvoke(state["messages"])
    else:
        response = await llm.ainvoke(state["messages"])

    return {"messages": [response]}


async def node_generate_final(state: AgentState) -> dict:
    """
    节点⑥：生成最终回复
    
    当 LLM 不再需要调工具时，生成最终的用户回复。
    """
    last_message = state["messages"][-1]
    return {"final_response": last_message.content}


# ==================== 路由函数（纯逻辑） ====================

def route_after_intent(state: AgentState) -> str:
    """意图分类后：总是先加载记忆"""
    return "load_memories"


def route_after_memories(state: AgentState) -> str:
    """记忆加载后：检查是否需要 RAG"""
    if state.get("_need_rag"):
        return "search_rag"
    return "assemble_context"


def route_after_rag(state: AgentState) -> str:
    """RAG 检索后：进入上下文组装"""
    return "assemble_context"


def route_after_llm(state: AgentState) -> str:
    """
    LLM 推理后的路由——这是整个 Agent 最关键的判断。
    
    如果 LLM 返回了 tool_calls → 去执行工具
    如果 LLM 返回了纯文本 → 这就是最终回复
    """
    last_message = state["messages"][-1]
    
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "tools"
    return "generate_final"


def route_after_tools(state: AgentState) -> str:
    """工具执行后：回到 LLM 继续推理（ReAct 循环）"""
    return "llm_reason"


# ==================== 构建 Graph ====================

def build_single_agent(checkpointer=None):
    graph = StateGraph(AgentState)

    # 注册节点
    graph.add_node("classify_intent", node_classify_intent)
    graph.add_node("load_memories", node_load_memories)
    graph.add_node("search_rag", node_search_rag)
    graph.add_node("assemble_context", node_assemble_context)
    graph.add_node("llm_reason", node_llm_reason)
    graph.add_node("tools", ToolNode(ALL_TOOLS))
    graph.add_node("generate_final", node_generate_final)

    # 连线
    graph.set_entry_point("classify_intent")
    graph.add_edge("classify_intent", "load_memories")
    graph.add_conditional_edges("load_memories", route_after_memories, {
        "search_rag": "search_rag",
        "assemble_context": "assemble_context",
    })
    graph.add_edge("search_rag", "assemble_context")
    graph.add_edge("assemble_context", "llm_reason")
    graph.add_conditional_edges("llm_reason", route_after_llm, {
        "tools": "tools",
        "generate_final": "generate_final",
    })
    graph.add_edge("tools", "llm_reason")       # ← ReAct 循环
    graph.add_edge("generate_final", END)

    return graph.compile(checkpointer=checkpointer)


# 全局编译实例
agent = build_single_agent(
    checkpointer=RedisSaver.from_conn_string("redis://localhost:6379/2")
)
```

### 1.1.3 上述代码的核心设计决策

**决策一：意图分类先于一切。** 不先搞清楚用户要干什么，后面全是盲目操作。把意图写入 State，后续每个节点都能据此决定自己要不要执行、怎么执行。

**决策二：记忆加载和 RAG 检索解耦为独立节点。** 记忆是"关于这个用户"的信息，RAG 是"关于这个问题"的知识——两者的检索条件和用途完全不同。解耦后可以按意图决定是否执行 RAG（闲聊不需要查知识库，省一次 Milvus 查询）。

**决策三：上下文组装是单独一个节点。** 把"拼 prompt"这件事集中在一个地方，方便调试——如果 Agent 回复质量差，先看这个节点拼出来的 messages 长什么样。

**决策四：LLM 推理 → 工具执行 → LLM 推理，形成 ReAct 循环。** 这是 Agent 的核心：不是一次 LLM 调用就完事，而是"思考-行动-观察-再思考"的迭代过程。

---

## 1.2 单 Agent 的执行时序：一个完整请求的 12 步

```
时间线（用户："帮我查一下上周那笔退款到账了没"）

 0ms  │  用户请求到达
      │
 50ms │  ① classify_intent    → intent=action, need_tools=true, need_rag=false
      │                          （"查退款"是操作，不是提问，不需要 RAG）
      │
 100ms│  ② load_memories      → 短期：刚才聊到蓝牙耳机退款
      │                          → 长期：用户是 VIP，偏好简洁回复
      │                          → 长期：用户最近有一笔订单 ORD-88483
      │
 150ms│  ③ route: need_rag=false → 跳过 search_rag
      │
 160ms│  ④ assemble_context    → 组装 system prompt + 长期记忆 + 短期记忆 + query
      │                          system prompt 包含：可用工具 = [order_tool]
      │
 200ms│  ⑤ llm_reason (第1次)  → LLM 决定调 order_tool(action="search", order_id="ORD-88483")
      │
 250ms│  ⑥ tools               → 执行 order_tool → 返回"订单 ORD-88483：退款 299 元，状态：处理中，
      │                          预计 3 个工作日内到账"
      │
 300ms│  ⑦ llm_reason (第2次)  → LLM 看到工具结果，决定不再调工具，生成回复
      │                          "您的退款 299 元正在处理中，预计 3 个工作日内原路退回。"
      │
 320ms│  ⑧ generate_final      → 提取最终回复
      │
 350ms│  → 返回给用户
```

**总耗时约 350ms（不含 LLM 推理时间）。** 其中 LLM 两次推理（⑤和⑦）占了大头，实际耗时取决于模型速度。其他节点（①~④、⑥、⑧）都在 50ms 以内。

---

## 1.3 为什么需要 Human-in-the-Loop

### 1.3.1 一个真实的事故场景

```
用户："帮我给全公司发一封邮件，通知明天放假。"

Agent 推理 → 调用 send_email(
    to="all@company.com",
    subject="明天放假",
    body="经管理层决定，明天全体员工放假一天..."
)

邮件发出去了。但这不是管理层的决定——是用户随口说的。
```

有些事情，Agent 不应该自己做主。发全员邮件、退款、删除数据、修改权限、签署合同——这些操作一旦执行就不可逆，必须经过人确认。

### 1.3.2 LangGraph 的 interrupt 机制

LangGraph 提供了 `interrupt_before` 参数，在指定节点执行前暂停整个工作流，等待外部输入：

```python
# 构建时指定哪些节点需要人工确认
agent = build_single_agent(
    checkpointer=RedisSaver.from_conn_string("redis://localhost:6379/2"),
)

# ============ 运行时 ============

# 1. 正常执行，但在 tools 节点前暂停（如果上一个节点是 llm_reason）
#    使用 interrupt_before 参数
config = {"configurable": {"thread_id": session_id}}

# 第一次调用：执行到 tools 节点前自动暂停
result = await agent.ainvoke(
    {"user_query": "帮我给全公司发邮件通知明天放假", ...},
    config=config,
)

# → LangGraph 抛出 GraphInterrupt 异常（或返回特殊状态）
# → 工作流暂停，State 保存到 checkpointer

# 2. 此时可以从 State 中查看 Agent 打算做什么
current_state = await agent.aget_state(config)
pending_tool_calls = current_state.values["messages"][-1].tool_calls
print(f"Agent 想要执行：{pending_tool_calls}")
# → [{name: "send_email", args: {to: "all@company.com", ...}}]

# 3. 人工审核：展示给用户确认
# 前端弹窗："Agent 想要发送邮件给全公司，确认吗？"

# 4a. 用户点"确认" → 恢复执行
await agent.aupdate_state(
    config,
    {"human_approved": True},   # 注入审批结果
)
result = await agent.ainvoke(None, config=config)  # 从断点继续

# 4b. 用户点"拒绝" → 修改 State，走向另一个分支
await agent.aupdate_state(
    config,
    {
        "human_approved": False,
        "messages": [{"role": "system", "content": "用户拒绝了发送邮件的操作，请告知用户操作已取消。"}],
    },
)
result = await agent.ainvoke(None, config=config)
```

### 1.3.3 更精细的做法：按工具的危险等级控制

不是所有工具都需要人工确认。查订单不需要，发邮件需要。可以按危险等级分类：

```python
from enum import Enum


class ToolRiskLevel(str, Enum):
    READ = "read"          # 只读操作：查订单、查库存、搜索 → 不需要确认
    WRITE = "write"        # 写入操作：创建订单、更新信息 → 轻度确认
    DANGEROUS = "dangerous" # 危险操作：发邮件、退款、删除 → 必须确认


TOOL_RISK_MAP = {
    "order_tool": ToolRiskLevel.READ,        # 查订单，只读
    "inventory_tool": ToolRiskLevel.READ,    # 查库存，只读
    "send_email": ToolRiskLevel.DANGEROUS,   # 发邮件，危险
    "refund": ToolRiskLevel.DANGEROUS,       # 退款，危险
    "update_user_info": ToolRiskLevel.WRITE, # 改信息，轻度
}


def route_tool_approval(state: AgentState) -> str:
    """
    工具执行前的审批路由。
    
    READ  → 直接执行
    WRITE → 检查用户是否已授权（首次 WRITE 时需确认，后续同会话可放行）
    DANGEROUS → 每次都必须确认
    """
    last_message = state["messages"][-1]
    tool_calls = last_message.tool_calls

    for tc in tool_calls:
        risk = TOOL_RISK_MAP.get(tc["name"], ToolRiskLevel.DANGEROUS)
        
        if risk == ToolRiskLevel.DANGEROUS:
            return "human_approval"       # 暂停，等人确认
        elif risk == ToolRiskLevel.WRITE:
            if not state.get("write_authorized"):
                return "human_approval"
    
    return "tools"  # 直接执行
```

### 1.3.4 完整示例：带审批的 Agent Graph

```python
def build_agent_with_approval():
    graph = StateGraph(AgentState)

    # 原有节点...
    graph.add_node("classify_intent", node_classify_intent)
    graph.add_node("load_memories", node_load_memories)
    graph.add_node("assemble_context", node_assemble_context)
    graph.add_node("llm_reason", node_llm_reason)
    graph.add_node("tools", ToolNode(ALL_TOOLS))
    graph.add_node("generate_final", node_generate_final)

    # 新增：人工审批节点
    async def node_human_approval(state: AgentState) -> dict:
        """人工审批节点。此节点不会真正被'执行'，而是通过 interrupt 暂停。"""
        # 这个函数体不会运行（因为 graph 在这之前就 interrupt 了）
        # 但还是返回空更新满足类型要求
        return {}

    graph.add_node("human_approval", node_human_approval)

    # 关键：llm_reason 后，根据工具危险等级路由
    graph.add_conditional_edges(
        "llm_reason",
        route_tool_approval,
        {
            "tools": "tools",
            "human_approval": "human_approval",
            "generate_final": "generate_final",
        },
    )
    graph.add_edge("tools", "llm_reason")
    graph.add_edge("human_approval", "tools")  # 审批通过后 → 执行工具
    graph.add_edge("generate_final", END)

    return graph.compile(
        checkpointer=RedisSaver.from_conn_string("redis://localhost:6379/2"),
        interrupt_before=["human_approval"],  # ← 在这个节点前暂停
    )
```

---

## 1.4 为什么需要 Plan-and-Execute

### 1.4.1 ReAct 循环的局限性

ReAct 模式（思考-行动-观察）对简单任务足够，但对复杂多步任务有两个致命问题：

**问题一：缺乏全局视角。** ReAct 是"走一步看一步"。对于"帮我写一份竞品分析报告"这种任务，LLM 第一步可能就跑去搜索竞品 A 的价格，然后发现不够、再搜竞品 B、再搜市场份额...每一步都是临时反应，没有提前想清楚"这份报告需要哪些章节、每章需要什么数据"。

**问题二：容易跑偏。** 没有计划约束，LLM 可能在第三步就偏题了——本来要写竞品分析，结果跑去深入调研某个竞品的技术架构细节，再也回不来。

```
ReAct 模式（无计划）：
  步骤1: 搜索"竞品A" → 发现很多信息
  步骤2: 深入搜索"竞品A技术架构" → 发现更多细节
  步骤3: 搜索"竞品A技术架构之分布式存储" → 完全偏了
  步骤4: ... 永远回不到"写报告"这个目标上

Plan-and-Execute 模式（先计划）：
  计划: ① 确定竞品名单 → ② 收集每个竞品的价格、功能、市场份额 
        → ③ 汇总对比 → ④ 生成报告
  
  执行① → 完成 ✓
  执行② → 完成 ✓（按计划限定了收集范围，不会深入技术细节）
  执行③ → 发现数据不足 → 回②补充 → 完成 ✓
  执行④ → 完成 ✓ → 输出报告
```

### 1.4.2 Plan-and-Execute 的核心流程

```
用户 query："帮我写一份竞品分析报告"
         │
         ▼
┌─────────────────────────────────────────────┐
│ 阶段一：Plan（生成计划）                      │
│                                             │
│  LLM 分析任务 → 拆解为子任务序列              │
│  [                                            │
│    {step:1, desc:"确定竞品名单",              │
│     tool:"search_competitors", depends:[]},    │
│    {step:2, desc:"收集竞品价格信息",           │
│     tool:"search_pricing", depends:[1]},       │
│    {step:3, desc:"汇总对比数据",              │
│     tool:"analyze", depends:[2]},             │
│    {step:4, desc:"生成最终报告",               │
│     tool:"generate_report", depends:[3]},     │
│  ]                                            │
└──────────────────────┬──────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────┐
│ 阶段二：Review（计划评审）—— 可选，可 HITL    │
│                                             │
│  将计划展示给用户确认："我打算按以下步骤：    │
│  1. 确定竞品名单 2. 收集价格 3. 对比分析     │
│  4. 生成报告。可以吗？"                      │
│                                             │
│  用户确认 / 修改 / 补充                       │
└──────────────────────┬──────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────┐
│ 阶段三：Execute（执行计划）                   │
│                                             │
│  按顺序执行每个子任务：                        │
│  ┌─────────┐    ┌─────────┐    ┌─────────┐  │
│  │ Step 1  │───→│ Step 2  │───→│ Step 3  │  │
│  │ 执行+验证 │    │ 执行+验证 │    │ 执行+验证 │  │
│  └─────────┘    └─────────┘    └─────────┘  │
│       │              │              │       │
│       └──────────────┴──────────────┘       │
│                      │                      │
│              执行失败？→ 重新规划该步骤       │
└──────────────────────┬──────────────────────┘
                       │
                       ▼
                    最终输出
```

### 1.4.3 LangGraph 实现 Plan-and-Execute

```python
# core/plan_and_execute_agent.py
from typing import TypedDict, Annotated
from operator import add
from pydantic import BaseModel, Field
from langgraph.graph import StateGraph, END


# ==================== 计划的数据结构 ====================

class PlanStep(BaseModel):
    step_id: int
    description: str
    tool_name: str = Field(default="")  # 此步骤需要调用的工具
    depends_on: list[int] = Field(default_factory=list)  # 依赖的前置步骤
    status: str = "pending"  # pending / in_progress / completed / failed


class Plan(BaseModel):
    steps: list[PlanStep]
    goal: str = Field(description="任务的最终目标，一句话描述")


# ==================== State 定义 ====================

class PlanAndExecuteState(TypedDict):
    user_query: str
    plan: Plan
    completed_steps: Annotated[list[dict], add]  # 已完成步骤的结果
    current_step: int                             # 当前正在执行的步骤 ID
    final_output: str
    messages: Annotated[list, add]


# ==================== 阶段一：生成计划 ====================

planning_llm = ChatOpenAI(model="gpt-4o").with_structured_output(Plan)

PLANNING_PROMPT = """你是一个任务规划助手。将用户的复杂任务拆解为 3~7 个具体可执行的步骤。

## 规则：
1. 每个步骤应该是原子的、可独立完成的
2. 步骤之间如果有依赖关系，在 depends_on 中标注
3. 每个步骤标注需要使用的工具名（查资料用 search_kb，分析数据用 analyze，生成文本用 generate_text）
4. 步骤描述要具体，包含"查什么""怎么分析""输出什么格式"

## 示例：
用户：帮我写一份蓝牙耳机市场分析报告
计划：
步骤1：搜索蓝牙耳机市场规模和增长趋势 → 工具 search_kb
步骤2：搜索蓝牙耳机主流品牌和市场份额 → 工具 search_kb
步骤3：对比各品牌的价格和功能 → 工具 analyze
步骤4：生成分析报告 → 工具 generate_text

用户：{query}"""


async def node_plan(state: PlanAndExecuteState) -> dict:
    """生成执行计划"""
    plan = await planning_llm.ainvoke(
        PLANNING_PROMPT.format(query=state["user_query"])
    )
    return {"plan": plan}


# ==================== 阶段二（可选）：计划评审 ====================

async def node_review_plan(state: PlanAndExecuteState) -> dict:
    """
    计划评审——让用户确认计划。
    
    此节点在 interrupt_before 列表里，执行前会暂停，
    前端展示计划后等用户确认。
    """
    return {}  # 空函数体，因为执行前已通过 interrupt 暂停


# ==================== 阶段三：逐步执行 ====================

async def node_execute_step(state: PlanAndExecuteState) -> dict:
    """执行当前步骤"""
    plan = state["plan"]
    current = state.get("current_step", 1)

    # 找到当前步骤
    step = next((s for s in plan.steps if s.step_id == current), None)
    if step is None:
        return {"final_output": "所有步骤已完成"}

    # 收集前置步骤的结果
    deps_context = ""
    if step.depends_on:
        for dep_id in step.depends_on:
            for completed in state.get("completed_steps", []):
                if completed["step_id"] == dep_id:
                    deps_context += f"\n步骤{dep_id}的结果：{completed['result'][:500]}"

    # 执行此步骤
    execute_prompt = f"""执行以下任务步骤：

## 任务目标
{plan.goal}

## 当前步骤（第 {current} 步 / 共 {len(plan.steps)} 步）
{step.description}

## 前置步骤的结果
{deps_context if deps_context else "（无依赖）"}

## 用户原始需求
{state['user_query']}

请完成此步骤，输出结果。如果此步骤需要搜索或查询，请使用工具。"""

    messages = [{"role": "user", "content": execute_prompt}]

    # 绑定工具
    execute_llm = llm.bind_tools([t for t in ALL_TOOLS if t.name == step.tool_name or step.tool_name == ""])

    response = await execute_llm.ainvoke(messages)

    return {
        "messages": [response],
        "completed_steps": [{
            "step_id": current,
            "description": step.description,
            "result": response.content,
            "status": "completed",
        }],
    }


def route_after_execute(state: PlanAndExecuteState) -> str:
    """执行完一个步骤后，判断下一步"""
    total_steps = len(state["plan"].steps)
    current = state.get("current_step", 1)

    # 检查当前步骤是否真的完成了
    last_completed = state["completed_steps"][-1] if state["completed_steps"] else None
    if last_completed and last_completed["status"] == "completed":
        if current >= total_steps:
            return "assemble_final"
        return "execute_step"  # 继续下一步
    else:
        return "execute_step"  # 重试当前步骤


async def node_assemble_final(state: PlanAndExecuteState) -> dict:
    """汇总所有步骤的结果，生成最终输出"""
    results_text = "\n\n".join(
        f"## 步骤 {s['step_id']}：{s['description']}\n{s['result'][:1000]}"
        for s in state.get("completed_steps", [])
    )

    final_prompt = f"""根据以下各步骤的执行结果，生成最终输出。

## 原始需求
{state['user_query']}

## 执行结果
{results_text}

请生成一份完整的、格式化良好的最终回复。"""

    response = await llm.ainvoke([{"role": "user", "content": final_prompt}])

    return {"final_output": response.content, "messages": [response]}


# ==================== 构建 Plan-and-Execute Graph ====================

def build_plan_and_execute_agent():
    graph = StateGraph(PlanAndExecuteState)

    graph.add_node("plan", node_plan)
    graph.add_node("review_plan", node_review_plan)
    graph.add_node("execute_step", node_execute_step)
    graph.add_node("assemble_final", node_assemble_final)

    graph.set_entry_point("plan")

    # plan → review（可选人工确认）→ execute
    graph.add_edge("plan", "review_plan")
    graph.add_edge("review_plan", "execute_step")

    # 循环执行直到所有步骤完成
    graph.add_conditional_edges("execute_step", route_after_execute, {
        "execute_step": "execute_step",
        "assemble_final": "assemble_final",
    })
    graph.add_edge("assemble_final", END)

    return graph.compile(
        checkpointer=RedisSaver.from_conn_string("redis://localhost:6379/3"),
        interrupt_before=["review_plan"],  # ← 计划确认点
    )
```

### 1.4.4 Plan-and-Execute vs ReAct：什么时候用哪个

| 场景 | 推荐模式 | 原因 |
|------|---------|------|
| 查订单、查天气、简单问答 | ReAct | 单步或两步完成，不需要计划开销 |
| 写报告、做分析、多步调研 | Plan-and-Execute | 步骤多且有依赖，需要全局视角 |
| 用户需求明确（"帮我做X"） | Plan-and-Execute | 目标清晰，拆分即可 |
| 用户需求模糊（"帮我看看这个"） | ReAct | 先探索，再逐步明确 |
| 需要人工确认关键步骤 | Plan-and-Execute + HITL | 在 review 阶段确认，在危险步骤再确认 |

**实际项目中两者共存：** 意图分类后，简单任务走 ReAct 快捷路径，复杂任务走 Plan-and-Execute 完整路径。

---

## 1.5 把三种机制整合进一个 Agent

```python
def route_after_classify(state: AgentState) -> str:
    """
    意图分类后，根据任务复杂度选择路径：
    - 简单任务 → ReAct 循环
    - 复杂任务 → Plan-and-Execute
    """
    # 复杂任务的关键词特征（实际项目中可以训练分类器）
    complex_keywords = ["报告", "分析", "调研", "总结", "对比", "方案", "计划"]
    query = state["user_query"]
    
    is_complex = any(kw in query for kw in complex_keywords)
    
    if is_complex:
        return "plan_mode"
    return "react_mode"


# 最终 Graph 结构：
#
#               ┌──→ [plan] → [review] → [execute_step] ↔ [assemble_final] → END
#               │    (Plan-and-Execute 路径)
# [classify] ──┤
#               │    (ReAct 路径)
#               └──→ [memories] → [rag?] → [llm] ↔ [tools] → [generate] → END
#                                                          ↑
#                                                    [human_approval]
#                                                   (危险工具前暂停)
```

---

## 1.6 Human-in-the-Loop 与 Plan-and-Execute 的关系

两者不是互斥的，而是作用于不同阶段：

```
Plan-and-Execute 的时间线：
      
  [Plan] ─────→ [Review] ─────→ [Execute Step 1] ─→ [Execute Step 2] ─→ ...
                   │                    │
                   │                    └── 如果步骤包含危险操作
                   │                        → HITL 暂停，等人确认
                   │
                   └── HITL：用户确认计划
                        "这个计划可以吗？"
                        "第三步需要补充XX信息"
```

- **Plan-and-Execute** 解决的是"任务太复杂，需要拆分"的问题
- **Human-in-the-Loop** 解决的是"某些操作太危险，需要人确认"的问题
- 两者叠加：计划阶段让人确认方向，执行阶段让人确认危险操作

---

## 1.7 单 Agent 的局限性——引出多 Agent

单 Agent 在以下场景会遇到瓶颈：

| 瓶颈 | 表现 | 根本原因 |
|------|------|---------|
| 上下文太长 | 同时加载 5 个工具 + 10 条记忆 + 5 个 RAG 结果 + 对话历史 = prompt 爆炸 | 一个 LLM 要操心所有事 |
| 能力冲突 | 同一个 system prompt 既要"严谨分析数据"又要"亲切回复用户" | 不同任务需要不同的人格/策略 |
| 工具污染 | 20 个工具全绑在一个 LLM 上，选错概率高 | 工具太多，意图识别负担重 |
| 单点故障 | LLM 推理失败，整个请求全失败 | 没有冗余和并行 |

这就引出了下一章的主题：**多 Agent 协作**——每个 Agent 只做一件事，通过消息传递和共享状态协作完成复杂任务。

---

## 1.8 本章小结

| 要点 | 核心做法 | 一句话 |
|------|---------|--------|
| 单 Agent 决策链 | 意图分类 → 记忆加载 → RAG 检索 → 上下文组装 → LLM 推理 ↔ 工具执行 | 四个问题依次回答：想干什么、知道什么、需要什么知识、要执行什么操作 |
| 子系统解耦 | 记忆、RAG、工具各自独立节点，按意图条件路由 | 不需要的子系统不调用，省延迟、省 token |
| Human-in-the-Loop | `interrupt_before` + 工具危险等级分类 + 人工确认后 `aupdate_state` 恢复 | 不可逆操作必须人拍板，机器做建议，人做决策 |
| Plan-and-Execute | Plan（生成计划）→ Review（可选 HITL）→ Execute（逐步执行+验证） | 复杂任务先想清楚再动手，避免 ReAct 的"走一步看一步"偏航 |
| 两者关系 | Plan-and-Execute 管理任务复杂度，HITL 管理操作风险度 | 不同维度，叠加使用：计划让人审方向，执行让人审操作 |

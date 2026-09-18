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
│  ┌────────────────┐  ┌────────────┐                 │
│  │ 会话历史        │  │ 长期记忆    │                 │
│  │ 刚才聊了啥      │  │ 用户是谁    │                 │
│  │ (checkpointer) │  │ (Milvus)   │                 │
│  └────────────────┘  └────────────┘                 │
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
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langchain_openai import ChatOpenAI
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import ToolException

from core.intent_classifier import classify_intent, Intent
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

    # 会话消息：**唯一真相**，由 checkpointer 按 thread_id=session_id 持久化。
    # 用 add_messages 而不是自定义 add：它按消息 id 去重/更新，并保证
    # assistant(tool_calls) 与对应 tool 结果的配对关系不被破坏。
    messages: Annotated[list[BaseMessage], add_messages]

    # 本轮组装出来的 system prompt（system 指令 + 长期记忆 + RAG + 工具提示）。
    # 注意它**不进入 messages 通道**——原因见 1.1.4。
    system_prompt: str

    # 各子系统注入的上下文
    intent: str                          # 意图分类结果
    long_term_context: list[dict]        # 长期记忆（用户画像）
    rag_context: list[dict]              # RAG 检索结果

    # 工具调用控制
    available_tools: list[str]           # 当前节点可用的工具名列表
    write_authorized: bool               # 本会话是否已授权过写操作（1.3.3 用）

    # 内部动态标记（下划线开头，节点之间传递的中间判断）
    # 显式声明，避免类型检查报"未声明的字段"错误
    _need_rag: bool                      # 是否需要 RAG 检索（由 node_classify_intent 写入）

    # 人工审批结果。必须显式声明：aupdate_state 只能更新 State 里已声明的字段，
    # 往未声明的键注入数据会被忽略——1.3.2 的"确认→恢复"就靠这个字段
    human_approved: bool

    # 最终输出
    final_response: str


# ==================== 节点实现 ====================

llm = ChatOpenAI(model="gpt-5.1")


async def node_classify_intent(state: AgentState) -> dict:
    """
    节点①：意图分类
    
    判断用户想干什么，决定后续哪些子系统需要参与。
    """
    intent_result = await classify_intent(state["user_query"])

    # 根据意图决定需要启用哪些能力
    need_tools = (intent_result.intent == Intent.ACTION)
    need_rag = (intent_result.intent == Intent.QUESTION)
    # 长期记忆总是检索。注意它并不"便宜"：检索前要先对 query 做一次 embedding，
    # 那是一次外部 API 往返（量级 100~300ms），不是几十毫秒的本地查询。
    # 对延迟敏感的场景，应把它做成"与意图分类并行"而不是串在后面。

    return {
        "intent": intent_result.intent,
        "available_tools": [t.name for t in ALL_TOOLS] if need_tools else [],
        "_need_rag": need_rag,
    }


async def node_load_memories(state: AgentState) -> dict:
    """
    节点②：加载长期记忆

    短期记忆（会话历史）已经由 checkpointer 随 messages 一起持久化，
    这里**不再**从 Redis 另取一份——同一份历史存在两处（checkpointer 与
    自维护的 Redis 消息列表），必然对不上：两边裁剪规则不同、写入时机不同，
    最终表现为"模型看到的上下文和界面显示的不一致"。
    """
    long_term = await search_similar_memories(
        user_id=state["user_id"],
        query_text=state["user_query"],
        top_k=5,
    )

    return {"long_term_context": long_term}


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
    节点④：组装本轮上下文

    只产出两样东西：
    - system_prompt：system 指令 + 长期记忆 + RAG 结果 + 可用工具提示
    - 本轮的用户消息（追加进 messages 通道，由 add_messages 维护）

     ❌ 不要在这里把"system + 全部历史 + 本轮提问"整体 return 给 messages。
     messages 是**追加语义**的通道，整段重建等于每轮都往历史里再插一份完整历史：
     system prompt 与旧对话从第 2 轮起成倍复制，checkpoint 体积持续膨胀，
     token 成本随之上涨——而回复内容看起来仍然正常，所以极难发现。
     原因与两个正确的写法见 1.1.4。
    """
    system_parts = ["你是用户的智能助手，回答准确、简洁。\n"]

    # 1. 长期记忆（用户画像）
    if state["long_term_context"]:
        facts = "\n".join(f"- {m['content']}" for m in state["long_term_context"])
        system_parts.append(f"## 关于此用户的已知信息\n{facts}\n")

    # 2. RAG 结果
    if state.get("rag_context"):
        docs = "\n\n".join(
            f"[{d['title']}]\n{d['content']}" for d in state["rag_context"]
        )
        system_parts.append(f"## 参考资料\n{docs}\n")

    # 3. 可用工具
    if state["available_tools"]:
        tools_hint = "你可以调用以下工具来完成任务：" + ", ".join(state["available_tools"])
        system_parts.append(tools_hint)

    return {
        "system_prompt": "\n".join(system_parts),
        "messages": [HumanMessage(content=state["user_query"])],
    }


async def node_llm_reason(state: AgentState) -> dict:
    """
    节点⑤：LLM 推理

    输入 = 本轮组装好的 system prompt + 持久化的 messages。
    messages 是唯一真相（含历史与已经发生的 tool 往返），所以历史只会有一份，
    ReAct 循环第二轮起也能自动看到上一轮的工具结果。
    """
    prompt = [SystemMessage(content=state["system_prompt"]), *state["messages"]]

    if state["available_tools"]:
        llm_with_tools = llm.bind_tools(
            [t for t in ALL_TOOLS if t.name in state["available_tools"]]
        )
        response = await llm_with_tools.ainvoke(prompt)
    else:
        response = await llm.ainvoke(prompt)

    return {"messages": [response]}


async def node_generate_final(state: AgentState) -> dict:
    """
    节点⑥：生成最终回复
    
    当 LLM 不再需要调工具时，生成最终的用户回复。
    """
    last_message = state["messages"][-1]
    return {"final_response": last_message.content}


# ==================== 路由函数（纯逻辑） ====================

# 说明：把路由集中在函数里、每个函数只做 if/else，是为了让"路径由 State 决定"。
# 不要在这里调 LLM——判断过程一旦放在路由里就不会进 State，路径也就不可回放了。

def route_after_memories(state: AgentState) -> str:
    """记忆加载后：检查是否需要 RAG"""
    if state.get("_need_rag"):
        return "search_rag"
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

def build_single_agent(checkpointer):
    """
    checkpointer 是**必填**参数，不给默认值。

    没有 checkpointer 就没有恢复、没有跨进程续跑、没有 HITL，
    而且只要图里挂了 checkpointer，运行时就必须传
    config={"configurable": {"thread_id": ...}}，
    否则抛 ValueError: Checkpointer requires one or more of the
    following 'configurable' keys: ['thread_id', 'checkpoint_ns', 'checkpoint_id']。
    """
    graph = StateGraph(AgentState)

    # 注册节点
    graph.add_node("classify_intent", node_classify_intent)
    graph.add_node("load_memories", node_load_memories)
    graph.add_node("search_rag", node_search_rag)
    graph.add_node("assemble_context", node_assemble_context)
    graph.add_node("llm_reason", node_llm_reason)
    # handle_tool_errors 一定要显式给：默认值只把"模型传错参数"（ToolInvocationError）
    # 转成 status="error" 的 ToolMessage，工具**运行时**抛的异常（连接失败、超时、
    # 业务异常）会原样向上抛、中断本次运行。想让它变成一段回灌给模型的错误信息，
    # 就得在这里声明要兜住的异常类型（详见第三章 3.3.3）。
    graph.add_node(
        "tools",
        ToolNode(ALL_TOOLS, handle_tool_errors=(TimeoutError, ConnectionError, ToolException)),
    )
    graph.add_node("generate_final", node_generate_final)

    # 连线（用 START/END 声明起止，set_entry_point/set_finish_point 已弃用）
    graph.add_edge(START, "classify_intent")
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


# ==================== 编译实例的创建时机 ====================

# ❌ 不要在模块顶层这样写：
#     agent = build_single_agent(checkpointer=RedisSaver.from_conn_string("redis://..."))
# from_conn_string 是 @contextmanager，直接赋值拿到的是上下文管理器而不是 saver；
# 而且首次使用必须先 setup() 建 RediSearch/RedisJSON 索引，否则运行时才报错。
#
# ✅ 正确做法：在应用 lifespan 里持有连接，把编译好的图挂到 app.state 上。
#
#     # main.py
#     from contextlib import asynccontextmanager
#     from fastapi import FastAPI
#     from langgraph.checkpoint.redis.aio import AsyncRedisSaver
#     from core.single_agent import build_single_agent
#
#     @asynccontextmanager
#     async def lifespan(app: FastAPI):
#         # 会话与 checkpointer 走 db 1，业务缓存在 db 0，两边互不干扰
#         async with AsyncRedisSaver.from_conn_string("redis://localhost:6379/1") as saver:
#             await saver.asetup()          # 幂等：建索引
#             app.state.agent = build_single_agent(saver)
#             yield
#
#     app = FastAPI(lifespan=lifespan)
#
# 调用侧：
#     config = {"configurable": {"thread_id": session_id}}
#     result = await app.state.agent.ainvoke(
#         {"user_query": query, "session_id": session_id, "user_id": user_id},
#         config=config,
#     )
```

### 1.1.3 上述代码的核心设计决策

**决策一：意图分类先于一切。** 不先搞清楚用户要干什么，后面全是盲目操作。把意图写入 State，后续每个节点都能据此决定自己要不要执行、怎么执行。

**决策二：记忆加载和 RAG 检索解耦为独立节点。** 记忆是"关于这个用户"的信息，RAG 是"关于这个问题"的知识——两者的检索条件和用途完全不同。解耦后可以按意图决定是否执行 RAG（闲聊不需要查知识库，省一次 Milvus 查询）。

**决策三：上下文组装是单独一个节点，但它只产出 `system_prompt`。** 把"拼 system prompt"这件事集中在一个地方，方便调试——如果 Agent 回复质量差，先看这个节点拼出来的 prompt 长什么样。而对话历史不在这里重建，"拼历史"这件事交给 messages 通道（决策四）。

**决策四：messages 是会话历史的唯一真相。** 历史只存一份（checkpointer 按 `thread_id=session_id` 持久化），组装节点每轮只追加当轮的用户消息。这一点很容易写错，1.1.4 单独讲。

**决策五：LLM 推理 → 工具执行 → LLM 推理，形成 ReAct 循环。** 这是 Agent 的核心：不是一次 LLM 调用就完事，而是"思考-行动-观察-再思考"的迭代过程。因为历史在 messages 里，循环第二轮起 LLM 能自动看到上一轮的工具结果，不需要特殊处理。

### 1.1.4 一个必须避开的坑：别把"重拼的历史"写进追加型通道

`messages` 是**追加语义**的通道（`Annotated[..., add_messages]`），节点返回的列表会被**合并**进已有历史，而不是替换它。于是下面这种看起来最自然的写法是错的：

```python
# ❌ 错误：把 system + 全部历史 + 本轮提问整体拼好，返回给 messages
async def node_assemble_context(state):
    messages = [{"role": "system", "content": ...}]
    for msg in state["short_term_context"]:     # 从 Redis 取来的历史
        messages.append(msg)
    messages.append({"role": "user", "content": state["user_query"]})
    return {"messages": messages}               # ← 被"追加"进已有历史
```

第一轮正常。但 checkpointer 会把 `messages` 存下来，第二轮开始，`messages` 里已经有第一轮的全套消息，这个节点又追加一份"重拼的全套"——于是 system prompt 与旧对话成倍复制：

| 轮次 | messages 里实际有什么 | system prompt 份数 |
|------|---------------------|------------------|
| 第 1 轮 | 重拼 1 份 | 1 |
| 第 2 轮 | 第 1 轮 1 份 + 重拼 1 份 | 2 |
| 第 3 轮 | 前两轮 + 重拼 1 份 | 3 |

后果是 checkpoint 体积与 token 成本随轮次线性上涨，而**回复内容看起来完全正常**——不报错、不变慢到明显察觉，只有账单和上下文长度在悄悄涨。

两种正确写法：

```python
# ✅ 写法 A（本章采用）：历史不重建，只追加"增量"
#    组装节点产出 system_prompt 与当轮用户消息，历史由 messages 通道维护
return {
    "system_prompt": "\n".join(system_parts),
    "messages": [HumanMessage(content=state["user_query"])],
}

# ✅ 写法 B：确实需要在节点里拼整段 prompt 时，用普通字段（覆盖语义）承载，
#    不要用追加型通道
return {"llm_input": messages}     # llm_input 未声明 reducer → 新值替换旧值
```

**一句话：追加型通道只放"增量"，要放"全量"就用覆盖型字段。**

---

## 1.2 单 Agent 的执行时序：一个完整请求的 8 步

```
时间线（用户："帮我查一下上周那笔退款到账了没"）

 0ms  │  用户请求到达
      │
 5ms  │  ① classify_intent    → intent=action, need_tools=true, need_rag=false
      │     （走小模型约 150~300ms；走规则/关键词命中可 <5ms）
      │     （"查退款"是操作，不是提问，不需要 RAG）
      │
 250ms│  ② load_memories      → 长期：用户是 VIP，偏好简洁回复
      │                          → 长期：用户最近有一笔订单 ORD-88483
      │     （检索前要对 query 做一次 embedding，是一次外部 API 往返）
      │
 255ms│  ③ route: need_rag=false → 跳过 search_rag
      │
 260ms│  ④ assemble_context    → 产出 system prompt（含长期记忆、工具提示）
      │                          并把本轮用户消息追加进 messages
      │
 900ms│  ⑤ llm_reason (第1次)  → LLM 决定调 order_tool(action="search", order_id="ORD-88483")
      │
1150ms│  ⑥ tools               → 执行 order_tool → 返回"订单 ORD-88483：退款 299 元，
      │                          状态：处理中，预计 3 个工作日内到账"
      │
3200ms│  ⑦ llm_reason (第2次)  → LLM 看到工具结果，决定不再调工具，生成回复
      │                          "您的退款 299 元正在处理中，预计 3 个工作日内原路退回。"
      │
3210ms│  ⑧ generate_final      → 提取最终回复
      │
3210ms│  → 返回给用户
```

**总耗时约 3 秒（其中首 token 约 0.9 秒）。** 拆开看，时间几乎全花在两次 LLM 推理上（⑤、⑦），其余节点是固定开销。

这张表要读出的判断有三条，比绝对数字重要：

1. **两次 LLM 推理占了大头**，而第二次的输入比第一次更长（多了工具结果），所以第二次的 prefill 会更慢。想压延迟，先压 LLM 调用次数与 prompt 长度，而不是去优化那 5ms 的节点调度。
2. **"长期记忆检索"不是免费操作。** 它必须先对 query 做一次 embedding，那是一次外部 API 往返（100~300ms）。对延迟敏感的场景应把它与意图分类**并行**，而不是串在后面。
3. **上表是单轮、无重试、网络顺畅的情况。** 真实 P99 要算上工具超时重试、LLM 重试、RAG 的 rerank 外网调用——量级往往是中位数的 3~5 倍。做容量规划时不能用平均值。

**想让首 token 更快，只有两条路**（其余都是微调）：把流式打开（用户先看到字，感知延迟从"总耗时"降到"首 token"），以及把可以并行的检索/分类并行起来。

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

LangGraph 提供两种"暂停"机制，容易混淆，先分清：

1. **`interrupt()`（节点内暂停）**：在节点内部调用，由图自己决定"要不要停、停在哪、带什么信息给人看"。停不下来的时候它并不向调用方抛异常——见下面的机制说明。**本章主推这一种**，因为"哪些工具需要确认"是运行时才知道的动态判断（1.3.3 的危险等级路由正是这种情况）。
2. **`compile(interrupt_before=[...])`（节点前暂停）**：编译时静态声明在哪些节点执行前暂停。适合"这个节点每次都必须人工过一遍"的固定审批点；代价是它不携带上下文（人看不到模型想干什么），且需要在图里专门放一个空节点当"汇合点"。

**关于 `interrupt()` 的异常语义，这是最容易搞错的一点：**

| 说法 | 是否成立 |
|------|---------|
| 节点内部 `interrupt()` 会抛 `GraphInterrupt` | ✅ 成立，但那是框架内部实现 |
| 这个异常会传播到调用 `ainvoke` 的地方，需要 try/except | ❌ **不成立** |

从调用方视角看，`interrupt()` 挂起时 `ainvoke` 是**正常返回**的，结果里带一个 `__interrupt__` 字段：

```python
result = await agent.ainvoke(input, config=config)
payload = result["__interrupt__"][0].value     # ← 中断载荷在这里，不是异常
```

恢复也不是"抛了异常所以重试"，而是显式地把人的决定送回去：

```python
from langgraph.types import Command

result = await agent.ainvoke(Command(resume=True), config=config)   # True = 批准
```

```python
# ============ 完整流程：读状态 → 人审 → 恢复 ============

config = {"configurable": {"thread_id": session_id}}

# 1. 第一次执行：跑到审批点会挂起，ainvoke 正常返回（不抛异常）
result = await agent.ainvoke(
    {
        "user_query": "帮我给全公司发邮件通知明天放假",
        "session_id": session_id,
        "user_id": user_id,
    },
    config=config,
)

# 2. 取出中断载荷——这才是"要给人看的东西"
interrupt_payload = result["__interrupt__"][0].value
# → {"type": "review_required", "tool": "send_email",
#    "args": {"to": "all@company.com", "subject": "明天放假"}}

# 3. 前端弹窗："Agent 想要发送邮件给全公司，确认吗？"

# 4a. 用户点"确认" → 把决定送回去，图从挂起点继续
result = await agent.ainvoke(Command(resume=True), config=config)

# 4b. 用户点"拒绝" → 同时把"改过的意见"送回去，让模型据此改道
result = await agent.ainvoke(
    Command(resume={"approved": False, "feedback": "不要提放假，改成通知明早 9 点开会"}),
    config=config,
)

# 补充：想在恢复前先看一眼"模型打算干什么"，用 aget_state 读快照
snapshot = await agent.aget_state(config)
pending_tool_calls = snapshot.values["messages"][-1].tool_calls
```

**注意：`interrupt()` 恢复时会从该节点重新执行**，所以 `interrupt()` 之前的代码会被跑第二遍——审批节点里不要放发消息、写库这类有副作用的操作（同一条约束在 1.4 的 Plan 评审里也适用）。

**如果坚持用 `interrupt_before` 做审批**，有两点必须补上，否则示例跑不通：

- **给图配一个审批汇合节点**，否则中断后没有可恢复的落点；
- **`aupdate_state` 只能更新 State 里已声明的字段**。像 `human_approved` 这种注入字段，必须先写进 `AgentState`（本章 1.1.2 的 State 定义里已经声明）——往未声明的键写数据会被忽略，表现为"点了确认但图的行为没变"，很难查。

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
    
    - LLM 返回纯文本（没有 tool_calls）→ 这就是最终回复，走 generate_final
      （否则会被路由回 tools，ToolNode 无工具可执行，再回到 llm_reason → 死循环）
    - READ  → 直接执行
    - WRITE → 检查用户是否已授权（首次 WRITE 时需确认，后续同会话可放行）
    - DANGEROUS → 每次都必须确认
    """
    last_message = state["messages"][-1]
    tool_calls = last_message.tool_calls

    # 纯文本回复 → 直接生成最终回答
    if not tool_calls:
        return "generate_final"

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

**注意这个示例与 1.1.2 的关系：审批图不是"原图 + `interrupt_before`"，而是把审批当成一个真实节点接进路由。** 这样做的好处是审批节点能看到 State（知道模型打算调哪个工具、带什么参数），人的决定也能作为数据写回 State。

```python
from langgraph.types import interrupt


def route_after_approval(state: AgentState) -> str:
    """审批后的分流：批准 → 执行工具；拒绝 → 回到 LLM 重新决策"""
    if state.get("write_authorized"):
        return "tools"
    return "llm_reason"


def build_agent_with_approval(checkpointer):
    graph = StateGraph(AgentState)

    # ---- 原有节点（与 1.1.2 一致）----
    graph.add_node("classify_intent", node_classify_intent)
    graph.add_node("load_memories", node_load_memories)
    graph.add_node("search_rag", node_search_rag)
    graph.add_node("assemble_context", node_assemble_context)
    graph.add_node("llm_reason", node_llm_reason)
    graph.add_node(
        "tools",
        ToolNode(ALL_TOOLS, handle_tool_errors=(TimeoutError, ConnectionError, ToolException)),
    )
    graph.add_node("generate_final", node_generate_final)

    # ---- 新增：人工审批节点（用 interrupt() 挂起）----
    async def node_human_approval(state: AgentState) -> dict:
        """
        审批节点。

        两个要点：
        1. interrupt() **之前**不要放副作用——恢复时本节点会从头重跑一遍；
        2. 人的决定不是"异常恢复"，而是作为 interrupt() 的返回值送进来
           （由外部用 Command(resume=...) 提供）。
        """
        last_message = state["messages"][-1]

        decision = interrupt({
            "type": "tool_approval_required",
            "tool_calls": [
                {"name": tc["name"], "args": tc["args"]} for tc in last_message.tool_calls
            ],
        })

        if not decision.get("approved"):
            # 人拒绝了：把反馈作为一条消息回灌，让模型据此改道，
            # 而不是直接把流程掐断——多数情况下用户想要的是"换个做法"，不是"别做了"
            return {
                "messages": [HumanMessage(
                    content=(
                        "用户拒绝执行该操作。"
                        f"反馈：{decision.get('feedback', '无')}。请据此调整方案，不要重复同样的调用。"
                    )
                )]
            }

        return {"write_authorized": True}   # 批准：记下授权，同会话内的写操作不再反复打断

    graph.add_node("human_approval", node_human_approval)

    # ---- 连线：要把整张图连通，不能只连新增部分 ----
    graph.add_edge(START, "classify_intent")
    graph.add_edge("classify_intent", "load_memories")
    graph.add_conditional_edges("load_memories", route_after_memories, {
        "search_rag": "search_rag",
        "assemble_context": "assemble_context",
    })
    graph.add_edge("search_rag", "assemble_context")
    graph.add_edge("assemble_context", "llm_reason")

    # llm_reason 后：按工具危险等级分流
    graph.add_conditional_edges("llm_reason", route_tool_approval, {
        "tools": "tools",
        "human_approval": "human_approval",
        "generate_final": "generate_final",
    })
    graph.add_edge("tools", "llm_reason")       # ← ReAct 循环
    graph.add_conditional_edges("human_approval", route_after_approval, {
        "tools": "tools",
        "llm_reason": "llm_reason",
    })
    graph.add_edge("generate_final", END)

    # interrupt() 必须有 checkpointer 才能工作：没有持久化就没有"挂起后恢复"这回事
    return graph.compile(checkpointer=checkpointer)
```

```python
# 编译实例同样在 lifespan 里创建（见 1.1.2 末尾），不要在这里直接建 Redis 连接
app.state.agent = build_agent_with_approval(saver)

# 调用侧：危险工具会挂起，ainvoke 正常返回并带 __interrupt__
result = await app.state.agent.ainvoke(
    {
        "user_query": "帮我给全公司发邮件通知明天放假",
        "session_id": session_id,
        "user_id": user_id,
    },
    config={"configurable": {"thread_id": session_id}},
)
if "__interrupt__" in result:
    payload = result["__interrupt__"][0].value      # 交给前端弹窗

# 用户确认后
result = await app.state.agent.ainvoke(
    Command(resume={"approved": True}),
    config={"configurable": {"thread_id": session_id}},
)
```

**图不完整的两个典型症状**（都是编译期就报错，比运行时崩好查）：注册了节点却没有入口（漏了 `add_edge(START, ...)`），或节点没有任何出边（注册了却没接线）。改图时最容易漏的就是这两处。

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
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt


# ==================== 计划的数据结构 ====================

class PlanStep(BaseModel):
    """
    计划中的一步。

    注意可选字段的默认值写法：`with_structured_output(Plan)` 默认走 strict
    Structured Outputs，它**不接受非 null 的默认值，也不接受 `default_factory`**
    ——写成 `tool_name: str = ""` 或 `depends_on: list[int] = Field(default_factory=list)`
    会直接 400（`Invalid schema for response_format`）。
    可选字段统一写成 `X | None = Field(default=None)`（详见第三章 4.4.2）。
    """
    step_id: int
    description: str
    tool_name: str | None = Field(default=None, description="此步骤需要调用的工具名，不需要工具时留空")
    depends_on: list[int] | None = Field(default=None, description="依赖的前置步骤 id 列表")
    status: str | None = Field(default=None, description="pending / in_progress / completed / failed")


class Plan(BaseModel):
    steps: list[PlanStep]
    goal: str = Field(description="任务的最终目标，一句话描述")


# ==================== State 定义 ====================

class PlanAndExecuteState(TypedDict):
    user_query: str
    plan: Plan
    completed_steps: Annotated[list[dict], add]  # 已完成步骤的结果（追加语义，放增量）
    current_step: int                             # 下一步要执行的步骤 id（由 execute_step 写回）
    plan_feedback: str | None                     # 用户对上一版计划的修改意见（评审未通过时写入）
    final_output: str
    messages: Annotated[list, add]


# ==================== 阶段一：生成计划 ====================

planning_llm = ChatOpenAI(model="gpt-5.1").with_structured_output(Plan)

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
    """生成执行计划。若上一版计划被用户要求修改，把意见一并带上重出。"""
    prompt = PLANNING_PROMPT.format(query=state["user_query"])

    if state.get("plan_feedback"):
        prompt += f"\n\n## 用户对上一版计划的修改意见（必须采纳）\n{state['plan_feedback']}"

    plan = await planning_llm.ainvoke(prompt)

    # 消费掉反馈：不清掉的话，后续每次重出计划都会把旧意见再带一遍
    return {"plan": plan, "plan_feedback": None}


# ==================== 阶段二（可选）：计划评审 ====================

async def node_review_plan(state: PlanAndExecuteState) -> dict:
    """
    计划评审——让用户确认计划。

    与 1.3.4 的审批节点同理：用 interrupt() 挂起，把计划本身作为载荷给人看，
    人的决定通过 Command(resume=...) 送回来。这比 interrupt_before + 空节点
    那套绕法好在"要确认什么"和"计划长什么样"在同一个节点里，且拒绝之后
    有明确的回流路径（回到 plan 重出），不会把人拒绝这件事静默吞掉。
    """
    decision = interrupt({
        "type": "plan_review_required",
        "goal": state["plan"].goal,
        "steps": [
            {"step_id": s.step_id, "description": s.description, "tool_name": s.tool_name}
            for s in state["plan"].steps
        ],
    })

    if decision.get("approved"):
        return {}

    return {"plan_feedback": decision.get("feedback", "用户未说明理由，请换一个更稳妥的拆解方式")}


def route_after_review(state: PlanAndExecuteState) -> str:
    """评审后：通过 → 开始执行；要求修改 → 回到 plan 重出计划"""
    if state.get("plan_feedback"):
        return "plan"
    return "execute_step"


# ==================== 阶段三：逐步执行 ====================

async def node_execute_step(state: PlanAndExecuteState) -> dict:
    """执行当前步骤"""
    plan = state["plan"]
    current = state.get("current_step", 1)

    # 找到当前步骤
    step = next((s for s in plan.steps if s.step_id == current), None)
    if step is None:
        return {"final_output": "所有步骤已完成"}

    # 说明：本示例按 step_id 顺序执行；生产实现应把计划按 depends_on 做拓扑排序，
    # 只有前置步骤完成后才调度后续步骤（可参考第二章 2.5.3 的依赖调度）。

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

    # ===== 内嵌 ReAct：一个步骤内部也要"思考→调工具→观察→再思考" =====
    # 注意：不能在节点外只 bind_tools 就完事——图里没有 ToolNode，
    # 一旦 LLM 返回 tool_calls，response.content 会是空的，工具也永远不会执行。
    # 正确做法：在节点内部做一轮 ReAct，LLM 要调工具就执行工具，
    # 把 ToolMessage 回填后再让 LLM 继续，直到 LLM 返回纯文本为止。
    step_tools = [t for t in ALL_TOOLS if not step.tool_name or t.name == step.tool_name]
    execute_llm = llm.bind_tools(step_tools)
    tool_node = ToolNode(step_tools)

    response = None
    for _ in range(5):  # 最多内嵌 5 轮 ReAct，防止死循环
        response = await execute_llm.ainvoke(messages)
        messages.append(response)

        # LLM 返回 tool_calls → 执行工具，把 ToolMessage 回填到消息里
        if getattr(response, "tool_calls", None):
            tool_output = await tool_node.ainvoke({"messages": [response]})
            messages.extend(tool_output["messages"])
            continue

        # LLM 返回纯文本 → 这就是该步骤的结果，跳出循环
        break

    # 极端情况：若 5 轮全是 tool_calls，可把最后一轮的 ToolMessage 当结果；
    # 教学示例只演示标准路径——LLM 返回纯文本即视为步骤完成。
    result_text = response.content if (response is not None and response.content) else ""

    return {
        "messages": messages,
        # 关键：把"下一步该做第几步"写回 State。
        # 路由函数只读 State 做判断，不会自己累加计数器——不写回这一步，
        # current_step 永远是 1，计划会在第一步上无限循环直到 GraphRecursionError。
        "current_step": current + 1,
        "completed_steps": [{
            "step_id": current,
            "description": step.description,
            "result": result_text,
            "status": "completed",
        }],
    }


def route_after_execute(state: PlanAndExecuteState) -> str:
    """
    执行完一个步骤后，判断下一步。

    路由函数**只读 State**：下一步是"第几步"由 execute_step 写入 current_step，
    这里不重新推算（同 1.1.2 的约定，理由也一样——路由自己算的进度不在 State 里，
    路径就不可回放、恢复后也会算错）。
    """
    total_steps = len(state["plan"].steps)
    next_step = state.get("current_step", 1)     # execute_step 已在完成后 +1

    # 检查上一步是否真的完成了（失败分支同理：没完成就重试当前步骤）
    last_completed = state["completed_steps"][-1] if state["completed_steps"] else None
    if last_completed is None or last_completed["status"] != "completed":
        return "execute_step"                     # 重试当前步骤

    if next_step > total_steps:
        return "assemble_final"                   # 全部完成
    return "execute_step"                         # 继续下一步


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

def build_plan_and_execute_agent(checkpointer):
    graph = StateGraph(PlanAndExecuteState)

    graph.add_node("plan", node_plan)
    graph.add_node("review_plan", node_review_plan)
    graph.add_node("execute_step", node_execute_step)
    graph.add_node("assemble_final", node_assemble_final)

    graph.add_edge(START, "plan")

    # plan → review（人工确认）→ 通过则 execute，被要求修改则回到 plan
    graph.add_edge("plan", "review_plan")
    graph.add_conditional_edges("review_plan", route_after_review, {
        "execute_step": "execute_step",
        "plan": "plan",
    })

    # 循环执行直到所有步骤完成
    graph.add_conditional_edges("execute_step", route_after_execute, {
        "execute_step": "execute_step",
        "assemble_final": "assemble_final",
    })
    graph.add_edge("assemble_final", END)

    return graph.compile(checkpointer=checkpointer)


# 调用侧（同样在 lifespan 里建 checkpointer）
# graph = build_plan_and_execute_agent(saver)
# result = await graph.ainvoke(
#     {"user_query": "帮我写一份竞品分析报告", "current_step": 1, "completed_steps": []},
#     config={"configurable": {"thread_id": session_id}, "recursion_limit": 60},
# )
#
# 必须显式传 current_step 的初值 1，否则第一轮 route_after_execute 读到缺键、
# 退回默认值后行为就取决于"你写没写默认值"——这类隐式默认是排查噩梦的源头。
#
# recursion_limit 也要按计划长度给够：默认 25 个 super-step，一个 N 步的计划
# 每步至少 2 个 super-step（execute_step + 路由判断），N 大时会先撞上
# GraphRecursionError 而不是正常结束。
#
# 另注：本模块用到的 llm / ALL_TOOLS 来自 core/single_agent.py 的模块级定义
# （同一个项目骨架里共享同一份 LLM 客户端与工具注册表），不是本文件里新造的。
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
    # ❌ 关键词匹配只是"能跑通"的教学写法，不要直接上生产：
    #    "帮我调研一下这个报错" 不含任何关键词却需要多步调研，
    #    "分析一下我这句话有没有错别字" 含"分析"却是一步就能答完。
    # ✅ 生产做法：让分类模型输出一个结构化字段（如 complexity: simple|complex）
    #    并写入 State，路由只读 State 做判断——与 1.1.2 的约定一致。
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
| 会话历史归属 | `messages` 是唯一真相（checkpointer 按 `thread_id=session_id` 持久化），组装节点只产出 `system_prompt` 与当轮用户消息 | 追加型通道只放增量：把"重拼的全量历史"写进去，每轮都会复制一份（见 1.1.4） |
| Human-in-the-Loop | `interrupt()`（动态判断，主推）+ `Command(resume=...)` 恢复；`interrupt_before` 只用于静态固定审批点 | 挂起时 `ainvoke` 正常返回、结果带 `__interrupt__`，不要写 try/except；恢复会重跑该节点，前面别放副作用 |
| Plan-and-Execute | Plan（生成计划）→ Review（可选 HITL）→ Execute（逐步执行） | 复杂任务先想清楚再动手，避免 ReAct 的"走一步看一步"偏航 |
| 两者关系 | Plan-and-Execute 管理任务复杂度，HITL 管理操作风险度 | 不同维度，叠加使用：计划让人审方向，执行让人审操作 |

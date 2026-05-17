# 第一章：对话接口

> **核心论点**：对话接口是 Agent 后端中调用频率最高、用户体验最敏感的模块。流式/非流式切换、session 会话隔离、高并发异步处理——这三个问题任一处理不当，用户感知就是"卡、乱、丢"。本章逐一拆解其原理和正确实现。

---

## 1.1 流式 vs 非流式：不是"加个 stream=True 就行"

### 1.1.1 两种模式的区别

用户发一条消息给 Agent，Agent 的回复可能长达数百 token。两种返回方式：

```
非流式（Non-streaming）：
用户 → "介绍一下深度学习"
         │
         ▼  等待 5 秒（LLM 生成全部内容）
         │
         ▼  一次性返回全部文本
Agent → "深度学习是机器学习的一个子集...（全文 500 字）"

流式（Streaming / SSE）：
用户 → "介绍一下深度学习"
         │
         ▼  逐 token 推送
Agent → "深度"
Agent → "学习"
Agent → "是"
Agent → "机器"
Agent → "学习"  ← 用户边看边等，体感延迟 ≈ 0
Agent → "的"
Agent → "一个"...
```

非流式模式下，用户盯着空白屏幕等 5 秒；流式模式下，首个 token 在 200ms 内到达，用户立刻看到文字开始"打字"。**体感延迟差了一个数量级。**

### 1.1.2 为什么不能"一直用流式"——两种模式各有适用场景

| 维度 | 非流式 | 流式 |
|------|--------|------|
| 首 token 延迟 | 高（等全部生成完） | 低（逐 token 推送） |
| 总传输时间 | 相同 | 相同 |
| 客户端复杂度 | 简单（一次 HTTP 请求） | 高（需处理 SSE 事件流） |
| 可解析性 | 直接拿完整 JSON | 需前端拼接 chunks |
| 适用场景 | 工具调用、结构化输出、后台任务 | C 端对话、实时展示 |
| LangGraph 支持 | `ainvoke` | `astream_events` |

**该用非流式的场景：**
- Agent 调用工具后需要返回结构化 JSON（前端不需要逐字展示中间推理）
- 后台批量处理任务（没有用户盯着屏幕）
- 输出需要完整校验后才能返回（如 SQL 生成、代码审查）

**该用流式的场景：**
- C 端对话机器人（用户在线等着看回复）
- 长文本生成（文章、报告、邮件）
- 多步推理 Agent（用户可以实时看到 Agent 的思考过程）

### 1.1.3 正确做法：一个参数统一控制两种模式

不要在项目里写两套完全不同的路由。用一个 `stream` 参数在同一个接口里分支：

```python
# models/chat.py
from pydantic import BaseModel, Field
from typing import Optional, Literal


class ChatRequest(BaseModel):
    session_id: str = Field(..., description="会话 ID，用于隔离不同用户/会话的上下文")
    message: str = Field(..., min_length=1, max_length=10000)
    stream: bool = Field(default=True, description="是否流式返回。true=SSE 逐 token 推送，false=一次性返回完整结果")
    model: Literal["gpt-4o", "gpt-4o-mini", "claude-sonnet-4-6"] = "gpt-4o"
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)


class ChatResponse(BaseModel):
    """非流式响应"""
    code: int = 0
    session_id: str
    reply: str
    model: str
    usage: Optional[dict] = None  # {"prompt_tokens": 150, "completion_tokens": 80}
```

```python
# routes/chat.py
import json
import asyncio
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from core.agent import agent
from core.cache import cache_get, cache_set
from core.logger import get_logger
from models.chat import ChatRequest, ChatResponse

logger = get_logger(__name__)
router = APIRouter(prefix="/v1/chat", tags=["对话接口"])


@router.post("/completions")
async def chat_completions(request: ChatRequest):
    """
    统一的对话端点，通过 request.stream 控制返回模式。
    
    - stream=True  → SSE 流式响应（默认）
    - stream=False → JSON 非流式响应
    """
    if request.stream:
        return await _handle_stream(request)
    else:
        return await _handle_non_stream(request)
```

### 1.1.4 非流式分支：直接 await，返回完整 JSON

```python
async def _handle_non_stream(request: ChatRequest) -> dict:
    """非流式处理：等 Agent 全部执行完，一次性返回"""
    try:
        # 从 Redis 读取会话历史
        history = await cache_get(f"session:{request.session_id}") or []
        
        # 追加用户消息
        history.append({"role": "user", "content": request.message})
        
        # Agent 同步执行（内部多次 await LLM，全部完成后返回）
        result = await agent.ainvoke({"messages": history})
        
        # 提取最终回复
        reply = result["messages"][-1].content
        
        # 写回 Redis
        history.append({"role": "assistant", "content": reply})
        await cache_set(f"session:{request.session_id}", history, ttl=3600)
        
        logger.info("非流式对话完成 | session_id=%s | reply_len=%d", request.session_id, len(reply))
        
        return {
            "code": 0,
            "session_id": request.session_id,
            "reply": reply,
            "model": request.model,
        }
    
    except asyncio.TimeoutError:
        logger.error("非流式对话超时 | session_id=%s", request.session_id)
        raise HTTPException(status_code=503, detail={"code": -2, "message": "服务繁忙，请稍后重试"})
    except Exception:
        logger.exception("非流式对话异常 | session_id=%s", request.session_id)
        raise HTTPException(status_code=500, detail={"code": -99, "message": "服务异常"})
```

### 1.1.5 流式分支：SSE 逐 token 推送

流式响应的核心是 **Server-Sent Events (SSE)**。HTTP 连接建立后，服务端持续推送 `data:` 开头的文本事件，客户端用 `EventSource` 或 `fetch` + `ReadableStream` 逐条接收。

```python
async def _handle_stream(request: ChatRequest) -> StreamingResponse:
    """流式处理：通过 SSE 逐 token 推送给前端"""

    async def event_generator():
        """
        异步生成器，逐 token yield SSE 事件。
        
        FastAPI 的 StreamingResponse 会持续读取此生成器，
        每 yield 一次就向客户端推送一个 SSE chunk。
        """
        full_reply_parts: list[str] = []  # 收集完整回复，用于写入 Redis
        
        try:
            # 发送开始事件（可选，用于前端展示加载状态）
            yield _sse("start", {"session_id": request.session_id})
            
            # 从 Redis 读取历史
            history = await cache_get(f"session:{request.session_id}") or []
            history.append({"role": "user", "content": request.message})
            
            # 使用 astream_events 逐事件获取 Agent 执行过程
            async for event in agent.astream_events(
                {"messages": history},
                version="v2",
            ):
                kind = event["event"]
                
                # 只捕获 LLM 的流式输出事件
                if kind == "on_chat_model_stream":
                    chunk = event["data"]["chunk"]
                    token = chunk.content
                    
                    if token:
                        full_reply_parts.append(token)
                        # 逐 token 推送
                        yield _sse("token", {"content": token})
                
                # 捕获工具调用开始事件
                elif kind == "on_tool_start":
                    tool_name = event.get("name", "unknown")
                    logger.info("Agent 调用工具 | session_id=%s | tool=%s", request.session_id, tool_name)
                    yield _sse("tool_call", {
                        "tool": tool_name,
                        "status": "start",
                    })
                
                # 捕获工具调用结束事件
                elif kind == "on_tool_end":
                    tool_name = event.get("name", "unknown")
                    yield _sse("tool_call", {
                        "tool": tool_name,
                        "status": "end",
                    })
            
            # 完整回复写回 Redis
            full_reply = "".join(full_reply_parts)
            history.append({"role": "assistant", "content": full_reply})
            await cache_set(f"session:{request.session_id}", history, ttl=3600)
            
            # 发送结束事件
            yield _sse("done", {"session_id": request.session_id})
            yield "data: [DONE]\n\n"
            
            logger.info("流式对话完成 | session_id=%s | reply_len=%d", request.session_id, len(full_reply))
        
        except Exception as e:
            logger.exception("流式对话异常 | session_id=%s", request.session_id)
            # 异常也要通过 SSE 通知前端，不能直接抛 500
            yield _sse("error", {
                "message": "生成过程出现异常，请重试",
                "code": -99,
            })
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁用 Nginx 缓冲，否则 SSE 变成非流式
        },
    )


def _sse(event_type: str, data: dict) -> str:
    """将 type 和 data 格式化为标准 SSE 事件字符串"""
    payload = {"type": event_type, **data}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
```

### 1.1.6 流式响应的三个关键细节

**细节一：`X-Accel-Buffering: no`**

如果前端 Nginx 反向代理没有配这个头，Nginx 会默认缓冲响应体。你的后端在逐 token yield，但 Nginx 帮你全攒着，等全部攒完了才一起发给前端——流式变成了非流式。这个头告诉 Nginx："这条连接不要缓冲，来一个 chunk 发一个 chunk。"

```nginx
# nginx.conf 对应配置（二选一）
location /v1/chat/completions {
    proxy_buffering off;           # 方案A：全局关缓冲
    proxy_pass http://backend;
}
# 或者后端代码加响应头（方案B，上面已加）
```

**细节二：SSE 格式必须严格**

SSE 协议的格式要求：`data: <内容>\n\n`。注意结尾必须有两个换行符 `\n\n`，少一个前端 EventSource 就不触发事件。不要在 `data:` 行和内容之间加多余空格，也不要在 JSON 里混入未转义的换行符。

**细节三：流式生成器的异常处理**

普通 FastAPI 路由抛异常，框架帮你返回 HTTP 500。但流式路由里，一旦 `StreamingResponse` 开始推送，HTTP 状态码早已发送（200），中途抛异常只会导致连接断开——前端看到的是"连接突然断了"，不是一个清晰的错误信息。所以生成器内部必须 try-catch，异常通过 SSE 的 error 事件传出去，而不是抛。

### 1.1.7 前端如何消费

```javascript
// 前端 fetch 流式消费示例
async function chatStream(sessionId, message) {
    const response = await fetch('/v1/chat/completions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId, message, stream: true }),
    });

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';  // 最后一行可能不完整，放回 buffer

        for (const line of lines) {
            if (line.startsWith('data: ')) {
                const data = JSON.parse(line.slice(6));
                if (data.type === 'token') {
                    // 逐字追加到 UI
                    appendToChat(data.content);
                } else if (data.type === 'done') {
                    finishMessage();
                } else if (data.type === 'error') {
                    showError(data.message);
                }
            }
        }
    }
}
```

---

## 1.2 session_id 隔离：多用户对话不能"串线"

### 1.2.1 问题场景

你把对话机器人部署上线，用户 A 和用户 B 同时在使用。用户 A 问"帮我查一下我的订单"，机器人跑去查了数据库——但返回的结果是用户 B 的订单。

这是因为会话上下文（对话历史、Agent 状态）没有按 `session_id` 隔离，A 和 B 的数据混在了一起。

### 1.2.2 session_id 的职责

在 Agent 后端里，`session_id` 是会话隔离的唯一标识。它负责关联以下数据：

```
session_id: "abc123"
      │
      ├── 对话历史（messages list）── Redis key: session:abc123
      │       [{role: user, content: ...}, {role: assistant, content: ...}, ...]
      │
      ├── Agent 检查点状态 ────────── LangGraph Checkpointer: thread_id=abc123
      │       (Agent 多步推理的中间状态，支持断点续跑)
      │
      └── 会话元数据 ─────────────── Redis key: session_meta:abc123
              {created_at, last_active, model, token_count, ...}
```

### 1.2.3 生成 session_id 的规范

**session_id 由前端生成，首次创建会话时传给后端。** 不要后端生成——因为前端需要立刻知道 session_id 来发起后续请求。

```python
# models/chat.py 中的校验
from pydantic import BaseModel, Field, field_validator
import re


class ChatRequest(BaseModel):
    session_id: str = Field(
        ...,
        min_length=8,
        max_length=64,
        description="会话 ID。前端用 UUID v4 生成，首次对话时创建，后续对话保持不变。",
    )

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, v: str) -> str:
        # 只允许字母、数字、连字符和下划线，防止注入攻击
        if not re.match(r"^[a-zA-Z0-9\-_]+$", v):
            raise ValueError("session_id 包含非法字符")
        return v
```

前端生成方式：

```javascript
// 前端：首次打开聊天窗口时生成
import { v4 as uuidv4 } from 'uuid';

const sessionId = uuidv4();  // 例如 "a3f2b1c4-d5e6-7890-abcd-ef1234567890"
```

### 1.2.4 Redis 中按 session_id 隔离数据

```python
# core/session.py —— 会话管理模块
import json
import time
from typing import Optional
import redis.asyncio as aioredis
from core.config import REDIS_URL
from core.logger import get_logger

logger = get_logger(__name__)
redis = aioredis.from_url(REDIS_URL)

# Redis Key 命名规范：前缀:session_id
SESSION_PREFIX = "session"          # 对话历史
META_PREFIX = "session_meta"       # 会话元数据
SESSION_TTL = 3600 * 24             # 会话保留 24 小时


async def get_history(session_id: str) -> list[dict]:
    """获取会话的对话历史"""
    raw = await redis.get(f"{SESSION_PREFIX}:{session_id}")
    if raw:
        return json.loads(raw)
    return []


async def save_history(session_id: str, messages: list[dict]) -> None:
    """保存会话的对话历史"""
    await redis.setex(
        f"{SESSION_PREFIX}:{session_id}",
        SESSION_TTL,
        json.dumps(messages, ensure_ascii=False),
    )


async def append_message(session_id: str, role: str, content: str) -> None:
    """追加一条消息到会话历史（避免全量读取再写入的竞争问题）"""
    key = f"{SESSION_PREFIX}:{session_id}"
    messages = await get_history(session_id)
    messages.append({"role": role, "content": content, "timestamp": time.time()})
    await redis.setex(key, SESSION_TTL, json.dumps(messages, ensure_ascii=False))


async def get_metadata(session_id: str) -> dict:
    """获取会话元数据"""
    raw = await redis.get(f"{META_PREFIX}:{session_id}")
    return json.loads(raw) if raw else {}


async def set_metadata(session_id: str, meta: dict) -> None:
    """设置会话元数据"""
    await redis.setex(
        f"{META_PREFIX}:{session_id}",
        SESSION_TTL,
        json.dumps(meta, ensure_ascii=False),
    )


async def delete_session(session_id: str) -> None:
    """删除会话所有数据"""
    await redis.delete(f"{SESSION_PREFIX}:{session_id}", f"{META_PREFIX}:{session_id}")
    logger.info("会话已删除 | session_id=%s", session_id)
```

### 1.2.5 LangGraph Checkpointer 中的 session_id 隔离

LangGraph 的 `Checkpointer` 用 `thread_id` 来隔离不同会话的 Agent 执行状态。直接把 `session_id` 映射为 `thread_id`：

```python
# core/agent.py
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.redis import RedisSaver
from langgraph.graph import StateGraph, MessagesState

# 开发环境：内存 checkpointer（重启丢失）
# memory_checkpointer = MemorySaver()

# 生产环境：Redis checkpointer（持久化 + 多进程共享）
redis_checkpointer = RedisSaver.from_conn_string("redis://localhost:6379/1")


def build_agent() -> StateGraph:
    graph = StateGraph(MessagesState)
    # ... 定义节点和边 ...
    return graph.compile(checkpointer=redis_checkpointer)


agent = build_agent()
```

```python
# routes/chat.py 中调用 Agent 时传入 thread_id
async def _handle_non_stream(request: ChatRequest) -> dict:
    # config 中的 thread_id 就是 session_id
    config = {"configurable": {"thread_id": request.session_id}}
    
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": request.message}]},
        config=config,  # ← 关键：LangGraph 用 thread_id 隔离状态
    )
    # ...
```

这样 LangGraph 自动以 `thread_id`（即 `session_id`）为 key 存储 Agent 的检查点状态，不同用户的 Agent 中间状态完全隔离。

### 1.2.6 防止 session 数据无限膨胀

Redis 内存不是无限的。需要两层防护：

**第一层：TTL 自动过期（上面已配）**

```python
SESSION_TTL = 3600 * 24  # 24 小时后自动删除
```

**第二层：单会话消息数量上限**

```python
MAX_HISTORY_LENGTH = 50  # 每会话最多保留 50 轮对话


async def append_message_with_limit(session_id: str, role: str, content: str) -> None:
    """追加消息，超过上限时裁剪最早的消息"""
    messages = await get_history(session_id)
    messages.append({"role": role, "content": content, "timestamp": time.time()})
    
    # 超过上限：保留最近 N 条
    if len(messages) > MAX_HISTORY_LENGTH:
        messages = messages[-MAX_HISTORY_LENGTH:]
        logger.info("会话消息裁剪 | session_id=%s | kept=%d", session_id, MAX_HISTORY_LENGTH)
    
    await redis.setex(
        f"{SESSION_PREFIX}:{session_id}",
        SESSION_TTL,
        json.dumps(messages, ensure_ascii=False),
    )
```

### 1.2.7 租户级隔离（多租户场景）

如果你的 SaaS 平台有多个租户（如多个企业客户），session_id 仅靠 UUID 不够，需要挂上租户标识：

```python
class ChatRequest(BaseModel):
    tenant_id: str = Field(..., description="租户 ID")
    session_id: str = Field(..., description="会话 ID")

# Redis key 加入租户前缀
def _session_key(tenant_id: str, session_id: str) -> str:
    return f"tenant:{tenant_id}:session:{session_id}"
```

这样即使两个租户碰巧生成了相同的 UUID，key 也不会冲突。

---

## 1.3 接口异步高性能处理

### 1.3.1 Agent 后端的性能瓶颈在哪

一个典型的 Agent 对话请求，耗时分布如下：

```
请求耗时组成（单次对话）：
┌────────────────────────────────────────────────────┐
│ Redis 读取历史        ██ 2ms                        │
│ LLM 第 1 次调用       ██████████████ 2500ms         │
│ 工具调用（查数据库）   ████ 50ms                     │
│ LLM 第 2 次调用       ████████ 1200ms               │
│ 工具调用（查向量库）   ████ 45ms                     │
│ LLM 第 3 次调用       ██████ 800ms                  │
│ Redis 写入历史        ██ 2ms                        │
│                       ─────────────────            │
│ 总计                   ≈ 4600ms                     │
│ 其中 LLM 等待          ≈ 4500ms (98%)               │
│ 其中 Redis + 工具      ≈ 100ms  (2%)                │
└────────────────────────────────────────────────────┘
```

**结论：瓶颈几乎全在 LLM 的网络 I/O 等待。** CPU 和 Redis 操作占比不到 2%。

这意味着性能优化的核心不是"让 CPU 算得更快"，而是"让等待不阻塞其他请求"。这就是异步 I/O 的用武之地。

### 1.3.2 异步的本质：协程不是在"加速"，而是在"填空"

```
同步模式（每请求独占一线程/进程）：
Worker 1: [请求A: 等 LLM ████████████████████] [请求D: 等 LLM ██████████]
Worker 2: [请求B: 等 LLM ██████████████████████████]
Worker 3: [请求C: 等 LLM ██████████]
Worker 4: [空闲]                                   ← 资源浪费

4 个 Worker，同时只能处理 4 个请求。
请求 A~C 的等待时间里，Worker 1~3 的 CPU 在空转，什么也做不了。
```

```
异步模式（事件循环中调度协程）：
单进程:  [A:等LLM] [B:等LLM] [C:等LLM] [D:等LLM] [A:工具] [B:工具] ...
         └─ 等待时立刻切换到下一个任务 ─┘
         └──────── 同时处理 100+ 个请求 ───────────┘

单进程可承载 100+ 并发请求。
因为 98% 的时间在"等网络"，协程调度用这 98% 的空档去处理别的请求。
```

### 1.3.3 全链路异步的四个要求

**要求一：Web 框架和服务器必须异步**

```python
# ✅ uvicorn 原生异步，配合 FastAPI async def
# 启动命令：
# uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
#                                     └──────────────┘
#                                      每个 worker 是独立进程，
#                                      每个进程内有独立的事件循环
```

`--workers 4` 启动 4 个进程，每个进程一个事件循环。总并发能力 = 4 × 单事件循环协程数。一般单进程轻松承载 100+ 并发 LLM 请求。

**要求二：所有 I/O 库必须是异步版本**

```python
# ❌ 同步库——阻塞事件循环
import requests
import redis

resp = requests.post("https://api.openai.com/...", json={...})  # 卡死
r = redis.Redis()
r.get("key")  # 卡死

# ✅ 异步库——让出控制权
from openai import AsyncOpenAI
import redis.asyncio as aioredis

resp = await AsyncOpenAI().chat.completions.create(...)  # 不卡
r = aioredis.from_url("redis://...")
await r.get("key")  # 不卡
```

**要求三：不要在 async 函数里混入同步阻塞调用**

```python
# ❌ 常见错误：在 async 路由里调同步库
@router.post("/chat")
async def chat(request: ChatRequest):
    import time
    time.sleep(2)  # 同步 sleep，阻塞整个事件循环！等同于 sync def
    ...

# ✅ 用异步版本
@router.post("/chat")
async def chat(request: ChatRequest):
    import asyncio
    await asyncio.sleep(2)  # 异步 sleep，不阻塞
    ...
```

如果确实有 CPU 密集型操作（如大文件解析、图像处理），用 `run_in_executor` 扔到线程池：

```python
import asyncio
from concurrent.futures import ThreadPoolExecutor

executor = ThreadPoolExecutor(max_workers=4)

@router.post("/chat")
async def chat(request: ChatRequest):
    loop = asyncio.get_running_loop()
    
    # 耗时 CPU 操作扔到线程池，不阻塞事件循环
    result = await loop.run_in_executor(
        executor,
        heavy_cpu_function,  # 同步函数
        request.message,
    )
    ...
```

**要求四：数据库连接池也必须异步**

```python
# core/database.py
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

engine = create_async_engine(
    "postgresql+asyncpg://user:pass@localhost:5432/agent_db",
    pool_size=20,        # 连接池大小
    max_overflow=10,     # 溢出连接数
    pool_pre_ping=True,  # 拿连接前先 ping 一下，确保连接没断
)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

async def get_db_session():
    async with AsyncSessionLocal() as session:
        yield session
```

### 1.3.4 并发控制：别把 LLM API 打爆

异步不等于无限制并发。LLM API 有速率限制（Rate Limit），并发太高会被 429 封杀。需要两层控制：

**第一层：信号量（Semaphore）限制并发数**

```python
# core/llm.py
import asyncio
from openai import AsyncOpenAI

client = AsyncOpenAI(...)

# 限制同时进行的 LLM 调用不超过 20 个
_llm_semaphore = asyncio.Semaphore(20)


async def call_llm(messages: list[dict], model: str = "gpt-4o") -> str:
    """带有并发控制的 LLM 调用"""
    async with _llm_semaphore:
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
        )
    return response.choices[0].message.content
```

`Semaphore(20)` 确保同时最多 20 个协程在等待 LLM 响应。第 21 个请求会等在 `async with _llm_semaphore` 处，不消耗 LLM API 的并发配额。

**第二层：超时控制**

Agent 多步推理可能无限循环（LLM 反复调同一个工具），或者某次 LLM 调用卡住不返回。每条请求必须设超时：

```python
import asyncio

@router.post("/completions")
async def chat_completions(request: ChatRequest):
    try:
        # 整个请求最多等 60 秒，超时直接返回 503
        result = await asyncio.wait_for(
            _process_chat(request),
            timeout=60.0,
        )
        return result
    except asyncio.TimeoutError:
        logger.warning("请求超时 | session_id=%s", request.session_id)
        raise HTTPException(
            status_code=503,
            detail={"code": -2, "message": "请求超时，请简化您的问题或稍后重试"},
        )
```

### 1.3.5 Agent 内 LLM 调用的并发：工具并行调用

当 Agent 决定同时调用多个工具时（如同时查订单 + 查库存 + 查物流），这些工具调用应该真正并行，而不是串行排队：

```python
# ❌ 串行调用——三个工具依次执行，总耗时 = A + B + C
result_a = await tool_a.invoke(...)
result_b = await tool_b.invoke(...)
result_c = await tool_c.invoke(...)

# ✅ 并行调用——三个工具同时执行，总耗时 = max(A, B, C)
results = await asyncio.gather(
    tool_a.invoke(...),
    tool_b.invoke(...),
    tool_c.invoke(...),
)
```

LangGraph 的 `ToolNode` 默认就支持并行工具调用——当 LLM 一次返回多个 `tool_calls` 时，LangGraph 会自动并行执行。

### 1.3.6 uvicorn 的 worker 数怎么设

```bash
uvicorn main:app --workers 4
```

这个 `--workers 4` 设多少合理？

**经验公式：`workers = (2 × CPU 核数) + 1`**

对于典型的 4 核服务器：`4 × 2 + 1 = 9` 个 worker。

但 Agent 后端的 CPU 负载极低（等 LLM 时 CPU 在发呆），所以 worker 数不需要太多。4 核机器 4~8 个 worker 就足够。

**不要设成 1**——设 1 个 worker，单进程挂了整个服务就挂了。至少 2 个。

### 1.3.7 高并发下的 Redis 连接池

```python
# core/cache.py
import redis.asyncio as aioredis

# 创建连接池，避免每次请求都建立新连接
redis_pool = aioredis.ConnectionPool.from_url(
    "redis://localhost:6379/0",
    max_connections=50,           # 最大连接数
    socket_timeout=5,             # 单次操作超时
    socket_connect_timeout=2,     # 连接超时
    retry_on_timeout=True,        # 超时自动重试一次
)

redis = aioredis.Redis(connection_pool=redis_pool)
```

`max_connections=50` 意味着同时最多 50 个协程在执行 Redis 操作。这通常远超实际需求——Redis 单次操作耗时 < 1ms，50 个连接意味着每秒可处理 50000 次操作。

### 1.3.8 性能监控：知道你的接口有多快

```python
# core/middleware.py
import time
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from core.logger import get_logger

logger = get_logger("performance")


class PerformanceMiddleware(BaseHTTPMiddleware):
    """记录每个请求的耗时，用于性能监控"""
    
    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        
        response = await call_next(request)
        
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "perf | %s %s | status=%d | %.1fms",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        
        return response


# main.py
app.add_middleware(PerformanceMiddleware)
```

有了这个中间件，你随时可以 grep 日志看接口耗时分布：

```bash
grep "perf | POST /v1/chat/completions" app.log | awk '{print $NF}' | sort -n
```

---

## 1.4 完整示例：把三点整合起来

下面是一个完整的对话接口实现，同时覆盖了本节三个核心要点：

```python
# routes/chat.py —— 对话接口完整实现
import json
import asyncio
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from core.agent import agent
from core.session import get_history, save_history, append_message_with_limit
from core.llm import call_llm_with_retry
from core.logger import get_logger
from models.chat import ChatRequest

logger = get_logger(__name__)
router = APIRouter(prefix="/v1/chat", tags=["对话接口"])

REQUEST_TIMEOUT = 60.0  # 单次请求总超时


@router.post("/completions")
async def chat_completions(request: ChatRequest):
    """
    对话补全接口。
    
    功能要点：
    1. stream 参数控制流式/非流式切换
    2. session_id 隔离不同会话的上下文
    3. 全链路异步，支持高并发
    """
    if request.stream:
        handler = _handle_stream(request)
    else:
        handler = _handle_non_stream(request)
    
    try:
        return await asyncio.wait_for(handler, timeout=REQUEST_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("请求超时 | session_id=%s", request.session_id)
        raise HTTPException(status_code=503, detail={"code": -2, "message": "请求超时"})


# ============ 非流式处理 ============

async def _handle_non_stream(request: ChatRequest) -> dict:
    try:
        history = await get_history(request.session_id)
        history.append({"role": "user", "content": request.message})
        
        config = {"configurable": {"thread_id": request.session_id}}
        result = await agent.ainvoke({"messages": history}, config=config)
        
        reply = result["messages"][-1].content
        await append_message_with_limit(request.session_id, "assistant", reply)
        
        logger.info("非流式完成 | session_id=%s | reply_len=%d", request.session_id, len(reply))
        return {"code": 0, "session_id": request.session_id, "reply": reply}
    
    except Exception:
        logger.exception("非流式异常 | session_id=%s", request.session_id)
        raise HTTPException(status_code=500, detail={"code": -99, "message": "服务异常"})


# ============ 流式处理 ============

async def _handle_stream(request: ChatRequest) -> StreamingResponse:
    
    async def event_generator():
        collected: list[str] = []
        try:
            yield _sse("start", {"session_id": request.session_id})
            
            history = await get_history(request.session_id)
            history.append({"role": "user", "content": request.message})
            
            config = {"configurable": {"thread_id": request.session_id}}
            
            async for event in agent.astream_events(
                {"messages": history},
                config=config,
                version="v2",
            ):
                kind = event["event"]
                
                if kind == "on_chat_model_stream":
                    token = event["data"]["chunk"].content
                    if token:
                        collected.append(token)
                        yield _sse("token", {"content": token})
                
                elif kind == "on_tool_start":
                    yield _sse("tool_call", {
                        "tool": event.get("name", "unknown"),
                        "status": "start",
                    })
                
                elif kind == "on_tool_end":
                    yield _sse("tool_call", {
                        "tool": event.get("name", "unknown"),
                        "status": "end",
                    })
            
            full_reply = "".join(collected)
            await append_message_with_limit(request.session_id, "assistant", full_reply)
            
            yield _sse("done", {"session_id": request.session_id})
            yield "data: [DONE]\n\n"
            
            logger.info("流式完成 | session_id=%s | reply_len=%d", request.session_id, len(full_reply))
        
        except Exception as e:
            logger.exception("流式异常 | session_id=%s", request.session_id)
            yield _sse("error", {"message": "生成异常，请重试", "code": -99})
            yield "data: [DONE]\n\n"
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(event_type: str, data: dict) -> str:
    payload = {"type": event_type, **data}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
```

---

## 1.5 常见踩坑清单

| 坑 | 现象 | 原因 | 解法 |
|----|------|------|------|
| 流式变成非流式 | 前端等很久后一次性收到全部文本 | Nginx 缓冲了 SSE 响应 | 响应头加 `X-Accel-Buffering: no` 或 nginx 配 `proxy_buffering off` |
| 流式中途断开 | 用户在手机上看回复看到一半停了 | 移动端代理超时切断了长连接 | SSE 中加入心跳注释行 `": heartbeat\n\n"`，每 15 秒发一次 |
| 不同用户串消息 | A 看到 B 的对话内容 | 前端 session_id 没变，或后端没按 session_id 隔离 | 验证前端传的是否正确 UUID，后端 Redis key 严格拼 `session:{session_id}` |
| 并发上去后 429 | 大量请求被 OpenAI 限流 | 没有并发控制 | 加 `asyncio.Semaphore` 限流 + 重试逻辑 |
| Agent 无限循环 | 一次对话跑了 5 分钟不结束 | LLM 反复调用同一个工具 | Agent config 设 `recursion_limit` 或在路由层设 `asyncio.wait_for` |
| Redis 内存爆了 | 服务跑几天后 Redis OOM | 会话历史只写不删 | 设 TTL + 单会话消息数上限 |
| 流式没反应 | 前端 fetch 一直 pending | SSE 格式不对（少了一个 `\n`） | `data: {json}\n\n` 必须是两个 `\n` |

---

## 1.6 本章小结

| 要点 | 核心原则 | 一句话记住 |
|------|---------|-----------|
| 流式/非流式参数切换 | 一个 `stream` 参数控制两套处理逻辑，共用同一个路由 | **前端传 `stream: true/false`，后端分支处理** |
| session_id 隔离 | 对话历史、Agent 检查点、会话元数据全部以 session_id 为 key 隔离 | **Redis key = `session:{session_id}`，LangGraph config = `thread_id=session_id`** |
| 异步高性能处理 | 全链路 async/await + Semaphore 并发控制 + asyncio.wait_for 超时兜底 | **98% 的时间在等网络，协程用这些空档处理其他请求** |
| SSE 流式格式 | `data: {json}\n\n`，双换行，Nginx 关缓冲 | **格式错一个字符，前端就收不到事件** |
| 异常处理双路径 | 非流式返 HTTP 错误码，流式通过 SSE error 事件 | **流式一旦开始，就不能再返 500，只能通过事件通道报错** |

对话接口是用户接触你的 Agent 的第一触点。它慢 = 你的 Agent 慢，它串线 = 你的 Agent 不可信，它崩 = 你的 Agent 挂了。把这三点做好，是 Agent 后端上生产的第一道门槛。

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
    model: Literal["gpt-5.1", "gpt-5-mini", "claude-sonnet-4-6"] = "gpt-5.1"
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
        
        # 图挂了 checkpointer 时，config 里的 thread_id 是硬要求（见 1.2.5）
        config = {"configurable": {"thread_id": request.session_id}}
        
        # Agent 执行（内部多次 await LLM，全部完成后返回）
        result = await agent.ainvoke({"messages": history}, config=config)
        
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

流式响应的核心是 **Server-Sent Events (SSE)**。HTTP 连接建立后，服务端持续推送 `data:` 开头的文本事件，客户端用 `fetch` + `ReadableStream` 逐条接收。

注意：**`EventSource` 只能发 GET，既不能带请求体也不能带自定义请求头**，无法调用本章的 `POST /v1/chat/completions`（1.1.7 的示例用的就是 `fetch`）。所以"POST + SSE"只能用 `fetch`（或 `XHR`）手工解析数据流；`EventSource` 仅适用于 GET 端点，且它会自动重连（重连语义见 1.1.6）。

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
            
            # 图挂了 checkpointer 时，不传 thread_id 会直接抛：
            # ValueError: Checkpointer requires one or more of the following
            #             'configurable' keys: ['thread_id', ...]
            config = {"configurable": {"thread_id": request.session_id}}
            
            # 使用 astream_events 逐事件获取 Agent 执行过程
            async for event in agent.astream_events(
                {"messages": history},
                config=config,
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
            
            # 发送结束事件（前端收到 type == "done" 即为流结束，不再补 [DONE] 哨兵）
            yield _sse("done", {"session_id": request.session_id})
            
            logger.info("流式对话完成 | session_id=%s | reply_len=%d", request.session_id, len(full_reply))
        
        except Exception as e:
            logger.exception("流式对话异常 | session_id=%s", request.session_id)
            # 异常也要通过 SSE 通知前端，不能直接抛 500
            yield _sse("error", {
                "message": "生成过程出现异常，请重试",
                "code": -99,
            })

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

**别发明 `data: [DONE]`。** `[DONE]` 是 OpenAI 的私有约定，不是 SSE 规范的一部分；上面的 `done` 事件已经承担了"流结束"的语义，再补一条 `data: [DONE]` 只会让前端多出一个必须特判的分支——`JSON.parse('[DONE]')` 会直接抛 `SyntaxError`，而流式解析循环通常没有 try/catch，一抛就是整个流渲染中断。跨厂商对接时以自定义事件类型为准（本章是 `data:` 内嵌的 `{"type": ...}`），不要依赖某家厂商的哨兵值。

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

**细节二：SSE 格式必须严格（而且要完整）**

SSE 的最小格式是 `data: <内容>\n\n`：结尾必须有两个换行符 `\n\n`，少一个前端就不触发事件；`data:` 与内容之间只保留一个空格；JSON 内部不能出现未转义的裸换行。

但"严格"不只这一条，规范里还有几个字段语义需要知道（以及本章为什么不用它们）：

| 字段 | 规范语义 | 本章的选择 |
|------|---------|-----------|
| `data:` | 事件负载。**值里的 `\n` 必须拆成多个 `data:` 行，客户端再按 `\n` 拼回** | 采用（负载是单行 JSON，天然满足） |
| `event:` | 事件类型名，客户端用 `addEventListener("token", ...)` 分派 | **未用**：改用 `data:` 内嵌 `{"type": "token"}`。可行，但等于放弃了 SSE 的事件类型语义——客户端只能一个 `onmessage` 全揽后再自己 if/else |
| `id:` + `retry:` | 事件 ID 与重连间隔；浏览器自动重连时会在请求头带 `Last-Event-ID`，可实现断点续传 | **不依赖**：会话状态在服务端（Redis / checkpointer），前端重连即重新发起请求，不需要重放语义 |
| CORS | 跨域部署时 SSE 同样受 CORS 约束；带 `credentials` 时 `Access-Control-Allow-Origin` **不能是 `*`** | 必须显式白名单来源 + `Access-Control-Allow-Credentials: true`（流式下配错的典型症状是"非流式正常、流式一条事件都收不到"） |

两条最容易翻车的细节：

1. **多行 `data` 不是"随便换行"**。一个事件里的换行必须写成多个 `data:` 行：
   ```
   data: 第一行
   data: 第二行
   ```
   客户端拿到的是 `"第一行\n第二行"`（用 `\n` 拼回）。如果直接在 `data:` 后面塞一个真实换行，第二行会被当作新字段解析，事件负载被静默截断。
2. **`json.dumps` 已经处理了换行**。本节的 `_sse()` 走的是 `json.dumps`，负载里的 `\n` 会被转义成 `\\n`，所以不会踩到多行语义——**不要**为了"看起来整齐"手工拼 SSE 字符串。

**一句话：`data: {json}\n\n` 是下限，不是全部——`event:`/`id:`/`retry:` 的语义、多行 `data` 的拼接方式、跨域 CORS 都得心里有数，才能判断"该用原生事件类型还是自定义 `type` 字段"。**

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
            if (!line.startsWith('data: ')) continue;   // 空行、注释行（": heartbeat"）直接跳过
            const raw = line.slice(6);
            if (raw === '[DONE]') break;                // 兼容 OpenAI 风格的哨兵值（本章后端不发）

            let data;
            try {
                data = JSON.parse(raw);
            } catch {
                continue;   // 非 JSON 行不能让整个流渲染崩掉
            }

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
```

### 1.1.8 客户端断连：取消、清理与 GeneratorExit

用户点了"停止生成"、直接关掉标签页，或者手机端网络切换——这些都会让 `StreamingResponse` 的生成器被关闭。这里有两个高频误解：

**误解一："断连会走 `except Exception`，我在那里收尾就行。"** 不会。生成器被关闭时收到的是 `GeneratorExit`，请求被取消时收到的是 `asyncio.CancelledError`——**自 Python 3.8 起 `CancelledError` 直接继承 `BaseException`**，`except Exception` 捕获不到它们（这是正确行为：取消不该被业务代码吞掉）。也就是说，1.1.5 里那条 `except Exception` → `error` 事件的分支在断连场景下根本不会执行；就算执行了，连接也没了，事件发不出去。

**误解二："客户端断了，上游 LLM 请求会自己停。"** 不会。客户端断开只关掉了下游的 socket，上游那次 `httpx` / LLM 调用还在继续跑（还占着 1.3.4 的信号量），直到它自己生成完——白烧 token。

正确做法有四条，缺一条就会漏资源：

| 要做的事 | 做法 |
|---------|------|
| 区分"正常结束"和"客户端断开" | 捕获 `starlette.requests.ClientDisconnect`，或用 `await request.is_disconnected()` 轮询 |
| 捕获取消信号 | 显式 `except (asyncio.CancelledError, GeneratorExit):`，清理后**原样 raise**，不要吞 |
| 取消上游 | `finally` 里 `await stream_iter.aclose()`（或关掉 `AsyncOpenAI` 的流），让图内还在跑的 LLM 调用一起取消 |
| 收尾历史 | 断连时把已生成的部分标记为"中断"落库，否则 Redis 里只剩 user 消息（半轮），下一轮上下文里变成用户在自言自语 |

```python
import asyncio
import contextlib

from starlette.requests import ClientDisconnect

from core.session import append_message_with_limit
from core.logger import get_logger

logger = get_logger(__name__)


async def event_generator(request, session_id: str, agent, config: dict):
    """带断连清理的 SSE 生成器"""
    stream_iter = None
    collected: list[str] = []
    disconnected = False

    try:
        yield _sse("start", {"session_id": session_id})

        stream_iter = agent.astream_events(
            {"messages": [{"role": "user", "content": request.message}]},
            config=config,
            version="v2",
        )
        async for event in stream_iter:
            if event["event"] == "on_chat_model_stream":
                token = event["data"]["chunk"].content
                if token:
                    collected.append(token)
                    yield _sse("token", {"content": token})

        yield _sse("done", {"session_id": session_id})

    except (asyncio.CancelledError, GeneratorExit):
        # 客户端断开 / 请求被取消：清理后原样抛出，绝不能吞掉取消信号
        disconnected = True
        logger.info("客户端断开，取消生成 | session_id=%s | partial_len=%d",
                    session_id, len(collected))
        raise

    except ClientDisconnect:
        disconnected = True
        logger.info("客户端已断开 | session_id=%s", session_id)

    except Exception:
        logger.exception("流式生成异常 | session_id=%s", session_id)
        with contextlib.suppress(Exception):
            yield _sse("error", {"message": "生成过程出现异常，请重试", "code": -99})

    finally:
        # 1) 取消上游：客户端没了，LLM 请求也该立刻停，否则继续烧 token 还占着信号量
        if stream_iter is not None:
            with contextlib.suppress(Exception):
                await stream_iter.aclose()
        # 2) 断连也要收尾：把半轮回复落库（生产上建议丢给后台任务，
        #    不要在取消路径里 await 主链路的 Redis 连接）
        if disconnected and collected:
            with contextlib.suppress(Exception):
                await append_message_with_limit(session_id, "assistant", "".join(collected))
```

**一句话：`except Exception` 罩不住断连——客户端断开走的是 `GeneratorExit` / `CancelledError`（`BaseException`），必须有单独的捕获分支 + `finally` 里的 `aclose()`，否则"用户已经走了"和"LLM 还在烧钱"会同时成立。**

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
    """追加一条消息到会话历史。

    注意：这是"读 → 改 → 写"三步，本身没有任何原子性——同一 session_id 的并发
    写入会互相覆盖（后写覆盖先写）。串行化方案见 1.2.8。
    """
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

LangGraph 的 `Checkpointer` 用 `thread_id` 来隔离不同会话的 Agent 执行状态。直接把 `session_id` 映射为 `thread_id`。

**先看一个能把生产打挂的写法：**在 `core/agent.py` 的**模块顶层**把 `RedisSaver.from_conn_string("redis://localhost:6379/1")` 的返回值直接赋给 `redis_checkpointer`，再 `graph.compile(checkpointer=redis_checkpointer)`。

这里有两个错，都会在运行时才炸：

1. `from_conn_string` 是 `@contextmanager`（异步版本是 `@asynccontextmanager`），**返回值是 `_GeneratorContextManager`，不是 `RedisSaver`**。必须 `with` 住它（`async with` 也行）才能拿到真正的 saver；模块顶层直接赋值拿到的只是一个"还没进入上下文"的生成器包装对象。
2. **首次使用前必须调 `setup()` 建索引**（RediSearch / RedisJSON，幂等可重复执行），否则不是启动就报错，而是第一次写检查点时才炸。另外这条路依赖 **Redis 8+ / Redis Stack**（要 RediSearch 模块），普通 Redis 装不上索引。

正确做法：**把 saver 的生命周期交给应用的 lifespan（startup），在 `with` 里 `setup()` 之后再注入，不要在模块顶层赋值。**

```python
# core/agent.py —— 只负责构图；checkpointer 由外部注入
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import StateGraph, MessagesState
from langgraph.graph.state import CompiledStateGraph


def build_agent(checkpointer: BaseCheckpointSaver | None = None) -> CompiledStateGraph:
    """返回编译后的图。

    注意注解：compile() 返回的是 CompiledStateGraph（一个 Runnable），
    而不是 StateGraph —— 后者只是"待编译的图定义"。
    checkpointer=None 表示不挂检查点（路线 A：历史由 Redis 承载，见 1.4 的契约）。
    """
    graph = StateGraph(MessagesState)
    # ... 定义节点和边 ...
    return graph.compile(checkpointer=checkpointer)
```

```python
# main.py —— 生命周期：startup 建 saver，shutdown 自动关闭连接
from contextlib import asynccontextmanager

from fastapi import FastAPI
from langgraph.checkpoint.redis import RedisSaver

from core.agent import build_agent

REDIS_CHECKPOINT_URL = "redis://localhost:6379/1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 开发环境想快速起步就用内存版（重启即丢）：
    #   from langgraph.checkpoint.memory import InMemorySaver
    #   app.state.agent = build_agent(InMemorySaver())
    async with RedisSaver.from_conn_string(REDIS_CHECKPOINT_URL) as saver:
        saver.setup()                          # 建索引，幂等
        app.state.checkpointer = saver
        app.state.agent = build_agent(saver)   # 图在这里才拿到真正的 saver 实例
        yield                                  # 退出 lifespan 时连接一并关闭


app = FastAPI(lifespan=lifespan)
```

内存版 checkpointer 的正式名是 `InMemorySaver`（`langgraph.checkpoint.memory`），老文档/老代码里的 `MemorySaver` 只是它保留的兼容别名，LangGraph 1.x 起统一用 `InMemorySaver`。

路由层通过 `request.app.state.agent`（或 startup 时注入的单例）拿到编译好的图，不要依赖模块顶层的 `agent = build_agent()`——导入期既没有可用的事件循环，也进不去 `from_conn_string` 的上下文。

```python
# routes/chat.py 中调用 Agent 时传入 thread_id
# agent 在 lifespan 里构建并注入，这里省略获取过程
config = {"configurable": {"thread_id": request.session_id}}

result = await agent.ainvoke(
    {"messages": [{"role": "user", "content": request.message}]},
    config=config,  # ← 关键：LangGraph 用 thread_id 隔离状态
)
# ...
```

**带 checkpointer 的图，`thread_id` 是硬要求**：不带 `config={"configurable": {"thread_id": ...}}` 调用，会直接抛

```
ValueError: Checkpointer requires one or more of the following 'configurable' keys: ['thread_id', ...]
```

所以本章所有 `ainvoke` / `astream_events` 调用都带了这个 config（1.1.4、1.1.5、1.4 的示例都已补上）。

这样 LangGraph 自动以 `thread_id`（即 `session_id`）为 key 存储 Agent 的检查点状态，不同用户的 Agent 中间状态完全隔离。

> **与历史存储的分工**：上面这个片段每轮只传"本条新消息"，历史由 checkpointer 按 `thread_id` 恢复——这是"checkpointer 承载历史"的接线方式。1.4 的完整示例走的是另一条路（Redis 的 `session:{session_id}` 承载历史）。**一份历史只能有一个 owner**，两条路线的差别和混用的后果写在 1.4 开头的契约里。

### 1.2.6 防止 session 数据无限膨胀

Redis 内存不是无限的。需要两层防护：

**第一层：TTL 自动过期（上面已配）**

```python
SESSION_TTL = 3600 * 24  # 24 小时后自动删除
```

**第二层：单会话消息数量上限**

裁剪不是"取尾部 N 条"那么简单——**按条裁剪会切在轮中间**，留下一条以 `tool` 开头的消息（缺了前一条带 `tool_calls` 的 assistant）或一个孤立的 `tool_calls`，这样的历史发给 OpenAI 直接 400。所以必须**按整轮裁剪**：

```python
MAX_HISTORY_LENGTH = 50  # 每会话最多保留 50 条消息（user + assistant 各算一条，≈25 轮）


def trim_history(messages: list[dict], max_messages: int = MAX_HISTORY_LENGTH) -> list[dict]:
    """按"整轮"裁剪历史，绝不在轮中间切开。

    一轮 = 一条 user 消息 + 它之后到下一个 user 之前的所有消息
    （assistant 可能带 tool_calls，后面还跟着对应的 tool 结果消息）。
    切割点只能落在 user 消息上，这样 assistant(tool_calls)/tool 的配对永远完整。
    """
    if len(messages) <= max_messages:
        return messages

    turn_starts = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if not turn_starts:
        # 全是 assistant/tool 的残缺历史：直接丢掉，别拿去喂 LLM
        return []

    # 从最后一轮往前整轮累加，直到再加一轮就超上限
    kept_from = turn_starts[-1]
    for start in reversed(turn_starts):
        if len(messages) - start > max_messages:
            break
        kept_from = start
    return messages[kept_from:]


async def append_message_with_limit(session_id: str, role: str, content: str) -> None:
    """追加消息，超过上限时按整轮裁剪最早的消息"""
    messages = await get_history(session_id)
    messages.append({"role": role, "content": content, "timestamp": time.time()})

    trimmed = trim_history(messages)
    if len(trimmed) != len(messages):
        logger.info("会话消息裁剪 | session_id=%s | %d → %d 条",
                    session_id, len(messages), len(trimmed))

    await redis.setex(
        f"{SESSION_PREFIX}:{session_id}",
        SESSION_TTL,
        json.dumps(trimmed, ensure_ascii=False),
    )
```

三条纪律：

1. **条数上限不是轮数上限**：`MAX_HISTORY_LENGTH = 50` 是 50 **条**（≈25 轮）。按轮计数要自己数 `role == "user"` 的条数，别把常量名当注释用。
2. **截断必须保护 `assistant(tool_calls)` / `tool` 配对**：切割点只能落在 user 消息上，理由见 `trim_history` 的 docstring。
3. **条数裁剪挡不住"单轮超长"**：一次工具返回几万 token 的 RAG 结果，条数再少也会超上下文窗口。这时要按 token 预算裁剪（`count_message_tokens`）或做摘要压缩（`compress_conversation`），把早期轮次压成一段摘要而不是直接丢掉。

**一句话：裁剪的单位是"轮"不是"条"——切在轮中间的历史不是"少了一点上下文"，而是直接让接口 400。**

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

### 1.2.8 同一 session 的并发写入：读改写必须串行化

1.2.4 的 `append_message` 和 1.2.6 的 `append_message_with_limit` 都是"get → 改 → setex"的无锁读改写。单用户单标签页时看不出问题，一旦同一个 `session_id` 出现并发写，就是"后写覆盖先写"：

```
时间 →
标签页 A : get(3 条) ── 改 ──────────────────────────── setex(4 条)
标签页 B :              get(3 条) ── 改 ─── setex(4 条)        ← 覆盖了 A
后台任务 :                       get(3 条) ── 改 ─── setex(4 条) ← 又覆盖一次
结果：实际写入 6 条，Redis 里只剩 4 条 —— 静默丢消息，日志里看不到任何异常
```

三个并发来源要认全：

| 来源 | 场景 |
|------|------|
| 同一用户多标签页 / 多设备 | 用户在两处同时发消息 |
| 后台任务与主链路 | 定时总结、异步记忆抽取与对话同时写同一个会话 |
| 重试与重放 | 网关超时重试，同一个 `session_id` 的请求打进来两遍 |

串行化的三种做法（按推荐顺序）：

| 方案 | 做法 | 代价 |
|------|------|------|
| **原子追加（首选）** | 不用"一个 key 存全量 JSON"，改用 Redis 原生结构：`RPUSH session:{id}:msgs` + `LTRIM` 保留尾部 N 条；或用 Stream（`XADD session:{id}:stream *`，天生带 ID、可回放、可按长度裁剪） | 读取侧要从 `LRANGE` / `XRANGE` 拼装，Key 结构变了 |
| **Redis 分布式锁** | `SET lock:session:{id} <uuid> NX PX 3000`，把"get → 改 → setex"整段放进锁内，解锁用 Lua 校验 uuid | 多一次 RTT；**必须设过期时间**，否则持有者崩溃就死锁 |
| **单消费者队列** | 写入投递到队列，由消费者按 `session_id` 分区串行落库 | 多一个组件，链路变长，延迟上升 |

**与 checkpointer `thread_id` 的关系：** `thread_id` 提供的是**不同会话之间的隔离**，不提供**同一会话内的写入顺序**——LangGraph 写自己的 checkpoint 同样是读改写，一样怕并发。所以不管历史放在 Redis 还是交给 checkpointer，同一 `session_id` 的并发控制都得自己写。最省事的做法是在接口层就把它挡住：按 `session_id` 加锁，或者对同一 session 的并发请求直接返回 409（前端按"请等待上一条回复"提示），让同一会话在任何时刻只有一个写入者。

**一句话：隔离靠 key（`session_id` / `thread_id`），不冲突靠锁——key 负责"不同会话互不干扰"，锁负责"同一会话不互相覆盖"，两者不能互相替代。**

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


def heavy_cpu_function(text: str) -> str:
    """同步的 CPU 密集函数（示例）"""
    return text.upper()


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


async def call_llm(messages: list[dict], model: str = "gpt-5.1") -> str:
    """带有并发控制的 LLM 调用"""
    async with _llm_semaphore:
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
        )
    return response.choices[0].message.content
```

`Semaphore(20)` 确保同时最多 20 个协程在等待 LLM 响应。第 21 个请求会等在 `async with _llm_semaphore` 处，不消耗 LLM API 的并发配额。

**但这个信号量只在单进程内有效。** 如果按 1.3.6 用 `uvicorn --workers 4` 启动，每个 worker 是独立进程、独立事件循环，各自持有一个 `Semaphore(20)`，全局并发上限就变成 `20 × 4 = 80`——限流实际失效，429 照旧。

| 部署形态 | 进程内 `Semaphore(20)` 的实际全局上限 |
|---------|-----------------------------------|
| `--workers 1` | 20 |
| `--workers 4` | 80 |
| `--workers 9` | 180 |

跨进程限流必须挪到 Redis（固定窗口计数器 / 漏桶 / 令牌桶，用 Lua 保证原子性），或者干脆 `--workers 1` + 进程内信号量——**二选一，不要既多 worker 又指望进程内信号量兜底**。Redis 计数器版本的思路：

```python
# core/llm.py —— 跨进程并发闸门（所有 worker 共享同一个计数）
INFLIGHT_KEY = "llm:inflight"

# 进入时 INCR，超过上限就放回（DECR）并等待/拒绝；离开时 DECR
# 生产建议写成 Lua 脚本，保证"判断 + INCR"原子；这里用 pipeline 示意
MAX_INFLIGHT = 20


async def call_llm_with_gate(messages: list[dict], model: str = "gpt-5.1") -> str:
    """跨进程生效的并发控制：在飞请求数超过 MAX_INFLIGHT 就直接拒绝"""
    inflight = await redis.incr(INFLIGHT_KEY)
    if inflight > MAX_INFLIGHT:
        await redis.decr(INFLIGHT_KEY)
        raise RuntimeError("LLM 并发已满，请稍后重试")
    try:
        response = await client.chat.completions.create(model=model, messages=messages)
        return response.choices[0].message.content
    finally:
        await redis.decr(INFLIGHT_KEY)   # 必须 finally，否则计数只增不减
```

**第二层：超时控制**

Agent 多步推理可能无限循环（LLM 反复调同一个工具），或者某次 LLM 调用卡住不返回。每条请求必须设超时——但**流式和非流式的超时机制不一样，不能共用一个 `asyncio.wait_for`**：

```python
import asyncio

@router.post("/completions")
async def chat_completions(request: ChatRequest):
    if request.stream:
        # 流式：这里套 wait_for 是无效的，超时放到生成器内部（见下）
        return await _handle_stream(request)

    try:
        # 非流式：整个请求最多等 60 秒，超时直接返回 503
        return await asyncio.wait_for(
            _handle_non_stream(request),
            timeout=REQUEST_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("请求超时 | session_id=%s", request.session_id)
        raise HTTPException(
            status_code=503,
            detail={"code": -2, "message": "请求超时，请简化您的问题或稍后重试"},
        )
```

**为什么 `wait_for` 罩不住流式：** `_handle_stream` 只是"构造并返回 `StreamingResponse`"——生成器的函数体要到响应发送阶段才被执行，所以 `wait_for(_handle_stream(request), timeout=...)` 计时的只是构造对象这一步（微秒级），真正的 LLM 生成过程完全不受约束。而且流式根本没法用 `wait_for` 兜：一旦开始推送，HTTP 200 早已发出，超时能做的只有"发一个 error 事件然后收流"，而不是抛 503。

流式超时要分三层设，各管一段：

| 层次 | 手段 | 拦住什么 |
|------|------|---------|
| 图内 | `config={"configurable": {...}, "recursion_limit": 25}` | LLM 反复调同一个工具导致的死循环 |
| LLM 客户端 | `AsyncOpenAI(timeout=httpx.Timeout(30.0, connect=5.0), max_retries=2)` | 单次调用卡住不返回（首字节超时 + 总时长超时） |
| 生成器 | 逐事件 `wait_for`（下例）或 `asyncio.timeout` 包住整段；配合每 15 秒一条心跳注释行 | 总时长兜底，并让前端知道"流还活着" |

```python
import asyncio

EVENT_TIMEOUT = 30.0   # 相邻两个事件之间的最长间隔（也覆盖首 token）
REQUEST_TIMEOUT = 60.0  # 整段生成的总时长上限


async def stream_with_timeout(agent, history: list[dict], config: dict):
    """流式超时的可行做法：在生成器内部逐事件计时"""
    stream_iter = agent.astream_events({"messages": history}, config=config, version="v2")
    try:
        # 总时长看门狗：asyncio.timeout 包住整个生成过程（Python 3.11+ 可用）
        async with asyncio.timeout(REQUEST_TIMEOUT):
            while True:
                try:
                    event = await asyncio.wait_for(
                        stream_iter.__anext__(),
                        timeout=EVENT_TIMEOUT,
                    )
                except StopAsyncIteration:
                    break
                yield event
    except TimeoutError:
        # 流已开始，HTTP 状态码改不了：只能发事件再收流
        yield _sse("error", {"message": "生成超时，请重试", "code": -2})
    finally:
        await stream_iter.aclose()   # 把图里还在跑的 LLM 调用一并取消
```

两个配套细节：

- **心跳不能少**：长时间没有事件（比如工具在跑一个 20 秒的查询）时，Nginx / 网关会按"连接空闲"掐断。生成器里加一个每 15 秒 yield 一次的 `": heartbeat\n\n"`（SSE 注释行，客户端自动忽略），既是保活也是超时看门狗的信号。
- **`recursion_limit` 是图内的硬刹车**：它比"总时长超时"更早生效，也更便宜——死循环在第 25 步就被拒绝，而不是等 60 秒后才发现。

**一句话：`wait_for` 只能罩住"非流式的整段处理"；流式必须在生成器内部计时（逐事件 `wait_for` 或 `asyncio.timeout`），外加图内 `recursion_limit` 和 LLM 客户端超时，三层叠加才叫"有超时"。**

### 1.3.5 Agent 内 LLM 调用的并发：工具并行调用

当 Agent 决定同时调用多个工具时（如同时查订单 + 查库存 + 查物流），这些工具调用应该真正并行，而不是串行排队。

先记住工具的调用入口：**同步入口是 `invoke`（直接返回结果，不是 awaitable，await 它必然 TypeError），异步入口是 `ainvoke`**。在 `async def` 链路里一律用 `ainvoke`：

```python
import asyncio

from langchain_core.tools import tool


@tool
async def tool_a(order_id: str) -> dict:
    """查订单"""
    return {"order_id": order_id, "status": "paid"}


@tool
async def tool_b(sku: str) -> dict:
    """查库存"""
    return {"sku": sku, "stock": 12}


@tool
async def tool_c(order_id: str) -> dict:
    """查物流"""
    return {"order_id": order_id, "carrier": "SF"}


order_id = "SO-2026-0001"
sku = "SKU-42"

# ❌ 串行调用——三个工具依次执行，总耗时 = A + B + C
result_a = await tool_a.ainvoke({"order_id": order_id})
result_b = await tool_b.ainvoke({"sku": sku})
result_c = await tool_c.ainvoke({"order_id": order_id})

# ✅ 并行调用——三个工具同时执行，总耗时 = max(A, B, C)
results = await asyncio.gather(
    tool_a.ainvoke({"order_id": order_id}),
    tool_b.ainvoke({"sku": sku}),
    tool_c.ainvoke({"order_id": order_id}),
)
```

LangGraph 的 `ToolNode` 默认就支持并行工具调用——当 LLM 一次返回多个 `tool_calls` 时，LangGraph 会自动并行执行。

**一句话：异步链路里工具一律走 `ainvoke`（`invoke` 是同步入口，返回的是结果不是协程），多个互不依赖的工具用 `asyncio.gather` 并发，总耗时才从"求和"变成"取最大"。**

### 1.3.6 uvicorn 的 worker 数怎么设

```bash
uvicorn main:app --workers 4
```

这个 `--workers 4` 设多少合理？

**经验公式：`workers = (2 × CPU 核数) + 1`**

对于典型的 4 核服务器：`4 × 2 + 1 = 9` 个 worker。

但 Agent 后端的 CPU 负载极低（等 LLM 时 CPU 在发呆），所以 worker 数不需要太多。4 核机器 4~8 个 worker 就足够。

**不要设成 1**——但理由不是"挂了没人管"：`uvicorn --workers N` 由父进程 fork 出 worker 并负责监管，单个 worker 崩溃会被自动重启，设 1 并不会让服务整体消失。真正的理由有两条：

1. **单核 CPU 用不满**：一个 worker 只有一个事件循环、基本绑在一个 CPU 核上，多核机器的其余核心闲置。Agent 的 CPU 负载虽低，但 SSE 分片、JSON 编解码、鉴权中间件、日志序列化都要算在核上。
2. **滚动重启期间零可用**：改代码重新部署时，1 个 worker 只能先停再起，这中间有一段没有任何进程监听端口的窗口；2 个以上可以滚动重启（先起新的、再优雅摘掉旧的），用户无感。

**注意 worker 数与 1.3.4 限流的关系：** worker 数一变，进程内 `Semaphore(20)` 就不再等于全局并发上限（`20 × N`）。要么把限流放 Redis、要么 `--workers 1` + 进程内信号量——**二选一**，别既开多 worker 又指望进程内信号量兜底。

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

下面是一个完整的对话接口实现，同时覆盖了本节三个核心要点。

**先立契约：一份对话历史只能有一个 owner。** 本章给两条路线，一个项目只能选其中一条：

| 路线 | 谁是历史 owner | 每轮传给图的内容 | 图的编译方式 | Redis 存什么 |
|------|---------------|-----------------|-------------|-------------|
| **A（1.1、1.4 采用）** | Redis `session:{session_id}` | 完整历史 + 本条 user 消息 | `build_agent()`（**不挂** checkpointer） | 对话历史 + 元数据 |
| **B（1.2.5 的接线）** | checkpointer（`thread_id = session_id`） | 只传本条 user 消息，历史由 `thread_id` 恢复 | `build_agent(saver)` | 仅元数据（model、token 计数、last_active） |

为什么不能混：`MessagesState` 的 `messages` 是 `add_messages` 语义（追加合并）。路线 A 每轮都传完整历史，如果图又挂了 checkpointer，同一批消息会被一轮轮重复追加进 checkpoint（第 2 轮 3 条、第 3 轮 5 条……），上下文和费用一起膨胀。

选定 owner 之后，落库规则同样只有一条：**user 与 assistant 必须成对写入**。把 user 只 append 在局部变量里、最后只写 assistant，结果是 Redis 里只剩 assistant 消息——下一轮读出来的历史就是"AI 的自言自语"（`save_history` 这类函数也就永远不会被调用）。下面的代码按路线 A 写：user 在调图前落库，assistant 在返回后落库。

```python
# routes/chat.py —— 对话接口完整实现
import asyncio
import contextlib
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from core.session import get_history, append_message_with_limit
from core.logger import get_logger
from models.chat import ChatRequest

logger = get_logger(__name__)
router = APIRouter(prefix="/v1/chat", tags=["对话接口"])

REQUEST_TIMEOUT = 60.0  # 非流式请求总超时
EVENT_TIMEOUT = 30.0    # 流式：相邻两个事件之间的最长间隔（含首 token）


@router.post("/completions")
async def chat_completions(request: ChatRequest, http_request: Request):
    """
    对话补全接口。
    
    功能要点：
    1. stream 参数控制流式/非流式切换
    2. session_id 隔离不同会话的上下文
    3. 全链路异步，支持高并发
    """
    # agent 在 lifespan 里构建并注入（见 1.2.5），不要依赖模块顶层的全局实例
    agent = http_request.app.state.agent

    if request.stream:
        # 流式不能在这里套 wait_for：它只能罩住"构造 StreamingResponse"这一步，
        # 生成过程要到响应发送阶段才跑（见 1.3.4）
        return await _handle_stream(request, agent)
    
    try:
        return await asyncio.wait_for(
            _handle_non_stream(request, agent),
            timeout=REQUEST_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("请求超时 | session_id=%s", request.session_id)
        raise HTTPException(status_code=503, detail={"code": -2, "message": "请求超时"})


# ============ 非流式处理 ============

async def _handle_non_stream(request: ChatRequest, agent) -> dict:
    try:
        history = await get_history(request.session_id)
        history.append({"role": "user", "content": request.message})

        # user 先落库：与下面的 assistant 成对（见本节开头的契约）
        await append_message_with_limit(request.session_id, "user", request.message)

        # 路线 A 下这行 config 是可选的（图不挂 checkpointer）；
        # 切到路线 B 时它是硬要求（缺 thread_id 直接 ValueError，见 1.2.5）
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

async def _handle_stream(request: ChatRequest, agent) -> StreamingResponse:
    
    async def event_generator():
        collected: list[str] = []
        disconnected = False
        try:
            yield _sse("start", {"session_id": request.session_id})
            
            history = await get_history(request.session_id)
            history.append({"role": "user", "content": request.message})
            
            # user 先落库：与收尾的 assistant 成对（见本节开头的契约）
            await append_message_with_limit(request.session_id, "user", request.message)
            
            config = {"configurable": {"thread_id": request.session_id}}
            
            # 逐事件取流：相邻事件间隔超时就收流 —— 这才是真正罩住 LLM 生成过程的超时
            # （wait_for 只对非流式分支有效，原因见 1.3.4）
            stream_iter = agent.astream_events(
                {"messages": history},
                config=config,
                version="v2",
            )
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(
                            stream_iter.__anext__(),
                            timeout=EVENT_TIMEOUT,
                        )
                    except StopAsyncIteration:
                        break
                    
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
            except asyncio.TimeoutError:
                # 流已开始、HTTP 200 早已发出：只能发事件后收流，不能返 503
                logger.warning("流式生成超时 | session_id=%s", request.session_id)
                yield _sse("error", {"message": "生成超时，请重试", "code": -2})
                return
            finally:
                await stream_iter.aclose()   # 取消图内仍在跑的 LLM 调用
            
            full_reply = "".join(collected)
            await append_message_with_limit(request.session_id, "assistant", full_reply)
            
            # 前端收到 type == "done" 即收尾，不再发送 [DONE] 哨兵
            yield _sse("done", {"session_id": request.session_id})
            
            logger.info("流式完成 | session_id=%s | reply_len=%d", request.session_id, len(full_reply))
        
        except (asyncio.CancelledError, GeneratorExit):
            # 客户端断连：CancelledError 继承 BaseException，不会进入下面的 except Exception，
            # 这里是唯一的清理钩子；清理后原样抛出，不能吞（见 1.1.8）
            disconnected = True
            logger.info("客户端断开 | session_id=%s | partial_len=%d",
                        request.session_id, len(collected))
            raise
        
        except Exception:
            logger.exception("流式异常 | session_id=%s", request.session_id)
            with contextlib.suppress(Exception):
                yield _sse("error", {"message": "生成异常，请重试", "code": -99})
        
        finally:
            # 断连也要收尾：否则 Redis 里只剩 user 消息（半轮），
            # 下一轮上下文里就变成"用户在自言自语"。生产上建议丢给后台任务落库，
            # 不要在取消路径里 await 主链路的 Redis 连接。
            if disconnected and collected:
                logger.warning("断连收尾 | session_id=%s | 已生成 %d 字符未写入历史",
                               request.session_id, len(collected))
    
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
| 并发上去后 429 | 大量请求被 OpenAI 限流 | 没有并发控制，或限流只写在进程内 | 跨进程限流放 Redis（漏桶/令牌桶）；`--workers 1` + `asyncio.Semaphore` 二选一 |
| Agent 无限循环 | 一次对话跑了 5 分钟不结束 | LLM 反复调用同一个工具 | Agent config 设 `recursion_limit`；`asyncio.wait_for` 只对非流式有效，流式按 1.3.4 在生成器内部逐事件超时 |
| 流式超时形同虚设 | 设置了 60 秒超时，流式请求跑了 10 分钟 | `wait_for` 只罩住"构造 StreamingResponse" | 生成器内部逐事件 `wait_for` / `asyncio.timeout` + LLM 客户端 timeout |
| Redis 内存爆了 | 服务跑几天后 Redis OOM | 会话历史只写不删 | 设 TTL + 按整轮裁剪的单会话上限 |
| 历史裁剪后接口 400 | 报 `tool` 消息缺少前置 `tool_calls` | 按条裁剪切断了 assistant(tool_calls)/tool 配对 | 按整轮（user 起）裁剪，见 1.2.6 |
| 同一会话丢消息 | 多标签页同时发消息，先发的被覆盖 | `append_message` 是无锁"读—改—写" | 按 `session_id` 加锁或改用 `RPUSH`/Stream 原子追加，见 1.2.8 |
| 断连后仍在烧 token | 用户关了页面，LLM 请求还在跑 | `CancelledError` / `GeneratorExit` 不走 `except Exception`，上游流没被取消 | 单独捕获取消分支 + `finally` 里 `aclose()`，见 1.1.8 |
| 流式没反应 | 前端 fetch 一直 pending | SSE 格式不对（少了一个 `\n`） | `data: {json}\n\n` 必须是两个 `\n`（本章后端不再发 `[DONE]`） |

---

## 1.6 本章小结

| 要点 | 核心原则 | 一句话记住 |
|------|---------|-----------|
| 流式/非流式参数切换 | 一个 `stream` 参数控制两套处理逻辑，共用同一个路由 | **前端传 `stream: true/false`，后端分支处理** |
| session_id 隔离 | 对话历史、Agent 检查点、会话元数据全部以 session_id 为 key 隔离 | **Redis key = `session:{session_id}`，LangGraph config = `thread_id=session_id`** |
| 会话历史的归属 | 一份历史只有一个 owner：Redis（全量传入图）或 checkpointer（只传新增消息），二选一 | **既传完整历史又挂 checkpointer，checkpoint 里的消息会一轮轮重复膨胀** |
| 同一 session 并发写 | key 负责隔离，锁负责顺序；读—改—写必须串行化 | **隔离靠 `session_id`/`thread_id`，不互相覆盖靠锁，两者不能互相替代** |
| 异步高性能处理 | 全链路 async/await + 并发控制（跨进程必须放 Redis）+ 非流式 `wait_for`、流式生成器内超时 | **98% 的时间在等网络，协程用这些空档处理其他请求；但 `wait_for` 罩不住流式生成过程** |
| SSE 流式格式 | `data: {json}\n\n`，双换行，Nginx 关缓冲；不发明 `[DONE]` | **格式错一个字符，前端就收不到事件；`[DONE]` 是厂商私有约定，不是 SSE 规范** |
| 断连与取消 | `CancelledError`/`GeneratorExit` 不走 `except Exception`，取消要在 `finally` 里 `aclose()` 上游 | **客户端走了，上游 LLM 不会自己停——取消必须显式做** |
| 异常处理双路径 | 非流式返 HTTP 错误码，流式通过 SSE error 事件 | **流式一旦开始，就不能再返 500，只能通过事件通道报错** |

对话接口是用户接触你的 Agent 的第一触点。它慢 = 你的 Agent 慢，它串线 = 你的 Agent 不可信，它崩 = 你的 Agent 挂了。把这三点做好，是 Agent 后端上生产的第一道门槛。

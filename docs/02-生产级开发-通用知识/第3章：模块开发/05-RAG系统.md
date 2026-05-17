# 第五章：RAG 系统

> **核心论点**：RAG 不是"把文档切片塞进向量库然后检索"就完事了。海量文本下检索准不准、召回全不全、检索结果怎么喂给 LLM、用户问题本身质量差怎么办——这四个问题决定了 RAG 系统是从"能跑"到"好用"的距离。本章逐一拆解 LangChain + Milvus 构建生产级 RAG 的完整链路。

---

## 5.1 RAG 是什么——先纠正一个常见误解

### 5.1.1 RAG 不是"知识库搜索"

很多教程把 RAG 讲成："把你的 PDF 丢进去，然后可以问它问题"。这在 demo 级别是成立的，但在生产环境远远不够。

真正的 RAG 系统需要回答几个硬问题：

- 文档切多长？切在哪？切完之后上下文断裂怎么办？
- 用户问"怎么退款"，文档里写的是"退款流程"，检索能匹配到吗？
- 1000 万篇文档，怎么保证 200ms 内返回最相关的 5 篇？
- 检索到的文档里 3 篇相关、2 篇不相关，LLM 会被带偏吗？
- 用户问题本身很短、很模糊（如"那个东西怎么用"），怎么搜得到？

**RAG 系统的工程质量，80% 在检索，20% 在生成。**

### 5.1.2 完整 RAG 链路

```
                         文档入库（离线）                    查询（在线）
                         ════════════                    ════════
                                                              
  PDF/网页/数据库                                         用户问题
       │                                                     │
       ▼                                                     ▼
  ┌──────────┐                                        ┌──────────────┐
  │ 文档解析  │                                        │ Prompt 改写   │
  │ 清洗切块  │                                        │ 扩展/分解/澄清│
  └────┬─────┘                                        └──────┬───────┘
       │                                                     │
       ▼                                                     ▼
  ┌──────────┐                                        ┌──────────────┐
  │ Embedding │                                        │ 多路检索      │
  │ 向量化    │                                        │ 稠密+稀疏+过滤│
  └────┬─────┘                                        └──────┬───────┘
       │                                                     │
       ▼                                                     ▼
  ┌──────────┐                                        ┌──────────────┐
  │ Milvus   │◄──────────── 向量相似度搜索 ────────────│ 重排序       │
  │ 向量存储  │                                        │ Reranker     │
  └──────────┘                                        └──────┬───────┘
                                                             │
                                                             ▼
                                                      ┌──────────────┐
                                                      │ LLM 生成     │
                                                      │ 检索结果+问题 │
                                                      │ → 最终回复   │
                                                      └──────────────┘
```

---

## 5.2 文档入库：切块策略是检索质量的第一关

### 5.2.1 切块不是越小越好，也不是越大越好

```
切太细（每块 50 token）：
  块1: "退款政策适用于"
  块2: "购买后 7 天内的"
  块3: "未开封商品。"
  → 每块信息不完整，检索到的块 LLM 看不懂

切太粗（每块 2000 token）：
  块1: 包含了退款、发货、换货、保修...（一大段混合内容）
  → 检索时相关和不相关的全混在一起，LLM 注意力被稀释
```

**经验值：每块 300~800 token，保证"一块 = 一个完整语义单元"。**

### 5.2.2 切块的三个工程要点

**要点一：按语义边界切，不按字符数机械切。**

```python
# ❌ 按固定字符数硬切
text = "..."  # 一篇文档
chunks = [text[i:i+500] for i in range(0, len(text), 500)]
# 块可能断在句子中间："...根据公司政策，用户可以在\n购买" ← 断了


# ✅ 用 LangChain 的语义感知切分器
from langchain_text_splitters import RecursiveCharacterTextSplitter

splitter = RecursiveCharacterTextSplitter(
    separators=["\n\n", "\n", "。", ".", " ", ""],  # 优先级从高到低
    chunk_size=500,         # 目标大小（token 数）
    chunk_overlap=80,       # 相邻块重叠 80 token
    length_function=len,    # 生产环境用 tiktoken 计数
)

chunks = splitter.split_text(document)
```

`RecursiveCharacterTextSplitter` 的处理逻辑：先尝试在段落边界（`\n\n`）切 → 如果还太长，找句子边界（`。`）切 → 还不够，找空格切 → 最后才硬切字符。这保证切出来的块尽量是完整的语义单元。

**要点二：chunk_overlap 解决"边界信息断裂"。**

```
chunk_overlap = 0（不重叠）：
  块1: "...用户需要提供订单号和"       ← 句尾被切断
  块2: "购买凭证才能办理退款。"         ← 句首孤立
  → 两句都不完整，检索时谁也匹配不到"退款需要什么材料"

chunk_overlap = 80（重叠 80 字符）：
  块1: "...用户需要提供订单号和购买凭证才能办理退款。"
  块2: "...购买凭证才能办理退款。用户还需要填写退款申请表。"
  → 核心信息在重叠区，两块都能命中
```

**overlap 的经验值：chunk_size 的 10%~20%。** 500 字的块，重叠 50~80 字。

**要点三：保留元数据——检索到块后要知道它来自哪篇文档。**

```python
from langchain_core.documents import Document

chunks_with_meta = []
for doc_id, text in enumerate(raw_documents):
    for i, chunk in enumerate(splitter.split_text(text)):
        chunks_with_meta.append(Document(
            page_content=chunk,
            metadata={
                "doc_id": f"doc_{doc_id:04d}",
                "chunk_index": i,
                "source": f"knowledge_base/{doc_id}.txt",
                "title": document_titles[doc_id],
                "category": document_categories[doc_id],
                "updated_at": document_dates[doc_id],
            },
        ))
```

metadata 的价值在后面：检索时可按来源、类别、时间过滤；生成时可以用 title 和 source 做引用标注。

### 5.2.3 多级索引：短摘要 + 长文档分层检索

当文档很长（如合同、论文），单个 chunk 无法代表全文。需要两层索引：

```
第一层：文档级摘要（粗筛）
  Doc A 摘要: "XX公司退款政策，含7天无理由、退货流程、运费承担规则"
  Doc B 摘要: "XX公司发货政策，含发货时效、缺货处理、物流查询"
  
第二层：块级详细内容（精排）
  Doc A - Chunk 1: "退款条件：购买后7天内..."
  Doc A - Chunk 2: "退款流程：填写申请表 → 审核 → 原路退回..."
```

检索时先在第一层找相关文档（缩小范围），再在命中的文档里做块级精确检索。这在百万级文档库中效果显著。

```python
# Milvus 中建立两个 Collection
# Collection 1: doc_summaries（文档级，粗筛）
# Collection 2: doc_chunks（块级，精排）

async def two_stage_retrieval(query: str, top_k: int = 5):
    # 阶段一：文档级粗筛
    relevant_docs = await search_collection(
        "doc_summaries", query, top_k=10,
    )
    doc_ids = [d["doc_id"] for d in relevant_docs]

    # 阶段二：在命中文档内做块级精排
    chunks = await search_collection(
        "doc_chunks", query, top_k=top_k,
        filter_expr=f'doc_id in {doc_ids}',
    )
    return chunks
```

---

## 5.3 海量文本精准匹配：多路召回 + 重排序

### 5.3.1 单靠向量检索不够

向量检索（Dense Retrieval）擅长语义匹配，但有盲区：

| 查询 | 向量检索结果 | 问题 |
|------|------------|------|
| "SKU-88483 的库存" | 可能搜到"商品编号说明"、"SKU 格式规范" | 精确关键词没命中，被语义相近但无关的内容稀释了 |
| "2024年Q3财报" | 搜到 Q2、Q4 财报 | 对精确数字、日期不敏感 |
| "合同第 12 条" | 搜不到 | 对编号、条款号不敏感 |

**向量检索解决"意思相近"，但不能解决"精确匹配"。海量文本下两者必须互补。**

### 5.3.2 混合检索：稠密向量 + 稀疏向量（BM25）

```
查询："SKU-88483 的库存" 
      │
      ├─→ 稠密检索（Embedding 向量相似度）
      │   找到："商品 SKU 编号的含义"、"库存管理指南"、"SKU 编码规则"
      │         ↑ 语义相关，但不是用户要的
      │
      └─→ 稀疏检索（BM25 关键词匹配）
          找到："SKU-88483：蓝牙耳机白色款，当前库存 120 件"
                ↑ 精确命中！
```

LangChain + Milvus 实现混合检索：

```python
from langchain_openai import OpenAIEmbeddings
from pymilvus import Collection, AnnSearchRequest, RRFRanker
from pymilvus.model.sparse import BM25EmbeddingFunction


# ============ 准备：训练 BM25 模型 ============
# BM25 需要先"学习"整个文档集的词频分布
bm25_ef = BM25EmbeddingFunction()
bm25_ef.fit([doc.page_content for doc in all_documents])


# ============ 入库：同时存稠密向量和稀疏向量 ============
embeddings = OpenAIEmbeddings(model="text-embedding-3-small")

for doc in chunks:
    dense_vector = await embeddings.aembed_query(doc.page_content)
    sparse_vector = bm25_ef.encode_queries([doc.page_content])[0]

    collection.insert([{
        "content": doc.page_content,
        "dense_vector": dense_vector,
        "sparse_vector": sparse_vector,  # 稀疏向量存入 Milvus
        "doc_id": doc.metadata["doc_id"],
        **doc.metadata,
    }])


# ============ 检索：双路召回 + RRF 融合 ============
async def hybrid_search(query: str, top_k: int = 10) -> list[dict]:
    collection = Collection("documents")

    # 1. 生成查询的稠密向量
    dense_query = await embeddings.aembed_query(query)

    # 2. 生成查询的稀疏向量
    sparse_query = bm25_ef.encode_queries([query])[0]

    # 3. 构造双路搜索请求
    dense_req = AnnSearchRequest(
        data=[dense_query],
        anns_field="dense_vector",
        param={"metric_type": "IP", "params": {"nprobe": 16}},
        limit=top_k * 2,
    )
    sparse_req = AnnSearchRequest(
        data=[sparse_query],
        anns_field="sparse_vector",
        param={"metric_type": "IP", "params": {"nprobe": 16}},
        limit=top_k * 2,
    )

    # 4. RRF（Reciprocal Rank Fusion）融合两路结果
    #    公式：score = Σ 1/(k + rank_i)
    #    每路的结果按排名加权融合，避免某一路的绝对分数碾压另一路
    rerank = RRFRanker(k=60)

    results = collection.hybrid_search(
        reqs=[dense_req, sparse_req],
        rerank=rerank,
        limit=top_k,
        output_fields=["content", "doc_id", "title"],
    )

    return [
        {"content": hit.entity.content, "title": hit.entity.title, "score": hit.score}
        for hit in results[0]
    ]
```

### 5.3.3 RRF 为什么能融合两路不同量纲的分数

稠密向量的相似度是 0.85，BM25 的分数可能是 12.7——两者不可比。如果直接加权叠加，大数值会淹没小数值。

RRF 不关心分数绝对值，只关心**排名**：

```
稠密检索排名：      稀疏检索排名：         RRF 融合后：
Doc A  rank=1        Doc A  rank=3          Doc A: 1/(60+1) + 1/(60+3) = 0.0323
Doc B  rank=2        Doc C  rank=1          Doc C: 1/(60+4) + 1/(60+1) = 0.0320
Doc C  rank=4        Doc B  rank=2          Doc B: 1/(60+2) + 1/(60+2) = 0.0323
```

Doc A 在稠密路排第一、稀疏路排第三，最终和两路都排第二的 Doc B 得到相近的融合分——避免了某一方的偏见。

### 5.3.4 第三层：Reranker 重排序

混合检索拿到了 10 个候选，但其中可能有 2 个其实不相关。用 Reranker（交叉编码器）做一次精细化的相关性判断：

```python
from langchain.retrievers import ContextualCompressionRetriever
from langchain_cohere import CohereRerank

# 基础检索器（混合检索）
base_retriever = HybridRetriever(...)

# 加 Reranker 做精排
compressor = CohereRerank(
    model="rerank-v3",     # Cohere 的 rerank 模型
    top_n=5,               # 从 10 个候选中保留前 5
)
retriever = ContextualCompressionRetriever(
    base_compressor=compressor,
    base_retriever=base_retriever,
)

# 检索
docs = await retriever.ainvoke("怎么退款？")
```

Reranker 的效果：向量检索只看 query 和 doc 的全局语义相似度；Reranker 把 query 和每个候选文档拼在一起，让 Transformer 做逐 token 的交叉注意力，判断"这个问题和这篇文档真的相关吗"。

**成本提示：** Reranker 需要为每个 (query, doc) 对做一次推理。10 个候选 = 10 次推理，所以只在候选数较少（≤20）时使用。不可对全库做 Rerank。

### 5.3.5 总结：三级检索金字塔

```
        ┌──────────────┐
        │  Reranker    │  ← 精排（10→5），交叉编码器，最准但最慢
        │  深度语义匹配  │
        ├──────────────┤
        │  RRF 融合    │  ← 粗排（100→10），多路融合，互补盲区
        │  稠密 + 稀疏   │
        ├──────────────┤
        │  向量/关键词   │  ← 召回（10万→100），最快，覆盖面广
        │  单路快速检索  │
        └──────────────┘
```

---

## 5.4 Prompt 改写：用户问得不好，检索就好不了

### 5.4.1 问题场景

真实用户的提问往往是这样的：

| 原始问题 | 问题 | 检索能搜到什么 |
|---------|------|-------------|
| "那个东西怎么退" | 指代不明 | 什么都搜不到 |
| "坏了" | 太短，无语义 | 搜到各种"坏了"的用法 |
| "上次说的那个退款政策具体是什么来着" | 依赖对话历史 | 脱离上下文搜不到 |
| "Python" | 太泛 | 整个 Python 文档都搜出来 |

**RAG 的铁律：垃圾输入 → 垃圾检索 → 垃圾输出。** Prompt 改写是检索质量的闸门。

### 5.4.2 改写策略一：结合对话历史补全指代

```python
# core/query_rewriter.py
from langchain_core.prompts import ChatPromptTemplate
from core.llm import call_llm_with_retry


REWRITE_WITH_HISTORY_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """你是一个查询改写助手。根据对话历史，将用户模糊的问题改写为一个完整的、独立的检索查询。

## 改写规则：
1. 将指代词替换为具体实体。如"那个东西" → 从历史中找到指代的具体物品名
2. 补充历史中提到的约束条件。如用户之前说了"预算 500"，改写成"500元以内的..."
3. 只输出改写后的查询，不要添加解释

## 示例：
历史：用户：我想买个蓝牙耳机。助手：有什么要求吗？用户：降噪好一点的。
当前问题：多少钱
改写：降噪好的蓝牙耳机价格
"""),
    ("user", "对话历史：\n{history}\n\n当前问题：{query}\n\n改写后的查询："),
])


async def rewrite_with_history(history: list[dict], query: str) -> str:
    """结合对话历史，将指代不清的问题改写成完整查询"""
    # 只取最近 6 条历史
    recent = history[-6:] if len(history) > 6 else history
    history_text = "\n".join(
        f"{m['role']}: {m['content']}" for m in recent
    )

    rewritten = await call_llm_with_retry(
        messages=REWRITE_WITH_HISTORY_PROMPT.format(
            history=history_text,
            query=query,
        ).to_messages(),
        model="gpt-4o-mini",
    )
    return rewritten.strip()
```

效果：

```
输入：历史=["我要退蓝牙耳机", "订单号 ORD-001"], query="怎么退"
输出："订单 ORD-001 的蓝牙耳机退款流程"
     ↑ 可以直接拿去检索，命中率大幅提升
```

### 5.4.3 改写策略二：查询分解——复杂问题拆成多个子查询

```python
DECOMPOSE_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """将用户的复杂查询分解为 2~4 个独立的子查询，每个子查询单独检索能得到更准确的结果。

## 示例：
用户：蓝牙耳机和有线耳机哪个音质好？
分解：
1. 蓝牙耳机音质评测
2. 有线耳机音质评测
3. 蓝牙耳机与有线耳机音质对比

输出格式：每行一个子查询，不要编号。"""),
    ("user", "{query}"),
])


async def decompose_query(query: str) -> list[str]:
    """将复杂问题分解为多个子查询，各自检索后合并结果"""
    response = await call_llm_with_retry(
        messages=DECOMPOSE_PROMPT.format(query=query).to_messages(),
        model="gpt-4o-mini",
    )
    return [line.strip("- ").strip() for line in response.split("\n") if line.strip()]
```

检索时每个子查询独立搜索，最后合并去重：

```python
async def multi_query_retrieval(query: str, top_k: int = 5) -> list[Document]:
    """多查询检索：分解 → 各自检索 → 合并去重"""
    sub_queries = await decompose_query(query)

    all_docs = []
    seen = set()

    for sq in sub_queries:
        docs = await hybrid_search(sq, top_k=top_k)
        for doc in docs:
            if doc["content"] not in seen:
                seen.add(doc["content"])
                all_docs.append(doc)

    # 每个子查询的 top_k 结果合并后，再做一次 Rerank 精排
    return await rerank(query, all_docs, final_top_k=top_k)
```

### 5.4.4 改写策略三：HyDE——假设答案来搜

这是 RAG 界一个反直觉但极其有效的技巧：

```
普通检索：query "怎么退款" → embedding → 搜文档库
          ↑ "怎么退款" 和文档里的 "退款流程如下" 语义相似度不够高

HyDE 检索：query "怎么退款"
            → LLM 生成假设答案："用户需要登录账号，进入我的订单，
              选择要退款的商品，填写退款申请表，等待审核通过后款项
              原路退回，通常需要 3-5 个工作日。"
            → embedding → 搜文档库
          ↑ 假设答案的语言风格、详细程度和文档库高度一致，匹配度极高
```

```python
HYDE_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """你是一个文档生成助手。根据用户的问题，生成一段假设性的文档内容。
这段内容应该像是一篇正式文档中的段落，语言风格应该是说明性的、详细的。

注意：你不需要保证内容的准确性，你只需要生成一段"看起来像文档"的文本。
这段文本会被用来做向量检索，所以风格越接近真实文档越好。"""),
    ("user", "问题：{query}\n\n假设文档片段："),
])


async def hyde_retrieval(query: str, top_k: int = 5) -> list[Document]:
    """HyDE 检索：生成假设答案 → 用假设答案做向量检索"""
    # 1. 生成假设文档
    hypothetical_doc = await call_llm_with_retry(
        messages=HYDE_PROMPT.format(query=query).to_messages(),
        model="gpt-4o-mini",
    )

    # 2. 用假设文档（而非原始 query）做向量检索
    docs = await hybrid_search(hypothetical_doc, top_k=top_k)

    return docs
```

HyDE 特别适合以下场景：
- 用户 query 很短、很口语化
- 用户 query 是疑问句，文档库是陈述句
- 用户和文档使用了不同的术语（用户说"取消"，文档写"注销"）

### 5.4.5 三条改写策略的选择

| 策略 | 解决什么问题 | 额外 LLM 调用 | 延迟增量 |
|------|------------|-------------|---------|
| 历史补全 | 指代不清、依赖上下文 | 1 次 | +0.5s |
| 查询分解 | 复杂问题、多方面比较 | 1 次（分解为多子查询，子查询并行检索） | +1s |
| HyDE | query 太短、口语化、与文档风格不匹配 | 1 次 | +1s |

**推荐组合：默认只用历史补全（解决最常见问题）。检测到以下特征时启动查询分解或 HyDE：**
- 问题包含"和"、"相比"、"有什么区别" → 查询分解
- 问题 ≤ 6 个字 → HyDE

---

## 5.5 完整示例：生产级 RAG 的检索链路

```python
# services/rag_service.py
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from core.llm import call_llm_with_retry
from core.query_rewriter import rewrite_with_history, decompose_query, hyde_retrieval
from core.hybrid_search import hybrid_search
from core.logger import get_logger

logger = get_logger(__name__)
embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
llm = ChatOpenAI(model="gpt-4o")


async def rag_query(
    query: str,
    history: list[dict],
    top_k: int = 5,
) -> dict:
    """
    完整的 RAG 查询流程：
    改写 → 检索（混合 + 可选多查询/HyDE） → 重排序 → 生成
    """

    # ====== 第零步：判断是否需要特殊处理 ======
    use_decompose = any(kw in query for kw in ["和", "相比", "区别", "对比", "vs"])
    use_hyde = len(query) <= 6

    # ====== 第一步：查询改写 ======
    rewritten_query = await rewrite_with_history(history, query)
    logger.info("查询改写 | before=%s | after=%s", query, rewritten_query)

    # ====== 第二步：检索（根据特征选择策略） ======
    if use_decompose:
        logger.info("使用查询分解策略")
        docs = await multi_query_retrieval(rewritten_query, top_k=top_k * 2)
    elif use_hyde:
        logger.info("使用 HyDE 策略")
        docs = await hyde_retrieval(rewritten_query, top_k=top_k * 2)
    else:
        docs = await hybrid_search(rewritten_query, top_k=top_k * 2)

    if not docs:
        return {
            "answer": "抱歉，未找到相关信息。请尝试更具体的提问方式。",
            "sources": [],
        }

    # ====== 第三步：Rerank 精排 ======
    docs = await rerank(query, docs, final_top_k=top_k)
    logger.info("检索结果 | query=%s | retrieved=%d", rewritten_query, len(docs))

    # ====== 第四步：组装 prompt 并生成 ======
    context = "\n\n---\n".join(
        f"[来源：{d['title']}]\n{d['content']}" for d in docs
    )

    system_prompt = f"""根据以下参考资料回答用户问题。

## 参考资料
{context}

## 回答要求
1. 基于参考资料回答，不要编造信息
2. 如果参考资料不足以回答，明确说"根据现有资料无法确定"
3. 在回答末尾标注引用的来源文档标题
4. 用中文回答，简洁准确"""

    response = await llm.ainvoke([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": query},  # 注意：用原始 query，不是改写后的
    ])

    return {
        "answer": response.content,
        "sources": [{"title": d["title"], "excerpt": d["content"][:200]} for d in docs],
    }
```

---

## 5.6 引用溯源：让用户知道"回答来自哪里"

### 5.6.1 为什么要做引用

- 用户需要验证信息的可信度（"这个退款金额是哪个文件说的？"）
- 合规要求（医疗、法律、金融领域，每条结论必须有出处）
- 降低幻觉的体感（"你有文档依据" vs "你说的"）

### 5.6.2 实现方式

```python
# 在 prompt 中要求 LLM 标注引用
system_prompt = f"""根据以下参考资料回答用户问题。

## 参考资料
{context}

## 回答格式要求
在回答中，每引用一条参考资料时，在句末标注来源编号，格式为 [1]、[2]。
只引用确实使用了的资料。

## 示例
用户：怎么退款？
回答：您需要在购买后7天内提交退款申请 [1]，申请审核通过后款项将在3-5个工作日内原路退回 [2]。

[1] 来源：退款政策文档
[2] 来源：退款流程说明"""

# 前端渲染时，将 [1] 解析为可点击的链接
```

更严格的做法——结构化输出强制要求引用：

```python
from pydantic import BaseModel, Field


class CitedAnswer(BaseModel):
    answer: str
    citations: list[dict]  # [{"source_title": "...", "excerpt": "...", "index": 1}]


structured_llm = llm.with_structured_output(CitedAnswer)

result = await structured_llm.ainvoke([
    {"role": "system", "content": system_prompt},
    {"role": "user", "content": query},
])
# result.answer: "需要7天内申请退款。"
# result.citations: [{"source_title": "退款政策", "excerpt": "...", "index": 1}]
```

---

## 5.7 长期记忆模块 与 RAG 的区别

这是最容易混淆的两个模块，因为它们都用了 Milvus + 向量检索。但本质完全不同：

| 维度 | RAG 系统 | 长期记忆模块 |
|------|---------|------------|
| **存什么** | 外部知识——文档、手册、政策、FAQ、产品说明 | 用户个人信息——喜好、习惯、身份、经历 |
| **数据归属** | 全局共享——所有用户查的是同一套文档 | 用户私有——每个用户有自己独立的记忆空间 |
| **谁写入** | 管理员/系统——上传文档、同步数据库 | LLM 自动提取——从对话中识别并存储 |
| **写入触发** | 文档变更、定时同步 | 每次对话结束后异步提取 |
| **检索条件** | 只用 query 做语义搜索 | query + `user_id` 过滤（只搜当前用户的记忆） |
| **生命周期** | 文档删除/过期 → 从 Milvus 移除 | 记忆更新/遗忘 → 覆盖或删除 |
| **典型问题** | "退款政策是什么？" | "推荐一本编程书"（需要知道用户正在学 Rust） |

```
同一个 Milvus 实例，不同的 Collection：

┌──────────────────────────────────────────────────┐
│                 Milvus                            │
│                                                   │
│  ┌─────────────────┐  ┌─────────────────────┐    │
│  │ Collection:     │  │ Collection:          │    │
│  │ doc_chunks      │  │ long_term_memories   │    │
│  │ (RAG 知识库)    │  │ (用户长期记忆)       │    │
│  │                 │  │                      │    │
│  │ 检索条件：       │  │ 检索条件：            │    │
│  │ query embedding  │  │ query embedding       │    │
│  │ + category 过滤  │  │ + user_id 过滤        │    │
│  │                 │  │ + importance 加权      │    │
│  └─────────────────┘  └─────────────────────┘    │
└──────────────────────────────────────────────────┘
```

**两者协作的典型场景：**

```
用户："帮我查一下退款政策"

1. （无长期记忆参与）Agent 检索 RAG 知识库 → 找到退款政策文档
2. 准备回复时 → 检索长期记忆 → 发现"用户是 VIP，偏好简洁直接的回复"
3. 最终回复：用 VIP 级别的服务语气 + 简短摘要退款要点，而不是贴一大段政策原文
```

RAG 提供了"正确答案"，长期记忆提供了"用什么方式表达给这个人"。

---

## 5.8 为什么有 RAG 还需要模型微调（简要了解）

### 5.8.1 RAG 解决什么，微调解决什么

```
RAG 解决的问题：
  "模型不知道退款政策具体是什么" → 把政策文档塞进 prompt → 模型知道了
  
微调解决的问题：
  "模型总是用太正式的语气、回答太长、不会拒绝不合理请求" → 这不是知识问题，是行为问题
```

| 维度 | RAG | 微调 |
|------|-----|------|
| 解决的 | 知识缺口——模型不知道某条信息 | 行为风格——模型不会以某种方式回答 |
| 更新速度 | 即时的（改文档=改输出） | 需要重新训练（小时/天） |
| 适用 | 频繁变化的知识（政策、价格、FAQ） | 稳定的行为规范（语气、格式、安全准则） |
| 成本 | 检索 + prompt 拼接，按次付费 | 训练 + 部署，一次性投入 |
| 典型场景 | "2024年的税率是多少？" | "请始终用母亲般的温柔语气回答" |

### 5.8.2 两者的关系不是"二选一"

工业界的最佳实践是 **RAG-FT 混合架构**（Google、Anthropic、OpenAI 的公开技术报告都指向这个方向）：

```
用户问题
    │
    ▼
┌──────────┐
│ RAG 检索  │  ← 注入最新知识（政策、文档、实时数据）
└────┬─────┘
     │
     ▼
┌──────────┐
│ 微调模型  │  ← 保证输出规范（语气、格式、安全护栏、领域术语习惯）
└────┬─────┘
     │
     ▼
  最终回复
```

- **RAG** 让模型"知道得更多"——注入动态的外部知识
- **微调** 让模型"做得更好"——固化期望的行为模式

对于本项目的学生场景：**先做好 RAG，微调是锦上添花。** 绝大多数场景下，好的 prompt 工程 + RAG 就足够解决问题。微调适合产品已经稳定、有了大量标注数据后才投入。

---

## 5.9 常见踩坑清单

| 坑 | 现象 | 原因 | 解法 |
|----|------|------|------|
| 检索结果全是同一篇文档 | 某篇长文档被切成 100 块，占了检索结果 | 检索后没有做文档级去重 | `retrieved_docs` 按 `doc_id` 去重，每篇文档最多保留 3 块 |
| 回答引用了过时文档 | 政策改了，用户看到的还是旧的 | Milvus 中旧文档没删 | 文档入库时存 `updated_at`，检索加过滤 `updated_at > 某个日期` |
| 用户问"那个"搜不到 | 口语化的指代词没有语义 | 没做历史补全改写 | 检索前先做 query 改写（5.4 节） |
| 中英混合术语匹配差 | "大模型" vs "LLM"——向量认为不相似 | 单一语言模型对跨语言映射弱 | 用多语言 embedding（text-embedding-3-large）或在 query 改写时统一术语 |
| 检索延迟太高 | 混合检索 + Reranker 耗时 2 秒 | Reranker 对每个候选做推理，候选太多 | 控制 Reranker 候选 ≤20，或用 BGE-Reranker 本地部署避免网络延迟 |
| 文档切块后上下文断裂 | chunk 1 说"根据上文的规定"，chunk 0 不在检索结果里 | chunk 间失去关联 | `chunk_overlap` 设 10~20%，或在检索时把命中的 chunk 的前后 chunk 也带回来（parent document retrieval） |

---

## 5.10 本章小结

| 要点 | 核心做法 | 一句话 |
|------|---------|--------|
| 文档切块 | `RecursiveCharacterTextSplitter` 语义感知切 + 10~20% overlap | 一块 = 一个完整语义单元，边界不断裂 |
| 混合检索 | 稠密向量（语义）+ 稀疏向量（关键词）+ RRF 融合 | 向量查"意思相近"，BM25 查"精确命中"，互补盲区 |
| 重排序 | Reranker 交叉编码器对候选做精排 | 从"大概相关"中挑出"真正相关" |
| Prompt 改写 | 历史补全指代 + 查询分解 + HyDE 假设答案 | 用户问得不好，检索就好不了——改写是闸门 |
| 引用溯源 | prompt 要求标注 [N] + 结构化输出强制要求 | 让用户知道答案来自哪里，降低幻觉体感 |
| RAG vs 长期记忆 | RAG = 外部知识，长期记忆 = 用户个人信息 | 一个回答"退款政策是什么"，一个知道"这个用户喜欢简短回答" |
| RAG vs 微调 | RAG = 装新知识，微调 = 改行为习惯 | 不是二选一，是两层互补：知识靠 RAG，风格靠微调 |

**RAG 系统的核心竞争力不在生成端（LLM 本身就擅长生成），而在检索端——怎么在 1000 万篇文档中准确锁定最相关的 5 个段落，耗时不超过 200ms。这就是为什么本章 80% 的篇幅在讲检索。**

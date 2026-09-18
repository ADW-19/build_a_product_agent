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

⚠️ 先把单位说清楚，否则"500"是 500 字符还是 500 token 会差一倍：

| 语言 | 换算（粗口径，仅供估算） | 300~800 token 约合 |
|------|----------------------|------------------|
| 英文 | 1 token ≈ 4 字符 | 1200~3200 字符 |
| 中文 | 1 个汉字 ≈ 0.6~1.5 token（`o200k_base` 约 0.6~0.8，`cl100k_base` 约 1~1.5） | 约 200~1300 字符，中位经验值 400~600 字符 |

**不要照抄这张表的比率**——tokenizer 换了、语料里混了代码或数字，比率都会变。先在自己的语料上量一次：

```python
import tiktoken

enc = tiktoken.get_encoding("o200k_base")   # 换成你实际用的模型对应的 tokenizer
sample = "退款政策适用于购买后 7 天内的未开封商品，SKU-88483 支持无理由退货"
print("字符数:", len(sample), "token 数:", len(enc.encode(sample)))
```

**本章后续示例统一按字符计**（`length_function=len`）：`chunk_size=500` 指 500 字符。中文语料下这大致对应 300~600 token，落在经验区间内；如果你按 token 估的库里配了个"500"，实际切出来可能只有 300 字符左右的块——同一份文档在两种口径下块数与内容都不一样，评估结果自然对不上。要严格按 token 切，就把 `length_function` 换成上面那行计数函数，并在注释里写清用的是哪个 tokenizer。

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
    chunk_size=500,         # 目标大小：500 字符，不是 500 token（口径见 5.2.1）
    chunk_overlap=80,       # 相邻块重叠 80 字符
    length_function=len,    # 按字符数计数；如需按 token 精确切分：
                            # length_function=lambda t: len(tiktoken.get_encoding("o200k_base").encode(t))
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

**overlap 的经验值：chunk_size 的 10%~20%。** 500 字符的块，重叠 50~80 字符（口径与 chunk_size 保持一致，不要一个按字符一个按 token）。

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
    if not doc_ids:
        return []  # 空列表时 Milvus 的 in 表达式非法，直接返回空结果
    # Milvus 过滤表达式要求字符串字面量用双引号：doc_id in ["a", "b"]
    id_expr = "doc_id in [" + ", ".join(f'"{d}"' for d in doc_ids) + "]"
    chunks = await search_collection(
        "doc_chunks", query, top_k=top_k,
        filter_expr=id_expr,
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
from pymilvus.model.sparse.bm25.tokenizers import build_default_analyzer


# ============ 准备：训练 BM25 模型 ============
# ⚠️ BM25EmbeddingFunction() 不传 analyzer 时用的是"内置英文分析器"——官方文档原话：
#    analyzer "Defaults to a built-in English language analyzer if not specified"。
#    本章语料是中文（"SKU-88483 的库存"），英文分析器会把中文整段当成一个 token，
#    稀疏路基本召回不到东西——而"稀疏路精确命中 SKU-88483"正是混合检索的立论依据。
#    build_default_analyzer(language="zh") 走 jieba，需先 pip install jieba。
analyzer = build_default_analyzer(language="zh")
bm25_ef = BM25EmbeddingFunction(analyzer=analyzer)

# BM25 需要先"学习"整个文档集的词频分布（词表 + IDF）
bm25_ef.fit([doc.page_content for doc in all_documents])

# ⚠️ fit 的结果是语料统计（词表/IDF），必须落盘共享：入库进程与查询进程是两个进程，
#    查询侧 load 不到同一份参数，就等于拿另一套词表去 encode_queries，
#    两边的稀疏向量不可比，检索分数没有意义。
bm25_ef.save("bm25_params.json")   # 入库侧保存
# 查询侧（另一个进程）：
# bm25_ef = BM25EmbeddingFunction(analyzer=build_default_analyzer(language="zh"))
# bm25_ef.load("bm25_params.json")
# 另外：语料追加后重新 fit 会改变词表与 IDF，此时必须重建稀疏索引，光换参数文件不够。


# ============ 入库：同时存稠密向量和稀疏向量 ============
embeddings = OpenAIEmbeddings(model="text-embedding-3-small")

for doc in chunks:
    dense_vector = await embeddings.aembed_query(doc.page_content)
    # 文档入库侧用 encode_documents（encode_queries 只用于查询侧）
    sparse_vector = bm25_ef.encode_documents([doc.page_content])[0]

    # doc_id 等字段由 doc.metadata 统一提供，避免显式键与 **doc.metadata 重复
    collection.insert([{
        **doc.metadata,
        "content": doc.page_content,
        "dense_vector": dense_vector,
        "sparse_vector": sparse_vector,  # 稀疏向量存入 Milvus
    }])

# 建索引：检索时该传什么参数，由索引类型决定（见下面的对照表）
collection.create_index(
    field_name="dense_vector",
    index_params={
        "index_type": "HNSW",
        "metric_type": "IP",
        "params": {"M": 16, "efConstruction": 200},   # 建索引参数
    },
)
collection.create_index(
    field_name="sparse_vector",
    index_params={"index_type": "SPARSE_INVERTED_INDEX", "metric_type": "IP"},
)


# ============ 检索：双路召回 + RRF 融合 ============
async def hybrid_search(
    query: str,
    top_k: int = 10,
    *,
    dense_text: str | None = None,
    sparse_text: str | None = None,
) -> list[dict]:
    """
    混合检索。两路允许用不同的查询文本：

    dense_text:  稠密路的查询文本，默认用 query。HyDE 场景传"假设文档"。
    sparse_text: 稀疏路（BM25）的查询文本，默认用 query。
                 ⚠️ 稀疏路永远喂用户原始 query：BM25 靠精确 token 命中，
                 LLM 编造的假设文档会把 SKU-88483 这类关键 token 稀释掉，
                 "稠密管语义、稀疏管精确"的互补性也就没了。
    """
    dense_text = query if dense_text is None else dense_text
    sparse_text = query if sparse_text is None else sparse_text
    collection = Collection("documents")

    # 1. 生成查询的稠密向量
    dense_query = await embeddings.aembed_query(dense_text)

    # 2. 生成查询的稀疏向量（必须用入库时 save 下来的同一份 BM25 参数）
    sparse_query = bm25_ef.encode_queries([sparse_text])[0]

    # 3. 构造双路搜索请求
    dense_req = AnnSearchRequest(
        data=[dense_query],
        anns_field="dense_vector",
        # ef 是 HNSW 的检索参数（建索引用的是 M/efConstruction）。
        # 这里传 nprobe 是 IVF 系列的参数：与 HNSW 不匹配时 Milvus 会抛
        # Search params check failed；更隐蔽的情况是参数被忽略、按默认值跑，召回悄悄变差。
        # ef 必须 >= limit。
        param={"metric_type": "IP", "params": {"ef": 64}},
        limit=top_k * 2,
    )
    sparse_req = AnnSearchRequest(
        data=[sparse_query],
        anns_field="sparse_vector",
        # 稀疏索引不使用 nprobe（那是稠密 IVF 索引的参数）；稀疏检索一般配置 drop_ratio_search
        param={"metric_type": "IP", "params": {"drop_ratio_search": 0.2}},
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

    # doc_id 必须一路带回来：下游要按文档去重/配额（见 5.10 踩坑表），
    # 只返回 content/title/score 的话，5.5 的 cap_per_document 无从执行。
    return [
        {
            "content": hit.entity.content,
            "doc_id": hit.entity.doc_id,
            "title": hit.entity.title,
            "score": hit.score,
        }
        for hit in results[0]
    ]
```

**检索参数必须和索引类型对上**——这是混合检索最常见的"静默劣化"来源：

| 索引类型 | 建索引参数（`create_index`） | 检索参数（`AnnSearchRequest` / `search`） | 说明 |
|---------|--------------------------|--------------------------------------|------|
| FLAT | 无 | 无 | 暴力检索，召回 100%，只适合小库（≤10 万向量） |
| AUTOINDEX | 无 | 无（传了也会被忽略） | Milvus 默认，自动选索引类型；不需要也不接受手工调参 |
| IVF_FLAT / IVF_SQ8 | `nlist` | `nprobe` | `1 ≤ nprobe ≤ nlist`；nprobe 越大越准越慢 |
| HNSW | `M`、`efConstruction` | `ef` | `ef ≥ limit`；ef 越大越准越慢。本章示例用 HNSW |
| DISKANN | `max_degree` 等 | `search_list` | 十亿级、数据落磁盘的场景 |
| SPARSE_INVERTED_INDEX | `drop_ratio_build` | `drop_ratio_search` | 稀疏向量（BM25）专用；`drop_ratio_search` 越大越省算力 |

**参数与索引不匹配的后果：** Milvus 会抛 `Search params check failed`——这是幸运的情况；另一种是参数被静默忽略、按默认值检索，召回率持续偏低而日志里什么都没有。

顺带一提：Milvus 2.5+ 提供了**服务端 BM25**（建 Collection 时用 `FunctionType.BM25` 定义全文检索函数，检索侧 `metric_type="BM25"`），分词、词表与 IDF 全部由服务端维护，本文这一整类"analyzer 选错 / 参数文件没同步 / 重建索引"的问题都不存在。新建系统建议优先评估它；下面仍按客户端 BM25 + 稀疏向量展开，因为这是"自己控制稀疏路"时更通用的写法。

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
# ContextualCompressionRetriever 随 chains/retrievers/indexes 一起被迁出了 langchain 包，
# LangChain v1 里要靠 langchain-classic：
#     pip install langchain-classic langchain-cohere
from langchain_classic.retrievers import ContextualCompressionRetriever
from langchain_cohere import CohereRerank
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import Field


class HybridMilvusRetriever(BaseRetriever):
    """把 5.3.2 的 hybrid_search 包成 LangChain 检索器。

    BaseRetriever 是 Pydantic 模型，附加配置要声明成字段。
    这里只实现异步入口：同步的 _get_relevant_documents 里再调 asyncio.run
    会和已有事件循环冲突，生产链路统一走 ainvoke / astream。
    """

    top_k: int = Field(default=10)

    async def _aget_relevant_documents(self, query: str, *, run_manager=None) -> list[Document]:
        hits = await hybrid_search(query, top_k=self.top_k)
        return [
            Document(
                page_content=h["content"],
                metadata={"doc_id": h["doc_id"], "title": h["title"], "score": h["score"]},
            )
            for h in hits
        ]


# 基础检索器（混合检索，见 5.3.2）
base_retriever = HybridMilvusRetriever(top_k=10)

# 加 Reranker 做精排
compressor = CohereRerank(
    model="rerank-v3.5",   # 现役的单模型多语言 reranker（4096 token 上下文）
    top_n=5,               # 从 10 个候选中保留前 5
)
retriever = ContextualCompressionRetriever(
    base_compressor=compressor,
    base_retriever=base_retriever,
)

# 检索
docs = await retriever.ainvoke("怎么退款？")
# 注意：docs 是按 relevance_score 从高到低排好的（每条的 metadata 里带 relevance_score）。
# 任何"再按原顺序排一遍"的后续处理都会抹掉精排结果——顺序本身就是精排的产出。
```

Reranker 的效果：向量检索只看 query 和 doc 的全局语义相似度；Reranker 把 query 和每个候选文档拼在一起，让 Transformer 做逐 token 的交叉注意力，判断"这个问题和这篇文档真的相关吗"。

**成本提示（一个常见的将错就错）：** 托管的 Cohere Rerank 是**一次 API 调用**，服务端对本次提交的候选文档批量打分，计费按**文档数**计，不是"10 个候选 = 10 次推理"。真正的约束是两条：

1. **服务端单次文档数上限**（Cohere 当前是 1000 篇）：超过要分批，分批就真的变成多次调用了。
2. **成本与延迟都随候选数增长**：线上 rerank 1000 篇就是为 1000 篇付费，往返时间也随传入文档数上升。

所以"不可对全库做 Rerank"这个结论成立，但理由是**钱和延迟**，而不是"候选数 ≤20"这条被当成硬约束的经验值。候选数该取多少由 5.9 的评估决定：先看把候选从 20 加到 50、100 时 Recall@5 / nDCG@5 还有没有提升，收益走平的那一点就是工作点。

模型版本也要跟上：`rerank-v3.5` 是现役的单个多语言模型（4096 token 上下文）；Cohere Rerank v3（含 `rerank-multilingual-v3.0`）已于 2025-03-31 弃用；2025-12 起另有 `rerank-v4.0-pro` / `rerank-v4.0-fast` 可选。

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
        messages=REWRITE_WITH_HISTORY_PROMPT.format_messages(
            history=history_text,
            query=query,
        ),
        model="gpt-5-mini",
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
蓝牙耳机音质评测
有线耳机音质评测
蓝牙耳机与有线耳机音质对比

输出格式：每行一个子查询，不要编号。"""),
    ("user", "{query}"),
])


async def decompose_query(query: str) -> list[str]:
    """将复杂问题分解为多个子查询，各自检索后合并结果"""
    response = await call_llm_with_retry(
        messages=DECOMPOSE_PROMPT.format_messages(query=query),
        model="gpt-5-mini",
    )
    return [line.strip("- ").strip() for line in response.split("\n") if line.strip()]


async def rerank(query: str, docs: list[dict], final_top_k: int = 5) -> list[dict]:
    """
    对检索候选做 Reranker 精排（见 5.3.4 节）。

    docs: list[dict]，每项含 content/title 等字段。
    内部用 CohereRerank 对 (query, 候选) 做交叉编码打分，只保留前 final_top_k 条。
    """
    from langchain_cohere import CohereRerank
    from langchain_core.documents import Document

    compressor = CohereRerank(model="rerank-v3.5", top_n=final_top_k)
    compressed = await compressor.acompress_documents(
        query=query,
        documents=[Document(page_content=d["content"], metadata={"title": d.get("title", "")}) for d in docs],
    )

    # ⚠️ compressed 已经是"按相关性从高到低排好序"的结果（交叉编码器的分数写在
    #    metadata 里，CohereRerank 的字段名是 relevance_score）。
    # 返回值必须按 compressed 的顺序重建，并带上这个分数。
    order = {c.page_content: i for i, c in enumerate(compressed)}
    score_of = {c.page_content: c.metadata.get("relevance_score") for c in compressed}
    ranked = sorted(
        (d for d in docs if d["content"] in order),
        key=lambda d: order[d["content"]],
    )
    return [{**d, "rerank_score": score_of[d["content"]]} for d in ranked]
```

**为什么这一步极容易写错：** 精排的输出不只是一个更短的列表，而是**顺序**。如果像下面这样只取它的内容做集合、再按原始候选顺序过滤 `docs`：

```python
# ❌ 精排的排序价值 100% 丢失，只剩"截断到 top_n"的作用
kept = {c.page_content for c in compressed}
return [d for d in docs if d["content"] in kept]
```

结果数量对得上、内容看着也都相关（毕竟是同一批候选），所以这种失效在联调时几乎看不出来——而"三级检索金字塔（召回→粗排→精排）"到这里实际只剩两级。

检索时每个子查询独立搜索，最后合并去重：

```python
import asyncio

from langchain_core.documents import Document


async def multi_query_retrieval(query: str, top_k: int = 5) -> list[Document]:
    """多查询检索：分解 → 并行检索 → 合并去重 → 精排"""
    sub_queries = await decompose_query(query)

    # ⚠️ 必须并行：串行的 for 循环会让延迟随子查询个数线性增长（分解出 4 个子查询 = 4 倍检索耗时）
    result_sets = await asyncio.gather(
        *(hybrid_search(sq, top_k=top_k) for sq in sub_queries)
    )

    all_docs = []
    seen = set()

    for docs in result_sets:
        for doc in docs:
            if doc["content"] not in seen:
                seen.add(doc["content"])
                all_docs.append(doc)

    # 每个子查询的 top_k 结果合并后，再做一次 Rerank 精排。
    # 注意：这里的候选数是 N × top_k（N=2~4），托管 reranker 按文档数计费，
    # 所以分解策略的代价不只是"多一次 LLM 调用"（见 5.4.5 表的延迟口径）。
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
from langchain_core.documents import Document


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
        messages=HYDE_PROMPT.format_messages(query=query),
        model="gpt-5-mini",
    )

    # 2. ⚠️ 假设文档只喂稠密路（dense_text），稀疏路仍然用原始 query。
    #    若把整段假设文档也丢给 BM25，查询侧会多出一大堆 LLM 编造的词，
    #    把 SKU-88483 这类关键 token 的权重稀释掉——两路召回退化成"两路都靠语义"。
    return await hybrid_search(query, top_k=top_k, dense_text=hypothetical_doc)
```

HyDE 特别适合以下场景：
- 用户 query 很短、很口语化
- 用户 query 是疑问句，文档库是陈述句
- 用户和文档使用了不同的术语（用户说"取消"，文档写"注销"）

### 5.4.5 三条改写策略的选择

| 策略 | 解决什么问题 | 额外 LLM 调用 | 延迟增量 |
|------|------------|-------------|---------|
| 历史补全 | 指代不清、依赖上下文 | 1 次 | +0.5s（只多一次改写调用，检索次数不变） |
| 查询分解 | 复杂问题、多方面比较 | 1 次 | +0.5~1s；**另需 N 次混合检索 + N×top_k 个 rerank 候选**（N=2~4），代价不是"只多一次 LLM 调用" |
| HyDE | query 太短、口语化、与文档风格不匹配 | 1 次 | +1s（生成假设文档，检索次数不变） |

**口径说明：** 表里的检索开销按 5.4.3 / 5.4.4 的实现计算——子查询用 `asyncio.gather` 并行，所以 N 次检索的墙钟时间约等于 1 次；但 rerank 的候选数是 N×top_k，托管 reranker 按文档数计费，这笔钱和这段延迟会实打实乘 N。串行 `for` 循环就没这么便宜了：延迟直接乘 N。

**推荐组合：默认只用历史补全（解决最常见问题）。检测到以下特征时启动查询分解或 HyDE：**
- 问题包含"和"、"相比"、"有什么区别" → 查询分解
- 问题 ≤ 6 个字 → HyDE

---

## 5.5 完整示例：生产级 RAG 的检索链路

```python
# services/rag_service.py
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from core.llm import call_llm_with_retry
from core.query_rewriter import (
    rewrite_with_history,
    decompose_query,
    hyde_retrieval,
    multi_query_retrieval,
    rerank,
)
from core.hybrid_search import hybrid_search
from core.logger import get_logger

logger = get_logger(__name__)
embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
llm = ChatOpenAI(model="gpt-5.1")


def cap_per_document(docs: list[dict], max_per_doc: int = 3) -> list[dict]:
    """同一篇文档在最终结果里最多保留 max_per_doc 块。

    一篇 100 块的长文档很容易占满 top 10（见 5.10 踩坑表），
    按 doc_id 做配额是唯一稳的解法——前提是 doc_id 一路带了回来：
    hybrid_search 的 output_fields 和返回 dict 里都必须有它。
    """
    kept: list[dict] = []
    count: dict[str, int] = {}
    for d in docs:
        doc_id = d.get("doc_id", "")
        if count.get(doc_id, 0) >= max_per_doc:
            continue
        count[doc_id] = count.get(doc_id, 0) + 1
        kept.append(d)
    return kept


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
    # 注意：不能把"和"这类超高频单字当作分解触发词，否则几乎每次都会误触发；
    # 改用更精确的多字短语模式，复杂场景交给 LLM 判断
    use_decompose = any(p in query for p in ["相比", "对比", "区别", "差异", "哪个好", "如何选择", "哪个更"])
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

    # ====== 第三步：Rerank 精排 + 文档级配额 ======
    # 精排时多要一些（top_k * 2）：同一篇文档可能霸榜，配额之后才凑得齐 top_k
    docs = await rerank(query, docs, final_top_k=top_k * 2)
    docs = cap_per_document(docs, max_per_doc=3)[:top_k]
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

## 5.7 长期记忆模块与 RAG 的区别

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

工业界的常见做法是 **RAG-FT 混合架构**——检索负责知识、微调负责行为：

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

## 5.9 检索效果的度量：怎么知道改动真的让检索变好了

### 5.9.1 先看错误的调优循环

```python
# ❌ 只靠"抽几个问题看看"验证检索改动
SAMPLE_QUERIES = ["怎么退款", "SKU-88483 库存", "发票怎么开"]   # 一共 3 个问题


async def eyeball_check() -> None:
    """改完 chunk_size / embedding / 候选数，就跑一遍这个函数，看眼缘决定上不上线"""
    for query in SAMPLE_QUERIES:
        docs = await hybrid_search(query, top_k=3)
        print(query, [d["title"] for d in docs])
```

上面这段的实质是：把 chunk_size 从 300 试到 800、每轮只抽 3 条看结果，看着 800 更好就上线。真实结论往往是整体 Recall 掉了一截，一周后靠用户投诉才发现。切块、混合检索、RRF、Reranker、Query 改写——每一项单个看都"有道理"，改动之后却**没有任何数字能证明它真的变好**：抽两三个问题看结果，判断会被"这也能搜到"的个别案例主导，而占多数的问题正在悄悄退化。在 1000 万篇文档上，抽 3 条看结果的有效样本量就是 3。

**本质原因：** "相关"必须先被固定在一条判定标准上，才谈得上比较。任何一次检索改动都会同时改善一部分查询、损害另一部分查询，只有在一批**人工判定过相关性**的问题上统计出数字，才能回答"新版比旧版好多少"。这批问题与这套数字，就是检索的评估体系。

### 5.9.2 六个指标：定义与分母口径

| 指标 | 回答什么问题 | 定义 | 分母 | 常见写错的地方 |
|------|------------|------|------|--------------|
| Recall@k | 该找到的找到了多少 | 前 k 命中数 / 相关总数 | 该查询标注的相关结果数 | 分母写成 k 或写成返回条数 → 数字虚高 |
| Precision@k | 喂进 prompt 的有多干净 | 前 k 命中数 / k | k | 用 `len(返回条数)` 当分母，改 k 之后不可比 |
| MRR | 第一条相关的排多前 | 1 / 第一条相关的排名，无命中记 0 | 查询数 | 有多条相关时会被系统性低估 |
| nDCG@k | 相关的排得够不够靠前 | DCG@k / IDCG@k，DCG = Σ gain_i / log2(i+1) | 理想排序的 DCG | 需要分级相关性（0/1/2/3）；二值标注下退化成加权 Recall，不如直接看 Recall |
| Hit Rate@k | 用户能不能"搜到" | 前 k 中只要有一个相关就记 1 | 查询数 | 二值指标，会掩盖"只命中 1 条"，必须和 Recall@k 一起看 |
| Faithfulness | 回答有没有编 | 回答中的断言能在检索上下文里找到依据的比例 | 回答中的断言数 | 分母是"断言数"不是"句子数"；属生成侧指标，标注成本高，通常只抽检 |

**两个口径必须在报告里写清楚，否则数字之间没法比：**

- **macro 还是 micro**：macro = 每条查询各算一次再取平均（推荐，避免长标注样本主导总分）；micro = 所有查询的命中数相加除以相关总数。两者可能给出相反结论。
- **报指标时附四要素**：embedding 模型 + 版本、切块方案（chunk_size / overlap / 按字符还是 token）、候选数与 k、标注集版本。缺一个，这个数字下周就没人能复现。

**第一个该盯的指标是 Recall@k（k 取最终喂进 prompt 的条数）。** 本章反复强调"80% 在检索"，而 Recall 就是检索端的天花板：rerank 只能重排已经召回的候选，召回阶段漏掉的 chunk 后面任何环节都救不回来。所以排查顺序永远是先看 Recall@k（召回够不够），再看 nDCG@5 / MRR（排得够不够前）。

### 5.9.3 参考实现

```python
# services/retrieval_eval.py
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class EvalCase:
    """一条标注样本：问题 + 该问题真正相关的 chunk 集合"""

    query: str
    relevant_chunk_ids: set[str]   # 必须是 chunk 级 id，不是 doc_id（见 5.9.4）


def recall_at_k(ranked_ids: list[str], relevant: set[str], k: int) -> float:
    """分母是"标注结果数"，不是 k、也不是实际返回条数"""
    if not relevant:
        raise ValueError("空标注样本不能参与统计（分母为 0），这类样本应在标注阶段补全")
    return len(set(ranked_ids[:k]) & relevant) / len(relevant)


def precision_at_k(ranked_ids: list[str], relevant: set[str], k: int) -> float:
    """分母固定为 k：返回不足 k 条也按 k 算，否则改了 k 就没法前后对比"""
    return len(set(ranked_ids[:k]) & relevant) / k


def mrr(ranked_ids: list[str], relevant: set[str]) -> float:
    """第一条相关的排名倒数；分母是查询数，没命中记 0"""
    for i, chunk_id in enumerate(ranked_ids, start=1):
        if chunk_id in relevant:
            return 1.0 / i
    return 0.0


def hit_rate_at_k(ranked_ids: list[str], relevant: set[str], k: int) -> float:
    """前 k 里有任意一条相关就记 1；分母是查询数"""
    return 1.0 if set(ranked_ids[:k]) & relevant else 0.0


def _dcg(gains: list[float]) -> float:
    return sum(gain / math.log2(i + 2) for i, gain in enumerate(gains))


def ndcg_at_k(ranked_gains: list[float], k: int) -> float:
    """gains 是分级相关性（如 0/1/2/3）；二值标注下不如直接用 Recall@k"""
    ideal = _dcg(sorted(ranked_gains, reverse=True)[:k])
    return _dcg(ranked_gains[:k]) / ideal if ideal > 0 else 0.0


async def evaluate_retriever(retrieve, cases: list[EvalCase], ks: tuple[int, ...] = (1, 3, 5, 10)) -> dict[str, float]:
    """跑一遍标注集，按查询维度取平均（macro 口径）。

    retrieve: async (query) -> list[dict]，每项含 chunk_id，返回顺序即检索顺序。
    """
    total: dict[str, float] = {"mrr": 0.0}
    for k in ks:
        total[f"recall@{k}"] = 0.0
        total[f"precision@{k}"] = 0.0
        total[f"hit_rate@{k}"] = 0.0

    for case in cases:
        ranked_ids = [h["chunk_id"] for h in await retrieve(case.query)]
        total["mrr"] += mrr(ranked_ids, case.relevant_chunk_ids)
        for k in ks:
            total[f"recall@{k}"] += recall_at_k(ranked_ids, case.relevant_chunk_ids, k)
            total[f"precision@{k}"] += precision_at_k(ranked_ids, case.relevant_chunk_ids, k)
            total[f"hit_rate@{k}"] += hit_rate_at_k(ranked_ids, case.relevant_chunk_ids, k)

    n = len(cases)
    return {name: round(value / n, 4) for name, value in total.items()}
```

两版对比的用法（"新版更好"这句话要有资格说，就得这么量）：

```python
import json


def load_eval_cases(path: str) -> list[EvalCase]:
    """标注文件一行一条：{"query": "...", "relevant_chunk_ids": ["doc_0007_c012", ...]}"""
    with open(path, encoding="utf-8") as f:
        return [
            EvalCase(query=row["query"], relevant_chunk_ids=set(row["relevant_chunk_ids"]))
            for row in (json.loads(line) for line in f if line.strip())
        ]


async def retrieve_v2(query: str) -> list[dict]:
    """新版链路：候选放大到 50 → rerank 精排 → 每篇文档最多 3 块"""
    candidates = await hybrid_search(query, top_k=50)
    ranked = await rerank(query, candidates, final_top_k=10)
    return cap_per_document(ranked, max_per_doc=3)


async def compare_versions() -> None:
    cases = load_eval_cases("evals/retrieval_cases.json")
    v1 = await evaluate_retriever(hybrid_search, cases)
    v2 = await evaluate_retriever(retrieve_v2, cases)
    print("v1:", v1)
    print("v2:", v2)
    # 报告里附上：embedding 模型与版本 / 切块方案 / k / 标注集版本
    # 先看 Recall@10（召回有没有变），再看 nDCG@5（排序有没有变）
```

### 5.9.4 标注集：指标的分子分母都来自它

标注集不是"随手找几个问题"，它的质量直接决定指标能不能用：

- **标注粒度必须到 chunk 级。** 最终喂给 LLM 的是若干 chunk，"这篇文档相不相关"回答不了"前 5 个 chunk 里几个有用"。doc 级标注只能算 doc 级 Recall：一篇文档切成 100 块时，"命中了这篇文档"和"命中了正确的那一块"完全是两件事，而后者才是本章所有优化（切块、混合、rerank）的作用对象。
- **chunk 边界会变，标注要用字符区间表达。** 改一次 chunk_size，原有的 chunk 级标注就全部失效。做法是标注时记录"答案所在原文的字符区间/片段"，chunk 方案变了就把区间重新映射到新 chunk（脚本化，几行代码）——这样"chunk_size 500 vs 300"这类实验才做得起来，否则每换一次切块方案都要重新标一遍。
- **池化（pooling）防偏。** 只把当前系统召回的结果拿去标注，漏召回的相关结果永远发现不了，指标会系统性偏高（pool bias）。至少用两三个不同配置（不同 embedding、加不加 BM25 路）的结果取并集作为标注池，再补一部分随机采样。
- **多人标注要算一致性。** 相关/不相关本身有主观性：抽 10%~20% 让 2 人各标一遍，算 Cohen's kappa，低于 0.6 说明判定标准还没统一——先对齐标注规范再回标，别急着拿它当基准。
- **规模与分层。** 起步 50~100 条 query 就足以驱动决策，但要按查询类型分层（精确 token 类 / 语义类 / 多跳对比类 / 口语化短句类），每层 ≥20 条，并按层看指标；否则 HyDE 在短句层的收益会被其它层稀释成"整体没变化"。样本量小时给出 bootstrap 置信区间，别把 ±3 个点的抖动当成优化效果。

### 5.9.5 阈值必须在标注集上标定，不能抄常量

```python
# ❌ 从博客、别的项目抄来的"经验阈值"
results = [d for d in hits if d["score"] >= 0.7]
```

**本质原因：** 相似度的**绝对值**会漂移。余弦相似度随 embedding 模型、是否归一化、语料分布变化——同一个 embedding 下，相关句对常落在 0.4~0.7，不相关也常有 0.2~0.4。卡 0.7 会把大量相关 chunk 直接滤掉，而且滤得毫无声息：不报错、不告警，只是结果变少、回答变差。reranker 的输出同理，`relevance_score` 既不是概率，也不跨模型可比。

正确做法是在自己的标注集上画"阈值 → 保留条数 / Precision / Hit Rate"曲线，挑工作点：

```python
async def calibrate_threshold(cases: list[EvalCase], retrieve_with_score, thresholds: list[float] | None = None) -> list[dict]:
    """画阈值-召回曲线：给定阈值下还剩多少条、有多干净、还能不能命中。

    retrieve_with_score: async (query) -> list[dict]，每项含 chunk_id 与 score。
    """
    thresholds = thresholds or [0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.7]
    rows: list[dict] = []
    for t in thresholds:
        kept_total = 0
        kept_correct = 0
        hit_cases = 0
        for case in cases:
            kept = [h for h in await retrieve_with_score(case.query) if h["score"] >= t]
            if kept:
                hit_cases += 1
            kept_total += len(kept)
            kept_correct += sum(1 for h in kept if h["chunk_id"] in case.relevant_chunk_ids)
        rows.append({
            "threshold": t,
            "avg_kept": round(kept_total / len(cases), 2),
            "precision": round(kept_correct / kept_total, 3) if kept_total else 0.0,
            "hit_rate": round(hit_cases / len(cases), 3),   # 至少有 1 条过线的查询占比
        })
    return rows
```

- **取点原则是业务偏好，不是某个"正确"的常数。** RAG 场景通常宁可多带一点噪声：取 Hit Rate 处仍在高位、再降阈值收益开始变平缓的位置；合规/医疗这类宁缺毋滥的场景，取 Precision 的拐点，并为漏检留人工兜底。
- **换 embedding 模型或换 reranker 之后，必须重标阈值并重跑评估。** 向量空间整体变了（或分数分布整体变了），昨天标定的 0.45 今天没有任何意义；同理，阈值也不跨 Collection 复用——`doc_chunks` 上标出来的阈值搬到用户记忆的 Collection 上会失效。

**一句话：检索的"好坏"只能由标注集上的 Recall@k / nDCG / MRR 这类指标说话，阈值是这套指标的函数，而不是可以照抄的常量。** 测试流程、CI 门禁、LLM-as-judge 自身的可靠性要求（位置偏见、与人工标注的一致性门槛）见第5章，本节只给指标定义与口径。

### 5.9.6 每个改动该看哪个指标

| 你的改动 | 先看 | 别看错 |
|---------|-----|-------|
| chunk_size / chunk_overlap | Recall@10（召回上限有没有变化）、nDCG@5 | 只看 top1"感觉更准了" |
| 换 embedding 模型 | Recall@10 + 重标阈值后的 Precision | 只跑几个 demo 问题 |
| 加/换 Reranker | nDCG@5、MRR（排序质量） | 拿 Recall 当 reranker 的指标——它改不了召回 |
| RRF 的 k 或两路权重 | Recall@10、Hit Rate@5 | 融合分的绝对值（RRF 分数本身没有业务含义） |
| Query 改写（含 HyDE） | 对应分层的 Recall@10 | 全局平均（收益会被其它层稀释） |
| 加时间/类目过滤 | Precision@5 与误滤率 | 只看 Precision（Recall 会掉，要算清这个代价） |

**一句话：先有标注集和指标，才谈得上"优化检索"；否则每一次调参都只是换了一种感觉。**

---

## 5.10 常见踩坑清单

| 坑 | 现象 | 原因 | 解法 |
|----|------|------|------|
| 检索结果全是同一篇文档 | 某篇长文档被切成 100 块，占了检索结果 | 检索后没有做文档级去重 | `hybrid_search` 的返回值里必须带 `doc_id`，再按 `doc_id` 做配额，每篇文档最多保留 3 块（5.5 的 `cap_per_document`）；只返回 content/title 的实现做不了这件事 |
| 回答引用了过时文档 | 政策改了，用户看到的还是旧的 | Milvus 中旧文档没删 | 文档入库时存 `updated_at`，检索加过滤 `updated_at > 某个日期` |
| 用户问"那个"搜不到 | 口语化的指代词没有语义 | 没做历史补全改写 | 检索前先做 query 改写（5.4 节） |
| 中英混合术语匹配差 | "大模型" vs "LLM" 查不到 | 入库与查询的术语体系不一致；只靠 BM25 路也命中不到，语义路则取决于模型的多语言对齐能力 | ①入库与查询必须用同一个 embedding 模型、同一个版本（跨模型/跨版本的向量不可比）；②做术语归一：同义词表或 query 改写把"大模型"扩成"大模型 LLM"（见 5.4 节）；③语料本身多语言混杂时，把 bge-m3 / multilingual-e5 / text-embedding-3-large 这类多语言更强的模型列为候选——换完必须重标阈值并重跑评估（见 5.9.5） |
| 检索延迟太高 | 混合检索 + Reranker 耗时 2 秒 | 托管 Reranker 是一次外网往返，成本与延迟都随候选文档数增长 | 候选数按 5.9 的评估取收益拐点（而不是固定的"≤20"），不对全库 rerank；延迟敏感时换本地部署的交叉编码器（BGE-Reranker 之类）省掉网络往返；并按 5.11 的延迟预算单独给 rerank 记账 |
| 文档切块后上下文断裂 | chunk 1 说"根据上文的规定"，chunk 0 不在检索结果里 | chunk 间失去关联 | `chunk_overlap` 设 10~20%，或在检索时把命中的 chunk 的前后 chunk 也带回来（parent document retrieval） |
| 改了检索参数但说不清有没有变好 | chunk_size 从 500 调到 300、embedding 换了一版，只有"感觉" | 没有标注集与指标，凭个案判断 | 按 5.9 建立 chunk 级标注集，比较两版的 Recall@k / nDCG@5；阈值在新标注集上重标 |

---

## 5.11 本章小结

| 要点 | 核心做法 | 一句话 |
|------|---------|--------|
| 文档切块 | `RecursiveCharacterTextSplitter` 语义感知切 + 10~20% overlap（单位口径写清） | 一块 = 一个完整语义单元，边界不断裂 |
| 混合检索 | 稠密向量（语义）+ 稀疏向量（关键词）+ RRF 融合，两路可各用不同查询文本 | 向量查"意思相近"，BM25 查"精确命中"，互补盲区 |
| 重排序 | Reranker 交叉编码器对候选做精排，**按它的输出顺序重建结果** | 从"大概相关"中挑出"真正相关"，顺序本身就是产出 |
| Prompt 改写 | 历史补全指代 + 查询分解（并行检索）+ HyDE 假设答案（只喂稠密路） | 用户问得不好，检索就好不了——改写是闸门 |
| 引用溯源 | prompt 要求标注 [N] + 结构化输出强制要求 | 让用户知道答案来自哪里，降低幻觉体感 |
| 检索评估 | chunk 级标注集 + Recall@k / nDCG / MRR / Hit Rate；阈值在标注集上画曲线取工作点 | 没有标注集，"检索变好了"就只是换了一种感觉 |
| RAG vs 长期记忆 | RAG = 外部知识，长期记忆 = 用户个人信息 | 一个回答"退款政策是什么"，一个知道"这个用户喜欢简短回答" |
| RAG vs 微调 | RAG = 装新知识，微调 = 改行为习惯 | 不是二选一，是两层互补：知识靠 RAG，风格靠微调 |

**RAG 系统的核心竞争力不在生成端（LLM 本身就擅长生成），而在检索端——怎么在 1000 万篇文档中准确锁定最相关的 5 个段落。这就是为什么本章 80% 的篇幅在讲检索。**

"200ms"是**检索与融合**这一段的目标，不是端到端。一次查询的延迟预算（P95，千万级 chunk、Milvus 与业务同机房，量级参考而非承诺）：

| 阶段 | 延迟量级 | 说明 |
|------|---------|------|
| Query 改写（可选） | 300~800ms | 一次 LLM 调用；命中缓存可降到 ~10ms |
| 混合检索（稠密 + 稀疏各取 top 100 + RRF） | 30~80ms | **这才是"检索 200ms 级"所指的部分** |
| Rerank（托管 API，20~50 个候选） | 150~400ms | 一次外网往返 + 服务端批量打分；换本地交叉编码器约 30~80ms |
| 生成 | 首 token 300~800ms | 与上面各阶段串行 |

**把 hosted rerank 也算进去，"端到端 200ms"就不成立了**——rerank 和生成要单独记账；反过来，如果目标里确实包含 rerank，就必须换成本地模型或接受一个更大的预算数字。

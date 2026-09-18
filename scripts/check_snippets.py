#!/usr/bin/env python3
"""文档代码块静态校验。

手册的硬约定是"示例代码能跑"，本脚本用三条纯机械的检查在提交前拦住最常见的退化：

  1. Python 代码块能否通过 ast.parse（语法错误、非法字面量）
  2. YAML 代码块能否通过 yaml.safe_load
  3. 围栏语言标注是否与实际内容一致
  4. 是否残留照抄就会报错的已弃用 API 用法

用法：
    python scripts/check_snippets.py                 # 检查 docs/ 下全部 Markdown
    python scripts/check_snippets.py docs/a.md ...   # 只检查指定文件

退出码：有问题时为 1，否则为 0。

依赖：PyYAML（仅检查 YAML 块时需要）。缺失时跳过对应检查并提示。

设计取舍：
  * 只报能明确定性的问题，不报"建议类"告警——本仓库的示例统一引用 core/ 骨架，
    跨文件引用的占位符号是刻意为之，把它们做成告警只会训练人忽略输出。
  * 弃用 API 扫描只扫代码、不扫注释（见 strip_python_comments），
    因此文章里可以放心写"❌ 不要这样写"的反面教材。
  * BANNED 是一份**会过期的清单**：每条都对应本仓库真实出现过的错误。
    生态前移时（参 CLAUDE.md 的版本基线复核）需要同步增删，不要只加不减。
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import sys
import tokenize
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - 环境缺依赖时降级
    yaml = None

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

# --------------------------------------------------------------------------
# 已弃用 / 错误的 API 用法：每条为 (正则, 说明)，命中即报错。
# 只收"照抄必然出错或必然产生错误行为"的写法；上下文相关、可能合法的不收
# （例如 /health_generate 在 SGLang 示例里是正确写法，因此不在此列）。
# --------------------------------------------------------------------------
BANNED: list[tuple[str, str]] = [
    (
        r"create_react_agent",
        "langgraph.prebuilt.create_react_agent 已弃用；改用 langchain.agents.create_agent（注意 system_prompt= 参数）",
    ),
    (
        r"from\s+langchain(\.chains|\.retrievers|\.indexes)",
        "LangChain v1 已将 chains/retrievers/indexes 迁出 langchain 包；改用 langchain_classic.*",
    ),
    (
        r"\bMemorySaver\b",
        "LangGraph 1.x 已改用 InMemorySaver（MemorySaver 仅作兼容别名保留）",
    ),
    (
        r"set_entry_point|set_finish_point",
        "已弃用；改用 add_edge(START, ...) / add_edge(..., END)",
    ),
    (
        r"=\s*RedisSaver\.from_conn_string",
        "from_conn_string 是 @contextmanager，不能直接当 checkpointer 实例；用 with ... as saver: saver.setup()",
    ),
    (
        r"await\s+httpx\.(post|get|put|delete)\(",
        "httpx.post/get 是同步顶层函数，不能 await；需 httpx.AsyncClient().post(...)",
    ),
    (
        r"await\s+[\w\.\[\]\"']+\.invoke\(",
        "LangChain 的同步入口是 invoke（返回非 awaitable）；异步应改用 ainvoke",
    ),
    (
        r"tasks/(send|get|cancel|stream|sendSubscribe)\b",
        "A2A 方法名：0.2.0 起为 message/send、message/stream、tasks/get、tasks/cancel；1.0 的 JSON-RPC 绑定为 PascalCase 名",
    ),
    (
        r"well-known/agent\.json",
        "A2A Agent Card 路径自 0.3.0 起为 /.well-known/agent-card.json",
    ),
    (
        r"connections\.connect|utility\.has_collection",
        "pymilvus 3.x 已弃用 ORM 写法（connections/utility/Collection）；官方要求用 MilvusClient",
    ),
    (
        r"rerank-multilingual-v3\.0|rerank-english-v3\.0",
        "Cohere Rerank v3 已于 2025-03-31 弃用；改用 rerank-v3.5 或 rerank-v4.0-*",
    ),
    (
        r"from\s+langfuse\.callback\s+import|langfuse\.trace\(",
        "Langfuse v3 SDK：回调改为 from langfuse.langchain import CallbackHandler，且已移除 Langfuse.trace()",
    ),
    (
        r"RagasEvaluator\b",
        "ragas 中不存在 RagasEvaluator；0.4+ 用 llm_factory + @experiment",
    ),
]

BANNED_COMPILED = [(re.compile(p), msg) for p, msg in BANNED]

# 会做"已弃用 API"扫描的围栏语言
SCANNED_LANGS = {
    "python", "py", "javascript", "js", "typescript", "ts",
    "json", "yaml", "yml", "bash", "sh", "text", "",
}


def strip_python_comments(src: str) -> str:
    """去掉 Python 源码中的注释，只留下可执行代码。

    手册大量使用"❌ 错误示范"注释（例如 `# 不要写 checkpointer=RedisSaver.from_conn_string(...)`），
    这些注释是教学内容，不是违规用法。弃用 API 扫描只针对真实代码，避免误报。
    """
    try:
        toks = [
            tok for tok in tokenize.generate_tokens(io.StringIO(src).readline)
            if tok.type != tokenize.COMMENT
        ]
        return tokenize.untokenize(toks)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # 源码本身不合法时按原样返回，让语法检查先暴露问题
        return src


def iter_fenced_blocks(text: str):
    """产出 (起始行号, 语言, 代码文本)。仅处理三反引号围栏。"""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = re.match(r"^\s*```+\s*([A-Za-z0-9_+-]*)", lines[i])
        if not m:
            i += 1
            continue
        lang = (m.group(1) or "").lower()
        start = i + 1
        i += 1
        buf = []
        while i < len(lines) and not re.match(r"^\s*```+\s*$", lines[i]):
            buf.append(lines[i])
            i += 1
        yield start + 1, lang, "\n".join(buf)
        i += 1


def check_file(path: Path, problems: list[str]) -> None:
    rel = path.relative_to(ROOT).as_posix()

    for lineno, lang, block in iter_fenced_blocks(path.read_text(encoding="utf-8")):
        loc = f"{rel}:{lineno}"

        # ---- 已弃用 API 扫描 ----
        if lang in SCANNED_LANGS:
            target = strip_python_comments(block) if lang in ("python", "py") else block
            for rx, msg in BANNED_COMPILED:
                m = rx.search(target)
                if m:
                    problems.append(f"{loc}: 命中 {m.group(0)!r} —— {msg}")

        if lang not in ("python", "py"):
            if lang in ("yaml", "yml") and yaml is not None:
                try:
                    yaml.safe_load(block)
                except yaml.YAMLError as e:
                    problems.append(f"{loc}: YAML 无法解析: {str(e).splitlines()[0]}")
            continue

        # ---- Python 块：语法 ----
        try:
            ast.parse(block)
        except SyntaxError as e:
            # 若它其实是合法 YAML，报语言标注错误比报语法错误更准确
            if is_mistagged_yaml(block):
                problems.append(f"{loc}: 标注为 python 但内容是合法 YAML，请把围栏语言改为 yaml")
                continue
            problems.append(f"{loc}: 语法错误（块内第 {e.lineno} 行）: {e.msg}")


# 行首出现这些就是 Python 语句，不可能是 YAML
PYTHON_STMT_HINTS = re.compile(
    r"^\s*(async\s+def\s|def\s|class\s|import\s|from\s+\S+\s+import|return\b|yield\b"
    r"|for\b|while\b|if\b|elif\b|else\s*:|try\s*:|except\b|finally\s*:|with\b"
    r"|await\b|raise\b|assert\b|lambda\b|@\w)",
    re.M,
)


def is_mistagged_yaml(block: str) -> bool:
    """判断一个 ast 解析失败的 python 块，是不是"内容其实是 YAML 但标错了语言"。

    YAML 极其宽松——任何缩进文本几乎都能解析成嵌套 mapping，所以单凭
    `yaml.safe_load` 成功就下结论会掩盖真实的 Python 语法错误。
    这里加两道门槛：块里不能出现 Python 语句特征，且解析结果必须是 dict/list。
    """
    if yaml is None or PYTHON_STMT_HINTS.search(block):
        return False
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError:
        return False
    return isinstance(data, (dict, list))


def main() -> int:
    ap = argparse.ArgumentParser(description="文档代码块静态校验")
    ap.add_argument("files", nargs="*", help="要检查的 Markdown 文件（默认 docs/ 下全部）")
    args = ap.parse_args()

    # 传相对路径时也要能用：先解析成绝对路径
    paths = [Path(f).resolve() for f in args.files] if args.files else sorted(DOCS.rglob("*.md"))
    paths = [p for p in paths if p.is_file()]
    if not paths:
        print("没有找到待检查的文件")
        return 1

    if yaml is None:
        print("提示：未安装 PyYAML，已跳过 YAML 块检查（pip install pyyaml）\n")

    problems: list[str] = []
    for p in paths:
        check_file(p, problems)

    for line in problems:
        print(line)

    print(f"\n检查 {len(paths)} 个文件：发现 {len(problems)} 个问题")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

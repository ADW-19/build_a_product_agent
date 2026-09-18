#!/usr/bin/env python3
"""文档代码块静态校验闸门。

手册的硬约定是"示例代码可运行"，本脚本用来在提交前拦住最常见的四类退化：
语法错误、YAML 无法解析、语言标注错误、已弃用 API 的残留用法。

用法：
    python scripts/check_snippets.py                 # 检查 docs/ 下全部 Markdown
    python scripts/check_snippets.py docs/a.md ...   # 只检查指定文件
    python scripts/check_snippets.py --no-warn       # 只报 ERROR，忽略 WARN

退出码：有 ERROR 时为 1，否则为 0。WARN 不改变退出码。

依赖：PyYAML（仅检查 YAML 块时需要）。缺失时跳过对应检查并提示。
"""

from __future__ import annotations

import argparse
import ast
import builtins
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
# 已弃用 / 错误的 API 用法
# 每条为 (正则, 说明)。命中即 ERROR——这些写法照抄会报错或产生错误行为。
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
        "LangGraph 1.x 文档已改用 InMemorySaver（MemorySaver 仅作兼容别名保留）",
    ),
    (
        r"set_entry_point|set_finish_point",
        "已弃用；改用 add_edge(START, ...) / add_edge(..., END)",
    ),
    (
        r"checkpointer\s*=\s*RedisSaver\.from_conn_string|=\s*RedisSaver\.from_conn_string",
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
        "A2A 方法名：0.2.0 起为 message/send、message/stream、tasks/get、tasks/cancel；1.0 的 JSON-RPC 绑定为 PasalCase 名",
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
        r"from\s+langfuse\.callback\s+import|\.trace\(",
        "Langfuse v3 SDK：回调改为 from langfuse.langchain import CallbackHandler，且已移除 Langfuse.trace()",
    ),
    (
        r"RagasEvaluator\b",
        "ragas 中不存在 RagasEvaluator；0.4+ 用 llm_factory + @experiment",
    ),
    (
        r"health_generate",
        "/health_generate 是 SGLang 端点，vLLM 只有 /health、/ping、/version",
    ),
    (
        r"created_at\s*>\s*.*90",
        "疑似遗忘任务的过期条件写反：应为 last_accessed_at < now - 90d（created_at 反向会删掉新记忆）",
    ),
]

BANNED_COMPILED = [(re.compile(p), msg) for p, msg in BANNED]

# --------------------------------------------------------------------------
# 骨架符号白名单补充
# 手册示例统一引用 core/ 骨架（见 CLAUDE.md），这些符号跨文件出现属约定行为，
# 在"未定义符号"检查里应放行。
# --------------------------------------------------------------------------
SKELETON_NAMES = {
    "agent", "llm", "logger", "redis", "settings", "config", "app",
    "ALL_TOOLS", "TOOLS_MAP", "SYSTEM_PROMPT", "PLANNING_PROMPT",
    "call_llm_with_retry", "get_history", "save_history", "append_message",
    "append_message_with_limit", "search_similar_memories", "hybrid_search",
    "rerank", "classify_intent", "Intent", "tool_guard", "embed_text",
    "insert_memories", "persist_memories", "build_context",
    "compress_conversation", "count_message_tokens", "trim_history",
    "get_redis", "planning_llm", "execute_llm", "multi_query_retrieval",
    "rewrite_with_history", "decompose_query", "hyde_retrieval",
    "extract_tool_calls", "compare_responses", "log_shadow_comparison",
    "send_single_request", "safe_eval", "core", "tools", "routes", "services",
}


def strip_python_comments(src: str) -> str:
    """去掉 Python 源码中的注释，只留下可执行代码。

    手册里大量使用"❌ 错误示范"注释（例如 `# 不要写 checkpointer=RedisSaver.from_conn_string(...)`），
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


def defined_names(tree: ast.AST) -> set[str]:
    """收集模块级与嵌套作用域内所有被定义/导入的名字（粗粒度，用于降噪）。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            args = getattr(node, "args", None)
            if args:
                for a in (
                    args.posonlyargs + args.args + args.kwonlyargs
                    + ([args.vararg] if args.vararg else [])
                    + ([args.kwarg] if args.kwarg else [])
                ):
                    names.add(a.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.alias):
            names.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.comprehension) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Global | ast.Nonlocal):
            names.update(node.names)
    return names


def loaded_names(tree: ast.AST) -> set[str]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            out.add(node.id)
    return out


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, loc: str, msg: str) -> None:
        self.errors.append(f"ERROR  {loc}: {msg}")

    def warn(self, loc: str, msg: str) -> None:
        self.warnings.append(f"WARN   {loc}: {msg}")


def check_file(path: Path, rep: Report, style: dict) -> None:
    text = path.read_text(encoding="utf-8")
    rel = path.relative_to(ROOT).as_posix()

    for lineno, lang, block in iter_fenced_blocks(text):
        loc = f"{rel}:{lineno}"

        # ---- 已弃用 API 扫描（对代码块生效；Python 块先剥掉注释）----
        if lang in ("python", "py", "javascript", "js", "typescript", "ts",
                    "json", "yaml", "yml", "bash", "sh", "text", ""):
            scan_target = strip_python_comments(block) if lang in ("python", "py") else block
            for rx, msg in BANNED_COMPILED:
                m = rx.search(scan_target)
                if m:
                    rep.error(loc, f"命中 {m.group(0)!r} —— {msg}")

        if lang not in ("python", "py"):
            if lang in ("yaml", "yml") and yaml is not None:
                try:
                    yaml.safe_load(block)
                except yaml.YAMLError as e:
                    rep.error(loc, f"YAML 无法解析: {str(e).splitlines()[0]}")
            continue

        # ---- python 块：语法 ----
        try:
            tree = ast.parse(block)
        except SyntaxError as e:
            # 若它其实是合法 YAML，报语言标注错误而不是语法错误（更准确的提示）
            if yaml is not None:
                try:
                    yaml.safe_load(block)
                    rep.error(loc, "该块标注为 python 但内容是合法 YAML，请把围栏语言改为 yaml")
                    continue
                except yaml.YAMLError:
                    pass
            rep.error(loc, f"语法错误（第 {e.lineno} 行）: {e.msg}")
            continue

        # ---- python 块：未定义符号（WARN，全仓库统一降噪）----
        used = loaded_names(tree)
        unknown = used - defined_names(tree) - SKELETON_NAMES - set(dir(builtins)) - style["repo_symbols"]
        unknown = {n for n in unknown if not n.startswith("__")}
        if unknown:
            rep.warn(loc, f"引用了本块未定义、且全仓库未定义的符号: {sorted(unknown)}")


def collect_repo_symbols(paths: list[Path]) -> set[str]:
    """全仓库 python 块里出现过的定义/导入名，用于跨文件骨架引用的降噪。"""
    syms: set[str] = set()
    for p in paths:
        for _, lang, block in iter_fenced_blocks(p.read_text(encoding="utf-8")):
            if lang not in ("python", "py"):
                continue
            try:
                syms |= defined_names(ast.parse(block))
            except SyntaxError:
                continue
    return syms


def main() -> int:
    ap = argparse.ArgumentParser(description="文档代码块静态校验")
    ap.add_argument("files", nargs="*", help="要检查的 Markdown 文件（默认 docs/ 下全部）")
    ap.add_argument("--no-warn", action="store_true", help="只输出 ERROR")
    args = ap.parse_args()

    # 传相对路径时也要能用：统一解析成绝对路径再算相对 ROOT 的展示路径
    paths = [Path(f).resolve() for f in args.files] if args.files else sorted(DOCS.rglob("*.md"))
    paths = [p for p in paths if p.is_file()]
    if not paths:
        print("没有找到待检查的文件")
        return 1

    if yaml is None:
        print("提示：未安装 PyYAML，已跳过 YAML 块检查（pip install pyyaml）\n")

    style = {"repo_symbols": collect_repo_symbols(paths)}
    rep = Report()
    for p in paths:
        check_file(p, rep, style)

    for line in rep.errors:
        print(line)
    if not args.no_warn:
        for line in rep.warnings:
            print(line)

    print(
        f"\n检查 {len(paths)} 个文件：ERROR {len(rep.errors)} 条，"
        f"WARN {len(rep.warnings)} 条"
    )
    return 1 if rep.errors else 0


if __name__ == "__main__":
    sys.exit(main())

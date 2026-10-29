"""Agent 模式：大模型自主调用 检索 / grep / 列目录 / 读文件 工具，多步追踪代码逻辑。"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, tool

from .chains import format_docs, number_lines, to_messages
from .index import RepoIndex
from .loader import build_tree, read_text
from .prompts import AGENT_SYSTEM

MAX_TOOL_OUTPUT = 6000


def _safe_path(root: Path, rel: str) -> Path:
    """把模型给出的相对路径限制在仓库根目录内，防止越权读取。"""
    p = (root / rel.strip().lstrip("/\\")).resolve()
    if p != root and root not in p.parents:
        raise ValueError(f"路径越界：{rel}")
    return p


def _clip(text: str) -> str:
    return text if len(text) <= MAX_TOOL_OUTPUT else text[:MAX_TOOL_OUTPUT] + "\n...（输出过长已截断）"


def make_tools(index: RepoIndex) -> list[BaseTool]:
    root = index.repo.root.resolve()
    retriever = index.retriever(k=5)

    @tool
    def search_code(query: str) -> str:
        """在仓库中混合检索（语义+关键词）与 query 最相关的代码片段，返回带路径和行号的片段。query 可以是中文描述或英文标识符。"""
        return _clip(format_docs(retriever.invoke(query)) or "没有找到相关代码。")

    @tool
    def grep_code(pattern: str, max_results: int = 30) -> str:
        """用正则表达式在仓库所有已索引文件中逐行搜索，返回 `路径:行号: 内容`。适合精确查找函数定义、调用位置、配置项。"""
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return f"正则表达式错误：{e}"
        hits = []
        for rel in index.files:
            text = read_text(root / rel) or ""
            for no, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{rel}:{no}: {line.strip()[:160]}")
                    if len(hits) >= max_results:
                        return "\n".join(hits) + "\n...（结果过多，已截断）"
        return "\n".join(hits) or "没有匹配结果。"

    @tool
    def list_directory(path: str = ".", depth: int = 2) -> str:
        """列出仓库中某个目录（相对路径，默认根目录）的树形结构。"""
        try:
            p = _safe_path(root, path)
        except ValueError as e:
            return str(e)
        if not p.is_dir():
            return f"不是目录：{path}"
        return _clip(build_tree(p, max_depth=max(1, min(depth, 4))))

    @tool
    def read_file(path: str, start_line: int = 1, end_line: int = 200) -> str:
        """读取仓库内文件（相对路径）的指定行范围，带行号。单次最多 300 行。"""
        try:
            p = _safe_path(root, path)
        except ValueError as e:
            return str(e)
        if not p.is_file():
            return f"文件不存在：{path}"
        lines = (read_text(p) or "").splitlines()
        start = max(1, start_line)
        end = min(len(lines), end_line, start + 299)
        body = number_lines("\n".join(lines[start - 1 : end]), start)
        return _clip(f"{path}（共 {len(lines)} 行，显示 L{start}-L{end}）\n{body}")

    return [search_code, grep_code, list_directory, read_file]


@dataclass
class AgentEvent:
    kind: str  # "tool_call" | "tool_result" | "answer"
    name: str = ""
    content: str = ""
    args: dict | None = None


class RepoAgent:
    def __init__(self, index: RepoIndex, llm: BaseChatModel, max_steps: int = 24):
        self.index = index
        self.max_steps = max_steps
        self.graph = create_agent(
            llm, make_tools(index), system_prompt=AGENT_SYSTEM.format(repo_name=index.repo.name)
        )

    def stream(self, question: str, history: list[dict] | None = None) -> Iterator[AgentEvent]:
        messages = to_messages(history or []) + [HumanMessage(content=question)]
        for update in self.graph.stream(
            {"messages": messages}, {"recursion_limit": self.max_steps}, stream_mode="updates"
        ):
            for node_output in update.values():
                for msg in (node_output or {}).get("messages", []):
                    if isinstance(msg, AIMessage):
                        for call in msg.tool_calls:
                            yield AgentEvent("tool_call", call["name"], args=call["args"])
                        if not msg.tool_calls and msg.content:
                            yield AgentEvent("answer", content=msg.text if hasattr(msg, "text") else str(msg.content))
                    elif isinstance(msg, ToolMessage):
                        yield AgentEvent("tool_result", msg.name or "", content=str(msg.content))

    def run(self, question: str, history: list[dict] | None = None) -> str:
        answer = ""
        for ev in self.stream(question, history):
            if ev.kind == "answer":
                answer = ev.content
        return answer

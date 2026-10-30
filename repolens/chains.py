"""LCEL 链：带引用的仓库问答、项目导览、单文件讲解。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass

from langchain_core.documents import Document
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.output_parsers import StrOutputParser

from .index import RepoIndex
from .loader import language_of, read_text
from .prompts import get_prompts

MANIFESTS = [
    "README.md", "readme.md", "README.rst", "README", "pyproject.toml", "requirements.txt", "setup.py",
    "package.json", "go.mod", "Cargo.toml", "pom.xml", "build.gradle", "composer.json", "Dockerfile",
]
ENTRY_HINTS = ["main", "app", "index", "server", "cli", "manage", "__main__", "run", "wsgi", "asgi"]


@dataclass
class Retrieved:
    query: str
    docs: list[Document]


def to_messages(history: list[dict]) -> list[BaseMessage]:
    """[{role, content}] → LangChain 消息；只保留最近 6 轮，控制上下文长度。"""
    msgs: list[BaseMessage] = []
    for h in history[-12:]:
        cls = HumanMessage if h["role"] == "user" else AIMessage
        msgs.append(cls(content=h["content"]))
    return msgs


def format_docs(docs: list[Document]) -> str:
    parts = []
    for i, d in enumerate(docs, 1):
        m = d.metadata
        lang = m.get("language", "")
        lang = "" if lang == "text" else lang
        parts.append(f"[{i}] {m['source']} (L{m['start_line']}-L{m['end_line']})\n```{lang}\n{d.page_content}\n```")
    return "\n\n".join(parts)


def number_lines(text: str, start: int = 1) -> str:
    return "\n".join(f"{i:>4} | {line}" for i, line in enumerate(text.splitlines(), start))


class RepoAssistant:
    def __init__(self, index: RepoIndex, llm: BaseChatModel, k: int = 6, lang: str = "zh"):
        self.index = index
        self.llm = llm
        self.retriever = index.retriever(k)
        self.prompts = get_prompts(lang)
        parser = StrOutputParser()
        self.rewrite_chain = self.prompts.rewrite | llm | parser
        self.qa_chain = self.prompts.qa | llm | parser
        self.overview_chain = self.prompts.overview | llm | parser
        self.explain_chain = self.prompts.explain_file | llm | parser

    @property
    def repo_name(self) -> str:
        return self.index.repo.name

    # ---------- 问答 ----------
    def retrieve(self, question: str, history: list[dict] | None = None) -> Retrieved:
        query = question
        if history:
            try:
                query = self.rewrite_chain.invoke({"question": question, "history": to_messages(history)}).strip() or question
            except Exception:
                query = question
        return Retrieved(query=query, docs=self.retriever.invoke(query))

    def stream_answer(self, question: str, retrieved: Retrieved, history: list[dict] | None = None) -> Iterator[str]:
        return self.qa_chain.stream(
            {
                "repo_name": self.repo_name,
                "context": format_docs(retrieved.docs) or self.prompts.no_context,
                "history": to_messages(history or []),
                "question": question,
            }
        )

    def ask(self, question: str, history: list[dict] | None = None) -> tuple[str, Retrieved]:
        r = self.retrieve(question, history)
        return "".join(self.stream_answer(question, r, history)), r

    # ---------- 项目导览 ----------
    def _key_files(self, budget: int = 9000) -> str:
        root = self.index.repo.root
        files = self.index.files
        picked: list[str] = [m for m in MANIFESTS if (root / m).is_file()]
        entries = sorted(
            (f for f in files if f.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower() in ENTRY_HINTS),
            key=lambda f: f.count("/"),
        )
        picked += [f for f in entries[:4] if f not in picked]
        parts, used = [], 0
        for rel in picked:
            text = read_text(root / rel) or ""
            snippet = text[: min(2500, budget - used)]
            if not snippet:
                break
            used += len(snippet)
            parts.append(f"#### {rel}\n```\n{snippet}\n```")
        return "\n\n".join(parts) or "（未找到 README 或依赖清单）"

    def stream_overview(self) -> Iterator[str]:
        s = self.index.stats
        stats = json.dumps({k: s[k] for k in ("files", "chunks", "lines", "languages") if k in s}, ensure_ascii=False)
        return self.overview_chain.stream(
            {"repo_name": self.repo_name, "stats": stats, "tree": self.index.tree(3), "key_files": self._key_files()}
        )

    # ---------- 单文件讲解 ----------
    def references_to(self, rel_path: str, limit: int = 8) -> list[str]:
        """粗略查找哪些文件引用了该模块（按文件名 stem 匹配 import/require 等）。"""
        stem = rel_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if len(stem) < 3 or stem in {"index", "main", "__init__"}:
            return []
        refs = []
        for c in self.index.chunks:
            if c.metadata["source"] == rel_path:
                continue
            for offset, line in enumerate(c.page_content.splitlines()):
                if stem in line and any(kw in line for kw in ("import", "require", "include", "from", "use ")):
                    refs.append(f"- `{c.metadata['source']}:{c.metadata['start_line'] + offset}`  {line.strip()[:120]}")
            if len(refs) >= limit:
                break
        return refs[:limit]

    def stream_explain_file(self, rel_path: str, max_chars: int = 14000) -> Iterator[str]:
        path = self.index.repo.root / rel_path
        text = read_text(path) or ""
        truncated = len(text) > max_chars
        content = number_lines(text[:max_chars]) + ("\n... (truncated)" if truncated else "")
        refs = self.references_to(rel_path)
        return self.explain_chain.stream(
            {
                "repo_name": self.repo_name,
                "path": rel_path,
                "language": language_of(path) or "",
                "content": content,
                "references": "\n".join(refs) or self.prompts.not_found,
            }
        )

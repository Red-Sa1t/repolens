"""仓库索引：加载 → 切分 → 向量化 → 持久化缓存；文件未变化时秒级复用。"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from langchain_core.documents import Document
from langchain_core.vectorstores import InMemoryVectorStore

from .config import Settings
from .llm import get_embeddings
from .loader import RepoInfo, build_tree, iter_repo_files, load_documents, resolve_repo
from .retriever import CodeBM25Retriever, HybridRetriever
from .splitter import split_documents

ProgressFn = Callable[[float, str], None]
INDEX_VERSION = 1
EMBED_BATCH = 32


@dataclass
class RepoIndex:
    repo: RepoInfo
    chunks: list[Document]
    vector_store: InMemoryVectorStore | None
    stats: dict = field(default_factory=dict)
    _bm25: CodeBM25Retriever | None = field(default=None, repr=False)

    @property
    def has_vectors(self) -> bool:
        return self.vector_store is not None

    def retriever(self, k: int = 6) -> HybridRetriever:
        if self._bm25 is None:
            self._bm25 = CodeBM25Retriever(docs=self.chunks)
        return HybridRetriever(bm25=self._bm25, vector_store=self.vector_store, k=k)

    def tree(self, max_depth: int = 3) -> str:
        return build_tree(self.repo.root, max_depth=max_depth)

    @property
    def files(self) -> list[str]:
        return sorted({c.metadata["source"] for c in self.chunks})


def _fingerprint(root: Path, settings: Settings) -> str:
    h = hashlib.md5()
    h.update(f"v{INDEX_VERSION}|{settings.embed_model}|{settings.chunk_size}|{settings.chunk_overlap}".encode())
    for p in iter_repo_files(root, settings.max_file_bytes, settings.max_files):
        st = p.stat()
        h.update(f"{p.relative_to(root).as_posix()}|{st.st_size}|{st.st_mtime_ns}".encode())
    return h.hexdigest()


def _index_dir(repo: RepoInfo, settings: Settings) -> Path:
    key = hashlib.md5(str(repo.root).lower().encode()).hexdigest()[:10]
    return settings.cache_dir / "index" / f"{repo.name}-{key}"


def _compute_stats(docs: list[Document], chunks: list[Document]) -> dict:
    langs = Counter(d.metadata["language"] for d in docs)
    return {
        "files": len(docs),
        "chunks": len(chunks),
        "lines": sum(d.metadata.get("lines", 0) for d in docs),
        "languages": dict(langs.most_common()),
    }


def build_index(source: str, settings: Settings, progress: ProgressFn | None = None, force: bool = False) -> RepoIndex:
    report = progress or (lambda *_: None)

    report(0.02, "解析仓库地址…")
    repo = resolve_repo(source, settings.cache_dir)
    out = _index_dir(repo, settings)
    fp = _fingerprint(repo.root, settings)
    embeddings = get_embeddings(settings)

    meta_path = out / "meta.json"
    if not force and meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        want_vectors = embeddings is not None
        if meta.get("fingerprint") == fp and meta.get("has_vectors") == want_vectors:
            report(0.5, "命中缓存，加载索引…")
            chunks = [Document(**d) for d in json.loads((out / "chunks.json").read_text(encoding="utf-8"))]
            vs = InMemoryVectorStore.load(str(out / "vectors.json"), embeddings) if want_vectors else None
            report(1.0, "索引已就绪（缓存）")
            return RepoIndex(repo, chunks, vs, meta["stats"] | {"cached": True})

    report(0.08, "读取源码文件…")
    docs = load_documents(repo, settings.max_file_bytes, settings.max_files)
    if not docs:
        raise RuntimeError("仓库中没有找到可索引的源码或文档文件。")

    report(0.15, f"语法感知切分 {len(docs)} 个文件…")
    chunks = split_documents(docs, settings.chunk_size, settings.chunk_overlap)

    vs = None
    if embeddings is not None:
        vs = InMemoryVectorStore(embeddings)
        total = len(chunks)
        for i in range(0, total, EMBED_BATCH):
            batch = chunks[i : i + EMBED_BATCH]
            # 向量化时带上文件路径，让“路由在哪定义”这类问题也能命中
            texts = [f"# {c.metadata['source']}\n{c.page_content}" for c in batch]
            vectors = embeddings.embed_documents(texts)
            vs.store.update(
                {
                    c.id: {"id": c.id, "vector": v, "text": c.page_content, "metadata": c.metadata}
                    for c, v in zip(batch, vectors)
                }
            )
            done = min(i + EMBED_BATCH, total)
            report(0.2 + 0.75 * done / total, f"本地向量化 {done}/{total} 个代码块…")
    else:
        report(0.9, "未检测到 Ollama 向量模型，降级为 BM25 关键词检索")

    stats = _compute_stats(docs, chunks)
    out.mkdir(parents=True, exist_ok=True)
    (out / "chunks.json").write_text(
        json.dumps([{"id": c.id, "page_content": c.page_content, "metadata": c.metadata} for c in chunks], ensure_ascii=False),
        encoding="utf-8",
    )
    if vs is not None:
        vs.dump(str(out / "vectors.json"))
    meta_path.write_text(
        json.dumps({"fingerprint": fp, "has_vectors": vs is not None, "stats": stats, "source": repo.source}, ensure_ascii=False),
        encoding="utf-8",
    )
    report(1.0, "索引构建完成")
    return RepoIndex(repo, chunks, vs, stats | {"cached": False})

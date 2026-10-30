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

# 进度回调：report(进度 0~1, 阶段代码, **参数)，由界面/CLI 自行翻译成文字
ProgressFn = Callable[..., None]
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

    @property
    def id(self) -> str:
        return self.stats.get("id", "")

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


def repo_id(repo: RepoInfo) -> str:
    return f"{repo.name}-{hashlib.md5(str(repo.root).lower().encode()).hexdigest()[:10]}"


def _index_dir(repo: RepoInfo, settings: Settings) -> Path:
    return settings.cache_dir / "index" / repo_id(repo)


def list_cached(settings: Settings) -> list[dict]:
    """列出已缓存的仓库索引（最近使用的在前），供界面“最近打开”使用。"""
    out = []
    for meta_path in (settings.cache_dir / "index").glob("*/meta.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({
            "id": meta_path.parent.name,
            "name": meta.get("name") or meta_path.parent.name.rsplit("-", 1)[0],
            "source": meta.get("source", ""),
            "stats": meta.get("stats", {}),
            "updated": meta_path.stat().st_mtime,
        })
    return sorted(out, key=lambda m: m["updated"], reverse=True)


def _compute_stats(docs: list[Document], chunks: list[Document]) -> dict:
    langs = Counter(d.metadata["language"] for d in docs)
    return {
        "files": len(docs),
        "chunks": len(chunks),
        "lines": sum(d.metadata.get("lines", 0) for d in docs),
        "languages": dict(langs.most_common()),
    }


def build_index(source: str, settings: Settings, progress: ProgressFn | None = None, force: bool = False) -> RepoIndex:
    report = progress or (lambda *_a, **_k: None)

    report(0.02, "resolve")
    repo = resolve_repo(source, settings.cache_dir)
    out = _index_dir(repo, settings)
    fp = _fingerprint(repo.root, settings)
    embeddings = get_embeddings(settings)

    meta_path = out / "meta.json"
    if not force and meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        want_vectors = embeddings is not None
        if meta.get("fingerprint") == fp and meta.get("has_vectors") == want_vectors:
            report(0.5, "cache")
            chunks = [Document(**d) for d in json.loads((out / "chunks.json").read_text(encoding="utf-8"))]
            vs = InMemoryVectorStore.load(str(out / "vectors.json"), embeddings) if want_vectors else None
            report(1.0, "done")
            return RepoIndex(repo, chunks, vs, meta["stats"] | {"cached": True, "id": repo_id(repo)})

    report(0.08, "read")
    docs = load_documents(repo, settings.max_file_bytes, settings.max_files)
    if not docs:
        raise RuntimeError("仓库中没有找到可索引的源码或文档文件。")

    report(0.15, "split", files=len(docs))
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
            report(0.2 + 0.75 * done / total, "embed", done=done, total=total)
    else:
        report(0.9, "bm25_only")

    stats = _compute_stats(docs, chunks)
    out.mkdir(parents=True, exist_ok=True)
    (out / "chunks.json").write_text(
        json.dumps([{"id": c.id, "page_content": c.page_content, "metadata": c.metadata} for c in chunks], ensure_ascii=False),
        encoding="utf-8",
    )
    if vs is not None:
        vs.dump(str(out / "vectors.json"))
    meta_path.write_text(
        json.dumps(
            {"fingerprint": fp, "has_vectors": vs is not None, "stats": stats, "source": repo.source, "name": repo.name},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    report(1.0, "done")
    return RepoIndex(repo, chunks, vs, stats | {"cached": False, "id": repo_id(repo)})

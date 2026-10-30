"""混合检索：BM25（精确匹配标识符）+ 向量（理解语义/中文提问），用 RRF 融合排序。"""

from __future__ import annotations

import re
from typing import Any

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from langchain_core.vectorstores import VectorStore
from pydantic import ConfigDict, PrivateAttr
from rank_bm25 import BM25Plus

DOC_LANGUAGES = {"markdown", "rst", "latex"}
_TOKEN_RE =re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+|[一-鿿]")
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def code_tokenize(text: str) -> list[str]:
    """面向代码的分词：`getUserName` / `get_user_name` 都会拆出 get、user、name。"""
    tokens: list[str] = []
    for tok in _TOKEN_RE.findall(text):
        low = tok.lower()
        tokens.append(low)
        parts = [p.lower() for seg in tok.split("_") for p in _CAMEL_RE.findall(seg)]
        if len(parts) > 1:
            tokens.extend(parts)
    return tokens


class CodeBM25Retriever(BaseRetriever):
    docs: list[Document]
    k: int = 6
    model_config = ConfigDict(arbitrary_types_allowed=True)
    _bm25: BM25Plus = PrivateAttr()
    _vocab: list[set[str]] = PrivateAttr()

    def model_post_init(self, __context: Any) -> None:
        corpus = [code_tokenize(d.metadata.get("source", "") + " " + d.page_content) for d in self.docs]
        # BM25Plus 的 IDF 恒为正，小仓库（文件很少）时也能稳定打分
        self._bm25 = BM25Plus(corpus or [[""]])
        self._vocab = [set(toks) for toks in corpus]

    def _get_relevant_documents(self, query: str, *, run_manager: CallbackManagerForRetrieverRun) -> list[Document]:
        if not self.docs:
            return []
        q = code_tokenize(query)
        scores = self._bm25.get_scores(q)
        q_set = set(q)
        ranked = sorted(
            (i for i in range(len(self.docs)) if q_set & self._vocab[i]), key=lambda i: scores[i], reverse=True
        )
        return [self.docs[i] for i in ranked[: self.k]]


class HybridRetriever(BaseRetriever):
    """Reciprocal Rank Fusion：score = Σ 1/(rrf_k + rank)。向量库缺失时退化为纯 BM25。"""

    bm25: CodeBM25Retriever
    vector_store: VectorStore | None = None
    k: int = 6
    fetch_k: int = 20
    rrf_k: int = 60
    # 文档类片段（README 等）适当降权：讲解代码时优先引用源码本身
    doc_weight: float = 0.8
    model_config = ConfigDict(arbitrary_types_allowed=True)

    def _get_relevant_documents(self, query: str, *, run_manager: CallbackManagerForRetrieverRun) -> list[Document]:
        self.bm25.k = self.fetch_k
        result_lists = [self.bm25.invoke(query)]
        if self.vector_store is not None:
            result_lists.append(self.vector_store.similarity_search(query, k=self.fetch_k))

        scores: dict[str, float] = {}
        by_id: dict[str, Document] = {}
        for results in result_lists:
            for rank, doc in enumerate(results):
                key = doc.id or f"{doc.metadata.get('source')}:{doc.metadata.get('start_line')}"
                by_id[key] = doc
                weight = self.doc_weight if doc.metadata.get("language") in DOC_LANGUAGES else 1.0
                scores[key] = scores.get(key, 0.0) + weight / (self.rrf_k + rank + 1)
        top = sorted(scores, key=scores.get, reverse=True)[: self.k]
        return [by_id[key] for key in top]

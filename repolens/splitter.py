"""语法感知切分：按语言的类/函数边界切块，并为每块记录起止行号，便于精确引用。"""

from __future__ import annotations

from langchain_core.documents import Document
from langchain_text_splitters import Language, RecursiveCharacterTextSplitter

_SUPPORTED = {lang.value for lang in Language}


def _splitter_for(language: str, chunk_size: int, chunk_overlap: int) -> RecursiveCharacterTextSplitter:
    if language in _SUPPORTED:
        return RecursiveCharacterTextSplitter.from_language(
            Language(language), chunk_size=chunk_size, chunk_overlap=chunk_overlap, add_start_index=True
        )
    return RecursiveCharacterTextSplitter(
        chunk_size=chunk_size, chunk_overlap=chunk_overlap, add_start_index=True
    )


def split_documents(docs: list[Document], chunk_size: int = 1500, chunk_overlap: int = 150) -> list[Document]:
    cache: dict[str, RecursiveCharacterTextSplitter] = {}
    chunks: list[Document] = []
    for doc in docs:
        lang = doc.metadata.get("language", "text")
        splitter = cache.setdefault(lang, _splitter_for(lang, chunk_size, chunk_overlap))
        text = doc.page_content
        for piece in splitter.split_documents([doc]):
            start = piece.metadata.pop("start_index", 0)
            start = max(start, 0)
            start_line = text.count("\n", 0, start) + 1
            end_line = start_line + piece.page_content.count("\n")
            piece.metadata.update(start_line=start_line, end_line=end_line)
            piece.metadata.pop("lines", None)
            chunks.append(piece)
    for i, c in enumerate(chunks):
        c.id = f"{c.metadata['source']}:{c.metadata['start_line']}:{i}"
    return chunks

"""模型工厂：同一套业务代码，在线 DeepSeek 与本地 Ollama 一键切换。"""

from __future__ import annotations

from functools import lru_cache

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel

from .config import Settings


class ModelConfigError(RuntimeError):
    pass


def get_chat_model(settings: Settings, streaming: bool = True) -> BaseChatModel:
    if settings.provider == "deepseek":
        if not settings.deepseek_api_key:
            raise ModelConfigError("未配置 DEEPSEEK_API_KEY，请在 .env 或侧边栏中填写，或切换到本地 Ollama。")
        from langchain_deepseek import ChatDeepSeek

        return ChatDeepSeek(
            model=settings.deepseek_model,
            api_key=settings.deepseek_api_key,
            temperature=settings.temperature,
            streaming=streaming,
            max_retries=2,
        )
    if settings.provider == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(
            model=settings.ollama_model,
            base_url=settings.ollama_base_url,
            temperature=settings.temperature,
            # 推理模型（如 deepseek-r1）的 <think> 内容单独存放，不混入回答
            reasoning=False,
        )
    raise ModelConfigError(f"未知的模型提供方：{settings.provider}")


@lru_cache(maxsize=4)
def _ollama_embeddings(model: str, base_url: str) -> Embeddings:
    from langchain_ollama import OllamaEmbeddings

    return OllamaEmbeddings(model=model, base_url=base_url)


def get_embeddings(settings: Settings) -> Embeddings | None:
    """返回本地向量模型；Ollama 不可用时返回 None，检索自动降级为纯 BM25。"""
    # 先用带超时的列表接口探测：Ollama 未启动时 embed 请求可能长时间挂起
    base = settings.embed_model.split(":")[0]
    if not any(m.split(":")[0] == base for m in list_ollama_models(settings.ollama_base_url)):
        return None
    try:
        emb = _ollama_embeddings(settings.embed_model, settings.ollama_base_url)
        emb.embed_query("ping")
        return emb
    except Exception:
        return None


def list_ollama_models(base_url: str) -> list[str]:
    try:
        import httpx

        r = httpx.get(f"{base_url}/api/tags", timeout=2)
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return []

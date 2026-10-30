"""Web 后端：FastAPI + Server-Sent Events 流式接口，前端位于 web/ 目录。

启动：python -m repolens serve   或   uvicorn repolens.server:app
"""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Iterator
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .agent import RepoAgent, _safe_path
from .chains import RepoAssistant
from .config import PROJECT_ROOT, Settings, get_settings
from .index import RepoIndex, build_index, list_cached
from .llm import get_chat_model, list_ollama_models
from .loader import language_of, read_text

WEB_DIR = PROJECT_ROOT / "web"
MAX_TOOL_PREVIEW = 2000

app = FastAPI(title="RepoLens", version=__version__)
REPOS: dict[str, RepoIndex] = {}
_lock = threading.Lock()


# ---------------- 请求模型 ----------------
class ModelChoice(BaseModel):
    provider: Literal["deepseek", "ollama"] | None = None
    model: str | None = None
    api_key: str | None = None


class IndexRequest(BaseModel):
    source: str
    force: bool = False


class BaseTask(BaseModel):
    repo_id: str
    lang: Literal["zh", "en"] = "zh"
    llm: ModelChoice = Field(default_factory=ModelChoice)


class ChatRequest(BaseTask):
    question: str
    history: list[dict] = Field(default_factory=list)
    mode: Literal["ask", "agent"] = "ask"


class ExplainRequest(BaseTask):
    path: str


# ---------------- 工具函数 ----------------
def _settings_for(choice: ModelChoice | None = None) -> Settings:
    choice = choice or ModelChoice()
    s = get_settings(provider=choice.provider)
    if choice.model:
        if s.provider == "deepseek":
            s.deepseek_model = choice.model
        else:
            s.ollama_model = choice.model
    if choice.api_key:
        s.deepseek_api_key = choice.api_key
    return s


def _repo(repo_id: str) -> RepoIndex:
    idx = REPOS.get(repo_id)
    if idx is None:
        raise HTTPException(404, "repo_not_loaded")
    return idx


def _sse(events: Iterator[dict]) -> StreamingResponse:
    def gen():
        try:
            for ev in events:
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001 — 错误也通过事件流告诉前端
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
        yield 'data: {"type": "done"}\n\n'

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


def _tokens(chunks: Iterator[str]) -> Iterator[dict]:
    for text in chunks:
        if text:
            yield {"type": "token", "text": text}


def repo_payload(idx: RepoIndex) -> dict:
    langs: dict[str, str] = {}
    for c in idx.chunks:
        langs.setdefault(c.metadata["source"], c.metadata.get("language", "text"))
    return {
        "id": idx.id,
        "name": idx.repo.name,
        "source": idx.repo.source,
        "stats": {k: v for k, v in idx.stats.items() if k != "id"},
        "has_vectors": idx.has_vectors,
        "files": [{"path": p, "language": langs[p]} for p in sorted(langs)],
    }


# ---------------- 接口 ----------------
@app.get("/api/settings")
def api_settings():
    s = get_settings()
    models = list_ollama_models(s.ollama_base_url)
    embed_base = s.embed_model.split(":")[0]
    return {
        "version": __version__,
        "provider": s.provider,
        "deepseek": {"model": s.deepseek_model, "has_key": bool(s.deepseek_api_key),
                     "models": list(dict.fromkeys([s.deepseek_model, "deepseek-chat", "deepseek-reasoner"]))},
        "ollama": {"model": s.ollama_model, "online": bool(models),
                   "models": [m for m in models if "embed" not in m and not m.startswith("bge")]},
        "embed": {"model": s.embed_model, "ok": any(m.split(":")[0] == embed_base for m in models)},
        "project_root": str(PROJECT_ROOT),
    }


@app.get("/api/recent")
def api_recent():
    return list_cached(get_settings())[:8]


@app.post("/api/index")
def api_index(req: IndexRequest):
    settings = get_settings()
    q: queue.Queue = queue.Queue()

    def work():
        try:
            idx = build_index(
                req.source, settings, force=req.force,
                progress=lambda p, key, **kw: q.put({"type": "progress", "p": round(p, 3), "key": key, "params": kw}),
            )
            with _lock:
                REPOS[idx.id] = idx
            q.put({"type": "repo", "repo": repo_payload(idx)})
        except Exception as e:  # noqa: BLE001
            q.put({"type": "error", "message": str(e)})
        q.put(None)

    threading.Thread(target=work, daemon=True).start()

    def events():
        while (ev := q.get()) is not None:
            yield ev

    return _sse(events())


@app.get("/api/repos/{repo_id}")
def api_repo(repo_id: str):
    return repo_payload(_repo(repo_id))


@app.get("/api/repos/{repo_id}/file")
def api_file(repo_id: str, path: str):
    idx = _repo(repo_id)
    try:
        p = _safe_path(idx.repo.root.resolve(), path)
    except ValueError:
        raise HTTPException(400, "bad_path") from None
    if not p.is_file():
        raise HTTPException(404, "file_not_found")
    return {"path": path, "language": language_of(p) or "text", "content": read_text(p) or ""}


@app.post("/api/chat")
def api_chat(req: ChatRequest):
    idx = _repo(req.repo_id)
    settings = _settings_for(req.llm)

    def events():
        llm = get_chat_model(settings)
        if req.mode == "agent":
            agent = RepoAgent(idx, llm, lang=req.lang)
            for ev in agent.stream(req.question, req.history):
                if ev.kind == "token":
                    yield {"type": "token", "text": ev.content}
                elif ev.kind == "tool_call":
                    yield {"type": "tool_call", "name": ev.name, "args": ev.args}
                elif ev.kind == "tool_result":
                    yield {"type": "tool_result", "name": ev.name, "content": ev.content[:MAX_TOOL_PREVIEW]}
                else:
                    yield {"type": "answer", "text": ev.content}
            return

        assistant = RepoAssistant(idx, llm, k=settings.top_k, lang=req.lang)
        r = assistant.retrieve(req.question, req.history)
        yield {"type": "query", "query": r.query}
        yield {
            "type": "sources",
            "sources": [
                {**{k: d.metadata[k] for k in ("source", "start_line", "end_line", "language")}, "content": d.page_content}
                for d in r.docs
            ],
        }
        yield from _tokens(assistant.stream_answer(req.question, r, req.history))

    return _sse(events())


@app.post("/api/overview")
def api_overview(req: BaseTask):
    idx = _repo(req.repo_id)
    settings = _settings_for(req.llm)

    # 模型在生成器内部创建：配置错误（如缺少 Key）也能以 error 事件返回给前端
    def events():
        yield from _tokens(RepoAssistant(idx, get_chat_model(settings), lang=req.lang).stream_overview())

    return _sse(events())


@app.post("/api/explain")
def api_explain(req: ExplainRequest):
    idx = _repo(req.repo_id)
    settings = _settings_for(req.llm)

    def events():
        assistant = RepoAssistant(idx, get_chat_model(settings), lang=req.lang)
        yield {"type": "references", "items": assistant.references_to(req.path)}
        yield from _tokens(assistant.stream_explain_file(req.path))

    return _sse(events())


# ---------------- 静态前端 ----------------
@app.get("/", include_in_schema=False)
def index_page():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/", StaticFiles(directory=WEB_DIR), name="web")

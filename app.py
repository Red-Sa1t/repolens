"""RepoLens Web 界面：streamlit run app.py"""

from __future__ import annotations

import json

import streamlit as st

from repolens.agent import RepoAgent
from repolens.chains import RepoAssistant
from repolens.config import PROJECT_ROOT, get_settings
from repolens.index import build_index
from repolens.llm import ModelConfigError, get_chat_model, list_ollama_models
from repolens.loader import read_text

st.set_page_config(page_title="RepoLens · 代码仓库讲解员", page_icon="🔭", layout="wide")

st.markdown(
    """
    <style>
      .block-container {padding-top: 2rem; max-width: 1200px;}
      .hero h1 {font-size: 2.2rem; margin-bottom: .2rem;}
      .hero p {color: #6b7280; font-size: 1.05rem;}
      .card {border: 1px solid rgba(128,128,128,.25); border-radius: 12px; padding: 1rem 1.1rem; height: 100%;}
      .card h4 {margin: 0 0 .4rem 0;}
      .src {font-family: ui-monospace, monospace; font-size: .85rem;}
    </style>
    """,
    unsafe_allow_html=True,
)

TOOL_LABELS = {
    "search_code": "🔎 混合检索",
    "grep_code": "🧵 正则搜索",
    "list_directory": "📁 查看目录",
    "read_file": "📖 读取文件",
}
SUGGESTIONS = [
    "这个项目是做什么的？整体架构是怎样的？",
    "程序的入口在哪里？启动后的执行流程是什么？",
    "核心的数据结构或模型有哪些？",
    "如果我要新增一个功能，应该改哪些文件？",
]

ss = st.session_state
ss.setdefault("index", None)
ss.setdefault("history", [])  # [{role, content, sources?, steps?, mode?}]
ss.setdefault("overview", None)
ss.setdefault("explanations", {})
ss.setdefault("pending", None)


# ---------------- 侧边栏 ----------------
with st.sidebar:
    st.markdown("## 🔭 RepoLens")
    st.caption("基于 LangChain 的代码仓库讲解员")

    st.markdown("#### ① 选择大模型")
    base = get_settings()
    provider_label = st.radio(
        "模型来源", ["DeepSeek（在线）", "Ollama（本地离线）"],
        index=0 if base.provider == "deepseek" else 1, horizontal=True, label_visibility="collapsed",
    )
    overrides: dict = {"provider": "deepseek" if provider_label.startswith("DeepSeek") else "ollama"}
    if overrides["provider"] == "deepseek":
        overrides["deepseek_api_key"] = st.text_input(
            "DeepSeek API Key", value=base.deepseek_api_key, type="password", placeholder="sk-...",
            help="也可以写在 .env 的 DEEPSEEK_API_KEY 中",
        )
        overrides["deepseek_model"] = st.selectbox(
            "模型", ["deepseek-chat", "deepseek-reasoner"],
            help="deepseek-chat 速度快、支持工具调用；deepseek-reasoner 推理更深",
        )
    else:
        models = list_ollama_models(base.ollama_base_url) or [base.ollama_model]
        chat_models = [m for m in models if "embed" not in m and "bge" not in m] or models
        default = next((i for i, m in enumerate(chat_models) if m.startswith(base.ollama_model)), 0)
        overrides["ollama_model"] = st.selectbox("本地模型", chat_models, index=default)
        st.caption("提示：Agent 模式需要支持工具调用的模型（如 llama3.2、qwen2.5）")

    settings = get_settings(**overrides)

    st.markdown("#### ② 加载代码仓库")
    source = st.text_input(
        "本地路径或 GitHub 地址", value=ss.get("source", ""),
        placeholder="D:/projects/demo 或 https://github.com/user/repo",
    )
    c1, c2 = st.columns(2)
    build = c1.button("构建索引", type="primary", use_container_width=True, disabled=not source.strip())
    if c2.button("用本项目试试", use_container_width=True):
        source, build = str(PROJECT_ROOT), True
    force = st.checkbox("忽略缓存，强制重建", value=False)

    if build:
        ss.source = source
        bar = st.progress(0.0, text="准备中…")
        try:
            idx = build_index(source, settings, progress=lambda p, m: bar.progress(min(p, 1.0), text=m), force=force)
        except Exception as e:  # noqa: BLE001
            bar.empty()
            st.error(f"构建失败：{e}")
        else:
            bar.empty()
            ss.index, ss.history, ss.overview, ss.explanations = idx, [], None, {}
            st.toast(f"索引就绪：{idx.repo.name}", icon="✅")

    idx = ss.index
    if idx is not None:
        st.markdown(f"#### 📦 {idx.repo.name}")
        m1, m2, m3 = st.columns(3)
        m1.metric("文件", idx.stats["files"])
        m2.metric("代码块", idx.stats["chunks"])
        m3.metric("行数", f"{idx.stats['lines'] / 1000:.1f}k" if idx.stats["lines"] >= 1000 else idx.stats["lines"])
        st.bar_chart(idx.stats["languages"], horizontal=True, height=180)
        mode = "BM25 + 向量混合检索" if idx.has_vectors else "仅 BM25（未检测到本地向量模型）"
        st.caption(f"检索模式：{mode}{' · 来自缓存' if idx.stats.get('cached') else ''}")
        if st.button("清空对话", use_container_width=True):
            ss.history = []
            st.rerun()
    else:
        emb_ok = any(m.split(":")[0] == settings.embed_model.split(":")[0] for m in list_ollama_models(settings.ollama_base_url))
        st.caption(("✅" if emb_ok else "⚠️") + f" 本地向量模型 `{settings.embed_model}`" + ("" if emb_ok else " 不可用，将使用 BM25"))


# ---------------- 欢迎页 ----------------
def welcome():
    st.markdown(
        '<div class="hero"><h1>🔭 RepoLens · 代码仓库讲解员</h1>'
        "<p>接手陌生项目？丢给 RepoLens 一个本地目录或 GitHub 地址，它会读懂整个仓库，"
        "用中文回答你的问题，并精确到 <code>文件:行号</code>。</p></div>",
        unsafe_allow_html=True,
    )
    st.write("")
    cols = st.columns(4)
    cards = [
        ("💬 引用式问答", "混合检索相关代码，回答附带出处，可点开核对原文。"),
        ("🤖 Agent 深度探索", "模型自主检索、grep、读文件，多步追踪调用链。"),
        ("🗺️ 项目导览", "一键生成技术栈、模块职责、推荐阅读顺序。"),
        ("📄 文件讲解", "逐函数讲解任意文件，并找出谁引用了它。"),
    ]
    for col, (t, d) in zip(cols, cards):
        col.markdown(f'<div class="card"><h4>{t}</h4>{d}</div>', unsafe_allow_html=True)
    st.write("")
    st.info("👈 在左侧选择模型并加载一个仓库开始。代码向量化全程在本地 Ollama 完成，源码不会上传给向量服务。")


def make_llm():
    try:
        return get_chat_model(settings)
    except ModelConfigError as e:
        st.error(str(e))
        st.stop()


def render_sources(docs_meta: list[dict]):
    with st.expander(f"📚 参考了 {len(docs_meta)} 个代码片段", expanded=False):
        for i, d in enumerate(docs_meta, 1):
            st.markdown(f'<span class="src">[{i}] {d["source"]} · L{d["start_line"]}-L{d["end_line"]}</span>', unsafe_allow_html=True)
            st.code(d["content"], language=d["language"] if d["language"] != "text" else None, line_numbers=False)


def render_steps(steps: list[dict]):
    with st.expander(f"🧭 探索过程（{sum(s['kind'] == 'tool_call' for s in steps)} 次工具调用）", expanded=False):
        for s in steps:
            if s["kind"] == "tool_call":
                st.markdown(f"**{TOOL_LABELS.get(s['name'], s['name'])}** `{json.dumps(s['args'], ensure_ascii=False)}`")
            else:
                st.code(s["content"][:1500] + ("…" if len(s["content"]) > 1500 else ""), language=None)


# ---------------- 对话 ----------------
def chat_tab(idx):
    mode = st.segmented_control(
        "模式", ["💬 检索问答", "🤖 Agent 探索"], default="💬 检索问答", label_visibility="collapsed"
    ) or "💬 检索问答"
    st.caption(
        "检索问答：一次检索、快速作答，适合“X 在哪 / 是什么”。"
        if mode.startswith("💬") else
        "Agent 探索：模型自主多步调用工具，适合“完整调用链是怎样的 / 改某功能要动哪些地方”，耗时更长。"
    )

    # 先占位消息区，再放输入框，保证输入框始终在对话下方
    box = st.container()
    question = st.chat_input("问问这个仓库，例如：用户登录的逻辑在哪里实现？")
    question = question or ss.pop("pending", None)
    with box:
        _chat_body(idx, mode, question)


def _chat_body(idx, mode: str, question: str | None):
    if not ss.history and not question:
        st.markdown("**试试这些问题：**")
        cols = st.columns(2)
        for i, q in enumerate(SUGGESTIONS):
            if cols[i % 2].button(q, key=f"sug{i}", use_container_width=True):
                ss.pending = q
                st.rerun()

    for h in ss.history:
        with st.chat_message(h["role"], avatar="🧑‍💻" if h["role"] == "user" else "🔭"):
            if h.get("steps"):
                render_steps(h["steps"])
            st.markdown(h["content"])
            if h.get("sources"):
                render_sources(h["sources"])

    if not question:
        return

    history = [{"role": h["role"], "content": h["content"]} for h in ss.history]
    ss.history.append({"role": "user", "content": question})
    with st.chat_message("user", avatar="🧑‍💻"):
        st.markdown(question)

    llm = make_llm()
    with st.chat_message("assistant", avatar="🔭"):
        try:
            if mode.startswith("💬"):
                assistant = RepoAssistant(idx, llm, k=settings.top_k)
                with st.spinner("检索相关代码…"):
                    r = assistant.retrieve(question, history)
                sources = [
                    {**{k: d.metadata[k] for k in ("source", "start_line", "end_line", "language")}, "content": d.page_content}
                    for d in r.docs
                ]
                if r.query != question:
                    st.caption(f"🔁 检索语句：{r.query}")
                answer = st.write_stream(assistant.stream_answer(question, r, history))
                render_sources(sources)
                ss.history.append({"role": "assistant", "content": answer, "sources": sources})
            else:
                agent = RepoAgent(idx, llm)
                steps, answer = [], ""
                with st.status("Agent 正在探索仓库…", expanded=True) as status:
                    for ev in agent.stream(question, history):
                        if ev.kind == "tool_call":
                            steps.append({"kind": "tool_call", "name": ev.name, "args": ev.args})
                            st.markdown(f"**{TOOL_LABELS.get(ev.name, ev.name)}** `{json.dumps(ev.args, ensure_ascii=False)}`")
                        elif ev.kind == "tool_result":
                            steps.append({"kind": "tool_result", "name": ev.name, "content": ev.content})
                        else:
                            answer = ev.content
                    n = sum(s["kind"] == "tool_call" for s in steps)
                    status.update(label=f"探索完成，共调用 {n} 次工具", state="complete", expanded=False)
                st.markdown(answer or "（Agent 没有给出结论，请换个问法或换用更强的模型）")
                ss.history.append({"role": "assistant", "content": answer, "steps": steps})
        except Exception as e:  # noqa: BLE001
            st.error(f"调用模型失败：{e}")
            ss.history.pop()


# ---------------- 项目导览 ----------------
def overview_tab(idx):
    c1, c2 = st.columns([1, 4])
    gen = c1.button("✨ 生成导览" if not ss.overview else "🔄 重新生成", type="primary")
    if ss.overview:
        c2.download_button("⬇️ 导出 Markdown", ss.overview, file_name=f"{idx.repo.name}-导览.md")
    if gen:
        assistant = RepoAssistant(idx, make_llm())
        try:
            ss.overview = st.write_stream(assistant.stream_overview())
        except Exception as e:  # noqa: BLE001
            st.error(f"调用模型失败：{e}")
        st.rerun()
    elif ss.overview:
        st.markdown(ss.overview)
    else:
        st.caption("RepoLens 会读取目录结构、README、依赖清单和入口文件，为你生成一份新人导览。")
        st.code(idx.tree(2), language=None)


# ---------------- 文件讲解 ----------------
def file_tab(idx):
    files = idx.files
    left, right = st.columns([1, 1], gap="large")
    with left:
        rel = st.selectbox("选择文件", files, placeholder="搜索文件名…")
        if rel:
            text = read_text(idx.repo.root / rel) or ""
            lang = next((c.metadata["language"] for c in idx.chunks if c.metadata["source"] == rel), None)
            st.code(text[:20000], language=None if lang == "text" else lang, line_numbers=True, height=560)
    with right:
        if not rel:
            return
        cached = ss.explanations.get(rel)
        if st.button("🧑‍🏫 讲解这个文件" if not cached else "🔄 重新讲解", type="primary"):
            assistant = RepoAssistant(idx, make_llm())
            try:
                ss.explanations[rel] = st.write_stream(assistant.stream_explain_file(rel))
            except Exception as e:  # noqa: BLE001
                st.error(f"调用模型失败：{e}")
        elif cached:
            st.markdown(cached)
        else:
            st.caption("点击按钮，RepoLens 会逐个函数讲解这个文件，并指出它在仓库中被谁引用。")


idx = ss.index
if idx is None:
    welcome()
else:
    t1, t2, t3 = st.tabs(["💬 对话", "🗺️ 项目导览", "📄 文件讲解"])
    with t1:
        chat_tab(idx)
    with t2:
        overview_tab(idx)
    with t3:
        file_tab(idx)

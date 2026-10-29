"""命令行入口：python -m repolens <命令> <仓库> [...]"""

from __future__ import annotations

import argparse
import sys

from .agent import RepoAgent
from .chains import RepoAssistant
from .config import get_settings
from .index import build_index
from .llm import get_chat_model


def _progress(p: float, msg: str) -> None:
    print(f"\r[{p * 100:5.1f}%] {msg:<40}", end="", file=sys.stderr, flush=True)
    if p >= 1:
        print(file=sys.stderr)


def _print_stream(chunks) -> str:
    out = []
    for c in chunks:
        print(c, end="", flush=True)
        out.append(c)
    print()
    return "".join(out)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(prog="repolens", description="RepoLens —— 基于 LangChain 的代码仓库讲解员")
    parser.add_argument("--provider", choices=["deepseek", "ollama"], help="对话模型来源（默认读取 .env）")
    parser.add_argument("--model", help="模型名，如 deepseek-chat / llama3.2")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("index", help="构建或刷新仓库索引")
    p.add_argument("repo")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("ask", help="对仓库提一个问题")
    p.add_argument("repo")
    p.add_argument("question")
    p.add_argument("--agent", action="store_true", help="使用 Agent 多步探索模式")

    p = sub.add_parser("overview", help="生成项目导览")
    p.add_argument("repo")

    p = sub.add_parser("explain", help="讲解单个文件")
    p.add_argument("repo")
    p.add_argument("file", help="仓库内相对路径")

    p = sub.add_parser("chat", help="交互式连续问答")
    p.add_argument("repo")
    p.add_argument("--agent", action="store_true")

    args = parser.parse_args(argv)
    overrides = {"provider": args.provider}
    if args.model:
        overrides["deepseek_model" if (args.provider or get_settings().provider) == "deepseek" else "ollama_model"] = args.model
    settings = get_settings(**overrides)

    index = build_index(args.repo, settings, progress=_progress, force=getattr(args, "force", False))
    s = index.stats
    print(f"📦 {index.repo.name}: {s['files']} 文件 / {s['chunks']} 代码块 / "
          f"{'混合检索' if index.has_vectors else '仅 BM25'}", file=sys.stderr)
    if args.cmd == "index":
        return 0

    llm = get_chat_model(settings)
    if args.cmd == "ask":
        if args.agent:
            for ev in RepoAgent(index, llm).stream(args.question):
                if ev.kind == "tool_call":
                    print(f"  → {ev.name}({ev.args})", file=sys.stderr)
                elif ev.kind == "answer":
                    print(ev.content)
        else:
            assistant = RepoAssistant(index, llm, k=settings.top_k)
            r = assistant.retrieve(args.question)
            _print_stream(assistant.stream_answer(args.question, r))
            print("\n参考片段：", *(f"  {d.metadata['source']}:L{d.metadata['start_line']}" for d in r.docs), sep="\n")
    elif args.cmd == "overview":
        _print_stream(RepoAssistant(index, llm).stream_overview())
    elif args.cmd == "explain":
        _print_stream(RepoAssistant(index, llm).stream_explain_file(args.file))
    elif args.cmd == "chat":
        assistant, agent = RepoAssistant(index, llm, k=settings.top_k), RepoAgent(index, llm) if args.agent else None
        history: list[dict] = []
        print("输入问题开始对话，输入 exit 退出。")
        while True:
            try:
                q = input("\n🧑‍💻 > ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if q.lower() in {"exit", "quit", "q"}:
                break
            if not q:
                continue
            print("🔭 ", end="")
            if agent:
                a = agent.run(q, history)
                print(a)
            else:
                a = _print_stream(assistant.stream_answer(q, assistant.retrieve(q, history), history))
            history += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

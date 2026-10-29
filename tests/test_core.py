from langchain_core.documents import Document
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from repolens import index as index_mod
from repolens.agent import _safe_path, make_tools
from repolens.chains import RepoAssistant, format_docs
from repolens.index import build_index
from repolens.loader import build_tree, is_remote, iter_repo_files
from repolens.retriever import CodeBM25Retriever, code_tokenize
from repolens.splitter import split_documents


def test_code_tokenize_splits_identifiers():
    toks = code_tokenize("getUserName check_password 登录")
    assert {"getusername", "get", "user", "name", "check_password", "check", "password", "登", "录"} <= set(toks)


def test_is_remote():
    assert is_remote("https://github.com/langchain-ai/langchain")
    assert is_remote("github.com/user/repo.git")
    assert not is_remote("D:/projects/demo")


def test_loader_respects_ignore_rules(sample_repo):
    files = {p.relative_to(sample_repo).as_posix() for p in iter_repo_files(sample_repo)}
    # node_modules、二进制图片、.gitignore 命中的 *.log 都应被排除
    assert files == {"README.md", "src/auth.py", "src/app.py"}


def test_splitter_tracks_line_numbers():
    text = "\n".join(f"def f{i}():\n    return {i}\n" for i in range(40))
    chunks = split_documents([Document(page_content=text, metadata={"source": "a.py", "language": "python"})], 120, 0)
    assert len(chunks) > 1
    lines = text.splitlines()
    for c in chunks:
        first = c.page_content.splitlines()[0]
        assert lines[c.metadata["start_line"] - 1] == first
        assert c.metadata["end_line"] >= c.metadata["start_line"]


def test_bm25_finds_identifier():
    docs = [
        Document(page_content="def login(user): pass", metadata={"source": "auth.py"}),
        Document(page_content="def render(page): pass", metadata={"source": "view.py"}),
    ]
    hits = CodeBM25Retriever(docs=docs, k=1).invoke("where is login")
    assert hits[0].metadata["source"] == "auth.py"


def test_build_index_and_cache(sample_repo, settings):
    idx = build_index(str(sample_repo), settings)
    assert idx.has_vectors and idx.stats["files"] == 3 and not idx.stats["cached"]
    # 测试用的是随机假向量，只断言 BM25 命中的文件能进入融合结果
    top = idx.retriever(k=2).invoke("UserService login check_password")
    assert "src/auth.py" in {d.metadata["source"] for d in top}

    again = build_index(str(sample_repo), settings)
    assert again.stats["cached"] and len(again.chunks) == len(idx.chunks)

    (sample_repo / "src" / "new.py").write_text("x = 1\n", encoding="utf-8")
    rebuilt = build_index(str(sample_repo), settings)
    assert not rebuilt.stats["cached"] and rebuilt.stats["files"] == 4


def test_build_index_without_embeddings_falls_back_to_bm25(sample_repo, settings, monkeypatch):
    monkeypatch.setattr(index_mod, "get_embeddings", lambda _s: None)
    idx = build_index(str(sample_repo), settings)
    assert not idx.has_vectors
    assert idx.retriever(k=1).invoke("check_password")[0].metadata["source"] == "src/auth.py"


def test_agent_tools(sample_repo, settings):
    idx = build_index(str(sample_repo), settings)
    tools = {t.name: t for t in make_tools(idx)}
    assert "src/auth.py:7:" in tools["grep_code"].invoke({"pattern": r"def login"})
    out = tools["read_file"].invoke({"path": "src/auth.py", "start_line": 4, "end_line": 5})
    assert "class UserService" in out and "L4-L5" in out
    assert "auth.py" in tools["list_directory"].invoke({"path": "src"})
    assert "越界" in tools["read_file"].invoke({"path": "../../etc/passwd"})
    assert "src/auth.py" in tools["search_code"].invoke({"query": "UserService login"})


def test_safe_path_blocks_escape(tmp_path):
    root = tmp_path.resolve()
    assert _safe_path(root, "a/b.py") == root / "a" / "b.py"
    try:
        _safe_path(root, "../x")
    except ValueError:
        pass
    else:
        raise AssertionError("should block path traversal")


def test_qa_chain_and_references(sample_repo, settings):
    idx = build_index(str(sample_repo), settings)
    llm = FakeListChatModel(responses=["登录逻辑在 [1] `src/auth.py:7`。"])
    assistant = RepoAssistant(idx, llm, k=3)
    answer, retrieved = assistant.ask("UserService.login 在哪里？")
    assert "src/auth.py" in answer and retrieved.docs
    assert "[1] " in format_docs(retrieved.docs)
    refs = assistant.references_to("src/auth.py")
    assert any("src/app.py:1" in r for r in refs)


def test_build_tree(sample_repo):
    tree = build_tree(sample_repo)
    assert "src/" in tree and "auth.py" in tree and "node_modules" not in tree

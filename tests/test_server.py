import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from repolens import server


def events(resp) -> list[dict]:
    return [json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")]


@pytest.fixture
def client(settings, monkeypatch):
    monkeypatch.setattr(server, "get_settings", lambda **kw: settings)
    monkeypatch.setattr(server, "get_chat_model", lambda _s: FakeListChatModel(responses=["登录在 [1] `src/auth.py:7`。"]))
    server.REPOS.clear()
    return TestClient(server.app)


def _load(client, sample_repo) -> dict:
    evs = events(client.post("/api/index", json={"source": str(sample_repo)}))
    assert evs[-1] == {"type": "done"}
    assert any(e["type"] == "progress" and e["key"] == "split" for e in evs)
    return next(e["repo"] for e in evs if e["type"] == "repo")


def test_index_repo_and_files(client, sample_repo):
    repo = _load(client, sample_repo)
    assert repo["stats"]["files"] == 3 and repo["has_vectors"]
    assert {"path": "src/auth.py", "language": "python"} in repo["files"]

    assert client.get(f"/api/repos/{repo['id']}").json()["name"] == "demo"
    f = client.get(f"/api/repos/{repo['id']}/file", params={"path": "src/auth.py"}).json()
    assert "class UserService" in f["content"]
    assert client.get(f"/api/repos/{repo['id']}/file", params={"path": "../../x"}).status_code == 400
    assert client.get("/api/repos/nope").status_code == 404
    assert client.get("/api/recent").json()[0]["id"] == repo["id"]


def test_index_error_is_reported_as_event(client, tmp_path):
    evs = events(client.post("/api/index", json={"source": str(tmp_path / "missing")}))
    assert any(e["type"] == "error" for e in evs) and not any(e["type"] == "repo" for e in evs)


def test_chat_ask_streams_sources_and_tokens(client, sample_repo):
    repo = _load(client, sample_repo)
    evs = events(client.post("/api/chat", json={"repo_id": repo["id"], "question": "UserService login", "lang": "en"}))
    kinds = [e["type"] for e in evs]
    assert kinds[0] == "query" and "sources" in kinds and kinds[-1] == "done"
    sources = next(e["sources"] for e in evs if e["type"] == "sources")
    assert any(s["source"] == "src/auth.py" for s in sources)
    assert "".join(e["text"] for e in evs if e["type"] == "token") == "登录在 [1] `src/auth.py:7`。"


def test_overview_and_explain(client, sample_repo):
    repo = _load(client, sample_repo)
    tokens = [e for e in events(client.post("/api/overview", json={"repo_id": repo["id"]})) if e["type"] == "token"]
    assert tokens
    evs = events(client.post("/api/explain", json={"repo_id": repo["id"], "path": "src/auth.py"}))
    assert evs[0]["type"] == "references" and any("src/app.py" in r for r in evs[0]["items"])


def test_static_frontend_served(client):
    assert "RepoLens" in client.get("/").text
    assert client.get("/app.js").status_code == 200

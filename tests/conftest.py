import textwrap
from pathlib import Path

import pytest
from langchain_core.embeddings import DeterministicFakeEmbedding

from repolens import index as index_mod
from repolens.config import get_settings


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    root = tmp_path / "demo"
    (root / "src").mkdir(parents=True)
    (root / "node_modules" / "x").mkdir(parents=True)
    (root / "README.md").write_text("# Demo\n一个演示用的小项目。\n", encoding="utf-8")
    (root / "src" / "auth.py").write_text(
        textwrap.dedent(
            '''\
            import hashlib


            class UserService:
                """用户服务"""

                def login(self, username, password):
                    digest = hashlib.sha256(password.encode()).hexdigest()
                    return self.check_password(username, digest)

                def check_password(self, username, digest):
                    return digest == "x"
            '''
        ),
        encoding="utf-8",
    )
    (root / "src" / "app.py").write_text(
        "from auth import UserService\n\n\ndef main():\n    UserService().login('a', 'b')\n", encoding="utf-8"
    )
    (root / "node_modules" / "x" / "index.js").write_text("module.exports = 1", encoding="utf-8")
    (root / "logo.png").write_bytes(b"\x89PNG\x00\x00")
    (root / "secret.log").write_text("ignored", encoding="utf-8")
    (root / ".gitignore").write_text("*.log\n", encoding="utf-8")
    return root


@pytest.fixture
def settings(tmp_path, monkeypatch):
    s = get_settings(cache_dir=tmp_path / "cache", chunk_size=300, chunk_overlap=0)
    monkeypatch.setattr(index_mod, "get_embeddings", lambda _s: DeterministicFakeEmbedding(size=32))
    return s

"""集中管理配置：环境变量 / .env 文件 → Settings 对象。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass
class Settings:
    # 对话模型：deepseek（在线）或 ollama（本地）
    provider: str = field(default_factory=lambda: _env("REPOLENS_PROVIDER", "deepseek"))
    deepseek_api_key: str = field(default_factory=lambda: _env("DEEPSEEK_API_KEY"))
    deepseek_model: str = field(default_factory=lambda: _env("DEEPSEEK_MODEL", "deepseek-chat"))
    ollama_base_url: str = field(default_factory=lambda: _env("OLLAMA_BASE_URL", "http://localhost:11434"))
    ollama_model: str = field(default_factory=lambda: _env("OLLAMA_MODEL", "llama3.2"))
    # 向量模型始终走本地 Ollama，代码不出本机
    embed_model: str = field(default_factory=lambda: _env("REPOLENS_EMBED_MODEL", "bge-m3"))
    temperature: float = 0.1

    # 索引参数
    chunk_size: int = 1500
    chunk_overlap: int = 150
    max_file_bytes: int = 200_000
    max_files: int = 3000
    top_k: int = 6

    cache_dir: Path = field(
        default_factory=lambda: Path(_env("REPOLENS_CACHE_DIR", str(PROJECT_ROOT / ".repolens_cache")))
    )

    @property
    def model_name(self) -> str:
        return self.deepseek_model if self.provider == "deepseek" else self.ollama_model


def get_settings(**overrides) -> Settings:
    s = Settings()
    for k, v in overrides.items():
        if v is not None and hasattr(s, k):
            setattr(s, k, v)
    return s

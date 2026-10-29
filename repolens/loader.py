"""仓库加载：本地目录或 GitHub 地址 → LangChain Document 列表。"""

from __future__ import annotations

import fnmatch
import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from langchain_core.documents import Document

# 扩展名 → 语言标识（与 langchain_text_splitters.Language 取值一致，None 表示按普通文本切分）
EXT_LANGUAGE: dict[str, str | None] = {
    ".py": "python", ".pyi": "python",
    ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js", ".vue": "js",
    ".ts": "ts", ".tsx": "ts",
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".scala": "scala",
    ".go": "go", ".rs": "rust", ".rb": "ruby", ".php": "php", ".swift": "swift",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp", ".cs": "csharp",
    ".lua": "lua", ".r": "r", ".sol": "sol", ".proto": "proto", ".ps1": "powershell",
    ".md": "markdown", ".rst": "rst", ".html": "html", ".htm": "html", ".tex": "latex",
    ".css": None, ".scss": None, ".sql": None, ".sh": None, ".bat": None,
    ".json": None, ".yaml": None, ".yml": None, ".toml": None, ".ini": None, ".cfg": None,
    ".xml": None, ".gradle": None, ".txt": None,
}
SPECIAL_FILES = {"dockerfile", "makefile", "readme", "license", "requirements.txt", ".env.example"}

IGNORE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", "target", "out", ".idea", ".vscode", ".next", ".nuxt",
    ".mypy_cache", ".pytest_cache", ".tox", "coverage", ".repolens_cache", "site-packages",
}
IGNORE_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock", "Cargo.lock"}
IGNORE_PATTERNS = ["*.min.js", "*.min.css", "*.map", "*.lock"]

GITHUB_RE = re.compile(r"^(https?://)?(www\.)?(github\.com|gitee\.com)/[\w.-]+/[\w.-]+?(\.git)?/?$")


@dataclass
class RepoInfo:
    name: str
    root: Path
    source: str  # 用户输入的原始地址


def is_remote(source: str) -> bool:
    return bool(GITHUB_RE.match(source.strip()))


def resolve_repo(source: str, cache_dir: Path) -> RepoInfo:
    """本地路径直接使用；GitHub/Gitee 地址浅克隆到缓存目录。"""
    source = source.strip().strip('"')
    if is_remote(source):
        url = source if source.startswith("http") else f"https://{source}"
        url = url.rstrip("/")
        name = url.removesuffix(".git").rsplit("/", 1)[-1]
        digest = hashlib.md5(url.encode()).hexdigest()[:8]
        dest = cache_dir / "repos" / f"{name}-{digest}"
        if not (dest / ".git").exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(
                ["git", "clone", "--depth", "1", url, str(dest)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            if proc.returncode != 0:
                raise RuntimeError(f"克隆仓库失败：{proc.stderr.strip()}")
        return RepoInfo(name=name, root=dest, source=source)

    root = Path(source).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"目录不存在：{root}")
    return RepoInfo(name=root.name, root=root, source=source)


def _gitignore_patterns(root: Path) -> list[str]:
    gi = root / ".gitignore"
    if not gi.exists():
        return []
    lines = gi.read_text(encoding="utf-8", errors="ignore").splitlines()
    return [ln.strip().rstrip("/") for ln in lines if ln.strip() and not ln.startswith(("#", "!"))]


def _ignored(rel: str, name: str, patterns: list[str]) -> bool:
    for p in patterns:
        p = p.lstrip("/")
        if fnmatch.fnmatch(name, p) or fnmatch.fnmatch(rel, p) or rel.startswith(p + "/"):
            return True
    return False


def language_of(path: Path) -> str | None:
    return EXT_LANGUAGE.get(path.suffix.lower())


def is_supported(path: Path) -> bool:
    return path.suffix.lower() in EXT_LANGUAGE or path.name.lower() in SPECIAL_FILES


def iter_repo_files(root: Path, max_file_bytes: int = 200_000, max_files: int = 3000):
    """遍历仓库内可读的源码/文档文件，遵守内置忽略规则与 .gitignore。"""
    patterns = IGNORE_PATTERNS + _gitignore_patterns(root)
    count = 0
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            entries = sorted(cur.iterdir(), key=lambda p: p.name.lower())
        except PermissionError:
            continue
        for p in entries:
            rel = p.relative_to(root).as_posix()
            if p.is_dir():
                if p.name not in IGNORE_DIRS and not p.name.startswith(".") and not _ignored(rel, p.name, patterns):
                    stack.append(p)
                continue
            if p.name in IGNORE_FILES or not is_supported(p) or _ignored(rel, p.name, patterns):
                continue
            try:
                if p.stat().st_size > max_file_bytes:
                    continue
            except OSError:
                continue
            yield p
            count += 1
            if count >= max_files:
                return


def read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:4096]:  # 二进制文件
        return None
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def load_documents(repo: RepoInfo, max_file_bytes: int = 200_000, max_files: int = 3000) -> list[Document]:
    docs = []
    for path in iter_repo_files(repo.root, max_file_bytes, max_files):
        text = read_text(path)
        if not text or not text.strip():
            continue
        docs.append(
            Document(
                page_content=text,
                metadata={
                    "source": path.relative_to(repo.root).as_posix(),
                    "language": language_of(path) or "text",
                    "lines": text.count("\n") + 1,
                },
            )
        )
    return docs


def build_tree(root: Path, max_depth: int = 3, max_entries: int = 300) -> str:
    """生成类似 `tree` 命令的目录结构字符串，供“项目导览”与 Agent 使用。"""
    lines = [root.name + "/"]
    patterns = _gitignore_patterns(root)
    budget = [max_entries]

    def walk(d: Path, prefix: str, depth: int):
        if depth > max_depth or budget[0] <= 0:
            return
        try:
            entries = sorted(d.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except PermissionError:
            return
        entries = [
            e for e in entries
            if not (e.is_dir() and (e.name in IGNORE_DIRS or e.name.startswith(".")))
            and not _ignored(e.relative_to(root).as_posix(), e.name, patterns)
        ]
        for i, e in enumerate(entries):
            if budget[0] <= 0:
                lines.append(prefix + "└── ...")
                return
            budget[0] -= 1
            last = i == len(entries) - 1
            lines.append(prefix + ("└── " if last else "├── ") + e.name + ("/" if e.is_dir() else ""))
            if e.is_dir():
                walk(e, prefix + ("    " if last else "│   "), depth + 1)

    walk(root, "", 1)
    return "\n".join(lines)

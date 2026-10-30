"""用 Playwright 驱动真实浏览器，自动走一遍核心功能并截取 README 用的高清截图。

用法（先启动服务：python -m repolens serve）：
    python scripts/screenshots.py                 # 中英文全部截图（会调用大模型）
    python scripts/screenshots.py --lang en       # 只截英文
    python scripts/screenshots.py --quick         # 只截不需要大模型的页面，用于调样式

需要：pip install playwright；默认使用本机已安装的 Edge（--channel chrome 可换成 Chrome）。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images"

QUESTIONS = {
    "zh": {
        "ask": "混合检索是怎么把 BM25 和向量检索的结果融合在一起的？",
        "agent": "在网页里提一个问题后，后端是怎样一步步生成带引用的回答的？",
        "file_filter": "retriever",
    },
    "en": {
        "ask": "How does hybrid retrieval fuse the BM25 and vector search results?",
        "agent": "When a question is asked in the web UI, how does the backend turn it into a cited answer, step by step?",
        "file_filter": "retriever",
    },
}
LLM_TIMEOUT = 240_000


def shot(page: Page, lang: str, name: str) -> None:
    path = OUT / lang / f"{name}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(400)
    page.screenshot(path=str(path))
    print("  saved", path.relative_to(ROOT))


def wait_chat_done(page: Page) -> None:
    page.wait_for_selector("#btn-send.stop", timeout=10_000)
    page.wait_for_selector("#btn-send:not(.stop)", timeout=LLM_TIMEOUT)


def open_self(page: Page) -> None:
    page.click("#try-self")
    page.wait_for_selector("#view-chat:not([hidden])", timeout=300_000)


def run(lang: str, base: str, quick: bool, channel: str) -> None:
    q = QUESTIONS[lang]
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=channel)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        ctx.add_init_script(
            f"if(!sessionStorage.getItem('rl.shot')){{localStorage.clear();"
            f"localStorage.setItem('rl.lang', JSON.stringify('{lang}'));"
            f"localStorage.setItem('rl.theme','light');sessionStorage.setItem('rl.shot','1');}}"
        )
        page = ctx.new_page()
        page.goto(base)
        page.wait_for_selector(".feature-grid")
        # 等 /api/settings 返回、侧边栏状态和模型标签渲染完成
        page.wait_for_function("document.querySelector('#model-label').textContent.length > 0")
        page.wait_for_timeout(600)
        shot(page, lang, "01-welcome")

        open_self(page)
        shot(page, lang, "02-chat-empty")

        if quick:
            page.click(".tab[data-view=files]")
            page.fill("#tree-filter", q["file_filter"])
            page.click("[data-file='repolens/retriever.py']")
            page.wait_for_selector("#code-view .code-grid")
            shot(page, lang, "quick-files")
            page.click("#btn-theme")
            page.click(".tab[data-view=chat]")
            shot(page, lang, "quick-dark")
            browser.close()
            return

        # 检索问答 + 引用抽屉
        page.fill("#chat-input", q["ask"])
        page.keyboard.press("Enter")
        wait_chat_done(page)
        page.evaluate("document.querySelector('#chat-scroll').scrollTop = 0")
        shot(page, lang, "03-ask")
        page.locator(".source-chip").first.click()
        page.wait_for_selector("#drawer.open .code-grid")
        page.wait_for_timeout(500)
        shot(page, lang, "04-citation")
        page.keyboard.press("Escape")

        # Agent 探索
        page.click("#btn-clear")
        page.click("#mode-seg [data-mode=agent]")
        page.fill("#chat-input", q["agent"])
        page.keyboard.press("Enter")
        page.wait_for_selector(".step", timeout=60_000)
        page.wait_for_timeout(4000)
        shot(page, lang, "05-agent-live")
        wait_chat_done(page)
        page.click(".timeline-head")
        page.evaluate("document.querySelector('#chat-scroll').scrollTop = 0")
        shot(page, lang, "06-agent")

        # 项目导览
        page.click(".tab[data-view=tour]")
        page.click("#tour-generate")
        page.wait_for_selector("#tour-copy:not([hidden])", timeout=LLM_TIMEOUT)
        shot(page, lang, "07-tour")

        # 文件讲解
        page.click(".tab[data-view=files]")
        page.fill("#tree-filter", q["file_filter"])
        page.click("[data-file='repolens/retriever.py']")
        page.wait_for_selector("#code-view .code-grid")
        page.click("#btn-explain")
        page.wait_for_selector("#btn-explain:not([disabled])", timeout=LLM_TIMEOUT)
        page.wait_for_timeout(300)
        shot(page, lang, "08-files")

        # 暗色主题
        page.click("#btn-theme")
        page.click(".tab[data-view=chat]")
        page.evaluate("document.querySelector('#chat-scroll').scrollTop = 0")
        shot(page, lang, "09-dark")
        browser.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", choices=["zh", "en", "all"], default="all")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--channel", default="msedge")
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()
    for lang in (["zh", "en"] if args.lang == "all" else [args.lang]):
        print(f"[{lang}]")
        run(lang, args.base, args.quick, args.channel)


if __name__ == "__main__":
    main()

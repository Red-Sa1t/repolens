/* RepoLens 前端：原生 JS 单页应用，通过 SSE 流式接收后端事件 */
(function () {
  "use strict";

  const t = (k, p) => I18n.t(k, p);
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const icon = (n, c) => Icons.icon(n, c);

  // ---------------- 本地存储（失败时静默降级） ----------------
  const LS = {
    get(k, d) { try { const v = localStorage.getItem("rl." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem("rl." + k, JSON.stringify(v, (key, val) => (key.startsWith("_") ? undefined : val))); } catch { /* 存储不可用 */ } },
    del(k) { try { localStorage.removeItem("rl." + k); } catch { /* ignore */ } },
  };

  // ---------------- 语言元数据 ----------------
  const LANG_META = {
    python: ["Python", "#3572A5"], js: ["JavaScript", "#e8c63a"], ts: ["TypeScript", "#3178c6"], java: ["Java", "#b07219"],
    kotlin: ["Kotlin", "#A97BFF"], scala: ["Scala", "#c22d40"], go: ["Go", "#00ADD8"], rust: ["Rust", "#dea584"], ruby: ["Ruby", "#701516"],
    php: ["PHP", "#4F5D95"], swift: ["Swift", "#F05138"], c: ["C", "#6e7681"], cpp: ["C++", "#f34b7d"], csharp: ["C#", "#178600"],
    lua: ["Lua", "#000080"], r: ["R", "#198CE7"], sol: ["Solidity", "#AA6746"], proto: ["Protobuf", "#5a8fd8"], powershell: ["PowerShell", "#012456"],
    markdown: ["Markdown", "#8b93a7"], rst: ["reST", "#141414"], html: ["HTML", "#e34c26"], latex: ["LaTeX", "#3D6117"], text: ["Other", "#c3c7d3"],
  };
  const EXT = {
    py: "python", pyi: "python", js: "javascript", jsx: "javascript", mjs: "javascript", cjs: "javascript", vue: "xml",
    ts: "typescript", tsx: "typescript", java: "java", kt: "kotlin", kts: "kotlin", scala: "scala", go: "go", rs: "rust",
    rb: "ruby", php: "php", swift: "swift", c: "c", h: "c", cpp: "cpp", cc: "cpp", hpp: "cpp", cs: "csharp", lua: "lua",
    r: "r", md: "markdown", rst: "plaintext", html: "xml", htm: "xml", xml: "xml", css: "css", scss: "scss", sql: "sql",
    sh: "bash", bat: "dos", ps1: "powershell", json: "json", yaml: "yaml", yml: "yaml", toml: "ini", ini: "ini", cfg: "ini",
    tex: "latex", gradle: "gradle", txt: "plaintext", dockerfile: "dockerfile", makefile: "makefile",
  };
  const EXT_COLOR = { css: "#563d7c", scss: "#c6538c", json: "#cbcb41", yaml: "#cb171e", yml: "#cb171e", toml: "#9c4221", sql: "#e38c00", sh: "#89e051", txt: "#9ca3af" };

  function fileExt(path) {
    const name = path.split("/").pop().toLowerCase();
    if (name === "dockerfile" || name === "makefile") return name;
    return name.includes(".") ? name.split(".").pop() : "";
  }
  function hlLang(path) {
    const l = EXT[fileExt(path)];
    return l && hljs.getLanguage(l) ? l : null;
  }
  function fileColor(path, lang) {
    return (LANG_META[lang] && lang !== "text" ? LANG_META[lang][1] : null) || EXT_COLOR[fileExt(path)] || "#9ca3af";
  }
  const fmtNum = (n) => (n >= 10000 ? (n / 1000).toFixed(0) + "k" : n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n));

  // ---------------- 状态 ----------------
  const S = {
    settings: null,
    llm: LS.get("llm", null),
    repo: null,
    fileSet: new Set(),
    view: "welcome",
    mode: LS.get("mode", "ask"),
    messages: [],
    ctrl: null,
    tourCtrl: null,
    explainCtrl: null,
    files: { current: null, cache: {}, open: new Set(), filter: "" },
    recent: [],
  };

  // ---------------- 网络 ----------------
  async function api(url) {
    const r = await fetch(url);
    if (!r.ok) { const e = new Error(await r.text()); e.status = r.status; throw e; }
    return r.json();
  }

  /** POST + Server-Sent Events：逐条解析 data: 行并回调 */
  async function sse(url, body, onEvent, signal) {
    const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal });
    if (!res.ok) {
      let detail = await res.text();
      try { detail = JSON.parse(detail).detail || detail; } catch { /* 原样 */ }
      const e = new Error(detail); e.status = res.status; throw e;
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const raw = buf.slice(0, i); buf = buf.slice(i + 2);
        if (raw.startsWith("data: ")) onEvent(JSON.parse(raw.slice(6)));
      }
    }
  }

  /** 后端重启后内存中的索引会丢失：自动用缓存重新加载后重试一次 */
  async function withRepo(fn) {
    try { return await fn(); } catch (e) {
      if (e.status !== 404 || !S.repo) throw e;
      await sse("/api/index", { source: S.repo.source }, () => {});
      return fn();
    }
  }

  const llmBody = () => ({ provider: S.llm.provider, model: S.llm.model, api_key: S.llm.api_key || null });
  const friendlyError = (m) => (/DEEPSEEK_API_KEY|api[_ ]key/i.test(m || "") ? t("error.no_key") : m);

  // ---------------- Markdown 渲染与增强 ----------------
  marked.setOptions({ gfm: true, breaks: false });

  function renderMd(el, text, sources) {
    el.innerHTML = DOMPurify.sanitize(marked.parse(text || ""));
    enhance(el, sources);
  }

  function enhance(root, sources) {
    root.querySelectorAll("pre code").forEach((code) => {
      const m = /language-([\w+-]+)/.exec(code.className);
      if (m && !hljs.getLanguage(m[1])) code.className = "";
      try { hljs.highlightElement(code); } catch { /* 忽略高亮失败 */ }
      const pre = code.parentElement;
      const b = document.createElement("button");
      b.className = "copy-code"; b.textContent = t("common.copy");
      b.onclick = () => copyText(code.textContent);
      pre.appendChild(b);
    });
    root.querySelectorAll(":not(pre) > code").forEach((code) => {
      const ref = parseRef(code.textContent);
      if (!ref) return;
      code.classList.add("code-link");
      code.title = ref.path + (ref.start ? `:${ref.start}` : "");
      code.onclick = () => openDrawer(ref.path, ref.start, ref.end);
    });
    if (sources && sources.length) linkCitations(root, sources);
  }

  /** 识别 `path`、`path:42`、`path:L10-L20` 这类内联代码，并映射到仓库内真实文件 */
  function parseRef(text) {
    const m = /^([\w@.\-/\\]+?\.[A-Za-z0-9]+|Dockerfile|Makefile)(?:[:#]\s*L?(\d+)(?:\s*[-–~]\s*L?(\d+))?)?(?:\s*\(L?(\d+)\s*[-–]\s*L?(\d+)\))?$/.exec(text.trim());
    if (!m) return null;
    const p = m[1].replace(/\\/g, "/").replace(/^\.\//, "");
    let path = S.fileSet.has(p) ? p : null;
    if (!path) path = [...S.fileSet].find((f) => f.endsWith("/" + p)) || null;
    if (!path) return null;
    const start = +(m[2] || m[4] || 0) || null;
    const end = +(m[3] || m[5] || 0) || start;
    return { path, start, end };
  }

  function linkCitations(root, sources) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => (n.parentElement.closest("code, pre, a, button") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
    });
    const nodes = [];
    while (walker.nextNode()) if (/\[\d{1,2}\]/.test(walker.currentNode.nodeValue)) nodes.push(walker.currentNode);
    nodes.forEach((node) => {
      const frag = document.createDocumentFragment();
      let last = 0;
      node.nodeValue.replace(/\[(\d{1,2})\]/g, (all, n, idx) => {
        const src = sources[+n - 1];
        if (!src) return all;
        frag.append(node.nodeValue.slice(last, idx));
        const b = document.createElement("button");
        b.className = "cite"; b.textContent = n;
        b.title = `${src.source}:${src.start_line}-${src.end_line}`;
        b.onclick = () => openDrawer(src.source, src.start_line, src.end_line);
        frag.append(b);
        last = idx + all.length;
        return all;
      });
      frag.append(node.nodeValue.slice(last));
      node.replaceWith(frag);
    });
  }

  // ---------------- 通用 UI ----------------
  function toast(msg, kind) {
    const el = document.createElement("div");
    el.className = "toast" + (kind === "err" ? " err" : "");
    el.innerHTML = icon(kind === "err" ? "alert" : "check") + `<span>${esc(msg)}</span>`;
    $("#toasts").append(el);
    setTimeout(() => { el.style.transition = "opacity .3s"; el.style.opacity = "0"; setTimeout(() => el.remove(), 300); }, 2200);
  }
  function copyText(text) {
    navigator.clipboard.writeText(text).then(() => toast(t("common.copied")), () => toast("Copy failed", "err"));
  }
  function nearBottom(el) { return el.scrollHeight - el.scrollTop - el.clientHeight < 140; }

  /** 用 requestAnimationFrame 合并流式渲染，避免每个 token 都重排 */
  const pending = new Map();
  function schedule(key, fn) {
    if (pending.has(key)) { pending.set(key, fn); return; }
    pending.set(key, fn);
    requestAnimationFrame(() => { const f = pending.get(key); pending.delete(key); f(); });
  }

  // ---------------- 视图切换 ----------------
  function showView(v) {
    if (v !== "welcome" && !S.repo) v = "welcome";
    S.view = v;
    ["welcome", "chat", "tour", "files"].forEach((name) => ($("#view-" + name).hidden = name !== v));
    $$(".tab").forEach((b) => { b.classList.toggle("active", b.dataset.view === v); b.disabled = !S.repo; });
    $("#sidebar").classList.remove("open");
    if (v === "chat") setTimeout(() => $("#chat-input").focus(), 50);
    if (v === "tour") renderTour();
    if (v === "files") renderFiles();
    if (v === "welcome") setTimeout(() => $("#load-input").focus(), 50);
  }

  // ---------------- 侧边栏 ----------------
  function renderStatus() {
    const s = S.settings;
    if (!s) return;
    const e = $("#status-embed");
    e.className = "status-line " + (s.embed.ok ? "ok" : "bad");
    e.textContent = t(s.embed.ok ? "status.embed_ok" : "status.embed_missing", { model: s.embed.model });
    const o = $("#status-ollama");
    o.className = "status-line " + (s.ollama.online ? "ok" : "bad");
    o.textContent = t(s.ollama.online ? "status.ollama_ok" : "status.ollama_off");
  }

  function renderRepoCard() {
    const r = S.repo;
    $("#repo-card").hidden = !r;
    if (!r) return;
    $("#repo-name").textContent = r.name;
    $("#repo-source").textContent = r.source;
    $("#repo-source").title = r.source;
    $("#stat-files").textContent = fmtNum(r.stats.files);
    $("#stat-chunks").textContent = fmtNum(r.stats.chunks);
    $("#stat-lines").textContent = fmtNum(r.stats.lines);
    const langs = Object.entries(r.stats.languages || {});
    const total = langs.reduce((a, [, n]) => a + n, 0) || 1;
    $("#lang-bar").innerHTML = langs.map(([l, n]) => `<span style="width:${(n / total) * 100}%;background:${(LANG_META[l] || LANG_META.text)[1]}" title="${esc((LANG_META[l] || [l])[0])} ${n}"></span>`).join("");
    $("#lang-legend").innerHTML = langs.slice(0, 5).map(([l, n]) => {
      const [name, color] = LANG_META[l] || [l, "#9ca3af"];
      return `<span><i style="background:${color}"></i>${esc(name)} ${Math.round((n / total) * 100)}%</span>`;
    }).join("");
    const b = $("#retrieval-badge");
    b.className = "badge" + (r.has_vectors ? "" : " warn");
    b.textContent = t(r.has_vectors ? "retrieval.hybrid" : "retrieval.bm25");
  }

  async function loadRecent() {
    try { S.recent = await api("/api/recent"); } catch { S.recent = []; }
    renderRecent();
  }
  function renderRecent() {
    const box = $("#recent-list");
    if (!S.recent.length) { box.innerHTML = `<div class="recent-empty">${esc(t("sidebar.no_recent"))}</div>`; return; }
    box.innerHTML = "";
    S.recent.forEach((item) => {
      const b = document.createElement("button");
      b.className = "recent-item" + (S.repo && S.repo.id === item.id ? " active" : "");
      b.innerHTML = `${icon(/^https?:|github\.com|gitee\.com/.test(item.source) ? "github" : "folder")}
        <span class="ri-text"><div class="ri-name">${esc(item.name)}</div>
        <div class="ri-sub">${fmtNum(item.stats.files || 0)} ${esc(t("stat.files"))} · ${fmtNum(item.stats.lines || 0)} ${esc(t("stat.lines"))}</div></span>`;
      b.title = item.source;
      b.onclick = () => openRepo(item.source, item.id);
      box.append(b);
    });
  }

  // ---------------- 加载仓库 ----------------
  function setProgress(p, text) {
    $("#load-progress").hidden = false;
    $("#load-fill").style.width = Math.max(3, p * 100) + "%";
    $("#load-text").textContent = text;
  }

  async function openRepo(source, id) {
    if (id) {
      try { setRepo(await api("/api/repos/" + encodeURIComponent(id))); return; } catch { /* 未加载，走索引流程 */ }
    }
    await indexRepo(source, false);
  }

  async function indexRepo(source, force) {
    source = (source || "").trim();
    $("#load-error").hidden = true;
    if (!source) { $("#load-error").hidden = false; $("#load-error").textContent = t("load.empty"); return; }
    showView("welcome");
    $("#load-input").value = source;
    $("#load-btn").disabled = true;
    const remote = /^(https?:\/\/)?(www\.)?(github|gitee)\.com\//.test(source);
    setProgress(0.02, t(remote ? "progress.clone" : "progress.resolve"));
    let repo = null, err = null;
    try {
      await sse("/api/index", { source, force }, (ev) => {
        if (ev.type === "progress") {
          const text = ev.key === "resolve" && remote ? t("progress.clone") : t("progress." + ev.key, ev.params);
          setProgress(ev.p, text);
        } else if (ev.type === "repo") repo = ev.repo;
        else if (ev.type === "error") err = ev.message;
      });
    } catch (e) { err = e.message; }
    $("#load-btn").disabled = false;
    if (err || !repo) {
      $("#load-progress").hidden = true;
      $("#load-error").hidden = false;
      $("#load-error").textContent = err || "Unknown error";
      return;
    }
    setTimeout(() => ($("#load-progress").hidden = true), 400);
    setRepo(repo);
  }

  function setRepo(repo) {
    const changed = !S.repo || S.repo.id !== repo.id;
    S.repo = repo;
    S.fileSet = new Set(repo.files.map((f) => f.path));
    LS.set("lastRepo", { id: repo.id, source: repo.source });
    if (changed) {
      S.messages = LS.get("chat." + repo.id, []);
      S.files = { current: null, cache: {}, open: new Set(defaultOpenFolders(repo.files)), filter: "" };
      $("#tree-filter").value = "";
    }
    renderRepoCard();
    loadRecent();
    renderChat();
    showView(S.view === "welcome" ? "chat" : S.view);
  }

  // ---------------- 对话 ----------------
  function saveChat() { if (S.repo) LS.set("chat." + S.repo.id, S.messages.slice(-40)); }

  function renderChat() {
    const list = $("#chat-list");
    list.innerHTML = "";
    if (!S.messages.length) {
      const sugIcons = ["layers", "entry", "book", "wrench"];
      list.innerHTML = `<div class="empty-chat">
          <div class="empty-logo icon-host">${icon("logo")}</div>
          <h2>${esc(t("chat.empty_title", { repo: S.repo ? S.repo.name : "" }))}</h2>
          <p>${esc(t("chat.empty_sub"))}</p>
          <div class="sug-grid">${[1, 2, 3, 4].map((i) => `<button class="sug" data-q="${esc(t("sug." + i))}">${icon(sugIcons[i - 1])}<span>${esc(t("sug." + i))}</span></button>`).join("")}</div>
        </div>`;
      $$(".sug", list).forEach((b) => (b.onclick = () => send(b.dataset.q)));
    } else {
      S.messages.forEach((m) => list.append(buildMsg(m)));
    }
    updateComposer();
  }

  function buildMsg(m) {
    const el = document.createElement("div");
    m._el = el;
    if (m.role === "user") {
      el.className = "msg msg-user";
      el.innerHTML = `<div class="bubble"></div>`;
      el.firstChild.textContent = m.content;
      return el;
    }
    el.className = "msg msg-assistant";
    el.innerHTML = `<div class="avatar icon-host">${icon("logo")}</div><div class="msg-body"></div>`;
    paintMsg(m);
    return el;
  }

  function stepLabel(step) {
    const a = step.args || {};
    if (step.name === "read_file") {
      const range = a.start_line ? ` L${a.start_line}–${a.end_line ?? ""}` : "";
      return { text: `${a.path || ""}${range}`, ref: parseRef(a.path || "") ? { path: parseRef(a.path).path, start: +a.start_line || null, end: +a.end_line || null } : null };
    }
    if (step.name === "grep_code") return { text: `/${a.pattern || ""}/` };
    if (step.name === "list_directory") return { text: a.path || "." };
    return { text: `“${a.query || JSON.stringify(a)}”` };
  }
  const TOOL_ICON = { search_code: "search", grep_code: "regex", list_directory: "folder", read_file: "file" };

  function timelineHtml(m) {
    const tools = m.steps.filter((s) => s.kind === "tool").length;
    const head = m.streaming
      ? `<span class="spinner"></span><span>${esc(t("agent.exploring"))}</span>`
      : `<span class="done-ic icon-host">${icon("check")}</span><span>${esc(tools ? t("agent.done", { n: tools }) : t("agent.done_none"))}</span>`;
    const steps = m.steps.map((s, i) => {
      if (s.kind === "thought") {
        return `<div class="step thought"><span class="step-ic icon-host">${icon("brain")}</span>
          <div class="step-line"><span class="step-name">${esc(t("agent.thought"))}</span></div><div class="step-thought">${esc(s.text)}</div></div>`;
      }
      const lab = stepLabel(s);
      const arg = lab.ref
        ? `<button class="step-arg" data-ref="${esc(JSON.stringify(lab.ref))}">${esc(lab.text)}</button>`
        : `<span class="step-arg">${esc(lab.text)}</span>`;
      const toggle = s.result != null ? `<button class="step-toggle" data-step="${i}">${s.open ? "▾" : "▸"} ${esc(t("agent.output"))}</button>` : `<span class="spinner"></span>`;
      return `<div class="step"><span class="step-ic icon-host">${icon(TOOL_ICON[s.name] || "terminal")}</span>
        <div class="step-line"><span class="step-name">${esc(t("tool." + s.name))}</span>${arg}${toggle}</div>
        ${s.open && s.result != null ? `<div class="step-result">${esc(s.result)}</div>` : ""}</div>`;
    }).join("");
    return `<div class="timeline${m.collapsed ? " collapsed" : ""}">
      <button class="timeline-head">${head}<span class="chev icon-host">${icon("chevron-down")}</span></button>
      ${steps ? `<div class="timeline-body">${steps}</div>` : ""}</div>`;
  }

  function paintMsg(m) {
    const body = m._el && m._el.querySelector(".msg-body");
    if (!body) return;
    const scroller = $("#chat-scroll");
    const stick = nearBottom(scroller);
    const text = m.content || m.buffer || "";
    let html = "";
    if (m.mode === "agent" && (m.steps.length || m.streaming)) html += timelineHtml(m);
    if (m.query) html += `<div class="query-note">${icon("search")}<span>${esc(t("chat.rewritten"))}</span><code>${esc(m.query)}</code></div>`;
    if (!text && m.streaming && m.mode === "ask") html += `<div class="status-text"><span class="spinner"></span>${esc(t("chat.thinking"))}</div>`;
    else if (!text && m.streaming) html += `<div class="typing"><i></i><i></i><i></i></div>`;
    html += `<div class="md answer${m.streaming && text ? " streaming-cursor" : ""}"></div>`;
    if (m.error) html += `<div class="msg-error">${icon("alert")}<span>${esc(friendlyError(m.error))}</span></div>`;
    if (m.note) html += `<div class="msg-note">${esc(m.note)}</div>`;
    if (m.sources && m.sources.length) {
      html += `<div class="sources"><div class="sources-title">${esc(t("chat.sources"))}</div><div class="source-list">${m.sources.map((s, i) =>
        `<button class="source-chip" data-src="${i}" title="${esc(s.source)}"><span class="n">${i + 1}</span><span class="p">${esc(s.source)}</span><span class="l">L${s.start_line}–${s.end_line}</span></button>`).join("")}</div></div>`;
    }
    if (!m.streaming && text) html += `<div class="msg-meta"><div class="msg-tools"><button class="mini-btn" data-copy>${icon("copy")}${esc(t("chat.copy"))}</button></div></div>`;
    body.innerHTML = html;
    renderMd(body.querySelector(".answer"), text, m.sources);

    // 事件绑定
    const head = body.querySelector(".timeline-head");
    if (head) head.onclick = () => { m.collapsed = !m.collapsed; paintMsg(m); };
    $$(".step-toggle", body).forEach((b) => (b.onclick = () => { const s = m.steps[+b.dataset.step]; s.open = !s.open; paintMsg(m); }));
    $$("button.step-arg", body).forEach((b) => (b.onclick = () => { const r = JSON.parse(b.dataset.ref); openDrawer(r.path, r.start, r.end); }));
    $$(".source-chip", body).forEach((b) => (b.onclick = () => { const s = m.sources[+b.dataset.src]; openDrawer(s.source, s.start_line, s.end_line); }));
    const cp = body.querySelector("[data-copy]");
    if (cp) cp.onclick = () => copyText(m.content);
    if (stick) scroller.scrollTop = scroller.scrollHeight;
  }

  function handleChatEvent(m, ev) {
    switch (ev.type) {
      case "query": if (ev.query && ev.query.trim() !== m.question.trim()) m.query = ev.query; break;
      case "sources": m.sources = ev.sources; break;
      case "token":
        if (m.mode === "agent") m.buffer = (m.buffer || "") + ev.text; else m.content += ev.text;
        break;
      case "tool_call":
        if ((m.buffer || "").trim()) m.steps.push({ kind: "thought", text: m.buffer.trim() });
        m.buffer = "";
        m.steps.push({ kind: "tool", name: ev.name, args: ev.args, result: null });
        break;
      case "tool_result": {
        const s = [...m.steps].reverse().find((x) => x.kind === "tool" && x.result == null && x.name === ev.name);
        if (s) s.result = ev.content;
        break;
      }
      case "answer": m.content = ev.text; m.buffer = ""; break;
      case "error": m.error = ev.message; break;
    }
    schedule(m, () => paintMsg(m));
  }

  async function send(question) {
    question = (question || "").trim();
    if (!question || S.ctrl || !S.repo) return;
    if (!S.messages.length) $("#chat-list").innerHTML = "";
    const history = S.messages.filter((x) => x.content && !x.error).slice(-12).map((x) => ({ role: x.role, content: x.content }));
    const user = { role: "user", content: question };
    const m = { role: "assistant", mode: S.mode, question, content: "", buffer: "", sources: [], steps: [], streaming: true, collapsed: false };
    S.messages.push(user, m);
    $("#chat-list").append(buildMsg(user), buildMsg(m));
    const scroller = $("#chat-scroll");
    scroller.scrollTop = scroller.scrollHeight;
    $("#chat-input").value = "";
    autosize();

    S.ctrl = new AbortController();
    updateComposer();
    try {
      await withRepo(() => sse("/api/chat", {
        repo_id: S.repo.id, question, history, mode: m.mode, lang: I18n.lang, llm: llmBody(),
      }, (ev) => handleChatEvent(m, ev), S.ctrl.signal));
    } catch (e) {
      if (e.name === "AbortError") m.note = t("chat.stopped"); else m.error = e.message;
    } finally {
      if (!m.content && m.buffer) m.content = m.buffer;
      m.buffer = "";
      m.streaming = false;
      if (m.mode === "agent" && m.content) m.collapsed = true;
      S.ctrl = null;
      paintMsg(m);
      updateComposer();
      saveChat();
    }
  }

  function updateComposer() {
    const btn = $("#btn-send");
    const busy = !!S.ctrl;
    btn.classList.toggle("stop", busy);
    btn.disabled = !busy && !$("#chat-input").value.trim();
    $("#chat-input").placeholder = t("chat.placeholder", { repo: S.repo ? S.repo.name : "" });
    $$("#mode-seg button").forEach((b) => b.classList.toggle("active", b.dataset.mode === S.mode));
    $("#mode-hint").textContent = t(S.mode === "agent" ? "mode.agent_hint" : "mode.ask_hint");
  }
  function autosize() {
    const ta = $("#chat-input");
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 200) + "px";
    updateComposer();
  }

  // ---------------- 代码视图 / 抽屉 ----------------
  async function fetchFile(path) {
    if (S.files.cache[path]) return S.files.cache[path];
    const data = await withRepo(() => api(`/api/repos/${encodeURIComponent(S.repo.id)}/file?path=${encodeURIComponent(path)}`));
    S.files.cache[path] = data;
    return data;
  }

  function renderCode(box, content, path, start, end) {
    const lang = hlLang(path);
    let html;
    try { html = lang ? hljs.highlight(content, { language: lang, ignoreIllegals: true }).value : esc(content); } catch { html = esc(content); }
    const lines = content.endsWith("\n") ? content.split("\n").length - 1 : content.split("\n").length;
    const nums = Array.from({ length: Math.max(lines, 1) }, (_, i) => i + 1).join("\n");
    const lh = 20;
    const band = start ? `<div class="code-hl" style="top:${10 + (start - 1) * lh}px;height:${((end || start) - start + 1) * lh}px"></div>` : "";
    box.innerHTML = `<div class="code-grid">${band}<pre class="code-gutter">${nums}</pre><pre class="code-text">${html}</pre></div>`;
    box.scrollTop = start ? Math.max(0, (start - 1) * lh - 90) : 0;
    box.scrollLeft = 0;
    return lines;
  }

  let drawerRef = null;
  async function openDrawer(path, start, end) {
    drawerRef = { path, start, end };
    $("#drawer-path").textContent = path;
    $("#drawer-sub").textContent = start ? t("drawer.lines", { a: start, b: end || start }) : "";
    $("#drawer-code").innerHTML = `<div class="placeholder"><span class="spinner"></span></div>`;
    $("#drawer-mask").hidden = false;
    $("#drawer").classList.add("open");
    $("#drawer").setAttribute("aria-hidden", "false");
    try {
      const f = await fetchFile(path);
      if (drawerRef.path === path) renderCode($("#drawer-code"), f.content, path, start, end);
    } catch (e) {
      $("#drawer-code").innerHTML = `<div class="placeholder">${esc(e.message)}</div>`;
    }
  }
  function closeDrawer() {
    $("#drawer").classList.remove("open");
    $("#drawer").setAttribute("aria-hidden", "true");
    $("#drawer-mask").hidden = true;
  }

  // ---------------- 项目导览 ----------------
  const tourKey = () => `tour.${S.repo.id}.${I18n.lang}`;

  function renderTour(streaming) {
    if (!S.repo) return;
    const text = LS.get(tourKey(), "") || S._tourDraft || "";
    const doc = $("#tour-doc");
    const busy = !!S.tourCtrl;
    $("#tour-generate-label").textContent = busy ? "…" : t(text ? "tour.regenerate" : "tour.generate");
    $("#tour-generate").disabled = busy;
    $("#tour-copy").hidden = $("#tour-download").hidden = !text || busy;
    if (busy && !S._tourDraft) {
      doc.innerHTML = `<div class="skeleton">${[90, 70, 82, 55, 76, 64, 88, 48].map((w) => `<i style="width:${w}%"></i>`).join("")}</div>`;
      return;
    }
    if (!text && !busy) {
      doc.innerHTML = `<div class="doc-empty"><div class="empty-logo icon-host">${icon("map")}</div>
        <h3>${esc(t("tour.empty_title", { repo: S.repo.name }))}</h3><p>${esc(t("tour.empty_sub"))}</p>
        <button class="btn btn-primary" id="tour-generate-2">${icon("sparkles")}${esc(t("tour.generate"))}</button></div>`;
      $("#tour-generate-2").onclick = generateTour;
      return;
    }
    renderMd(doc, busy ? S._tourDraft : text);
    doc.classList.toggle("streaming-cursor", !!streaming);
  }

  async function generateTour() {
    if (S.tourCtrl || !S.repo) return;
    const key = tourKey();
    S.tourCtrl = new AbortController();
    S._tourDraft = "";
    renderTour(true);
    let err = null;
    try {
      await withRepo(() => sse("/api/overview", { repo_id: S.repo.id, lang: I18n.lang, llm: llmBody() }, (ev) => {
        if (ev.type === "token") { S._tourDraft += ev.text; schedule("tour", () => renderTour(true)); }
        if (ev.type === "error") err = ev.message;
      }, S.tourCtrl.signal));
    } catch (e) { err = e.message; }
    S.tourCtrl = null;
    if (S._tourDraft && !err) LS.set(key, S._tourDraft);
    S._tourDraft = "";
    if (err) toast(friendlyError(err), "err");
    if (S.view === "tour") renderTour(false);
  }

  // ---------------- 文件讲解 ----------------
  function defaultOpenFolders(files) {
    const dirs = new Set();
    files.forEach((f) => { const parts = f.path.split("/"); for (let i = 1; i < parts.length; i++) dirs.add(parts.slice(0, i).join("/")); });
    return files.length <= 80 ? [...dirs] : [...dirs].filter((d) => !d.includes("/"));
  }

  function buildTree(files) {
    const root = { dirs: {}, files: [] };
    files.forEach((f) => {
      const parts = f.path.split("/");
      let node = root;
      parts.slice(0, -1).forEach((p, i) => {
        node.dirs[p] = node.dirs[p] || { name: p, path: parts.slice(0, i + 1).join("/"), dirs: {}, files: [] };
        node = node.dirs[p];
      });
      node.files.push(f);
    });
    return root;
  }

  function renderTree() {
    const box = $("#file-tree");
    const filter = S.files.filter.trim().toLowerCase();
    const rows = [];
    const fileRow = (f, depth, label) => `<button class="tree-row${S.files.current === f.path ? " active" : ""}" data-file="${esc(f.path)}" style="padding-left:${8 + depth * 14}px" title="${esc(f.path)}">
        <span class="fdot" style="background:${fileColor(f.path, f.language)}"></span><span class="tname">${label}</span></button>`;
    if (filter) {
      S.repo.files.filter((f) => f.path.toLowerCase().includes(filter)).forEach((f) => {
        const i = f.path.toLowerCase().indexOf(filter);
        const label = esc(f.path.slice(0, i)) + "<mark>" + esc(f.path.slice(i, i + filter.length)) + "</mark>" + esc(f.path.slice(i + filter.length));
        rows.push(fileRow(f, 0, label));
      });
    } else {
      const walk = (node, depth) => {
        Object.values(node.dirs).sort((a, b) => a.name.localeCompare(b.name)).forEach((d) => {
          const open = S.files.open.has(d.path);
          rows.push(`<button class="tree-row${open ? " open" : ""}" data-dir="${esc(d.path)}" style="padding-left:${8 + depth * 14}px">
            <span class="chev icon-host">${icon("chevron-right")}</span>${icon(open ? "folder-open" : "folder")}<span class="tname">${esc(d.name)}</span></button>`);
          if (open) walk(d, depth + 1);
        });
        node.files.sort((a, b) => a.path.localeCompare(b.path)).forEach((f) => rows.push(fileRow(f, depth, esc(f.path.split("/").pop()))));
      };
      walk(buildTree(S.repo.files), 0);
    }
    box.innerHTML = rows.join("") || `<div class="recent-empty">—</div>`;
    $$("[data-dir]", box).forEach((b) => (b.onclick = () => {
      const p = b.dataset.dir;
      S.files.open.has(p) ? S.files.open.delete(p) : S.files.open.add(p);
      renderTree();
    }));
    $$("[data-file]", box).forEach((b) => (b.onclick = () => selectFile(b.dataset.file)));
  }

  const explainKey = (path) => `explain.${S.repo.id}.${I18n.lang}.${path}`;

  function renderFiles() {
    if (!S.repo) return;
    $("#tree-filter").placeholder = t("files.filter");
    renderTree();
    const cur = S.files.current;
    if (!cur) {
      $("#code-path").textContent = "";
      $("#code-meta").textContent = "";
      $("#code-view").innerHTML = `<div class="placeholder">${icon("file")}<span>${esc(t("files.pick"))}</span></div>`;
    }
    renderExplain();
  }

  async function selectFile(path, start, end) {
    S.files.current = path;
    // 展开所在目录
    const parts = path.split("/");
    for (let i = 1; i < parts.length; i++) S.files.open.add(parts.slice(0, i).join("/"));
    renderTree();
    const dir = parts.slice(0, -1).join(" / ");
    $("#code-path").innerHTML = (dir ? esc(dir) + " / " : "") + `<b>${esc(parts[parts.length - 1])}</b>`;
    $("#code-view").innerHTML = `<div class="placeholder"><span class="spinner"></span></div>`;
    renderExplain();
    try {
      const f = await fetchFile(path);
      if (S.files.current !== path) return;
      const n = renderCode($("#code-view"), f.content, path, start, end);
      const lang = (LANG_META[f.language] || [])[0] || fileExt(path).toUpperCase() || "Text";
      $("#code-meta").textContent = `${lang === "Other" ? fileExt(path).toUpperCase() : lang} · ${t("files.lines", { n })}`;
    } catch (e) {
      $("#code-view").innerHTML = `<div class="placeholder">${esc(e.message)}</div>`;
    }
  }

  function renderExplain(streamingText, refs) {
    const body = $("#explain-body");
    const cur = S.files.current;
    const btn = $("#btn-explain");
    btn.disabled = !cur || !!S.explainCtrl;
    if (!cur) { $("#btn-explain-label").textContent = t("files.explain"); body.innerHTML = `<div class="placeholder">${icon("sparkles")}<span>${esc(t("files.explain_empty"))}</span></div>`; return; }
    const saved = streamingText == null ? LS.get(explainKey(cur), null) : { text: streamingText, refs };
    $("#btn-explain-label").textContent = S.explainCtrl ? "…" : t(saved ? "files.re_explain" : "files.explain");
    if (!saved) { body.innerHTML = `<div class="placeholder">${icon("sparkles")}<span>${esc(t("files.explain_empty"))}</span></div>`; return; }
    let html = "";
    if (saved.refs && saved.refs.length) html += `<div class="refs"><div class="refs-title">${esc(t("files.refs"))}</div><div class="refs-md md"></div></div>`;
    html += `<div class="explain-md${streamingText != null ? " streaming-cursor" : ""}"></div>`;
    body.innerHTML = html;
    if (saved.refs && saved.refs.length) renderMd(body.querySelector(".refs-md"), saved.refs.join("\n"));
    if (!saved.text && streamingText != null) body.querySelector(".explain-md").innerHTML = `<div class="skeleton">${[86, 64, 78, 52].map((w) => `<i style="width:${w}%"></i>`).join("")}</div>`;
    else renderMd(body.querySelector(".explain-md"), saved.text);
  }

  async function explainFile() {
    const path = S.files.current;
    if (!path || S.explainCtrl) return;
    const key = explainKey(path);
    S.explainCtrl = new AbortController();
    let text = "", refs = [], err = null;
    renderExplain("", refs);
    try {
      await withRepo(() => sse("/api/explain", { repo_id: S.repo.id, path, lang: I18n.lang, llm: llmBody() }, (ev) => {
        if (ev.type === "references") refs = ev.items;
        else if (ev.type === "token") text += ev.text;
        else if (ev.type === "error") err = ev.message;
        if (S.files.current === path) schedule("explain", () => S.explainCtrl && renderExplain(text, refs));
      }, S.explainCtrl.signal));
    } catch (e) { err = e.message; }
    S.explainCtrl = null;
    if (text && !err) LS.set(key, { text, refs });
    if (err) toast(friendlyError(err), "err");
    if (S.files.current === path) renderExplain();
  }

  // ---------------- 模型设置 ----------------
  function modelLabel() {
    const name = S.llm.provider === "deepseek" ? "DeepSeek" : "Ollama";
    return `${name} · ${S.llm.model}`;
  }
  function renderModelPill() {
    if (!S.llm || !S.settings) return;
    $("#model-label").textContent = modelLabel();
    const bad = S.llm.provider === "deepseek" ? !(S.settings.deepseek.has_key || S.llm.api_key) : !S.settings.ollama.online;
    $("#model-dot").classList.toggle("bad", bad);
  }

  let draftProvider = null;
  function openModelModal() {
    draftProvider = S.llm.provider;
    $("#key-input").value = S.llm.api_key || "";
    paintModelModal(S.llm.model);
    $("#model-modal").hidden = false;
  }
  function paintModelModal(selected) {
    const s = S.settings;
    $$(".provider-card").forEach((c) => {
      const on = c.dataset.provider === draftProvider;
      c.classList.toggle("selected", on);
      c.querySelector("input").checked = on;
    });
    const models = draftProvider === "deepseek" ? s.deepseek.models : s.ollama.models;
    const fallback = draftProvider === "deepseek" ? s.deepseek.model : s.ollama.model;
    const list = models.length ? models : [fallback];
    const pick = list.includes(selected) ? selected : list.find((m) => m.startsWith(fallback)) || list[0];
    $("#model-select").innerHTML = list.map((m) => `<option ${m === pick ? "selected" : ""}>${esc(m)}</option>`).join("");
    $("#key-field").hidden = draftProvider !== "deepseek";
    $("#key-hint").textContent = t(s.deepseek.has_key ? "model.key_set" : "model.key_unset");
    $("#model-hint").textContent = draftProvider === "ollama" ? t(s.ollama.online ? "model.agent_tip" : "model.ollama_off") : "";
  }
  function saveModel() {
    S.llm = { provider: draftProvider, model: $("#model-select").value, api_key: $("#key-input").value.trim() };
    LS.set("llm", S.llm);
    $("#model-modal").hidden = true;
    renderModelPill();
    toast(t("model.saved", { model: modelLabel() }));
  }

  // ---------------- 语言 / 主题 ----------------
  function setLang(l) {
    I18n.lang = l;
    LS.set("lang", l);
    I18n.apply();
    $("#lang-text").textContent = l === "zh" ? "EN" : "中";
    $("#load-input").placeholder = t("load.placeholder");
    renderStatus(); renderRepoCard(); renderRecent(); renderModelPill();
    if (S.repo) { renderChat(); if (S.view === "tour") renderTour(); if (S.view === "files") renderFiles(); }
    updateComposer();
  }
  function toggleTheme() {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("rl.theme", next); } catch { /* ignore */ }
  }

  // ---------------- 事件绑定 ----------------
  function bind() {
    $$(".tab").forEach((b) => (b.onclick = () => showView(b.dataset.view)));
    $("#btn-new-repo").onclick = () => { showView("welcome"); $("#load-input").select(); };
    $("#load-form").onsubmit = (e) => { e.preventDefault(); indexRepo($("#load-input").value, $("#load-force").checked); };
    $("#try-self").onclick = () => indexRepo(S.settings.project_root, false);
    $$("[data-example]").forEach((b) => (b.onclick = () => indexRepo(b.dataset.example, false)));

    const ta = $("#chat-input");
    ta.addEventListener("input", autosize);
    ta.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); send(ta.value); }
    });
    $("#btn-send").onclick = () => (S.ctrl ? S.ctrl.abort() : send(ta.value));
    $$("#mode-seg button").forEach((b) => (b.onclick = () => { S.mode = b.dataset.mode; LS.set("mode", S.mode); updateComposer(); ta.focus(); }));
    $("#btn-clear").onclick = () => {
      if (S.ctrl) S.ctrl.abort();
      S.messages = []; saveChat(); renderChat(); toast(t("chat.cleared"));
    };

    $("#tour-generate").onclick = generateTour;
    $("#tour-copy").onclick = () => copyText(LS.get(tourKey(), ""));
    $("#tour-download").onclick = () => {
      const blob = new Blob([LS.get(tourKey(), "")], { type: "text/markdown;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob); a.download = `${S.repo.name}-tour.md`; a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    };

    $("#tree-filter").addEventListener("input", (e) => { S.files.filter = e.target.value; renderTree(); });
    $("#btn-explain").onclick = explainFile;

    $("#drawer-close").onclick = closeDrawer;
    $("#drawer-mask").onclick = closeDrawer;
    $("#drawer-open").onclick = () => { const r = drawerRef; closeDrawer(); showView("files"); selectFile(r.path, r.start, r.end); };

    $("#btn-model").onclick = openModelModal;
    $$(".provider-card").forEach((c) => (c.onclick = (e) => { e.preventDefault(); draftProvider = c.dataset.provider; paintModelModal(); }));
    $$("#model-modal [data-close]").forEach((b) => (b.onclick = () => ($("#model-modal").hidden = true)));
    $("#model-modal").onclick = (e) => { if (e.target.id === "model-modal") $("#model-modal").hidden = true; };
    $("#model-save").onclick = saveModel;

    $("#btn-lang").onclick = () => setLang(I18n.lang === "zh" ? "en" : "zh");
    $("#btn-theme").onclick = toggleTheme;
    $("#btn-menu").onclick = () => $("#sidebar").classList.toggle("open");

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { closeDrawer(); $("#model-modal").hidden = true; }
    });
  }

  // ---------------- 启动 ----------------
  async function init() {
    I18n.lang = LS.get("lang", (navigator.language || "zh").toLowerCase().startsWith("zh") ? "zh" : "en");
    Icons.hydrate();
    bind();
    setLang(I18n.lang);
    showView("welcome");
    try {
      S.settings = await api("/api/settings");
    } catch (e) {
      toast(t("error.generic", { msg: e.message }), "err");
      return;
    }
    const p = S.settings.provider;
    if (!S.llm || !S.llm.provider) S.llm = { provider: p, model: S.settings[p].model, api_key: "" };
    renderStatus(); renderModelPill(); loadRecent();
    const last = LS.get("lastRepo", null);
    if (last) await openRepo(last.source, last.id);
  }

  init();
})();

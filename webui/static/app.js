/* 博客生成器前端。无框架，直接操作 DOM。 */

const state = {
  materials: [],      // [{path, kind}]
  jobs: new Map(),    // jobId -> {snapshot, events, streams: EventSource}
  config: null,
  background: null,
  articles: [],       // 历史列表（封面提示词页复用同一份）
  currentArticle: null,
  page: "home",
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

/* ---------------- 工具 ---------------- */

async function api(url, options = {}) {
  let res;
  try {
    res = await fetch(url, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
  } catch (e) {
    // fetch 在连接层失败时只抛 "Failed to fetch"，看不出原因。
    // 最常见的情况是服务被关掉了，或者浏览器连接数被占用完了。
    throw new Error(
      "连不上后端服务。如果服务已经关掉，重新双击「启动博客生成器.bat」，再刷新页面"
    );
  }
  let data = {};
  try { data = await res.json(); } catch (e) { /* 可能是空响应 */ }
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || `请求失败 (HTTP ${res.status})`);
  }
  return data;
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function setStatus(text) { $("#status").textContent = text; }

/* 轻提示。原来是 alert()，会打断操作且和整体视觉不搭；3 秒后自动收起。 */
let toastTimer = null;
function toast(message, kind = "info") {
  const box = $("#toast");
  box.textContent = message;
  box.className = "toast " + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => box.classList.add("hidden"), 3000);
}

/* 通用确认框。替代 confirm()，样式统一，也方便日后加更多说明。 */
function confirmDialog(title, text, okLabel = "确定") {
  return new Promise((resolve) => {
    const box = $("#confirm");
    $("#confirm-title").textContent = title;
    $("#confirm-text").textContent = text || "";
    $("#confirm-yes").textContent = okLabel;
    box.classList.remove("hidden");

    const done = (val) => {
      box.classList.add("hidden");
      $("#confirm-yes").removeEventListener("click", onYes);
      $("#confirm-no").removeEventListener("click", onNo);
      resolve(val);
    };
    const onYes = () => done(true);
    const onNo = () => done(false);
    $("#confirm-yes").addEventListener("click", onYes);
    $("#confirm-no").addEventListener("click", onNo);
  });
}

/* 顶部配置提醒条。没有提醒时要收起来，否则切换模型后旧的警告会一直挂着。 */
function renderConfigWarnings(warnings) {
  const box = $("#config-warnings");
  box.innerHTML = "";
  (warnings || []).forEach((w) => box.appendChild(el("div", null, "⚠️ " + w)));
  box.classList.toggle("hidden", !(warnings && warnings.length));
}

const STATUS_LABEL = {
  queued: "排队中",
  running: "生成中",
  awaiting_confirm: "待确认大纲",
  done: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

/* ---------------- 页面导航 ---------------- */

function showPage(name) {
  state.page = name;
  $$(".nav-item").forEach((n) =>
    n.classList.toggle("is-active", n.dataset.page === name));
  $$(".page").forEach((p) => p.classList.toggle("is-active", p.id === `page-${name}`));
  $("#sidebar").classList.remove("open");
  $("#nav-toggle").setAttribute("aria-expanded", "false");

  if (name === "history") loadHistory();
  if (name === "cover") loadCoverList();
  if (name === "spec") loadSpec();
  if (name === "settings") loadBackground();
  if (name === "home") renderHome();
}

$$(".nav-item").forEach((item) =>
  item.addEventListener("click", () => showPage(item.dataset.page)));

$("#nav-toggle").addEventListener("click", () => {
  const open = $("#sidebar").classList.toggle("open");
  $("#nav-toggle").setAttribute("aria-expanded", String(open));
});

/* ---------------- 配置 ---------------- */

async function loadConfig() {
  const data = await api("/api/config");
  state.config = data;

  const sel = $("#article-type");
  sel.innerHTML = "";
  data.article_types.forEach((t) => {
    const o = el("option", null, t.label);
    o.value = t.value;
    sel.appendChild(o);
  });

  $("#category").placeholder = `留空用默认：${data.default_category}`;
  if (data.default_tags && data.default_tags.length) {
    $("#tags").placeholder = `逗号分隔，默认：${data.default_tags.join(",")}`;
  }

  renderConfigWarnings(data.warnings);

  $("#submit-hint").textContent = `输出到 ${data.output_base}`;
  $("#tutorial-output-path").textContent = data.output_base || "output/";
  if (data.review_timeout) {
    $("#opt-review").parentElement.title =
      `勾选后先看大纲；${data.review_timeout} 秒内不确认会自动取消`;
  }
}

/* ---------------- 材料选择 ---------------- */

function renderMaterials() {
  const box = $("#materials");
  box.innerHTML = "";
  state.materials.forEach((m, i) => {
    const chip = el("div", "material-chip");
    const kind = el("span", "kind" + (m.kind === "dir" ? " dir" : ""),
      m.kind === "dir" ? "目录" : "文件");
    chip.appendChild(kind);
    chip.appendChild(el("span", "path", m.path));
    const del = el("button", null, "×");
    del.type = "button";
    del.title = "移除";
    del.setAttribute("aria-label", "移除这条材料");
    del.addEventListener("click", () => {
      state.materials.splice(i, 1);
      renderMaterials();
    });
    chip.appendChild(del);
    box.appendChild(chip);
  });
}

function addMaterial(path, kind) {
  if (!path) return;
  if (state.materials.some((m) => m.path === path)) return;
  state.materials.push({ path, kind: kind || "file" });
  renderMaterials();
}

async function pickPath(kind) {
  setStatus("等待选择…");
  try {
    const data = await api("/api/pick-path", {
      method: "POST",
      body: JSON.stringify({ kind }),
    });
    if (data.path) {
      // 目录选择框返回目录，文件选择框返回文件；用户取消时返回空串
      addMaterial(data.path, kind === "dir" ? "dir" : "file");
    }
  } catch (e) {
    toast("打开选择框失败：" + e.message, "error");
  } finally {
    setStatus("就绪");
  }
}

$("#add-file").addEventListener("click", () => pickPath("file"));
$("#add-dir").addEventListener("click", () => pickPath("dir"));

$("#manual-path").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    const v = e.target.value.trim();
    if (v) {
      addMaterial(v, /[\\/]$/.test(v) || !/\.[a-z0-9]+$/i.test(v) ? "dir" : "file");
      e.target.value = "";
    }
  }
});

/* ---------------- 提交任务 ---------------- */

$("#submit").addEventListener("click", async () => {
  const topic = $("#topic").value.trim();
  if (!topic) { toast("请先填写题材", "error"); $("#topic").focus(); return; }

  const tagsRaw = $("#tags").value.trim();
  const payload = {
    topic,
    category: $("#category").value.trim(),
    tags: tagsRaw ? tagsRaw.split(/[,，]/).map((s) => s.trim()).filter(Boolean) : [],
    materials: state.materials.map((m) => m.path),
    article_type: $("#article-type").value,
    review: $("#opt-review").checked,
    fast: $("#opt-fast").checked,
    no_humanize: $("#opt-nohumanize").checked,
    no_selfcheck: $("#opt-noselfcheck").checked,
    no_images: $("#opt-noimages").checked,
    no_cover_prompt: $("#opt-nocover").checked,
  };

  const btn = $("#submit");
  btn.disabled = true;
  btn.textContent = "提交中…";
  try {
    const data = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.materials = [];
    renderMaterials();
    $("#topic").value = "";
    toast("已加入队列", "ok");
    await loadJobs();
    openStream(data.job.id);
    const node = document.querySelector(`[data-job="${data.job.id}"]`);
    if (node) node.classList.add("open");
  } catch (e) {
    toast("提交失败：" + e.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "加入队列";
  }
});

/* ---------------- 任务列表 ---------------- */

function jobNode(job) {
  const wrap = el("div", "job");
  wrap.dataset.job = job.id;

  const head = el("div", "job-head");
  head.appendChild(el("span", "job-topic", job.params.topic || ""));

  const meta = el("span", "job-meta", "");
  const badge = el("span", "badge " + job.status, STATUS_LABEL[job.status] || job.status);
  meta.appendChild(badge);
  head.appendChild(meta);

  const body = el("div", "job-body");
  const log = el("div", "log");
  log.dataset.log = job.id;
  body.appendChild(log);

  head.addEventListener("click", () => wrap.classList.toggle("open"));
  wrap.appendChild(head);
  wrap.appendChild(body);
  return wrap;
}

async function loadJobs() {
  const data = await api("/api/jobs");
  const box = $("#jobs");

  data.jobs.forEach((job) => {
    let node = box.querySelector(`[data-job="${job.id}"]`);
    if (!node) {
      node = jobNode(job);
      box.appendChild(node);
      // 新任务：补发历史事件并订阅。已经跑完的不订阅，省一条连接。
      mergeJob(job);
      if (!["done", "failed", "cancelled"].includes(job.status)) openStream(job.id);
    } else {
      updateJobHeader(node, job);
      // 刷新一次操作区，取消按钮/查看按钮要跟着状态走
      renderJobActions(node, job);
    }
    state.jobs.set(job.id, Object.assign(state.jobs.get(job.id) || {}, { snapshot: job }));
  });

  $("#jobs-empty").classList.toggle("hidden", data.jobs.length > 0);
  renderHomeStatus();
}

function updateJobHeader(node, job) {
  const badge = node.querySelector(".badge");
  if (badge) {
    badge.className = "badge " + job.status;
    badge.textContent = STATUS_LABEL[job.status] || job.status;
  }
}

function mergeJob(job) {
  const entry = state.jobs.get(job.id) || {};
  entry.snapshot = job;
  state.jobs.set(job.id, entry);
}

$("#refresh-jobs").addEventListener("click", loadJobs);

/* ---------------- SSE ---------------- */

function openStream(jobId) {
  const entry = state.jobs.get(jobId) || {};
  if (entry.streams) return;   // 已经订阅过

  const since = entry.lastSeq || 0;
  const es = new EventSource(`/api/jobs/${jobId}/events?since=${since}`);
  entry.streams = es;
  state.jobs.set(jobId, entry);

  es.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch (err) { return; }
    entry.lastSeq = ev.seq || entry.lastSeq;
    appendEvent(jobId, ev);
  };
  es.onerror = () => {
    // 任务结束后连接关闭是正常的；浏览器会自动重连，这里不额外处理
  };
}

function closeStream(jobId) {
  const entry = state.jobs.get(jobId);
  if (entry && entry.streams) {
    entry.streams.close();
    entry.streams = null;
  }
}

function appendEvent(jobId, ev) {
  const log = document.querySelector(`[data-log="${jobId}"]`);
  if (!log) return;

  if (ev.kind === "review" && ev.need_confirm) {
    showReview(jobId, ev);
    return;
  }
  if (ev.kind === "review" && ev.need_confirm === undefined) {
    // 确认结果的回显，收掉确认面板
    const rv = document.querySelector(`[data-review="${jobId}"]`);
    if (rv) rv.remove();
  }
  if (ev.kind === "state") {
    const node = document.querySelector(`[data-job="${jobId}"]`);
    if (node) node.querySelector(".badge") && refreshOne(jobId);
    // 任务到终态就关流。不关的话每个跑完的任务都占着一条浏览器连接，
    // 攒够 6 条之后后续请求全部失败（表现为「加载失败：Failed to fetch」）
    if (["done", "failed", "cancelled"].includes(ev.status)) closeStream(jobId);
  }

  const line = el("div", "l-" + (ev.level || "info"));
  if (ev.banner) line.classList.add("banner");
  line.textContent = ev.message === undefined || ev.message === "" ? "" : ev.message;
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

async function refreshOne(jobId) {
  try {
    const data = await api(`/api/jobs/${jobId}`);
    const node = document.querySelector(`[data-job="${jobId}"]`);
    if (node) {
      updateJobHeader(node, data.job);
      renderJobActions(node, data.job);
    }
    state.jobs.set(jobId, Object.assign(state.jobs.get(jobId) || {}, { snapshot: data.job }));
    if (["done", "failed", "cancelled"].includes(data.job.status)) closeStream(jobId);
    if (data.job.status === "done") {
      loadHistory();
      renderHomeStatus();
    }
  } catch (e) { /* 忽略 */ }
}

/* ---------------- 大纲确认 ---------------- */

function showReview(jobId, ev) {
  const body = document.querySelector(`[data-job="${jobId}"] .job-body`);
  if (!body) return;
  const old = document.querySelector(`[data-review="${jobId}"]`);
  if (old) old.remove();

  const box = el("div", "review");
  box.dataset.review = jobId;
  box.appendChild(el("h4", null, "大纲已生成，确认后继续写正文"));
  box.appendChild(el("pre", null, ev.outline || ""));

  const actions = el("div", "row gap");
  const go = el("button", "btn primary small", "继续写正文");
  go.type = "button";
  const no = el("button", "btn small", "取消本次生成");
  no.type = "button";
  go.addEventListener("click", () => confirmOutline(jobId, true, box));
  no.addEventListener("click", () => confirmOutline(jobId, false, box));
  actions.appendChild(go);
  actions.appendChild(no);

  if (ev.timeout) {
    actions.appendChild(el("span", "hint", `${Math.round(ev.timeout / 60)} 分钟内不确认会自动取消`));
  }
  box.appendChild(actions);

  body.appendChild(box);
  box.scrollIntoView({ block: "nearest" });
}

async function confirmOutline(jobId, decision, box) {
  box.querySelectorAll("button").forEach((b) => (b.disabled = true));
  try {
    await api(`/api/jobs/${jobId}/confirm`, {
      method: "POST",
      body: JSON.stringify({ continue: decision }),
    });
  } catch (e) {
    toast("确认失败：" + e.message, "error");
    box.querySelectorAll("button").forEach((b) => (b.disabled = false));
  }
}

/* ---------------- 任务结束后的操作 ---------------- */

function renderJobActions(node, job) {
  const body = node.querySelector(".job-body");
  if (!body) return;
  let actions = node.querySelector(".job-actions");
  if (!actions) {
    actions = el("div", "job-actions");
    body.appendChild(actions);
  }
  actions.innerHTML = "";

  if (job.status === "running" || job.status === "queued") {
    const cancel = el("button", "btn small", "取消");
    cancel.type = "button";
    cancel.addEventListener("click", async () => {
      cancel.disabled = true;
      try {
        await api(`/api/jobs/${job.id}/cancel`, { method: "POST" });
        toast("已请求取消，将在阶段边界生效");
      } catch (e) { toast(e.message, "error"); }
    });
    actions.appendChild(cancel);
  }

  if (job.status === "failed" && job.error) {
    actions.appendChild(el("span", "hint", "失败原因：" + job.error));
  }

  if (job.result && job.result.output_path) {
    const dir = job.result.output_path.replace(/[\\/][^\\/]+$/, "");
    const view = el("button", "btn small", "查看文章");
    view.type = "button";
    view.addEventListener("click", () => openArticle(dir.split(/[\\/]/).pop()));
    actions.appendChild(view);

    const folder = el("button", "btn small", "打开文件夹");
    folder.type = "button";
    folder.addEventListener("click", () => openFolder(dir));
    actions.appendChild(folder);
  }
}

async function openFolder(path) {
  try {
    await api("/api/open-folder", {
      method: "POST",
      body: JSON.stringify({ path }),
    });
  } catch (e) { toast("打开失败：" + e.message, "error"); }
}

/* ---------------- 历史列表 ---------------- */

async function loadHistory() {
  try {
    const data = await api("/api/history");
    state.articles = data.articles;
    renderHistory();
    fillHistoryFilter();
    renderHomeRecent();
    renderHomeStatus();
  } catch (e) {
    setStatus("加载历史失败：" + e.message);
  }
}

function fillHistoryFilter() {
  const sel = $("#history-filter");
  const current = sel.value;
  const types = [...new Set(state.articles.map((a) => a.article_type).filter(Boolean))];
  sel.innerHTML = "";
  sel.appendChild(el("option", null, "全部类型")).value = "";
  types.forEach((t) => {
    const o = el("option", null, t);
    o.value = t;
    sel.appendChild(o);
  });
  sel.value = types.includes(current) ? current : "";
}

function filteredArticles() {
  const kw = ($("#history-search").value || "").trim().toLowerCase();
  const type = $("#history-filter").value;
  return state.articles.filter((a) => {
    if (type && a.article_type !== type) return false;
    if (kw && !(a.title || "").toLowerCase().includes(kw)) return false;
    return true;
  });
}

function articleRow(a) {
  const item = el("div", "hitem");
  item.appendChild(el("span", "h-title", a.title));

  const badges = el("div", "h-badges");
  if (a.selfcheck_ok === true) badges.appendChild(el("span", "badge ok", "自检通过"));
  if (a.selfcheck_ok === false) badges.appendChild(el("span", "badge bad", "自检未通过"));
  if (a.has_cover_prompt) badges.appendChild(el("span", "badge", "封面提示词"));
  if (a.image_count > 0) badges.appendChild(el("span", "badge", `${a.image_count} 图`));
  if (a.article_type) badges.appendChild(el("span", "badge", a.article_type));
  item.appendChild(badges);

  item.appendChild(el("span", "h-date", a.date || ""));
  item.addEventListener("click", () => openArticle(a.dir));
  return item;
}

function renderHistory() {
  const box = $("#history");
  box.innerHTML = "";
  const list = filteredArticles();
  list.forEach((a) => box.appendChild(articleRow(a)));

  const total = state.articles.length;
  $("#history-count").textContent =
    list.length === total ? `共 ${total} 篇` : `${list.length} / ${total} 篇`;
  $("#history-empty").classList.toggle("hidden", total > 0);
  $("#history-empty").textContent =
    total === 0 ? "还没有生成过文章" : "没有匹配的文章";
}

$("#refresh-history").addEventListener("click", loadHistory);
$("#history-search").addEventListener("input", renderHistory);
$("#history-filter").addEventListener("change", renderHistory);
$("#open-output").addEventListener("click", () => openFolder(""));

/* ---------------- 封面提示词页 ---------------- */

async function loadCoverList() {
  try {
    if (!state.articles.length) {
      const data = await api("/api/history");
      state.articles = data.articles;
      renderHomeRecent();
    }
    renderCoverList();
  } catch (e) {
    setStatus("加载封面提示词失败：" + e.message);
  }
}

function renderCoverList() {
  const box = $("#cover-list");
  box.innerHTML = "";
  const list = state.articles.filter((a) => a.has_cover_prompt);
  list.forEach((a) => {
    const item = articleRow(a);
    // 这一页点开直接落在封面提示词标签上
    item.addEventListener("click", (e) => {
      e.stopPropagation();
      openArticle(a.dir, "cover");
    }, true);
    box.appendChild(item);
  });
  $("#cover-count").textContent = list.length ? `${list.length} 篇` : "";
  $("#cover-empty").classList.toggle("hidden", list.length > 0);
  $("#cover-empty").textContent = state.articles.length
    ? "已生成的文章里还没有封面提示词。生成新文章时会自动产出。"
    : "还没有生成过文章";
}

$("#refresh-cover").addEventListener("click", async () => {
  const data = await api("/api/history");
  state.articles = data.articles;
  renderCoverList();
  renderHomeRecent();
});

/* ---------------- 写作规范页 ---------------- */

let specLoaded = false;

async function loadSpec(force = false) {
  if (specLoaded && !force) return;
  try {
    const data = await api("/api/writing-spec");
    const meta = $("#spec-meta");
    meta.innerHTML = "";
    const rows = [
      ["规范名称", data.name],
      ["生效文件", data.path || "（未找到）"],
      ["正文体量", `${data.chars} 字符`],
    ];
    rows.forEach(([k, v]) => {
      meta.appendChild(el("dt", null, k));
      meta.appendChild(el("dd", null, v));
    });
    $("#spec-body").innerHTML = data.content_html ||
      "<p class='empty'>没读到规范文件。检查 .claude/skills/learning-tech-blog/ 是否存在。</p>";
    specLoaded = true;
  } catch (e) {
    setStatus("加载写作规范失败：" + e.message);
  }
}

$("#refresh-spec").addEventListener("click", () => {
  specLoaded = false;
  loadSpec(true);
});

/* ---------------- 首页 ---------------- */

function renderHomeStatus() {
  const box = $("#home-status");
  if (!box) return;
  const bg = state.background;
  const cfg = state.config;
  if (!bg || !cfg) return;

  box.innerHTML = "";
  const rows = [
    ["写作模型", bg.models.writing],
    ["截图转录", bg.models.vision],
    ["插图生成", bg.models.image === "none" ? "手动配图（不出图）" : bg.models.image],
    ["写作规范", bg.skills[0] ? `${bg.skills[0].label}（${bg.skills[0].source_label}）` : "—"],
    ["输出目录", cfg.output_base],
  ];
  rows.forEach(([k, v]) => {
    box.appendChild(el("dt", null, k));
    box.appendChild(el("dd", null, v || "—"));
  });

  const running = [...state.jobs.values()]
    .filter((e) => e.snapshot && !["done", "failed", "cancelled"].includes(e.snapshot.status));
  $("#home-queue").textContent = running.length
    ? `队列中 ${running.length} 个任务`
    : "队列空闲";
}

function renderHomeRecent() {
  const box = $("#home-recent");
  if (!box) return;
  box.innerHTML = "";
  const list = state.articles.slice(0, 5);
  list.forEach((a) => box.appendChild(articleRow(a)));
  $("#home-empty").classList.toggle("hidden", state.articles.length > 0);
  if (state.articles.length) {
    const n = state.articles.filter((a) => a.has_cover_prompt).length;
    $("#quick-cover-desc").textContent = n
      ? `${n} 篇有封面提示词`
      : "查看文章的封面提示词";
  }
}

$("#quick-generate").addEventListener("click", () => showPage("generate"));
$("#quick-cover").addEventListener("click", () => showPage("cover"));
$("#quick-output").addEventListener("click", () => openFolder(""));
$("#home-refresh").addEventListener("click", async () => {
  await loadHistory();
  toast("已刷新");
});

/* ---------------- 文章详情 ---------------- */

async function openArticle(dir, tab = "article") {
  setStatus("加载文章…");
  try {
    const data = await api(`/api/history/${encodeURIComponent(dir)}`);
    const a = data.article;
    state.currentArticle = a;

    $("#modal-title").textContent = a.title || dir;
    $("#mpane-article").innerHTML = a.article_html || "<p class='empty'>没有正文</p>";
    $("#mpane-cover").innerHTML = a.cover_prompt_html ||
      "<p class='empty'>这篇没有封面提示词。生成文章时如果勾了「跳过封面提示词」，就不会有。</p>";
    $("#mpane-selfcheck").innerHTML = a.selfcheck_html || "<p class='empty'>这篇没有自检报告</p>";
    $("#mpane-changes").innerHTML = a.changes_html || "<p class='empty'>这篇没有去 AI 味改动清单</p>";
    $("#mpane-outline").innerHTML = a.outline_html || "<p class='empty'>这篇没有保存大纲</p>";
    $("#mpane-raw").querySelector("pre").textContent = a.article_raw || "";

    // 没有封面提示词时，复制按钮禁用，避免点了没反应还以为坏了
    $("#modal-copy-cover").disabled = !a.cover_prompt_md;

    $("#modal").classList.remove("hidden");
    switchModalTab(tab);
  } catch (e) {
    toast("加载失败：" + e.message, "error");
  } finally {
    setStatus("就绪");
  }
}

function switchModalTab(name) {
  $$(".mtab").forEach((t) => t.classList.toggle("is-active", t.dataset.mtab === name));
  $$(".mpane").forEach((p) => p.classList.toggle("is-active", p.id === `mpane-${name}`));
}

$$(".mtab").forEach((t) =>
  t.addEventListener("click", () => switchModalTab(t.dataset.mtab))
);

function closeModal() { $("#modal").classList.add("hidden"); }
$("#modal-close").addEventListener("click", closeModal);
$("#modal").addEventListener("click", (e) => {
  if (e.target === $("#modal")) closeModal();
});

$("#modal-folder").addEventListener("click", () => {
  if (state.currentArticle) openFolder(state.currentArticle.abs_path);
});

$("#modal-copy").addEventListener("click", async () => {
  if (!state.currentArticle) return;
  await copyText(state.currentArticle.article_raw, "已复制文章 Markdown");
});

$("#modal-copy-cover").addEventListener("click", async () => {
  if (!state.currentArticle) return;
  await copyText(state.currentArticle.cover_prompt_md, "已复制封面提示词");
});

async function copyText(text, okMessage) {
  if (!text) { toast("没有可复制的内容", "error"); return; }
  try {
    await navigator.clipboard.writeText(text);
    toast(okMessage, "ok");
  } catch (e) {
    toast("复制失败：" + e.message + "（可手动选中文本复制）", "error");
  }
}

/* ---------------- 设置与帮助 ---------------- */

let authorFields = [];

async function loadBackground() {
  try {
    const data = await api("/api/background");
    state.background = data;
    authorFields = data.author_fields || [];

    renderSkills(data.skills);
    renderTypeList(data.article_types);
    renderStages(data.stages);
    renderAuthorForm(data.author);
    renderModels(data);
    renderHomeStatus();
  } catch (e) {
    setStatus("加载设置失败：" + e.message);
  }
}

function renderSkills(skills) {
  const box = $("#skill-list");
  box.innerHTML = "";
  (skills || []).forEach((s) => {
    const card = el("div", "skill-item");
    const head = el("div", "skill-head");
    head.appendChild(el("span", "skill-name", s.label));
    head.appendChild(el("span", "badge " + (s.source === "skill" ? "ok" : ""),
      s.source_label));
    card.appendChild(head);
    if (s.purpose) card.appendChild(el("p", "note", s.purpose));

    const files = el("div", "skill-files");
    files.appendChild(el("span", "skill-label", "加载文件："));
    (s.files && s.files.length ? s.files : ["（回退到 prompts 副本）"]).forEach((f) => {
      files.appendChild(el("code", null, f));
    });
    card.appendChild(files);

    const path = el("div", "skill-files");
    path.appendChild(el("span", "skill-label", "路径："));
    path.appendChild(el("code", null, s.path || "—"));
    card.appendChild(path);

    card.appendChild(el("div", "skill-meta",
      `体量 ${s.chars} 字符` + (s.missing && s.missing.length
        ? `　缺失：${s.missing.join("、")}` : "")));
    box.appendChild(card);
  });
}

function renderTypeList(types) {
  const box = $("#type-list");
  box.innerHTML = "";
  (types || []).forEach((t) => {
    const item = el("div", "type-item");
    item.appendChild(el("div", "type-name", t.label));
    item.appendChild(el("div", "note", t.rule));
    box.appendChild(item);
  });
}

function renderStages(stages) {
  const box = $("#stage-list");
  box.innerHTML = "";
  (stages || []).forEach((s) => {
    const li = el("li");
    li.appendChild(el("b", null, s.name));
    li.appendChild(el("span", "note", "　" + s.desc));
    box.appendChild(li);
  });
}

function renderModels(data) {
  const box = $("#model-list");
  box.innerHTML = "";
  const rows = [
    ["写作 / 去 AI 味 / 封面提示词", data.models.writing],
    ["截图转录", data.models.vision],
    ["插图生成", data.models.image === "none" ? "手动配图（none）" : data.models.image],
  ];
  rows.forEach(([k, v]) => {
    box.appendChild(el("dt", null, k));
    box.appendChild(el("dd", null, v || "—"));
  });
  $("#writing-focus").textContent = data.writing_focus || "";
  renderLlmSwitch(data.llm);
}

/* 写作模型切换：DeepSeek ↔ OpenAI，写回 config.yaml */
function renderLlmSwitch(llm) {
  const seg = $("#llm-provider-btns");
  const hint = $("#llm-switch-hint");
  seg.innerHTML = "";
  hint.className = "hint";

  if (!llm) {
    $("#llm-switch").classList.add("hidden");
    return;
  }
  $("#llm-switch").classList.remove("hidden");

  (llm.providers || []).forEach((p) => {
    const btn = el("button", "btn small", p.label);
    btn.type = "button";
    if (p.key === llm.provider) btn.classList.add("is-active");
    if (!llm.switchable) btn.disabled = true;
    btn.title = `${p.model}${p.api_key_configured ? "" : `　（${p.api_key_env} 未设置）`}`;
    btn.addEventListener("click", () => switchLlmProvider(p.key, btn));
    seg.appendChild(btn);
  });

  if (!llm.switchable) {
    hint.className = "hint warn";
    hint.textContent = "当前 config.yaml 是旧版形状（没有 llm 节），请手动改配置后再用界面切换";
  } else if (!llm.api_key_configured) {
    hint.className = "hint warn";
    hint.textContent =
      `${llm.api_key_env} 未设置，当前选中的 ${llm.label} 调用会失败` +
      (llm.provider === "openai" ? "（需要梯子 + 预付费额度）" : "");
  } else {
    hint.className = "hint ok";
    hint.textContent = `正在使用 ${llm.model}`;
  }
}

async function switchLlmProvider(provider, btn) {
  if (btn) btn.disabled = true;
  setStatus("正在切换写作模型…");
  try {
    const data = await api("/api/llm-provider", {
      method: "POST",
      body: JSON.stringify({ provider }),
    });
    renderLlmSwitch(data.llm);
    renderConfigWarnings(data.warnings);
    const target = (data.llm.providers || []).find((p) => p.key === provider);
    const okKey = target && target.api_key_configured;
    toast(
      `写作模型已切换为 ${data.llm.label}（${data.llm.model}）` +
        (okKey ? "" : " —— 但 API key 还没配"),
      okKey ? "ok" : "error"
    );
    loadBackground(); // 把「模型配置」那几行也刷新掉
  } catch (e) {
    toast("切换失败：" + e.message, "error");
    if (btn) btn.disabled = false;
  } finally {
    setStatus("就绪");
  }
}

function renderAuthorForm(author) {
  const box = $("#author-form");
  box.innerHTML = "";
  authorFields.forEach((f) => {
    const wrap = el("label", "field");
    wrap.appendChild(el("span", "label", f.label));
    const input = el("input");
    input.type = "text";
    input.dataset.key = f.key;
    input.placeholder = f.placeholder || "";
    input.value = (author && author[f.key]) || "";
    input.addEventListener("input", updateAuthorPreview);
    wrap.appendChild(input);
    box.appendChild(wrap);
  });
  updateAuthorPreview();
}

function collectAuthor() {
  const out = {};
  $$("#author-form input").forEach((i) => { out[i.dataset.key] = i.value.trim(); });
  return out;
}

function updateAuthorPreview() {
  const a = collectAuthor();
  const parts = [];
  if (a.identity) parts.push(a.identity);
  if (a.tech_stack) parts.push(`技术栈偏 ${a.tech_stack}`);
  if (a.experience) parts.push(`经验：${a.experience}`);
  if (a.extra) parts.push(a.extra);

  const pre = $("#author-preview");
  if (!parts.length) {
    pre.textContent = "（字段全空，不会注入任何内容，文章按默认方式写）";
    return;
  }
  pre.textContent =
    `## 作者背景（轻度参考）\n\n这篇的写作者是：${parts.join("；")}\n\n` +
    "用法要求：\n" +
    "1. 只在题材和这个背景相关时，自然地体现这个视角。关系不大就当它不存在\n" +
    "2. 不要以第一人称自述身份或经历，不要写「作为后端开发者」这类句子\n" +
    "3. 上面所有写作规则不受影响，不要因为作者背景而改变文章的深度、范围和结构\n" +
    "4. 以「略微影响举例和侧重」为限，不要改变主题、结构和结论";
}

async function saveAuthor() {
  const btn = $("#save-author");
  btn.disabled = true;
  try {
    const data = await api("/api/author", {
      method: "POST",
      body: JSON.stringify(collectAuthor()),
    });
    $("#author-hint").textContent = "已保存，下一篇生效";
    $("#author-preview").textContent =
      data.author_prompt_preview || "（字段全空，不会注入）";
    setTimeout(() => { $("#author-hint").textContent = ""; }, 3000);
    toast("写作者背景已保存", "ok");
  } catch (e) {
    toast("保存失败：" + e.message, "error");
  } finally {
    btn.disabled = false;
  }
}

$("#save-author").addEventListener("click", saveAuthor);

// 清空会直接写盘，先确认一次，避免误点把填过的背景清掉
$("#clear-author").addEventListener("click", async () => {
  const filled = Object.values(collectAuthor()).some((v) => v);
  if (filled) {
    const ok = await confirmDialog(
      "清空写作者背景",
      "会把四个字段都清空并立即保存到 data/writer_profile.json。清空后文章按默认方式写。",
      "清空并保存"
    );
    if (!ok) return;
  }
  $$("#author-form input").forEach((i) => { i.value = ""; });
  await saveAuthor();
});

$("#refresh-background").addEventListener("click", loadBackground);

/* ---------------- 键盘 ---------------- */

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("#confirm").classList.contains("hidden")) {
    $("#confirm-no").click();
    return;
  }
  if (!$("#modal").classList.contains("hidden")) closeModal();
});

/* ---------------- 启动 ---------------- */

(async function init() {
  try {
    await loadConfig();
    await loadBackground();   // 设置页与首页共用这份数据
    await loadHistory();      // 首页「最近生成」和历史页共用
    await loadJobs();
    // 队列里可能有未完成的任务，刷新一次状态
    for (const id of state.jobs.keys()) refreshOne(id);
    renderHomeStatus();
  } catch (e) {
    setStatus("初始化失败：" + e.message);
    toast("初始化失败：" + e.message, "error");
  }
  // 兜底轮询，防止 SSE 掉线后漏掉状态变化
  setInterval(loadJobs, 15000);
})();

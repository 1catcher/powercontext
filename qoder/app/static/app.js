const $ = (id) => document.getElementById(id);

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try { detail = (await response.json()).error || detail; } catch (_) {}
    throw new Error(detail);
  }
  return response.json();
}

function renderResults(list, items, highlight) {
  list.innerHTML = "";
  if (!items.length) {
    const li = document.createElement("li");
    li.textContent = "没有匹配结果";
    li.style.color = "var(--muted)";
    list.appendChild(li);
    return;
  }
  for (const item of items) {
    const li = document.createElement("li");
    const head = document.createElement("div");
    const title = document.createElement("span");
    title.className = "title";
    title.textContent = item.title;
    const kind = document.createElement("span");
    kind.className = "kind";
    kind.textContent = item.kind;
    head.append(title, kind);
    li.appendChild(head);
    if (item.snippet) {
      const snippet = document.createElement("div");
      snippet.className = "snippet";
      let text = item.snippet;
      const terms = highlight.split(/\s+/).filter(Boolean);
      for (const term of terms) {
        text = text.replace(
          new RegExp(term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi"),
          (match) => `<mark>${match}</mark>`
        );
      }
      snippet.innerHTML = text;
      li.appendChild(snippet);
    }
    list.appendChild(li);
  }
}

function renderMaterials(items) {
  const list = $("material-list");
  list.innerHTML = "";
  if (!items.length) {
    const li = document.createElement("li");
    li.textContent = "资料库为空，先导入一份资料";
    li.style.color = "var(--muted)";
    list.appendChild(li);
    return;
  }
  for (const item of items) {
    const li = document.createElement("li");
    const head = document.createElement("div");
    const title = document.createElement("span");
    title.className = "title";
    title.textContent = item.title;
    const kind = document.createElement("span");
    kind.className = "kind";
    kind.textContent = `${item.kind} · ${item.chars} 字符 · ${item.created_at}`;
    head.append(title, kind);
    li.appendChild(head);
    list.appendChild(li);
  }
}

async function refreshBadges() {
  try {
    await api("/api/materials");
    $("seekdb-badge").textContent = "seekdb: 已连接";
    $("seekdb-badge").className = "badge ok db";
  } catch (_) {
    $("seekdb-badge").textContent = "seekdb: 未连接";
    $("seekdb-badge").className = "badge fail";
  }
  try {
    const payload = await api("/api/state");
    $("pc-badge").textContent = payload.ok
      ? "PowerContext: 已连接"
      : "PowerContext: 已连接（暂无状态）";
    $("pc-badge").className = "badge ok mem";
  } catch (_) {
    $("pc-badge").textContent = "PowerContext: 未连接";
    $("pc-badge").className = "badge fail";
  }
}

async function refreshMaterials() {
  const payload = await api("/api/materials");
  renderMaterials(payload.items);
}

async function loadState() {
  const payload = await api("/api/state");
  if (payload.state) {
    const todo = Array.isArray(payload.state.todo)
      ? payload.state.todo.join("\n")
      : (payload.state.todo || "");
    $("state-todo").value = todo;
    $("state-notes").value = payload.state.notes || "";
    $("state-updated").textContent = `上次保存: ${payload.state.updated_at || "未知"}`;
  } else {
    $("state-updated").textContent = "暂无已保存状态";
  }
}

$("search-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = $("search-q").value.trim();
  if (!query) return;
  const kind = $("search-kind").value;
  const payload = await api(
    `/api/search?q=${encodeURIComponent(query)}` +
      (kind ? `&kind=${encodeURIComponent(kind)}` : "")
  );
  $("search-meta").textContent =
    `检索方式: ${payload.mode} · 命中 ${payload.count} 条`;
  renderResults($("search-results"), payload.results, query);
});

$("ingest-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = await api("/api/ingest", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      title: $("ingest-title").value.trim(),
      kind: $("ingest-kind").value,
      content: $("ingest-content").value,
    }),
  });
  $("ingest-msg").textContent =
    `已导入 #${payload.row.id}《${payload.row.title}》（${payload.row.chars} 字符），可立即检索`;
  $("ingest-title").value = "";
  $("ingest-content").value = "";
  refreshMaterials();
});

$("save-state").addEventListener("click", async () => {
  const payload = await api("/api/state", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      todo: $("state-todo").value
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean),
      notes: $("state-notes").value.trim(),
    }),
  });
  $("state-msg").textContent = `已保存到 PowerContext: ${payload.source_id}`;
  loadState();
});

refreshBadges();
refreshMaterials();
loadState();

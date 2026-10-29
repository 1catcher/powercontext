/**
 * Browser side of the knowledge base.
 *
 * The embedding model runs here, on the user's device. WebGPU is used when the
 * browser offers it and the model falls back to WASM otherwise; the active
 * backend is always shown, because a silent fallback would make the measured
 * latency look like a GPU result.
 */

import { env, pipeline } from "https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.7.6";

const backendLabel = document.getElementById("backend");
const loadButton = document.getElementById("load");
const importButton = document.getElementById("import");
const searchButton = document.getElementById("search");
const documentsField = document.getElementById("documents");
const queryField = document.getElementById("query");
const importStatus = document.getElementById("import-status");
const searchStatus = document.getElementById("search-status");
const results = document.getElementById("results");
const modelSelect = document.getElementById("model");
const dtypeSelect = document.getElementById("dtype");

env.allowLocalModels = false;

let extractor = null;
let activeDevice = null;

async function pick_device() {
  if (typeof navigator !== "undefined" && navigator.gpu) {
    try {
      const adapter = await navigator.gpu.requestAdapter();
      if (adapter) {
        return "webgpu";
      }
    } catch {
      // The adapter request failed: the WASM path is the honest answer.
    }
  }
  return "wasm";
}

async function load_model() {
  loadButton.disabled = true;
  importStatus.textContent = "正在加载模型（首次会下载模型文件）…";
  const device = await pick_device();
  const dtype = dtypeSelect.value;
  try {
    extractor = await pipeline("feature-extraction", modelSelect.value, { device, dtype });
    activeDevice = device;
    backendLabel.textContent = `推理后端：${device === "webgpu" ? "WebGPU" : "WASM（浏览器未提供 WebGPU）"} · 模型 ${modelSelect.value} · ${dtype}`;
    importStatus.textContent = "模型就绪，可以导入文档。";
    importButton.disabled = false;
    searchButton.disabled = false;
  } catch (error) {
    // A GPU build can fail on a driver that advertises the API but cannot run
    // the model. The WASM build is the documented fallback.
    if (device === "webgpu") {
      importStatus.textContent = `WebGPU 初始化失败（${error.message}），改用 WASM。`;
      backendLabel.textContent = "推理后端：WASM（WebGPU 不可用）";
      extractor = await pipeline("feature-extraction", modelSelect.value, { device: "wasm", dtype });
      activeDevice = "wasm";
      importButton.disabled = false;
      searchButton.disabled = false;
    } else {
      importStatus.textContent = `模型加载失败：${error.message}`;
    }
  } finally {
    loadButton.disabled = false;
  }
}

function parse_documents(raw) {
  const parsed = [];
  for (const line of raw.split(/\n{2,}|\n/)) {
    const trimmed = line.trim();
    if (!trimmed) {
      continue;
    }
    const separator = trimmed.indexOf("|");
    const title = separator > 0 ? trimmed.slice(0, separator).trim() : `文档 ${parsed.length + 1}`;
    const text = separator > 0 ? trimmed.slice(separator + 1).trim() : trimmed;
    if (text) {
      parsed.push({ id: `doc-${parsed.length + 1}-${Date.now()}`, title, text });
    }
  }
  return parsed;
}

async function embed(texts) {
  const output = await extractor(texts, { pooling: "mean", normalize: true });
  return Array.from(output.data, (vector, index) => Array.from(vector.slice(index * output.dims, (index + 1) * output.dims)));
}

async function import_documents() {
  importButton.disabled = true;
  const documents = parse_documents(documentsField.value);
  if (documents.length === 0) {
    importStatus.textContent = "没有可导入的文档。";
    importButton.disabled = false;
    return;
  }
  importStatus.textContent = `正在本机向量化 ${documents.length} 篇文档…`;
  try {
    const started = performance.now();
    const embeddings = await embed(documents.map((document) => document.text));
    const embed_ms = performance.now() - started;
    const response = await fetch("/api/import", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ documents, embeddings }),
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error ?? "写入失败");
    }
    importStatus.textContent =
      `已写入 ${payload.stored} 篇文档（seekdb 写入 ${Math.round(payload.write_ms)} ms，` +
      `浏览器向量化 ${Math.round(embed_ms)} ms，库内共 ${payload.count} 篇）`;
  } catch (error) {
    importStatus.textContent = `导入失败：${error.message}`;
  } finally {
    importButton.disabled = false;
  }
}

async function search() {
  const query = queryField.value.trim();
  if (!query) {
    return;
  }
  searchButton.disabled = true;
  searchStatus.textContent = "正在本机向量化查询…";
  results.replaceChildren();
  try {
    const started = performance.now();
    const [vector] = await embed([query]);
    const embed_ms = performance.now() - started;
    const response = await fetch("/api/search", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ query, vector, nResults: 10 }),
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error ?? "检索失败");
    }
    searchStatus.textContent =
      `查询向量化 ${Math.round(embed_ms)} ms · seekdb 混合检索 ${Math.round(payload.search_ms)} ms · ` +
      `命中 ${payload.hits.length} 条（后端 ${activeDevice}）`;
    for (const hit of payload.hits) {
      const item = document.createElement("li");
      const title = document.createElement("strong");
      title.textContent = hit.metadata?.title || hit.id;
      const body = document.createElement("p");
      body.textContent = (hit.document ?? "").slice(0, 400);
      const meta = document.createElement("small");
      meta.textContent = `id ${hit.id} · distance ${hit.distance ?? "-"}`;
      item.append(title, body, meta);
      results.append(item);
    }
  } catch (error) {
    searchStatus.textContent = `检索失败：${error.message}`;
  } finally {
    searchButton.disabled = false;
  }
}

loadButton.addEventListener("click", load_model);
importButton.addEventListener("click", import_documents);
searchButton.addEventListener("click", search);

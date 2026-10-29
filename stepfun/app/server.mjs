/**
 * Backend of the on-device knowledge base.
 *
 * The browser does the inference: it downloads an open embedding model, runs it
 * with WebGPU (or WASM) and posts the vectors here. This service owns the
 * seekdb collection, so the vectors, the source text and the metadata live in
 * seekdb, and the hybrid retrieval is one seekdb query.
 */

import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

import { FulltextIndexConfig, Schema, SeekdbAdminClient, SeekdbClient, VectorIndexConfig } from "seekdb";

const PUBLIC_DIR = fileURLToPath(new URL("./public/", import.meta.url));
const COLLECTION = process.env.SEEKDB_COLLECTION ?? "documents";
const EMBED_DIMENSION = Number(process.env.EMBED_DIMENSION ?? 384);
const PORT = Number(process.env.PORT ?? 8080);

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
};

const DATABASE = process.env.SEEKDB_DATABASE ?? "knowledge";

function connection() {
  const path = process.env.SEEKDB_PATH;
  if (path) {
    // Embedded mode: seekdb runs inside this process, no server to deploy.
    return { path };
  }
  return {
    host: process.env.SEEKDB_HOST ?? "127.0.0.1",
    port: Number(process.env.SEEKDB_PORT ?? 2881),
    user: process.env.SEEKDB_USER ?? "root",
    password: process.env.SEEKDB_PASSWORD ?? "",
  };
}

async function connect() {
  const settings = connection();
  const admin = new SeekdbAdminClient(settings);
  await admin.createDatabase(DATABASE).catch(() => {});
  await admin.close?.();
  return new SeekdbClient({ ...settings, database: DATABASE });
}

const client = await connect();
const schema = new Schema({
  fulltextIndex: new FulltextIndexConfig("space"),
  vectorIndex: new VectorIndexConfig({ hnsw: { dimension: EMBED_DIMENSION, distance: "l2" } }),
});
// The browser produces the vectors, so seekdb never calls a model itself.
const collection = await create_collection();

async function create_collection() {
  try {
    return await client.createCollection({
      name: COLLECTION,
      schema,
      embeddingFunction: null,
    });
  } catch (error) {
    // A collection that is already there is the normal case on a restart; any
    // other failure is reported instead of being hidden behind it.
    try {
      return await client.getCollection({ name: COLLECTION, embeddingFunction: null });
    } catch {
      throw error;
    }
  }
}

function send(response, status, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body),
  });
  response.end(body);
}

async function read_json(request) {
  const chunks = [];
  for await (const chunk of request) {
    chunks.push(chunk);
  }
  if (chunks.length === 0) {
    return {};
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf-8"));
}

async function handle_import(request, response) {
  const payload = await read_json(request);
  const documents = Array.isArray(payload.documents) ? payload.documents : [];
  const embeddings = Array.isArray(payload.embeddings) ? payload.embeddings : [];
  if (documents.length === 0 || documents.length !== embeddings.length) {
    send(response, 400, { error: "documents and embeddings must be the same non-empty length" });
    return;
  }
  const ids = documents.map((document) => String(document.id));
  const texts = documents.map((document) => `${document.title ?? ""}\n${document.text ?? ""}`.trim());
  const metadatas = documents.map((document) => ({
    title: document.title ?? "",
    source: document.source ?? "",
    characters: (document.text ?? "").length,
  }));
  const started = process.hrtime.bigint();
  await collection.add({ ids, embeddings, documents: texts, metadatas });
  const elapsed = Number(process.hrtime.bigint() - started) / 1e6;
  const state = await collection.count();
  send(response, 200, { stored: documents.length, count: state.count ?? null, write_ms: elapsed });
}

async function handle_search(request, response) {
  const payload = await read_json(request);
  const query = String(payload.query ?? "").trim();
  const vector = Array.isArray(payload.vector) ? payload.vector : null;
  const nResults = Math.min(Math.max(Number(payload.nResults ?? 10), 1), 50);
  if (!query || !vector) {
    send(response, 400, { error: "query and vector are required" });
    return;
  }
  const started = process.hrtime.bigint();
  // One seekdb statement: the full-text branch and the vector branch are fused
  // by seekdb, not by this service.
  const results = await collection.hybridSearch({
    query: { whereDocument: { $contains: query } },
    knn: { queryEmbeddings: vector },
    rank: { rrf: { rankWindowSize: 50, rankConstant: 60 } },
    nResults,
    include: ["documents", "metadatas", "distances"],
  });
  const elapsed = Number(process.hrtime.bigint() - started) / 1e6;
  const ids = results.ids?.flat().filter((value) => value !== null && value !== undefined) ?? [];
  const documents = results.documents?.flat() ?? [];
  const metadatas = results.metadatas?.flat() ?? [];
  const distances = results.distances?.flat() ?? [];
  send(response, 200, {
    search_ms: elapsed,
    hits: ids.map((id, index) => ({
      id,
      document: documents[index] ?? "",
      metadata: metadatas[index] ?? {},
      distance: distances[index] ?? null,
    })),
  });
}

async function handle_state(request, response) {
  const state = await collection.count();
  send(response, 200, { collection: COLLECTION, count: state.count ?? null });
}

async function serve_static(request, response) {
  const requested = new URL(request.url, "http://localhost").pathname;
  const relative = requested === "/" ? "index.html" : requested.replace(/^\/+/, "");
  const target = join(PUBLIC_DIR, normalize(relative));
  if (!target.startsWith(PUBLIC_DIR)) {
    send(response, 403, { error: "forbidden" });
    return;
  }
  try {
    const body = await readFile(target);
    response.writeHead(200, { "content-type": MIME[extname(target)] ?? "application/octet-stream" });
    response.end(body);
  } catch {
    send(response, 404, { error: "not found" });
  }
}

const server = createServer((request, response) => {
  const route = new URL(request.url, "http://localhost").pathname;
  const work = async () => {
    if (route === "/api/import" && request.method === "POST") {
      await handle_import(request, response);
    } else if (route === "/api/search" && request.method === "POST") {
      await handle_search(request, response);
    } else if (route === "/api/state" && request.method === "GET") {
      await handle_state(request, response);
    } else {
      await serve_static(request, response);
    }
  };
  work().catch((error) => {
    console.error(error);
    send(response, 500, { error: String(error?.message ?? error) });
  });
});

server.listen(PORT, () => {
  console.log(`knowledge base on http://127.0.0.1:${PORT} (seekdb collection ${COLLECTION})`);
});

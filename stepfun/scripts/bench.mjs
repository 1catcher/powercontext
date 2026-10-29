/**
 * Measure the knowledge base: model size, embedding throughput, retrieval
 * latency and retrieval quality for the quantised model against the full
 * precision one.
 *
 * The browser runs the same model files through the same runtime, so the sizes
 * and the quality measured here are the ones the browser sees; only the
 * execution backend differs (WebGPU in the browser, WASM here).
 */

import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

import { FulltextIndexConfig, Schema, SeekdbClient, VectorIndexConfig } from "seekdb";
import { env, pipeline } from "@huggingface/transformers";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const MODEL = process.env.MODEL ?? "Xenova/all-MiniLM-L6-v2";
const VARIANTS = [
  { name: "int8", dtype: "q8", file: "onnx/model_quantized.onnx" },
  { name: "fp32", dtype: "fp32", file: "onnx/model.onnx" },
  { name: "fp16", dtype: "fp16", file: "onnx/model_fp16.onnx" },
];

// The model files can be served from a local folder, which is what an offline
// deployment does; the browser fetches the same files from the model hub.
const LOCAL_MODEL_PATH = process.env.LOCAL_MODEL_PATH ?? "";
env.allowLocalModels = Boolean(LOCAL_MODEL_PATH);
if (LOCAL_MODEL_PATH) {
  env.localModelPath = LOCAL_MODEL_PATH;
}

const corpus = JSON.parse(await readFile(`${HERE}corpus.json`, "utf-8"));

async function remote_size(model, file) {
  // The size of the model file is a property of the model, not of this run:
  // it is read from the local copy when there is one.
  if (LOCAL_MODEL_PATH) {
    const { statSync } = await import("node:fs");
    try {
      return statSync(`${LOCAL_MODEL_PATH}/${model}/${file}`).size;
    } catch {
      return null;
    }
  }
  const url = `https://huggingface.co/${model}/resolve/main/${file}`;
  const response = await fetch(url, { method: "HEAD" });
  const length = response.headers.get("content-length");
  return length ? Number(length) : null;
}

async function embed(extractor, texts) {
  const output = await extractor(texts, { pooling: "mean", normalize: true });
  const vectors = [];
  for (let index = 0; index < texts.length; index += 1) {
    vectors.push(Array.from(output.data.slice(index * output.dims, (index + 1) * output.dims)));
  }
  return vectors;
}

function recall_at_k(ranked, relevant, k) {
  const wanted = new Set(relevant);
  const hits = ranked.slice(0, k).filter((id) => wanted.has(id)).length;
  return hits / Math.max(wanted.size, 1);
}

function ndcg_at_k(ranked, relevant, k) {
  const wanted = new Set(relevant);
  let gain = 0;
  let ideal = 0;
  for (let index = 0; index < k; index += 1) {
    if (wanted.has(ranked[index])) {
      gain += 1 / Math.log2(index + 2);
    }
    if (index < wanted.size) {
      ideal += 1 / Math.log2(index + 2);
    }
  }
  return ideal > 0 ? gain / ideal : 0;
}

const client = new SeekdbClient({
  host: process.env.SEEKDB_HOST ?? "127.0.0.1",
  port: Number(process.env.SEEKDB_PORT ?? 2881),
  user: process.env.SEEKDB_USER ?? "root",
  password: process.env.SEEKDB_PASSWORD ?? "",
  database: process.env.SEEKDB_DATABASE ?? "bench",
});

const report = { model: MODEL, variants: [] };

for (const variant of VARIANTS) {
  const size = await remote_size(MODEL, variant.file);
  let extractor;
  try {
    extractor = await pipeline("feature-extraction", MODEL, { device: "wasm", dtype: variant.dtype });
  } catch (error) {
    report.variants.push({ ...variant, error: String(error.message) });
    continue;
  }
  const texts = corpus.documents.map((document) => `${document.title}\n${document.text}`);
  const warm = await embed(extractor, texts.slice(0, 2));
  const started = performance.now();
  const document_vectors = await embed(extractor, texts);
  const document_ms = performance.now() - started;
  const query_started = performance.now();
  const query_vectors = await embed(extractor, corpus.queries.map((query) => query.text));
  const query_ms = performance.now() - query_started;

  const collection_name = `bench_${variant.name}`;
  const schema = new Schema({
    fulltextIndex: new FulltextIndexConfig("space"),
    vectorIndex: new VectorIndexConfig({ hnsw: { dimension: document_vectors[0].length, distance: "l2" } }),
  });
  const collection = await client.createCollection({
    name: collection_name,
    schema,
    embeddingFunction: null,
  }).catch(async () => client.getCollection(collection_name, { embeddingFunction: null }));
  await collection.add({
    ids: corpus.documents.map((document) => document.id),
    embeddings: document_vectors,
    documents: corpus.documents.map((document) => `${document.title}\n${document.text}`),
    metadatas: corpus.documents.map((document) => ({ title: document.title })),
  });

  const latencies = [];
  const recalls = [];
  const ndcgs = [];
  for (let index = 0; index < corpus.queries.length; index += 1) {
    const query = corpus.queries[index];
    const started_at = performance.now();
    const results = await collection.hybridSearch({
      query: { whereDocument: { $contains: query.text } },
      knn: { queryEmbeddings: query_vectors[index] },
      rank: { rrf: { rankWindowSize: 50, rankConstant: 60 } },
      nResults: 10,
      include: ["documents", "distances"],
    });
    latencies.push(performance.now() - started_at);
    const ids = results.ids?.flat().filter(Boolean) ?? [];
    recalls.push(recall_at_k(ids, query.relevant, 5));
    ndcgs.push(ndcg_at_k(ids, query.relevant, 5));
  }
  latencies.sort((left, right) => left - right);
  report.variants.push({
    name: variant.name,
    dtype: variant.dtype,
    model_file: variant.file,
    model_bytes: size,
    document_embed_ms: Math.round(document_ms),
    document_embed_per_second: Number((corpus.documents.length / (document_ms / 1000)).toFixed(2)),
    query_embed_ms: Math.round(query_ms / corpus.queries.length),
    search_p50_ms: Math.round(latencies[Math.floor(latencies.length / 2)]),
    recall_at_5: Number((recalls.reduce((total, value) => total + value, 0) / recalls.length).toFixed(4)),
    ndcg_at_5: Number((ndcgs.reduce((total, value) => total + value, 0) / ndcgs.length).toFixed(4)),
    vector_sample: document_vectors[0].slice(0, 4),
  });
}

const int8 = report.variants.find((variant) => variant.name === "int8");
const fp32 = report.variants.find((variant) => variant.name === "fp32");
if (int8?.model_bytes && fp32?.model_bytes) {
  report.compression_ratio = Number((fp32.model_bytes / int8.model_bytes).toFixed(2));
}
console.log(JSON.stringify(report, null, 2));
await client.close();

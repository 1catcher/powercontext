/**
 * Retrieval quality and latency of the knowledge base, measured on seekdb.
 *
 * The vectors come from `embed_corpus.py` (the same ONNX files the browser
 * downloads), so this script can be run without a browser: it writes every
 * variant into its own seekdb collection, asks the hybrid query and reports
 * Recall@5, nDCG@5 and the query latency.
 */

import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

import { FulltextIndexConfig, Schema, SeekdbClient, VectorIndexConfig } from "seekdb";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const corpus = JSON.parse(await readFile(`${HERE}corpus.json`, "utf-8"));
const vectors = JSON.parse(await readFile(`${HERE}vectors.json`, "utf-8"));

const client = new SeekdbClient({
  host: process.env.SEEKDB_HOST ?? "127.0.0.1",
  port: Number(process.env.SEEKDB_PORT ?? 2881),
  user: process.env.SEEKDB_USER ?? "root",
  password: process.env.SEEKDB_PASSWORD ?? "",
  database: process.env.SEEKDB_DATABASE ?? "bench",
});

function recall_at_k(ranked, relevant, k) {
  const wanted = new Set(relevant);
  return ranked.slice(0, k).filter((id) => wanted.has(id)).length / Math.max(wanted.size, 1);
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

const report = { model: vectors.model, variants: [] };

for (const variant of vectors.variants) {
  const collection_name = `quality_${variant.name}`;
  const schema = new Schema({
    fulltextIndex: new FulltextIndexConfig("space"),
    vectorIndex: new VectorIndexConfig({
      hnsw: { dimension: variant.document_vectors[0].length, distance: "l2" },
    }),
  });
  const collection = await client.createCollection({
    name: collection_name,
    schema,
    embeddingFunction: null,
  }).catch(async () => client.getCollection({ name: collection_name, embeddingFunction: null }));
  await collection.add({
    ids: corpus.documents.map((document) => document.id),
    embeddings: variant.document_vectors,
    documents: corpus.documents.map((document) => `${document.title}\n${document.text}`),
    metadatas: corpus.documents.map((document) => ({ title: document.title })),
  });

  const latencies = [];
  const recalls = [];
  const ndcgs = [];
  for (let index = 0; index < corpus.queries.length; index += 1) {
    const query = corpus.queries[index];
    const started = performance.now();
    const results = await collection.hybridSearch({
      query: { whereDocument: { $contains: query.text } },
      knn: { queryEmbeddings: variant.query_vectors[index] },
      rank: { rrf: { rankWindowSize: 50, rankConstant: 60 } },
      nResults: 10,
      include: ["documents", "distances"],
    });
    latencies.push(performance.now() - started);
    const ids = results.ids?.flat().filter(Boolean) ?? [];
    recalls.push(recall_at_k(ids, query.relevant, 5));
    ndcgs.push(ndcg_at_k(ids, query.relevant, 5));
  }
  latencies.sort((left, right) => left - right);
  report.variants.push({
    name: variant.name,
    model_bytes: variant.model_bytes,
    recall_at_5: Number((recalls.reduce((total, value) => total + value, 0) / recalls.length).toFixed(4)),
    ndcg_at_5: Number((ndcgs.reduce((total, value) => total + value, 0) / ndcgs.length).toFixed(4)),
    search_p50_ms: Number(latencies[Math.floor(latencies.length / 2)].toFixed(2)),
    search_max_ms: Number(latencies[latencies.length - 1].toFixed(2)),
  });
}

const int8 = report.variants.find((variant) => variant.name === "int8");
const fp32 = report.variants.find((variant) => variant.name === "fp32");
if (int8?.model_bytes && fp32?.model_bytes) {
  report.compression_ratio = Number((fp32.model_bytes / int8.model_bytes).toFixed(2));
}
console.log(JSON.stringify(report, null, 2));
await client.close();

"""Embed the benchmark corpus with each model variant.

The browser runs the same ONNX files, so the vectors produced here are the
vectors the browser produces; only the execution backend differs.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

HERE = Path(__file__).parent
MODEL_DIR = Path("/root/models/Xenova/all-MiniLM-L6-v2")
VARIANTS = [
    ("int8", "onnx/model_quantized.onnx"),
    ("fp32", "onnx/model.onnx"),
    ("fp16", "onnx/model_fp16.onnx"),
]


def embed(session, tokenizer, texts: list[str]) -> np.ndarray:
    encoded = tokenizer.encode_batch(texts)
    ids = np.array([item.ids for item in encoded], dtype=np.int64)
    mask = np.array([item.attention_mask for item in encoded], dtype=np.int64)
    type_ids = np.zeros_like(ids)
    outputs = session.run(
        None,
        {"input_ids": ids, "attention_mask": mask, "token_type_ids": type_ids},
    )
    hidden = outputs[0]
    weights = mask[..., None].astype(np.float32)
    pooled = (hidden * weights).sum(axis=1) / np.clip(weights.sum(axis=1), 1e-9, None)
    norms = np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12, None)
    return (pooled / norms).astype(np.float32)


def main() -> None:
    corpus = json.loads((HERE / "corpus.json").read_text(encoding="utf-8"))
    tokenizer = Tokenizer.from_file(str(MODEL_DIR / "tokenizer.json"))
    tokenizer.enable_truncation(max_length=256)
    tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
    documents = [f"{item['title']}\n{item['text']}" for item in corpus["documents"]]
    queries = [item["text"] for item in corpus["queries"]]
    report = {"model": "Xenova/all-MiniLM-L6-v2", "variants": []}
    for name, relative in VARIANTS:
        path = MODEL_DIR / relative
        session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        embed(session, tokenizer, documents[:2])
        started = time.perf_counter()
        document_vectors = embed(session, tokenizer, documents)
        document_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        query_vectors = embed(session, tokenizer, queries)
        query_ms = (time.perf_counter() - started) * 1000
        report["variants"].append(
            {
                "name": name,
                "file": relative,
                "model_bytes": path.stat().st_size,
                "document_embed_ms": round(document_ms, 1),
                "document_embed_per_second": round(len(documents) / (document_ms / 1000), 2),
                "query_embed_ms": round(query_ms / len(queries), 1),
                "document_vectors": [vector.tolist() for vector in document_vectors],
                "query_vectors": [vector.tolist() for vector in query_vectors],
            }
        )
    (HERE / "vectors.json").write_text(json.dumps(report), encoding="utf-8")
    summary = {
        "model": report["model"],
        "variants": [
            {key: value for key, value in variant.items() if key not in {"document_vectors", "query_vectors"}}
            for variant in report["variants"]
        ],
    }
    print(json.dumps(summary, indent=2))


main()

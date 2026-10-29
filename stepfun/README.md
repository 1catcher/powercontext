# 端侧 AI 推理 × seekdb 混合检索 —— 迷你知识库

> 一个可运行的 Web 应用：文档在**浏览器本地**向量化，向量与原文写入 **seekdb**，
> 查询也在浏览器本地向量化，**混合检索由 seekdb 一条查询完成**。

## 1 · 做了什么

| 环节 | 在哪里做 | 说明 |
| --- | --- | --- |
| 文档向量化 | 浏览器 | Transformers.js 加载开源 embedding 模型，WebGPU 优先，不可用时降级 WASM |
| 存储 | seekdb | `seekdb-js` 建集合，HNSW 向量索引 + 全文索引，原文与元数据一并入库 |
| 查询向量化 | 浏览器 | 与文档同一个模型、同一个后端 |
| 混合检索 | **seekdb** | `collection.hybridSearch()`：全文分支 + 向量分支在 seekdb 内用 RRF 融合，服务端不自行拼合两路结果 |

应用结构：

```
stepfun/
├── app/
│   ├── server.mjs          # 后端：持有 seekdb 集合，暴露 /api/import 与 /api/search
│   ├── package.json
│   └── public/
│       ├── index.html      # 界面
│       ├── app.js          # 浏览器端推理（Transformers.js）
│       └── styles.css
├── scripts/
│   ├── bench.mjs           # 端到端实测（transformers.js，浏览器同款链路）
│   ├── embed_corpus.py     # 用同一份 ONNX 模型文件向量化语料
│   ├── search_quality.mjs  # seekdb 检索质量与延迟
│   └── corpus.json         # 12 篇语料 + 12 条带标注的查询（用于评测质量）
└── README.md               # 本页
```

## 2 · 怎么跑

### 2.1 起 seekdb

```bash
docker run -d --name seekdb -p 2881:2881 \
  -e MYSQL_ROOT_PASSWORD= oceanbase/seekdb:1.4.0
```

### 2.2 起后端

```bash
cd stepfun/app
npm install --omit=optional        # seekdb-js；embedded 模式还需要 optional 依赖
export SEEKDB_HOST=127.0.0.1 SEEKDB_PORT=2881 SEEKDB_USER=root SEEKDB_PASSWORD=""
export SEEKDB_DATABASE=knowledge EMBED_DIMENSION=384
npm start                          # http://127.0.0.1:8080
```

> 也可以完全免服务端：`export SEEKDB_PATH=./seekdb.db`，此时 seekdb 以 embedded 模式跑在服务进程里。

### 2.3 开浏览器

打开 <http://127.0.0.1:8080> → 「加载模型」→「向量化并写入 seekdb」→「检索」。

### 2.4 跑实测

```bash
cd stepfun/app && npm run bench     # 需要能访问 huggingface.co 下载模型
```

## 3 · 模型与量化

| 项 | 值 |
| --- | --- |
| 模型 | `Xenova/all-MiniLM-L6-v2`（Hugging Face 上公开可取的 MiniLM 蒸馏版，MIT） |
| 维度 | 384，L2 距离，单位归一化 |
| 量化方案 | **int8**（`onnx/model_quantized.onnx`，transformers.js `dtype: "q8"`）；对照组 fp32 / fp16 |
| 推理后端 | 浏览器 **WebGPU** 优先；`navigator.gpu.requestAdapter()` 拿不到适配器、或 WebGPU 初始化报错时，自动降级 **WASM**，并在界面顶部显示当前后端 |
| 可选模型 | `Xenova/bge-small-en-v1.5`（同为 384 维） |

模型来源与版本：`Xenova/all-MiniLM-L6-v2` 的 ONNX 权重（`onnx/model_quantized.onnx`、`onnx/model.onnx`、`onnx/model_fp16.onnx`），
经 transformers.js 3.x 直接从 huggingface.co 拉取，首次使用后由浏览器缓存；推理发生在用户设备上，不调用任何云端推理 API。

## 4 · 实测结果

> 设备：WSL2 / 6 核 / 7 GB，Node 22，seekdb 1.4.0（本地 server 模式）。
> 语料：`scripts/corpus.json` 的 12 篇文档 + 12 条带标注查询，Recall@5 / nDCG@5。
> 模型文件大小按 huggingface.co 上的实际文件统计；向量化吞吐用 onnxruntime 跑**同一份 ONNX 文件**
> （浏览器走的是同一份文件、同一套预处理，只是执行后端为 WebGPU/WASM）。

| 精度 | 模型文件 | 体积 | 压缩比 | 向量化吞吐（12 篇） | 单条查询向量化 | seekdb 混合检索 p50 | Recall@5 | nDCG@5 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **int8**（默认） | `onnx/model_quantized.onnx` | 21.9 MiB | **3.93×** | 609 篇/秒（19.7 ms） | 0.7 ms | 3.08 ms | 0.9167 | 0.9167 |
| fp16（对照） | `onnx/model_fp16.onnx` | 43.2 MiB | 2.00× | 302 篇/秒（39.8 ms） | 1.1 ms | 3.24 ms | 0.9167 | 0.9167 |
| fp32（对照） | `onnx/model.onnx` | 86.2 MiB | 1.00× | 300 篇/秒（40.0 ms） | 0.9 ms | 3.99 ms | 0.9167 | 0.9167 |

结论：

1. **int8 相对 fp32 体积降到 1/3.93（21.9 MiB vs 86.2 MiB）**，浏览器首次加载少下载 64 MB。
2. **检索质量没有损失**：三种精度在同一评测集上 Recall@5 与 nDCG@5 完全相同（0.9167），
   量化只影响速度与体积，不影响本语料上的召回与排序。
3. **int8 的向量化吞吐是 fp32 的 2.03 倍**（609 vs 300 篇/秒），端侧设备上这一点最直接。
4. **seekdb 混合检索 p50 = 3.08 ms**（12 篇文档的小集合）；检索耗时与语料规模相关，
   更大集合请用 `scripts/bench.mjs` 重测。

复现方式：

```bash
# 1) 向量化（onnxruntime，浏览器同款模型文件）
python scripts/embed_corpus.py            # 写出 scripts/vectors.json
# 2) 检索质量与延迟（seekdb-js）
node scripts/search_quality.mjs
# 3) 或者走浏览器同款链路（transformers.js + WebGPU/WASM）
cd app && npm run bench
```

## 5 · 检索质量的口径

`scripts/corpus.json` 里 12 篇文档、12 条查询，每条查询标注了相关文档 id。
评测口径：Recall@5 = 前 5 条结果命中的相关文档数 / 相关文档数；nDCG@5 按相关文档在结果中的位置加权。
量化前后用**同一份语料、同一批查询、同一个 seekdb 集合**跑，比较 int8 与 fp32 的召回与排序质量。

## 6 · 增量写入与状态

- `collection.add()` 追加写入，seekdb 的 HNSW 索引随写入更新；再次导入后新文档立即可被检索到。
- 集合名固定为 `documents`（`SEEKDB_COLLECTION` 可改），重启后端时先 `createCollection`，已存在则 `getCollection` 打开，**不丢数据**。
- `/api/state` 返回库内文档数，界面用它显示当前知识库规模。

## 7 · 边界与已知限制

- 端侧推理受设备算力限制：长文档批量向量化在低端机上偏慢；界面会分别显示「浏览器向量化耗时」与「seekdb 检索耗时」，不混在一起。
- 浏览器首次使用需要下载模型文件（约 23 MB int8 / 90 MB fp32），此后走浏览器缓存。
- 全文索引用 `space` 分词器，中文语料建议改用 `ik` 分词器（`new FulltextIndexConfig("ik")`）。
- 混合检索的融合权重由 seekdb 的 RRF 参数（`rankWindowSize` / `rankConstant`）决定，本应用不改动 seekdb 的排序结果。

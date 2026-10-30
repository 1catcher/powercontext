# 论文笔记：Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks

论文：Lewis et al., NeurIPS 2020

## 核心思路
把语言模型的参数化记忆与外部非参数化知识库结合：
1. 检索器（retriever）从知识库取 top-k 相关片段；
2. 生成器（generator）以查询 + 片段为条件生成回答。

## 混合检索要点
- 稠密检索（dense）：查询与文档分别编码为向量，用内积/余弦做相似度。
- 稀疏检索（sparse）：BM25 等词项匹配，对专有名词与精确匹配更可靠。
- 实践中两者互补：稠密解决语义相近词不同，稀疏解决关键词精确命中。
- 向量与全文的融合：分数归一化（min-max 或 RRF）后加权。

## 与课程知识点对应
倒排索引 -> 稀疏检索；向量索引 -> 稠密检索；RRF -> 分数融合。

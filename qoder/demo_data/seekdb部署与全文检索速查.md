# seekdb 部署与全文检索速查

## 部署
- 嵌入式：pip install pylibseekdb，进程内直连，适合本地开发。
- 服务端：deb/rpm 安装后 systemctl start seekdb，默认端口 2881，
  兼容 MySQL 协议，root 用户默认空密码。
- Docker：官方镜像一键启动，适合评审环境。

## 全文索引
```sql
CREATE TABLE docs(id BIGINT PRIMARY KEY, content LONGTEXT);
CREATE FULLTEXT INDEX ft_docs_content ON docs(content);
SELECT * FROM docs WHERE MATCH(content) AGAINST('混合检索');
```
- 中文分词由 IK 分词器处理，支持自定义词典。
- 全文索引不可用时，应用层应降级为 LIKE 查询。

## 混合检索
seekdb 支持向量列（VECTOR）与全文索引共存，可在同一条 SQL 中
用结构化条件过滤 + 全文匹配 + 向量相似度一起检索，减少往返。

## 注意
- LONGTEXT 上限 128MB，建表时列长度不要超过。
- 全文索引名在同一表内不能重复（error 1061）。

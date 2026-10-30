# booklib 规格说明书

> 本文档是 SDD 上下文模块的验证样例输入。修改本文档后重新执行 `ingest`，
> 上下文模块必须返回更新后的内容。

## SPEC-1 图书列表与最小页数过滤

`Catalog.list_books(min_pages: int = 0)` 返回页数大于等于 `min_pages` 的图书：

- 返回顺序与插入顺序一致；
- `min_pages` 缺省为 0，返回全部图书；
- 空目录返回空列表；
- `min_pages` 为负数时视为 0。

适用代码：`booklib/catalog.py` 中的 `Catalog.list_books`。
适用测试：`tests/test_catalog.py` 中的 `test_min_pages`、`test_insertion_order`、
`test_empty_catalog`。

## SPEC-2 关键词过滤（本任务新增）

`Catalog.list_books(min_pages: int = 0, keyword: str | None = None)` 在 SPEC-1
的基础上增加关键词过滤：

- `keyword` 缺省为 `None`，行为与 SPEC-1 完全一致；
- `keyword` 非空时，只保留 **书名或作者** 中包含该关键词的图书；
- 关键词匹配**不区分大小写**；
- 与 `min_pages` 同时生效：两个条件同时满足才保留。

适用代码：`booklib/catalog.py` 中的 `Catalog.list_books`。
适用测试：`tests/test_catalog.py` 中的 `test_filter_by_keyword`。

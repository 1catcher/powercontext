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

"""Functional and regression tests for booklib.

- Target test (fails before the SDD task is implemented): test_filter_by_keyword
- Regression tests (must pass before AND after): all others
"""

from booklib import Book, Catalog


def _sample_catalog() -> Catalog:
    return Catalog(
        [
            Book(title="Clean Code", author="Robert C. Martin", pages=464),
            Book(title="The Art of Computer Programming", author="Donald Knuth", pages=672),
            Book(title="Design Patterns", author="Erich Gamma", pages=395),
            Book(title="Refactoring", author="Martin Fowler", pages=448),
        ]
    )


def test_filter_by_keyword() -> None:
    catalog = _sample_catalog()

    # keyword matches title, case-insensitive
    assert [b.title for b in catalog.list_books(keyword="CLEAN")] == ["Clean Code"]
    # keyword matches author
    assert [b.title for b in catalog.list_books(keyword="knuth")] == [
        "The Art of Computer Programming"
    ]
    # keyword combined with min_pages (author "knuth" matches only TAOCP)
    assert [b.title for b in catalog.list_books(min_pages=400, keyword="knuth")] == [
        "The Art of Computer Programming"
    ]
    # no match -> empty result, not an error
    assert catalog.list_books(keyword="zzz-not-a-book") == []


def test_min_pages() -> None:
    catalog = _sample_catalog()
    assert len(catalog.list_books(min_pages=450)) == 2
    assert len(catalog.list_books(min_pages=1000)) == 0


def test_insertion_order() -> None:
    catalog = _sample_catalog()
    assert [b.title for b in catalog.list_books()] == [
        "Clean Code",
        "The Art of Computer Programming",
        "Design Patterns",
        "Refactoring",
    ]


def test_empty_catalog() -> None:
    assert Catalog().list_books() == []

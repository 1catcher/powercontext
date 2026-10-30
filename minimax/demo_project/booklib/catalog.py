"""Book catalog with min-pages filtering.

This module is the target of the SDD demo task: add a keyword filter to
``Catalog.list_books``.  All pre-existing behavior documented below must be
preserved by the change (see specs/catalog-spec.md SPEC-1).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Book:
    title: str
    author: str
    pages: int


class Catalog:
    """An in-memory book catalog."""

    def __init__(self, books: list[Book] | None = None):
        self._books: list[Book] = list(books or [])

    def add(self, book: Book) -> None:
        self._books.append(book)

    def list_books(self, min_pages: int = 0, keyword: str | None = None) -> list[Book]:
        """Return books with at least ``min_pages`` pages, in insertion order.

        When ``keyword`` is given, additionally keep only books whose title or
        author contains the keyword (case-insensitive).
        """
        matched = []
        for book in self._books:
            if book.pages < min_pages:
                continue
            if keyword is not None:
                needle = keyword.lower()
                if needle not in book.title.lower() and needle not in book.author.lower():
                    continue
            matched.append(book)
        return matched

from dataclasses import dataclass


@dataclass
class Book:
    title: str
    author: str
    pages: int


class Catalog:
    def __init__(self, books: list[Book] | None = None):
        self._books = list(books) if books is not None else []

    def add(self, book: Book) -> None:
        self._books.append(book)

    def list_books(
        self, min_pages: int = 0, keyword: str | None = None
    ) -> list[Book]:
        books = [book for book in self._books if book.pages >= min_pages]

        if keyword is not None:
            normalized_keyword = keyword.casefold()
            books = [
                book
                for book in books
                if normalized_keyword in book.title.casefold()
                or normalized_keyword in book.author.casefold()
            ]

        return books

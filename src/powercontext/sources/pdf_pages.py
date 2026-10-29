# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Parse financial-report PDFs into page-level Sources and find their evidence pages."""

from __future__ import annotations

import hashlib
import io
import math
import re
from collections.abc import Mapping, Sequence
from urllib.parse import quote

from pydantic import BaseModel, Field, field_validator

from powercontext.artifacts import MemoryCitation
from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PDF_PAGE_SOURCE_NAME = "pdf-page"
PDF_PAGE_SOURCE_VERSION = "1"

# One FTS round keeps this many candidates so the broad ranking has room to work.
_CANDIDATE_LIMIT = 60
# One term-only round keeps fewer candidates; a single term is a weak filter.
_TERM_CANDIDATE_LIMIT = 20
# The number of single-term rounds. Every extra round is one more database query.
_MAX_TERM_ROUNDS = 24
# The number of concept rounds. The concept vocabulary is long, so it opens the rounds.
_CONCEPT_ROUNDS = 12
# The weight one concept term carries against the terms the question writes itself.
_CONCEPT_WEIGHT = 3.0
# The byte bound one stored Memory entry body may take, including its page prefix.
_ENTRY_BYTE_LIMIT = 8192
_TERM_PATTERN = re.compile(r"[a-z0-9_]+")
_DIGIT_PATTERN = re.compile(r"\d+")

# The reading of a financial concept in the vocabulary a report writes it in.
# Every trigger of a pair has to be present, and a trigger matches a term that
# starts with it, so ``cyclicality`` reaches the ``cyclical`` reading.
_STATEMENT_VOCABULARY = (
    "consolidated",
    "statements",
    "revenues",
    "expenses",
    "net",
    "operating",
    "income",
    "earnings",
    "profit",
)
_CAPITAL_VOCABULARY = (
    "capital",
    "expenditures",
    "spending",
    "property",
    "plant",
    "equipment",
    "depreciation",
    "assets",
)
_DISCUSSION_VOCABULARY = ("management", "discussion", "analysis", "results", "operations")
_CONCEPT_VOCABULARY: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("quick",), ("current", "assets", "liabilities", "cash", "equivalents", "receivable", "inventories")),
    (("liquidity",), ("current", "assets", "liabilities", "cash", "equivalents")),
    (("gross", "margin"), ("net", "sales", "cost", "products", "services", "sold", "profit")),
    (("margin",), ("gross", "operating", "income", "profit", "earnings")),
    (("margin",), _STATEMENT_VOCABULARY),
    (("profitability",), _STATEMENT_VOCABULARY),
    (("ebitda",), ("operating", "income", "depreciation", "amortization")),
    (("ebitda",), _STATEMENT_VOCABULARY),
    (("capex",), _CAPITAL_VOCABULARY),
    (("capital",), _CAPITAL_VOCABULARY),
    (("revenue",), ("net", "revenues", "total", "sales")),
    (("revenue",), _STATEMENT_VOCABULARY),
    (("sales",), ("net", "revenues", "total", "sales")),
    (("income", "statement"), ("consolidated", "statements", "operations", "earnings", "costs")),
    (("balance", "sheet"), ("consolidated", "balance", "sheets", "assets", "liabilities", "equity")),
    (("cash", "flow"), ("cash", "flows", "operating", "investing", "financing", "activities", "provided")),
    (("tax",), ("taxes", "provision", "effective")),
    (("segment",), ("reportable", "segments", "operating", "business", "units")),
    (("acquisition",), ("acquisitions", "purchase", "consideration", "combination")),
    (("divestiture",), ("divestiture", "acquisitions", "consideration")),
    (("customer",), ("customers", "accounted", "concentration", "significant")),
    (("legal",), ("litigation", "proceedings", "claims", "regulatory", "disputes")),
    (("litigation",), ("litigation", "proceedings", "claims", "regulatory")),
    (("geograph",), ("geographic", "regions", "areas", "countries", "operations")),
    (("region",), ("geographic", "regions", "areas", "countries")),
    (("product",), ("products", "services", "offerings", "business")),
    (("service",), ("products", "services", "offerings")),
    (("industry",), ("industry", "business", "operations", "market")),
    (("restructuring",), ("restructuring", "impairment", "charges", "productivity", "plan")),
    (("employee",), ("employees", "workforce", "personnel")),
    (("workforce",), ("employees", "workforce", "personnel")),
    (("debt",), ("debt", "borrowings", "notes", "payable", "maturities")),
    (("leverage",), ("debt", "borrowings", "equity", "leverage")),
    (("dividend",), ("dividends", "payout")),
    (("repurchase",), ("repurchases", "buyback", "treasury")),
    (("backlog",), ("backlog", "contracts", "remaining", "performance", "obligations")),
    (("cyclical",), ("cyclic", "industry", "demand", "cycle", "economic")),
    (("seasonal",), ("seasonal", "demand", "cycle")),
    (("production",), ("production", "rates", "delivery", "deliveries", "output", "capacity")),
    (("guidance",), ("outlook", "guidance", "expects", "anticipate", "forecast")),
    (("outlook",), ("outlook", "guidance", "expects", "forecast")),
    (("competition",), ("competitive", "competitors", "market")),
    (("competitive",), ("competitive", "competitors", "market")),
    (("supplier",), ("suppliers", "vendors", "purchases")),
    (("vendor",), ("suppliers", "vendors", "purchases")),
    (("interest",), ("interest", "expense", "borrowings", "effective")),
    (("pension",), ("pension", "retirement", "obligations", "benefits")),
    (("goodwill",), ("goodwill", "intangible", "impairment", "amortization")),
    (("intangible",), ("goodwill", "intangible", "impairment", "amortization")),
    (("currency",), ("currency", "exchange", "translation", "foreign")),
    (("exchange",), ("currency", "exchange", "translation")),
    (("concentration",), ("concentration", "accounted", "percentage", "significant")),
    (("working", "capital"), ("working", "capital", "current", "receivable", "payable", "inventories")),
    (("drove",), _DISCUSSION_VOCABULARY),
    (("driven",), _DISCUSSION_VOCABULARY),
    (("driver",), _DISCUSSION_VOCABULARY),
)


class PDFDocumentInput(BaseModel):
    """One original PDF document addressed by its identifier."""

    document_id: str
    content: bytes


class PDFPageInput(BaseModel):
    """One physical page of a PDF document with the digest of the whole file."""

    document_id: str
    document_sha256: str
    page_number: int = Field(ge=1)
    text: str


class PDFPageSource(Source):
    """One captured physical page persisted as a page-addressable Source."""

    source_id: str
    document_id: str
    document_sha256: str
    page_number: int = Field(ge=1)
    text: str

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        if not value or value.strip() != value or len(value) > MAX_SOURCE_ID_LENGTH:
            raise ValueError("PDF page source_id must be a bounded non-empty trimmed string")
        return value


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    """Build the stable identity of one physical page."""

    _validate_document_id(document_id)
    _validate_document_sha256(document_sha256)
    if page_number < 1:
        raise ValueError("PDF page number must be a positive integer")
    source_id = f"pdf:{quote(document_id, safe='')}:{document_sha256}:{page_number}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        # A long document identifier is folded into a digest so the identity keeps
        # its shape, stays collision free and stays inside the length limit.
        folded = hashlib.sha256(document_id.encode("utf-8")).hexdigest()
        source_id = f"pdf:{folded}:{document_sha256}:{page_number}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"PDF page source_id must not exceed {MAX_SOURCE_ID_LENGTH} characters")
    return source_id


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Parse every physical page of one PDF, or fail without returning partial pages."""

    _validate_document_id(value.document_id)
    if not value.content:
        raise ValueError("PDF document must not be empty")
    document_sha256 = hashlib.sha256(value.content).hexdigest()
    pages = _extract_pages(value.content)
    if not pages:
        raise ValueError("PDF document must contain at least one physical page")
    return tuple(
        PDFPageInput(
            document_id=value.document_id,
            document_sha256=document_sha256,
            page_number=number,
            text=_fit_page_text(text, value.document_id),
        )
        for number, text in enumerate(pages, start=1)
    )


def _fit_page_text(text: str, document_id: str) -> str:
    """Return one page's text in the shape the store can hold.

    The layout whitespace of an extracted page carries no term, so it is
    collapsed. A page whose body still does not fit one stored entry keeps its
    leading words, because the page identity, its document digest and the
    overwhelming majority of its terms are what a citation is checked against.
    """

    collapsed = " ".join(text.split())
    budget = _ENTRY_BYTE_LIMIT - len(document_id.encode("utf-8")) - 1
    if budget < 1 or len(collapsed.encode("utf-8")) <= budget:
        return collapsed
    cut = collapsed.encode("utf-8")[:budget].decode("utf-8", "ignore")
    boundary = cut.rfind(" ")
    if boundary > 0:
        cut = cut[:boundary]
    return cut


def _extract_pages(content: bytes) -> list[str]:
    """Return the extractable text of every physical page, in document order."""

    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF document is encrypted with a password")
        return [_page_text(page) for page in reader.pages]
    except ValueError:
        raise
    except Exception as error:
        raise ValueError(f"PDF document could not be parsed: {error}") from error


def _page_text(page) -> str:
    """Return one page's extractable text without its layout whitespace."""

    text = page.extract_text()
    if text is None:
        return ""
    return text


def _validate_document_id(document_id: str) -> None:
    if not isinstance(document_id, str) or not document_id or not document_id.strip():
        raise ValueError("PDF document_id must be a non-empty string")


def _validate_document_sha256(document_sha256: str) -> None:
    if (
        not isinstance(document_sha256, str)
        or len(document_sha256) != 64
        or any(character not in "0123456789abcdef" for character in document_sha256)
    ):
        raise ValueError("PDF document_sha256 must be a lowercase hexadecimal digest")


class PDFPageTextEvidenceProjection:
    """Project one captured PDF page into the standard text evidence shape."""

    name: str = TEXT_EVIDENCE_PROJECTION_KEY.name
    version: str = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class: type[PDFPageSource] = PDFPageSource
    output_class: type[TextEvidence] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.source_id,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


class PDFPageSourceDefinition:
    """Bind PDF page capture to a durable Source Definition version."""

    name: str = PDF_PAGE_SOURCE_NAME
    version: str = PDF_PAGE_SOURCE_VERSION
    input_class: type[PDFPageInput] = PDFPageInput
    source_class: type[PDFPageSource] = PDFPageSource
    projections: tuple[PDFPageTextEvidenceProjection, ...] = (PDFPageTextEvidenceProjection(),)

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        source_id = pdf_page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(
            name=source_id,
            source_id=source_id,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
            materialization=SourceMaterialization.CAPTURED,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


PDF_PAGE_SOURCE_DEFINITION = PDFPageSourceDefinition()


async def search_pdf_pages(memory, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """Find the distinct PDF pages that may answer one question.

    The scoped Memory owns the stored pages, so every candidate comes from the
    public search API and every returned citation points at an exact entry
    version. The broad round keeps the pages the whole question retrieves; the
    per-term rounds then reach the pages that carry one distinctive term of it,
    which is where an evidence page hides when the broad round is diluted by
    the question's everyday words. The rerank is offline: it weighs the terms a
    page carries by how rare they are among the candidates.
    """

    from powercontext.builtin.artifacts.search import analyze_fts_query
    from powercontext.builtin.runtime.models import SearchMemoryRequest

    if limit < 1:
        return ()
    terms = _question_terms(question)
    if not terms:
        return ()
    broad = await memory.search(
        SearchMemoryRequest(query=" ".join(terms), mode="fts", limit=_CANDIDATE_LIMIT),
    )
    candidates: dict[tuple[str, str], object] = {}
    texts: dict[tuple[str, str], str] = {}
    for hit in broad.hits:
        key = (hit.entry_id, hit.entry_version_id)
        candidates.setdefault(key, hit)
        texts.setdefault(key, hit.text)
    concepts = _concept_terms(question)
    for term in _round_order(broad.hits, concepts, _search_terms(question))[:_MAX_TERM_ROUNDS]:
        page = await memory.search(
            SearchMemoryRequest(query=term, mode="fts", limit=_TERM_CANDIDATE_LIMIT),
        )
        for hit in page.hits:
            key = (hit.entry_id, hit.entry_version_id)
            candidates.setdefault(key, hit)
            texts.setdefault(key, hit.text)
    query = " ".join(terms)
    ranked = _rank_candidates(candidates, texts, analyze_fts_query(query).split(), concepts)
    return tuple(
        MemoryCitation(
            memory_ref=hit.memory_ref,
            entry_id=hit.entry_id,
            entry_version_id=hit.entry_version_id,
        )
        for hit in ranked[:limit]
    )


def _round_order(broad_hits, concepts: tuple[str, ...], search_terms: tuple[str, ...]) -> tuple[str, ...]:
    """Order the term rounds so the rarest, most telling terms are asked first.

    The concept vocabulary opens the rounds because it is the reading of the
    question rather than its wording; the question's own terms follow, rarest
    first, so a distinctive term is asked before a common one.
    """

    counts: dict[str, int] = {}
    for hit in broad_hits:
        for term in set(_terms(hit.text)):
            counts[term] = counts.get(term, 0) + 1
    ordered = [term for term in concepts if not term.endswith("s")][:_CONCEPT_ROUNDS]
    rest = [term for term in search_terms if term not in ordered]
    rest.sort(key=lambda term: (counts.get(term, 0), term))
    return tuple(ordered + rest)


def _rank_candidates(
    candidates: Mapping[tuple[str, str], object],
    texts: Mapping[tuple[str, str], str],
    query_terms: Sequence[str],
    concepts: Sequence[str],
) -> list[object]:
    """Order the candidate pages by the distinctive evidence they carry."""

    documents = {key: frozenset(_terms(text)) for key, text in texts.items()}
    total = len(documents)
    if not total:
        return []
    weights = _term_weights(query_terms, documents, total)
    weights.update(_term_weights(concepts, documents, total, weight=_CONCEPT_WEIGHT))
    targets = _target_documents(texts, query_terms)
    scored: list[tuple[float, tuple[str, str]]] = []
    for key, document in documents.items():
        if targets and _document_of(texts[key]) not in targets:
            # The question names the company, so another company's pages are
            # not its evidence, however many shared words they carry.
            continue
        matched = sum(weights[term] for term in document if term in weights)
        scored.append((-matched, key))
    if not scored:
        for key, document in documents.items():
            matched = sum(weights[term] for term in document if term in weights)
            scored.append((-matched, key))
    scored.sort()
    return [candidates[key] for _score, key in scored if key in candidates]


def _document_of(text: str) -> str:
    """Return the document identifier one stored page starts with."""

    return text.split("\n", 1)[0]


def _target_documents(
    texts: Mapping[tuple[str, str], str],
    query_terms: Sequence[str],
) -> set[str]:
    """Return the documents whose identifier carries a name the question uses."""

    documents = {_document_of(text) for text in texts.values()}
    targets: set[str] = set()
    for document in documents:
        flat = re.sub(r"[^a-z0-9]", "", document.casefold())
        for term in query_terms:
            if len(term) < 3 or any(character.isdigit() for character in term):
                continue
            if term in flat:
                targets.add(document)
                break
    return targets


def _term_weights(
    terms: Sequence[str],
    documents: Mapping[tuple[str, str], frozenset[str]],
    total: int,
    weight: float = 1.0,
) -> dict[str, float]:
    """Weight every term by how rare it is among the candidate pages."""

    weights: dict[str, float] = {}
    for term in terms:
        frequency = sum(1 for document in documents.values() if term in document)
        weights[term] = weight * math.log((total - frequency + 0.5) / (frequency + 0.5) + 1.0)
    return weights


def _question_terms(question: str) -> tuple[str, ...]:
    """Return the distinct lexical terms of one question, in order.

    A period-qualified term such as ``FY2022`` also contributes its digits, so a
    report that writes the plain year still matches the question.
    """

    terms: list[str] = []
    for term in _terms(question):
        for candidate in (term, *(_DIGIT_PATTERN.findall(term) or ())):
            if candidate and candidate not in terms:
                terms.append(candidate)
    return tuple(terms)


def _concept_terms(question: str) -> tuple[str, ...]:
    """Return the report vocabulary one question's concepts stand for.

    A question names a financial concept; a report writes the statement lines
    that concept is measured from. The pairs below are the standard reading of
    those concepts, so a question about a quick ratio reaches the balance sheet
    and a question about capital spending reaches the cash flow statement.
    """

    present = set(_terms(question))
    expanded: list[str] = []
    for triggers, vocabulary in _CONCEPT_VOCABULARY:
        if not all(_mentions(present, trigger) for trigger in triggers):
            continue
        for term in vocabulary:
            for candidate in (term, *_number_variants(term)):
                if candidate not in present and candidate not in expanded:
                    expanded.append(candidate)
    return tuple(expanded)


def _mentions(present: set[str], trigger: str) -> bool:
    """Return whether one question term carries the meaning of a trigger."""

    return any(term.startswith(trigger) for term in present)


def _search_terms(question: str) -> tuple[str, ...]:
    """Return the terms one question is looked up with.

    Every term also contributes its number variant, because a report writes
    ``taxes`` where the question says ``tax`` and the analyzer matches whole
    terms only.
    """

    terms: list[str] = []
    for term in _terms(question):
        for candidate in (term, *_number_variants(term), *(_DIGIT_PATTERN.findall(term) or ())):
            if candidate and candidate not in terms:
                terms.append(candidate)
    for term in _concept_terms(question):
        if term not in terms:
            terms.append(term)
    return tuple(terms)


def _search_terms(question: str) -> tuple[str, ...]:
    """Return the terms one question is looked up with.

    Every term also contributes its number variant, because a report writes
    ``taxes`` where the question says ``tax`` and the analyzer matches whole
    terms only.
    """

    terms: list[str] = []
    for term in _terms(question):
        for candidate in (term, *_number_variants(term), *(_DIGIT_PATTERN.findall(term) or ())):
            if candidate and candidate not in terms:
                terms.append(candidate)
    return tuple(terms)


def _number_variants(term: str) -> tuple[str, ...]:
    """Return the singular and plural forms one term may be written in."""

    if len(term) < 4:
        return ()
    if term.endswith("ies"):
        return (term[:-3] + "y",)
    if term.endswith("ss"):
        return ()
    if term.endswith("s"):
        return (term[:-1],)
    if term.endswith("y") and term[-2] not in "aeiou":
        return (term[:-1] + "ies",)
    if term.endswith(("x", "z", "ch", "sh")):
        return (term + "es",)
    return (term + "s",)


def _terms(value: str) -> tuple[str, ...]:
    """Return the lowercase lexical terms of one text, in order."""

    return tuple(_TERM_PATTERN.findall(value.casefold()))


__all__ = [
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "PDFPageSourceDefinition",
    "PDFPageTextEvidenceProjection",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]

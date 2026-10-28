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

"""Capture offline CSV documents as row-level Sources with stable identity."""

from __future__ import annotations

import codecs
import csv
import io
import json
from urllib.parse import quote

from pydantic import BaseModel, Field, field_validator, model_validator

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

CSV_ROW_SOURCE_NAME = "csv-row"
CSV_ROW_SOURCE_VERSION = "1"


class CSVDocumentInput(BaseModel):
    """One offline CSV document addressed by its identifier and primary key."""

    document_id: str
    primary_key: str
    content: bytes


class CSVRowInput(BaseModel):
    """One captured CSV row, keeping the original column order and values."""

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    @model_validator(mode="after")
    def validate_row_alignment(self) -> CSVRowInput:
        if len(self.columns) != len(self.values):
            raise ValueError("CSV row columns and values must have the same length")
        return self


class CSVRowSource(Source):
    """A captured CSV row persisted as one row-addressable Source."""

    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        if not value or value.strip() != value or len(value) > MAX_SOURCE_ID_LENGTH:
            raise ValueError("CSV row source_id must be a bounded non-empty trimmed string")
        return value


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Build the stable identity of one CSV row."""

    _validate_document_id(document_id)
    _validate_primary_key_value(primary_key_value)
    source_id = f"csv:{quote(document_id, safe='')}:{quote(primary_key_value, safe='')}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"CSV row source_id must not exceed {MAX_SOURCE_ID_LENGTH} characters")
    return source_id


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    """Parse one CSV document into validated row inputs, or fail atomically."""

    _validate_document_id(value.document_id)
    _validate_primary_key(value.primary_key)
    records = _read_records(_decode_document(value.content))
    if not records:
        raise ValueError("CSV document must contain a header record")
    columns = _validate_header(records[0])
    if value.primary_key not in columns:
        raise ValueError(f"CSV primary key {value.primary_key!r} is not a header column")
    key_index = columns.index(value.primary_key)
    rows: list[CSVRowInput] = []
    seen: set[str] = set()
    for record in records[1:]:
        if not record:
            # A completely empty physical record carries no columns at all.
            continue
        if len(record) != len(columns):
            raise ValueError("CSV data record must have exactly the header column count")
        primary_key_value = record[key_index]
        if not primary_key_value.strip():
            raise ValueError("CSV primary key value must not be empty or whitespace only")
        if primary_key_value in seen:
            raise ValueError(f"CSV primary key value {primary_key_value!r} is not unique")
        seen.add(primary_key_value)
        rows.append(
            CSVRowInput(
                document_id=value.document_id,
                primary_key=value.primary_key,
                primary_key_value=primary_key_value,
                columns=columns,
                values=tuple(record),
            )
        )
    return tuple(rows)


def _validate_document_id(document_id: str) -> None:
    if not document_id or not document_id.strip():
        raise ValueError("CSV document_id must be a non-empty string")
    if document_id != document_id.strip():
        raise ValueError("CSV document_id must not contain leading or trailing whitespace")


def _validate_primary_key(primary_key: str) -> None:
    if not primary_key or not primary_key.strip():
        raise ValueError("CSV primary_key must be a non-empty string")


def _validate_primary_key_value(primary_key_value: str) -> None:
    if not primary_key_value or not primary_key_value.strip():
        raise ValueError("CSV primary key value must be a non-empty string")


def _validate_header(header: list[str]) -> tuple[str, ...]:
    if not header:
        raise ValueError("CSV header record must not be empty")
    seen: set[str] = set()
    for column in header:
        if not column or not column.strip():
            raise ValueError("CSV header column must be a non-empty non-whitespace string")
        if column in seen:
            raise ValueError(f"CSV header column {column!r} is duplicated")
        seen.add(column)
    return tuple(header)


def _decode_document(content: bytes) -> str:
    if content.startswith(codecs.BOM_UTF8):
        content = content[len(codecs.BOM_UTF8) :]
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("CSV document must be valid UTF-8") from error


def _read_records(text: str) -> list[list[str]]:
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        return list(reader)
    except csv.Error as error:
        raise ValueError(f"CSV document is malformed: {error}") from error


class CSVRowTextEvidenceProjection:
    """Project one captured CSV row into the standard text evidence shape."""

    name: str = TEXT_EVIDENCE_PROJECTION_KEY.name
    version: str = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class: type[CSVRowSource] = CSVRowSource
    output_class: type[TextEvidence] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row = dict(zip(source.columns, source.values, strict=True))
        return TextEvidence(
            source_type=CSV_ROW_SOURCE_NAME,
            source_id=source.source_id,
            content=json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row,
            },
        )


class CSVRowSourceDefinition:
    """Bind CSV row capture to a durable Source Definition version."""

    name: str = CSV_ROW_SOURCE_NAME
    version: str = CSV_ROW_SOURCE_VERSION
    input_class: type[CSVRowInput] = CSVRowInput
    source_class: type[CSVRowSource] = CSVRowSource
    projections: tuple[CSVRowTextEvidenceProjection, ...] = (CSVRowTextEvidenceProjection(),)

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        source_id = csv_row_source_id(value.document_id, value.primary_key_value)
        return CSVRowSource(
            name=source_id,
            source_id=source_id,
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=value.columns,
            values=value.values,
            materialization=SourceMaterialization.CAPTURED,
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


CSV_ROW_SOURCE_DEFINITION = CSVRowSourceDefinition()


__all__ = [
    "CSV_ROW_SOURCE_DEFINITION",
    "CSVDocumentInput",
    "CSVRowInput",
    "CSVRowSource",
    "CSVRowSourceDefinition",
    "CSVRowTextEvidenceProjection",
    "csv_row_source_id",
    "parse_csv_document",
]

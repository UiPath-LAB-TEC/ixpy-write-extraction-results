from __future__ import annotations

import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from uipath.platform import UiPath
from uipath.platform.entities.entities import Entity, FieldMetadata
from uipath.platform.documents.documents import ValidateExtractionAction

DEFAULT_BATCH_SIZE = 100
DEFAULT_PAGE_SIZE = 200
DEFAULT_UPSERT = True
DEFAULT_LOG_LEVEL = "INFO"
LOG_LEVEL_ENV_VAR = "IXPY_LOG_LEVEL"
RUN_ID_ENV_VAR = "IXPY_RUN_ID"


def _setup_logger() -> logging.LoggerAdapter:
    logger_name = "ixpy.write_results"
    base_logger = logging.getLogger(logger_name)
    if not base_logger.handlers:
        handler = logging.StreamHandler(stream=sys.stdout)
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s run_id=%(run_id)s %(message)s"
        )
        handler.setFormatter(formatter)
        base_logger.addHandler(handler)
        base_logger.propagate = False
    level_name = os.getenv(LOG_LEVEL_ENV_VAR, DEFAULT_LOG_LEVEL).upper()
    base_logger.setLevel(getattr(logging, level_name, logging.INFO))
    run_id = os.getenv(RUN_ID_ENV_VAR) or f"{int(time.time())}-{os.getpid()}"
    return logging.LoggerAdapter(base_logger, {"run_id": run_id})


LOGGER = _setup_logger()


def _json_fields(**fields: Any) -> str:
    compact = {key: value for key, value in fields.items() if value is not None}
    return json.dumps(compact, default=str, separators=(",", ":"), sort_keys=True)


def log_event(level: int, event: str, **fields: Any) -> None:
    LOGGER.log(level, "%s %s", event, _json_fields(**fields))


def log_exception(event: str, **fields: Any) -> None:
    LOGGER.exception("%s %s", event, _json_fields(**fields))


@contextmanager
def log_duration(event: str, **fields: Any) -> Iterator[None]:
    start = time.perf_counter()
    log_event(logging.INFO, f"{event}.start", **fields)
    try:
        yield
    except Exception:
        elapsed = round(time.perf_counter() - start, 3)
        log_exception(f"{event}.failed", elapsed_s=elapsed, **fields)
        raise
    elapsed = round(time.perf_counter() - start, 3)
    log_event(logging.INFO, f"{event}.done", elapsed_s=elapsed, **fields)


log_event(
    logging.INFO,
    "module.loaded",
    python=sys.version.split()[0],
    pid=os.getpid(),
)


class Input(BaseModel):
    extraction_results: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Extraction results returned by UiPath Documents.",
    )
    validate_extraction: bool = Field(
        default=False,
        description="When true, fetch validated extraction results from UiPath using validation_action_data.",
    )
    validation_action_data: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Validate Extraction action payload used to fetch validated extraction results from UiPath.",
    )
    batch_record_id: Optional[str] = Field(
        default=None,
        description="Data Fabric record Id for the related batch entity. Use this when batchId is a business key rather than the related record Id.",
    )
    filename: Optional[str] = Field(
        default=None,
        description="Original document filename for Data Service records.",
    )
    batch_id: Optional[str] = Field(
        default=None,
        alias="batchId",
        description="Batch identifier (Data Service relationship field).",
    )
    batch_size: int = Field(
        default=DEFAULT_BATCH_SIZE,
        description="Batch size for Data Service insert/update requests.",
    )
    page_size: int = Field(
        default=DEFAULT_PAGE_SIZE,
        description="Page size for Data Service list_records when upsert is enabled.",
    )
    upsert: bool = Field(
        default=DEFAULT_UPSERT,
        description="When true, update existing rows that match filename/field keys.",
    )

    @field_validator("validation_action_data", mode="before")
    @classmethod
    def normalize_optional_dict_payloads(cls, value: Any) -> Any:
        if value == "":
            return None
        return value

    @field_validator("batch_size", "page_size", mode="before")
    @classmethod
    def default_int_fields(cls, value: Any, info) -> Any:
        if value is None:
            return DEFAULT_BATCH_SIZE if info.field_name == "batch_size" else DEFAULT_PAGE_SIZE
        return value

    @field_validator("upsert", mode="before")
    @classmethod
    def default_upsert_field(cls, value: Any) -> Any:
        if value is None:
            return DEFAULT_UPSERT
        return value

    @field_validator("validate_extraction", mode="before")
    @classmethod
    def default_validate_extraction_field(cls, value: Any) -> Any:
        if value is None:
            return False
        return value

    @model_validator(mode="after")
    def validate_inputs(self) -> "Input":
        log_event(
            logging.INFO,
            "input.validate",
            has_extraction=bool(self.extraction_results),
            validate_extraction=self.validate_extraction,
            has_validation_action_data=bool(self.validation_action_data),
            has_batch_record_id=bool(self.batch_record_id),
            has_batch_id=bool(self.batch_id),
            batch_size=self.batch_size,
            page_size=self.page_size,
            upsert=self.upsert,
        )
        if not os.getenv("UIPATH_ENTITY_KEY"):
            log_event(
                logging.ERROR,
                "input.validate.failed",
                reason="missing_uipath_entity_key",
            )
            raise ValueError("UIPATH_ENTITY_KEY environment variable is required.")
        if self.validate_extraction and not self.validation_action_data:
            log_event(
                logging.ERROR,
                "input.validate.failed",
                reason="missing_validation_action_data",
            )
            raise ValueError(
                "validation_action_data is required when validate_extraction is true."
            )
        if (
            not self.extraction_results
            and not self.validation_action_data
        ):
            log_event(
                logging.ERROR,
                "input.validate.failed",
                reason="missing_extraction_and_action_data",
            )
            raise ValueError(
                "extraction_results or validation_action_data is required."
            )
        log_event(logging.INFO, "input.validate.passed")
        return self


class Output(BaseModel):
    entity_key: str = Field(
        description="UiPath Data Service entity key used for writes."
    )
    inserted: int
    updated: int
    insert_failures: int
    update_failures: int
    skipped_validation_updates: int
    errors: List[str]


class ExtractionRecord(BaseModel):
    filename: str
    documentId: Optional[str] = None
    documentTypeId: Optional[str] = None
    batchId: Optional[Any] = None
    operationId: Optional[str] = None
    fieldId: Optional[str] = None
    field: Optional[str] = None
    isMissing: Optional[bool] = None
    fieldValue: Optional[str] = None
    confidence: Optional[float] = None
    ocrConfidence: Optional[float] = None
    operatorConfirmed: Optional[bool] = None
    isCorrect: Optional[bool] = None
    pageRange: Optional[str] = None
    pageCount: Optional[int] = None
    rowIndex: Optional[int] = None
    columnIndex: Optional[int] = None
    validatedFieldValue: Optional[str] = None


class RecordUpdate(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str = Field(alias="Id")
    batchId: Optional[Any] = None
    operationId: Optional[str] = None
    validatedFieldValue: Optional[str] = None
    operatorConfirmed: Optional[bool] = None
    isCorrect: Optional[bool] = None


class RecordIndex:
    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.by_full: Dict[Tuple[Any, ...], str] = {}
        self.by_field_id: Dict[Tuple[Any, ...], str] = {}
        self.by_rowcol: Dict[Tuple[Any, ...], str] = {}

    def add(self, record: Any) -> None:
        if getattr(record, "filename", None) != self.filename:
            return
        field_id = getattr(record, "fieldId", None)
        field_name = getattr(record, "field", None)
        row_index = getattr(record, "rowIndex", None)
        column_index = getattr(record, "columnIndex", None)
        full_key = (self.filename, field_id, field_name, row_index, column_index)
        self.by_full[full_key] = record.id
        if row_index is None and column_index is None:
            self.by_field_id[(self.filename, field_id)] = record.id
        else:
            self.by_rowcol[(self.filename, field_id, row_index, column_index)] = record.id

    def find(
        self,
        field_id: Optional[str],
        field_name: Optional[str],
        row_index: Optional[int],
        column_index: Optional[int],
    ) -> Optional[str]:
        full_key = (self.filename, field_id, field_name, row_index, column_index)
        record_id = self.by_full.get(full_key)
        if record_id:
            return record_id
        if row_index is None and column_index is None:
            return self.by_field_id.get((self.filename, field_id))
        return self.by_rowcol.get((self.filename, field_id, row_index, column_index))


class ExtractionResultsWriter:
    def __init__(
        self,
        uipath: UiPath,
        entity_key: str,
        document_path: str,
        extraction_results: Optional[Dict[str, Any]] = None,
        validation_results: Optional[Dict[str, Any]] = None,
        batch_id: Optional[str] = None,
        batch_record_id: Optional[str] = None,
        document_id: Optional[str] = None,
        operation_id: Optional[str] = None,
        batch_size: int = 100,
        page_size: int = 200,
        upsert: bool = True,
    ) -> None:
        self.entities = uipath.entities
        self.api_client = uipath.api_client
        self.entity_key = entity_key
        self.extraction_results = extraction_results
        self.validation_results = validation_results
        self.batch_size = max(1, batch_size)
        self.page_size = max(1, page_size)
        self.upsert = upsert
        self.filename = os.path.basename(document_path)
        self.batch_id = batch_id
        self.batch_record_id = batch_record_id
        self.document_id = document_id
        self.operation_id = operation_id
        self._entity_metadata: Optional[Entity] = None
        self._resolved_batch_record_id: Optional[str] = None
        log_event(
            logging.INFO,
            "writer.initialized",
            entity_key=self.entity_key,
            filename=self.filename,
            batch_id=self.batch_id,
            batch_record_id=self.batch_record_id,
            document_id=self.document_id,
            has_extraction=bool(self.extraction_results),
            has_validation=bool(self.validation_results),
            upsert=self.upsert,
            batch_size=self.batch_size,
            page_size=self.page_size,
        )

    def create_headers_lookup_dict(self, table_data: List[Dict[str, Any]]) -> Dict[str, Dict[int, str]]:
        header_dict: Dict[str, Dict[int, str]] = {}
        for field in table_data:
            field_id = field["FieldId"]
            field_headers: Dict[int, str] = {}
            for value in field["Values"]:
                for cell in value["Cells"]:
                    if cell["RowIndex"] == 0 and cell["IsHeader"]:
                        column_index = cell["ColumnIndex"]
                        header_value = cell["Values"][0]["Value"]
                        field_headers[column_index] = header_value
            header_dict[field_id] = field_headers
        return header_dict

    def _chunk(self, items: List[Any]) -> Iterable[List[Any]]:
        for start in range(0, len(items), self.batch_size):
            yield items[start : start + self.batch_size]

    def _relationship_reference(self, record_id: Optional[str]) -> Optional[Dict[str, str]]:
        if not record_id:
            return None
        return {"Id": record_id}

    def _get_entity_metadata(self) -> Entity:
        if self._entity_metadata is None:
            self._entity_metadata = self.entities.retrieve(self.entity_key)
        entity = self._entity_metadata
        if entity is None:
            raise ValueError(f"Unable to load entity metadata for {self.entity_key}.")
        return entity

    def _get_field_metadata(self, field_name: str) -> Optional[FieldMetadata]:
        entity = self._get_entity_metadata()
        for field in entity.fields or []:
            if field.name == field_name:
                return field
        return None

    def _query_entity(
        self,
        entity_key: str,
        query_filters: List[Dict[str, Any]],
        selected_fields: Optional[List[str]] = None,
        start: int = 0,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload = {
            "selectedFields": selected_fields or [],
            "filterGroup": {
                "logicalOperator": 0,
                "queryFilters": query_filters,
                "filterGroups": [],
            },
            "start": start,
            "limit": limit or self.page_size,
        }
        response = self.api_client.request(
            "POST",
            f"datafabric_/api/EntityService/entity/{entity_key}/query",
            json=payload,
        )
        return response.json() or {}

    def _resolve_batch_record_id(self) -> Optional[str]:
        if self.batch_record_id:
            return self.batch_record_id
        if not self.batch_id:
            return None
        if self._resolved_batch_record_id is not None:
            return self._resolved_batch_record_id

        batch_field = self._get_field_metadata("batchId")
        if (
            not batch_field
            or not batch_field.is_foreign_key
            or not batch_field.reference_entity
        ):
            self._resolved_batch_record_id = self.batch_id
            return self._resolved_batch_record_id

        related_entity = batch_field.reference_entity
        related_field_names = {field.name for field in related_entity.fields or []}

        direct_match = self._query_entity(
            entity_key=related_entity.id,
            query_filters=[{"fieldName": "Id", "operator": "=", "value": self.batch_id}],
            selected_fields=["Id"],
            limit=2,
        )
        direct_records = direct_match.get("Value") or direct_match.get("value") or []
        if len(direct_records) == 1:
            self._resolved_batch_record_id = direct_records[0]["Id"]
            log_event(
                logging.INFO,
                "batch.resolve.direct_id_match",
                batch_lookup_value=self.batch_id,
                resolved_batch_record_id=self._resolved_batch_record_id,
                related_entity_key=related_entity.id,
            )
            return self._resolved_batch_record_id

        candidate_fields: List[str] = []
        if batch_field.reference_name:
            candidate_fields.append(batch_field.reference_name)
        candidate_fields.extend(["batchId", "BatchId", "batch_id", "name", "Name"])

        seen_fields = set()
        for candidate_field in candidate_fields:
            if candidate_field in seen_fields:
                continue
            seen_fields.add(candidate_field)
            if candidate_field not in related_field_names:
                continue
            result = self._query_entity(
                entity_key=related_entity.id,
                query_filters=[
                    {
                        "fieldName": candidate_field,
                        "operator": "=",
                        "value": self.batch_id,
                    }
                ],
                selected_fields=["Id", candidate_field],
                limit=2,
            )
            records = result.get("Value") or result.get("value") or []
            if len(records) == 1:
                self._resolved_batch_record_id = records[0]["Id"]
                log_event(
                    logging.INFO,
                    "batch.resolve.reference_match",
                    batch_lookup_value=self.batch_id,
                    resolved_batch_record_id=self._resolved_batch_record_id,
                    related_entity_key=related_entity.id,
                    related_field=candidate_field,
                )
                return self._resolved_batch_record_id
            if len(records) > 1:
                raise ValueError(
                    f"Multiple related batch records matched {candidate_field}={self.batch_id}."
                )

        log_event(
            logging.ERROR,
            "batch.resolve.failed",
            batch_lookup_value=self.batch_id,
            related_entity_key=related_entity.id,
            available_related_fields=sorted(related_field_names),
        )
        raise ValueError(
            "Unable to resolve related batch record for "
            f"batchId={self.batch_id}. "
            f"Related entity key={related_entity.id}. "
            f"Available related fields={sorted(related_field_names)}."
        )

    def _serialize_entity_record(self, record: ExtractionRecord) -> Dict[str, Any]:
        payload = record.model_dump(exclude_none=True)
        batch_record_id = self._resolve_batch_record_id()
        if batch_record_id:
            payload["batchId"] = self._relationship_reference(batch_record_id)
        return payload

    def _serialize_record_update(self, record: RecordUpdate) -> Dict[str, Any]:
        payload = record.model_dump(by_alias=True, exclude_none=True)
        batch_record_id = self._resolve_batch_record_id()
        if batch_record_id:
            payload["batchId"] = self._relationship_reference(batch_record_id)
        return payload

    def _load_existing_index(self) -> RecordIndex:
        index = RecordIndex(self.filename)
        with log_duration(
            "index.load",
            filename=self.filename,
            batch_id=self.batch_id,
            document_id=self.document_id,
            upsert=self.upsert,
            page_size=self.page_size,
        ):
            if not self.upsert:
                log_event(
                    logging.INFO,
                    "index.load.skipped",
                    reason="upsert_disabled",
                    filename=self.filename,
                )
                return index

            selected_fields = [
                "Id",
                "filename",
                "documentId",
                "batchId",
                "fieldId",
                "field",
                "rowIndex",
                "columnIndex",
            ]
            query_filters: List[Dict[str, Any]] = []
            batch_record_id = self._resolve_batch_record_id()
            if batch_record_id:
                query_filters.append(
                    {"fieldName": "batchId", "operator": "=", "value": batch_record_id}
                )
            if self.document_id:
                query_filters.append(
                    {
                        "fieldName": "documentId",
                        "operator": "=",
                        "value": self.document_id,
                    }
                )
            if not query_filters and self.filename:
                query_filters.append(
                    {"fieldName": "filename", "operator": "=", "value": self.filename}
                )

            if not query_filters:
                log_event(
                    logging.WARNING,
                    "index.load.skipped",
                    reason="no_query_filters_available",
                    filename=self.filename,
                )
                return index

            start = 0
            page = 0
            matched_records = 0
            while True:
                page += 1
                page_start = time.perf_counter()
                data = self._query_entity(
                    entity_key=self.entity_key,
                    query_filters=query_filters,
                    selected_fields=selected_fields,
                    start=start,
                    limit=self.page_size,
                )
                records = data.get("Value") or data.get("value") or []
                elapsed = round(time.perf_counter() - page_start, 3)
                record_count = len(records)
                matched_records += record_count
                log_event(
                    logging.INFO,
                    "index.load.query_page",
                    page=page,
                    start=start,
                    received=record_count,
                    total_record_count=data.get("TotalRecordCount", data.get("totalRecordCount")),
                    elapsed_s=elapsed,
                    matched_records=matched_records,
                )
                if not records:
                    break
                for record in records:
                    index.add(self._coerce_record(record))
                if len(records) < self.page_size:
                    break
                start += self.page_size
            log_event(
                logging.INFO,
                "index.load.summary",
                page_count=page,
                matched_records=matched_records,
                indexed_records=len(index.by_full),
            )
        return index

    def _is_table_or_header_field(self, field: Dict[str, Any]) -> bool:
        values = field.get("Values") or []
        return any(value.get("Cells") for value in values)

    def _build_field_records(self) -> List[ExtractionRecord]:
        root = (self.extraction_results or {}).get("extractionResult", {})
        results_document = root.get("ResultsDocument", {})
        bounds = results_document.get("Bounds", {})
        document_id = root.get("DocumentId")
        document_type_id = results_document.get("DocumentTypeId")
        page_range = bounds.get("PageRange")
        page_count = bounds.get("PageCount")

        records: List[ExtractionRecord] = []
        for field in results_document.get("Fields", []):
            if self._is_table_or_header_field(field):
                log_event(
                    logging.INFO,
                    "records.build_fields.skipped_table_header_field",
                    filename=self.filename,
                    field_id=field.get("FieldId"),
                    field_name=field.get("FieldName"),
                )
                continue
            record = ExtractionRecord(
                filename=self.filename,
                documentId=document_id,
                documentTypeId=document_type_id,
                batchId=self.batch_id,
                operationId=self.operation_id,
                fieldId=field.get("FieldId"),
                field=field.get("FieldName"),
                isMissing=field.get("IsMissing"),
                isCorrect=True,
                pageRange=page_range,
                pageCount=page_count,
            )
            values = field.get("Values") or []
            if values:
                first_value = values[0]
                record.fieldValue = first_value.get("Value")
                record.confidence = first_value.get("Confidence")
                record.ocrConfidence = first_value.get("OcrConfidence")
                record.operatorConfirmed = first_value.get("OperatorConfirmed")
            records.append(record)
        log_event(
            logging.INFO,
            "records.build_fields",
            field_count=len(results_document.get("Fields", [])),
            record_count=len(records),
            filename=self.filename,
        )
        return records

    def _build_table_records(self) -> List[ExtractionRecord]:
        root = (self.extraction_results or {}).get("extractionResult", {})
        results_document = root.get("ResultsDocument", {})
        bounds = results_document.get("Bounds", {})
        document_id = root.get("DocumentId")
        document_type_id = results_document.get("DocumentTypeId")
        page_range = bounds.get("PageRange")
        page_count = bounds.get("PageCount")

        records: List[ExtractionRecord] = []
        tables = results_document.get("Tables", [])
        for table in tables:
            headers_lookup = self.create_headers_lookup_dict([table])
            field_id = table["FieldId"]
            headers = headers_lookup.get(field_id, {})
            for value in table.get("Values", []):
                for cell in value.get("Cells", []):
                    if cell["RowIndex"] != 0 and not cell["IsHeader"]:
                        cell_values = cell.get("Values") or []
                        first_value = cell_values[0] if cell_values else {}
                        record = ExtractionRecord(
                            filename=self.filename,
                            documentId=document_id,
                            documentTypeId=document_type_id,
                            batchId=self.batch_id,
                            operationId=self.operation_id,
                            fieldId=field_id,
                            field=headers.get(cell["ColumnIndex"]),
                            isMissing=cell.get("IsMissing", False),
                            fieldValue=first_value.get("Value"),
                            confidence=first_value.get("Confidence"),
                            ocrConfidence=first_value.get("OcrConfidence"),
                            operatorConfirmed=first_value.get("OperatorConfirmed"),
                            isCorrect=first_value.get("DataSource")
                            != "ManuallyChanged",
                            pageRange=page_range,
                            pageCount=page_count,
                            rowIndex=cell["RowIndex"],
                            columnIndex=cell["ColumnIndex"],
                        )
                        records.append(record)
        log_event(
            logging.INFO,
            "records.build_tables",
            table_count=len(tables),
            record_count=len(records),
            filename=self.filename,
        )
        return records

    def _prepare_extraction_batches(
        self, index: RecordIndex
    ) -> Tuple[List[ExtractionRecord], List[RecordUpdate]]:
        inserts: List[ExtractionRecord] = []
        updates: List[RecordUpdate] = []
        for record in self._build_field_records() + self._build_table_records():
            record_id = index.find(
                record.fieldId, record.field, record.rowIndex, record.columnIndex
            )
            if record_id:
                updates.append(RecordUpdate(Id=record_id, **record.model_dump()))
            else:
                inserts.append(record)
        log_event(
            logging.INFO,
            "records.prepare_extraction",
            inserts=len(inserts),
            updates=len(updates),
            filename=self.filename,
        )
        return inserts, updates

    def _prepare_validation_updates(
        self, index: RecordIndex
    ) -> Tuple[List[RecordUpdate], int, List[str]]:
        updates: List[RecordUpdate] = []
        skipped = 0
        errors: List[str] = []
        update_context = {}
        batch_record_id = self._resolve_batch_record_id()
        if batch_record_id is not None:
            update_context["batchId"] = self._relationship_reference(batch_record_id)
        if self.operation_id is not None:
            update_context["operationId"] = self.operation_id
        root = (self.validation_results or {}).get("result", {}).get(
            "validatedExtractionResults", {}
        )
        results_document = root.get("ResultsDocument", {})
        for field in results_document.get("Fields", []):
            field_id = field.get("FieldId")
            field_name = field.get("FieldName")
            record_id = index.find(field_id, field_name, None, None)
            if not record_id:
                skipped += 1
                continue
            values = field.get("Values") or []
            validated_value = values[0].get("Value") if values else None
            data_source = field.get("DataSource")
            is_correct = data_source not in {"ManuallyChanged", "Manual"}
            updates.append(
                RecordUpdate(
                    Id=record_id,
                    **update_context,
                    validatedFieldValue=validated_value,
                    operatorConfirmed=field.get("OperatorConfirmed"),
                    isCorrect=is_correct,
                )
            )

        tables = results_document.get("Tables", [])
        for table in tables:
            headers_lookup = self.create_headers_lookup_dict([table])
            field_id = table["FieldId"]
            headers = headers_lookup.get(field_id, {})
            for value in table.get("Values", []):
                for cell in value.get("Cells", []):
                    if cell["RowIndex"] != 0 and not cell["IsHeader"]:
                        field_name = headers.get(cell["ColumnIndex"])
                        if field_name is None:
                            errors.append(
                                f"Missing header for column index {cell['ColumnIndex']}"
                            )
                            continue
                        record_id = index.find(
                            field_id,
                            field_name,
                            cell["RowIndex"],
                            cell["ColumnIndex"],
                        )
                        if not record_id:
                            skipped += 1
                            continue
                        cell_values = cell.get("Values") or []
                        first_value = cell_values[0] if cell_values else {}
                        data_source = cell.get("DataSource")
                        updates.append(
                            RecordUpdate(
                                Id=record_id,
                                **update_context,
                                validatedFieldValue=first_value.get("Value"),
                                operatorConfirmed=cell.get("OperatorConfirmed"),
                                isCorrect=data_source
                                not in {"ManuallyChanged", "Manual"},
                            )
                        )
        log_event(
            logging.INFO,
            "records.prepare_validation",
            updates=len(updates),
            skipped=skipped,
            issues=len(errors),
            filename=self.filename,
        )
        return updates, skipped, errors

    def _apply_inserts(
        self, records: List[ExtractionRecord], index: RecordIndex
    ) -> Tuple[int, int, List[str]]:
        inserted = 0
        failures = 0
        errors: List[str] = []
        total_batches = (len(records) + self.batch_size - 1) // self.batch_size
        log_event(
            logging.INFO,
            "insert_batches.start",
            total_records=len(records),
            total_batches=total_batches,
            batch_size=self.batch_size,
        )
        for batch_number, batch in enumerate(self._chunk(records), start=1):
            payload = [self._serialize_entity_record(record) for record in batch]
            with log_duration(
                "insert_batch",
                batch_number=batch_number,
                total_batches=total_batches,
                batch_records=len(batch),
            ):
                success, failure = self._post_entity_batch(
                    "insert-batch",
                    payload,
                )
            inserted += len(success)
            failures += len(failure)
            log_event(
                logging.INFO,
                "insert_batch.result",
                batch_number=batch_number,
                success=len(success),
                failure=len(failure),
            )
            for record in success:
                index.add(self._coerce_record(record))
            for record in failure:
                errors.append(self._format_failure("Insert", record))
        log_event(
            logging.INFO,
            "insert_batches.done",
            inserted=inserted,
            failures=failures,
            error_count=len(errors),
        )
        return inserted, failures, errors

    def _apply_updates(self, records: List[RecordUpdate]) -> Tuple[int, int, List[str]]:
        updated = 0
        failures = 0
        errors: List[str] = []
        total_batches = (len(records) + self.batch_size - 1) // self.batch_size
        log_event(
            logging.INFO,
            "update_batches.start",
            total_records=len(records),
            total_batches=total_batches,
            batch_size=self.batch_size,
        )
        for batch_number, batch in enumerate(self._chunk(records), start=1):
            payload = [self._serialize_record_update(record) for record in batch]
            with log_duration(
                "update_batch",
                batch_number=batch_number,
                total_batches=total_batches,
                batch_records=len(batch),
            ):
                success, failure = self._post_entity_batch(
                    "update-batch",
                    payload,
                )
            updated += len(success)
            failures += len(failure)
            log_event(
                logging.INFO,
                "update_batch.result",
                batch_number=batch_number,
                success=len(success),
                failure=len(failure),
            )
            for record in failure:
                errors.append(self._format_failure("Update", record))
        log_event(
            logging.INFO,
            "update_batches.done",
            updated=updated,
            failures=failures,
            error_count=len(errors),
        )
        return updated, failures, errors

    def _post_entity_batch(
        self, action: str, payload: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        endpoint = f"datafabric_/api/EntityService/entity/{self.entity_key}/{action}"
        with log_duration(
            "entity.batch.request",
            action=action,
            endpoint=endpoint,
            payload_records=len(payload),
        ):
            response = self.api_client.request("POST", endpoint, json=payload)
            status_code = getattr(response, "status_code", None)
            data = response.json() or {}
            success = data.get("successRecords") or []
            failure = data.get("failureRecords") or []
            log_event(
                logging.INFO,
                "entity.batch.response",
                action=action,
                status_code=status_code,
                success_records=len(success),
                failure_records=len(failure),
            )
            return success, failure

    def _coerce_record(self, record: Dict[str, Any]) -> Any:
        obj = SimpleNamespace(**record)
        if hasattr(obj, "Id") and not hasattr(obj, "id"):
            setattr(obj, "id", getattr(obj, "Id"))
        elif "Id" in record and not hasattr(obj, "id"):
            setattr(obj, "id", record["Id"])
        return obj

    def _format_failure(self, action: str, record: Dict[str, Any]) -> str:
        error = record.get("error") or record.get("Error") or "Unknown error"
        field_id = record.get("fieldId") or record.get("FieldId")
        field_name = record.get("field")
        row_index = record.get("rowIndex")
        column_index = record.get("columnIndex")
        record_id = record.get("Id") or record.get("id")
        details = []
        if record_id:
            details.append(f"Id={record_id}")
        if field_id:
            details.append(f"fieldId={field_id}")
        if field_name:
            details.append(f"field={field_name}")
        if row_index is not None:
            details.append(f"rowIndex={row_index}")
        if column_index is not None:
            details.append(f"columnIndex={column_index}")
        suffix = f" ({', '.join(details)})" if details else ""
        return f"{action} failed: {error}{suffix}"

    def write_results(self) -> Output:
        inserted = 0
        updated = 0
        insert_failures = 0
        update_failures = 0
        skipped_validation_updates = 0
        errors: List[str] = []
        with log_duration(
            "write_results",
            filename=self.filename,
            has_extraction=bool(self.extraction_results),
            has_validation=bool(self.validation_results),
        ):
            if not self.extraction_results and not self.validation_results:
                log_event(
                    logging.WARNING,
                    "write_results.no_input",
                    filename=self.filename,
                )
                return Output(
                    entity_key=self.entity_key,
                    inserted=0,
                    updated=0,
                    insert_failures=0,
                    update_failures=0,
                    skipped_validation_updates=0,
                    errors=["No extraction or validation results provided."],
                )

            index = self._load_existing_index()
            if self.extraction_results:
                inserts, updates = self._prepare_extraction_batches(index)
                inserted, insert_failures, insert_errors = self._apply_inserts(
                    inserts, index
                )
                updated, update_failures, update_errors = self._apply_updates(updates)
                errors.extend(insert_errors)
                errors.extend(update_errors)
                log_event(
                    logging.INFO,
                    "write_results.extraction_complete",
                    inserted=inserted,
                    updated=updated,
                    insert_failures=insert_failures,
                    update_failures=update_failures,
                    errors=len(errors),
                )

            if self.validation_results:
                if self.extraction_results:
                    index = self._load_existing_index()
                validation_updates, skipped, validation_errors = (
                    self._prepare_validation_updates(index)
                )
                validation_updated, validation_failures, validation_update_errors = (
                    self._apply_updates(validation_updates)
                )
                updated += validation_updated
                update_failures += validation_failures
                skipped_validation_updates += skipped
                errors.extend(validation_errors)
                errors.extend(validation_update_errors)
                log_event(
                    logging.INFO,
                    "write_results.validation_complete",
                    validation_updated=validation_updated,
                    validation_failures=validation_failures,
                    skipped_validation_updates=skipped_validation_updates,
                    errors=len(errors),
                )

        return Output(
            entity_key=self.entity_key,
            inserted=inserted,
            updated=updated,
            insert_failures=insert_failures,
            update_failures=update_failures,
            skipped_validation_updates=skipped_validation_updates,
            errors=errors,
        )


def main(input_data: Input) -> Output:
    with log_duration("main.run", input_type=type(input_data).__name__):
        if isinstance(input_data, dict):
            payload = dict(input_data)
            preserved_fields = {}
            log_event(
                logging.INFO,
            "main.input.dict_received",
            keys=len(payload),
            has_extraction_results="extraction_results" in payload,
            has_validate_extraction="validate_extraction" in payload,
            has_validation_action_data="validation_action_data" in payload,
            has_batch_record_id="batch_record_id" in payload,
        )
            for key in (
                "batchId",
                "batch_id",
                "batch_record_id",
                "filename",
                "validate_extraction",
                "validation_action_data",
            ):
                if key in payload:
                    preserved_fields[key] = payload[key]
            if (
                "extraction_results" not in payload
                and "validation_action_data" not in payload
            ):
                if "extractionResult" in payload or "extractionResults" in payload:
                    payload = {"extraction_results": payload, **preserved_fields}
                elif "actionData" in payload and "operationId" in payload:
                    payload = {"validation_action_data": payload, **preserved_fields}
            with log_duration("main.input.model_validate"):
                input_data = Input.model_validate(payload)

        validation_results: Optional[Dict[str, Any]] = None
        if (
            input_data.extraction_results
            and "extractionResult" not in input_data.extraction_results
            and "DocumentId" in input_data.extraction_results
            and "ResultsDocument" in input_data.extraction_results
        ):
            input_data.extraction_results = {"extractionResult": input_data.extraction_results}
        with log_duration("uipath.client.init"):
            uipath = UiPath()
        if input_data.validate_extraction and input_data.validation_action_data:
            with log_duration("main.validation.fetch_result"):
                validation_action = ValidateExtractionAction.model_validate(
                    input_data.validation_action_data
                )
                validation_result = uipath.documents.get_validate_extraction_result(
                    validation_action
                )
                validation_results = {
                    "result": {
                        "validatedExtractionResults": validation_result.extraction_result.model_dump(
                            by_alias=True,
                            exclude_none=True,
                        ),
                        "actionStatus": validation_action.action_status,
                    }
                }
            log_event(
                logging.INFO,
                "main.validation.fetch_result.completed",
                operation_id=validation_action.operation_id,
                document_type_id=validation_action.document_type_id,
                project_id=validation_action.project_id,
            )
        entity_key = os.getenv("UIPATH_ENTITY_KEY")
        if not entity_key:
            log_event(
                logging.ERROR,
                "main.run.failed",
                reason="missing_uipath_entity_key",
            )
            raise ValueError("UIPATH_ENTITY_KEY environment variable is required.")
        document_id = None
        if input_data.extraction_results:
            document_id = input_data.extraction_results.get("extractionResult", {}).get(
                "DocumentId"
            )
        if not document_id and validation_results:
            document_id = (
                validation_results.get("result", {})
                .get("validatedExtractionResults", {})
                .get("DocumentId")
            )
        document_path = input_data.filename or document_id or "unknown-document"
        log_event(
            logging.INFO,
            "main.writer.prepare",
            entity_key=entity_key,
            document_path=document_path,
            has_extraction=bool(input_data.extraction_results),
            has_validation=bool(validation_results),
        )
        writer = ExtractionResultsWriter(
            uipath=uipath,
            entity_key=entity_key,
            document_path=document_path,
            extraction_results=input_data.extraction_results,
            validation_results=validation_results,
            batch_id=input_data.batch_id,
            batch_record_id=input_data.batch_record_id,
            document_id=document_id,
            batch_size=input_data.batch_size,
            page_size=input_data.page_size,
            upsert=input_data.upsert,
        )
        with log_duration("main.write_results"):
            summary = writer.write_results()
        output = Output(
            entity_key=summary.entity_key,
            inserted=summary.inserted,
            updated=summary.updated,
            insert_failures=summary.insert_failures,
            update_failures=summary.update_failures,
            skipped_validation_updates=summary.skipped_validation_updates,
            errors=summary.errors,
        )
        log_event(
            logging.INFO,
            "main.run.completed",
            inserted=output.inserted,
            updated=output.updated,
            insert_failures=output.insert_failures,
            update_failures=output.update_failures,
            skipped_validation_updates=output.skipped_validation_updates,
            errors=len(output.errors),
        )
        return output


if __name__ == "__main__":
    print("This module is intended to be executed as a UiPath coded agent.")

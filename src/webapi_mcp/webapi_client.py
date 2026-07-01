import httpx
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .auth import current_api_key
from .config import settings


class WebApiError(Exception):
    pass


class WebApiClient:
    def __init__(self) -> None:
        # Resolve TLS verification setting. A CA bundle path takes precedence
        # over the boolean flag, so users with self-signed corporate certs can
        # just point WEBAPI_MCP_CA_BUNDLE at their PEM file.
        verify: bool | str
        if settings.ca_bundle:
            verify = settings.ca_bundle
        else:
            verify = settings.verify_ssl

        self._client = httpx.AsyncClient(
            base_url=settings.webapi_base_url.rstrip("/"),
            timeout=settings.request_timeout_s,
            verify=verify,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        key = current_api_key()
        headers = {"Accept": "application/json"}
        if key:
            headers["X-API-KEY"] = key
        return headers

    @staticmethod
    def _scalar(value: Any) -> Any:
        if isinstance(value, list) and value:
            return value[0]
        return value

    @staticmethod
    def _iso_utc_from_millis(value: Any) -> str | None:
        scalar = WebApiClient._scalar(value)
        if scalar is None:
            return None
        try:
            millis = int(scalar)
        except (TypeError, ValueError):
            return None
        dt = datetime.fromtimestamp(millis / 1000, tz=timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")

    @staticmethod
    def _normalize_cohort_definition_payload(payload: Any) -> dict[str, Any]:
        # Some WebAPI deployments serialize the single-row response as
        # a JSON string inside a list; normalize all forms to a dict.
        if isinstance(payload, dict):
            return payload

        if isinstance(payload, list) and payload:
            payload = payload[0]

        if isinstance(payload, str):
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                return {}
            if isinstance(parsed, dict):
                return parsed

        return {}

    @staticmethod
    def _parse_expression(value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    @classmethod
    def _unwrap_cohort_expression(cls, value: Any) -> Any:
        parsed = cls._parse_expression(value)
        if isinstance(parsed, dict) and "expression" in parsed:
            return cls._parse_expression(parsed.get("expression"))
        return parsed

    async def _resolve_cohort_expression(
        self,
        *,
        cohort_id: int | None,
        cohort_definition_expression: Any | None,
    ) -> dict[str, Any]:
        if cohort_id is None and cohort_definition_expression is None:
            raise WebApiError(
                "Provide either `cohort_id` or `cohort_definition_expression`."
            )
        if cohort_id is not None and cohort_definition_expression is not None:
            raise WebApiError(
                "Provide only one of `cohort_id` or `cohort_definition_expression`."
            )

        if cohort_id is not None:
            expression = await self.get_cohort_definition(cohort_id=cohort_id)
        else:
            expression = self._unwrap_cohort_expression(cohort_definition_expression)

        if not isinstance(expression, dict):
            raise WebApiError(
                "Cohort definition expression must be a JSON object."
            )
        return expression

    async def _get_print_friendly_markdown(self, endpoint: str, payload: Any) -> str:
        headers = {**self._headers(), "Accept": "text/markdown"}
        r = await self._client.post(endpoint, json=payload, headers=headers)
        if r.status_code == 401:
            raise WebApiError(
                "WebAPI returned 401 (unauthorized). If your instance requires an "
                "API key, set `X-WebAPI-Key` in mcp.json. If you already set one, "
                "it may be disabled, expired, or unknown."
            )
        r.raise_for_status()
        return r.text

    async def _resolve_concept_set_payload(
        self,
        *,
        concept_set_id: int | None,
        concept_set_expression: Any | None,
    ) -> list[dict[str, Any]]:
        if concept_set_id is None and concept_set_expression is None:
            raise WebApiError(
                "Provide either `concept_set_id` or `concept_set_expression`."
            )
        if concept_set_id is not None and concept_set_expression is not None:
            raise WebApiError(
                "Provide only one of `concept_set_id` or `concept_set_expression`."
            )

        if concept_set_id is not None:
            r = await self._client.get(f"/conceptset/{concept_set_id}", headers=self._headers())
            if r.status_code == 401:
                raise WebApiError(
                    "WebAPI returned 401 (unauthorized). If your instance requires an "
                    "API key, set `X-WebAPI-Key` in mcp.json. If you already set one, "
                    "it may be disabled, expired, or unknown."
                )
            r.raise_for_status()

            row = self._normalize_cohort_definition_payload(r.json())
            if not row:
                raise WebApiError("Concept set not found or empty response.")
            expression = self._parse_expression(row.get("expression"))
            if not isinstance(expression, dict):
                raise WebApiError("Concept set expression must be a JSON object.")
            return [
                {
                    "id": row.get("id", concept_set_id),
                    "name": row.get("name") or f"Concept Set {concept_set_id}",
                    "expression": expression,
                }
            ]

        parsed = self._parse_expression(concept_set_expression)
        if not isinstance(parsed, dict):
            raise WebApiError("Concept set expression must be a JSON object.")

        if "expression" in parsed:
            expression = self._parse_expression(parsed.get("expression"))
            if not isinstance(expression, dict):
                raise WebApiError("Concept set expression must be a JSON object.")
            return [
                {
                    "id": parsed.get("id", 0),
                    "name": parsed.get("name") or "Concept Set",
                    "expression": expression,
                }
            ]

        return [
            {
                "id": 0,
                "name": "Concept Set",
                "expression": parsed,
            }
        ]

    @staticmethod
    def _fts_search_rows(
        rows: list[dict[str, Any]],
        *,
        id_field: str,
        indexed_fields: list[str],
        query_text: str,
        page_size: int,
        table_name: str,
    ) -> list[dict[str, Any]]:
        tokens = [token for token in query_text.split() if token]
        if not tokens:
            return []

        fts_query = " ".join(f'"{token.replace("\"", "\"\"")}"*' for token in tokens)
        fts_columns = ", ".join(indexed_fields)
        insert_columns = ", ".join([id_field, *indexed_fields])
        placeholders = ", ".join("?" for _ in range(len(indexed_fields) + 1))

        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                f"CREATE VIRTUAL TABLE {table_name} USING fts5({id_field} UNINDEXED, {fts_columns})"
            )
            conn.executemany(
                f"INSERT INTO {table_name}({insert_columns}) VALUES({placeholders})",
                [
                    tuple(
                        [row[id_field]]
                        + [
                            row[field] if isinstance(row.get(field), str) else ""
                            for field in indexed_fields
                        ]
                    )
                    for row in rows
                ],
            )

            matched_ids = [
                int(result[0])
                for result in conn.execute(
                    f"SELECT {id_field} FROM {table_name} WHERE {table_name} MATCH ? "
                    f"ORDER BY bm25({table_name}), {id_field} LIMIT ?",
                    (fts_query, page_size),
                )
            ]
        finally:
            conn.close()

        rank_by_id = {result_id: rank for rank, result_id in enumerate(matched_ids)}
        matched = [row for row in rows if row[id_field] in rank_by_id]
        matched.sort(key=lambda row: rank_by_id[row[id_field]])
        return matched[:page_size]

    async def search_concept(
        self,
        query: str,
        source_key: str,
        concept_class: list[str] | None,
        domain: list[str] | None,
        vocabulary: list[str] | None,
        standard_concept: str | None,
        page_size: int,
    ) -> list[dict[str, Any]]:
        # WebAPI exposes POST /vocabulary/{sourceKey}/search with a filter body.
        # Field names mirror the WebAPI Vocabulary controller.
        body: dict[str, Any] = {"QUERY": query}
        if concept_class:
            body["CONCEPT_CLASS_ID"] = concept_class
        if domain:
            body["DOMAIN_ID"] = domain
        if vocabulary:
            body["VOCABULARY_ID"] = vocabulary
        if standard_concept:
            body["STANDARD_CONCEPT"] = standard_concept

        url = f"/vocabulary/search"
        r = await self._client.post(url, json=body, headers=self._headers())
        if r.status_code == 401:
            raise WebApiError(
                "WebAPI returned 401 (unauthorized). If your instance requires an "
                "API key, set `X-WebAPI-Key` in mcp.json. If you already set one, "
                "it may be disabled, expired, or unknown."
            )
        r.raise_for_status()
        rows = r.json()
        # Trim the payload to fields that matter to an agent
        slim = [
            {
                "conceptId": row.get("CONCEPT_ID"),
                "conceptName": row.get("CONCEPT_NAME"),
                "conceptCode": row.get("CONCEPT_CODE"),
                "domainId": row.get("DOMAIN_ID"),
                "vocabularyId": row.get("VOCABULARY_ID"),
                "conceptClassId": row.get("CONCEPT_CLASS_ID"),
                "standardConcept": row.get("STANDARD_CONCEPT"),
                "invalidReason": row.get("INVALID_REASON"),
            }
            for row in rows
        ]

        concept_ids = list(
            dict.fromkeys(
                concept_id
                for concept_id in (row.get("conceptId") for row in slim)
                if isinstance(concept_id, int)
            )
        )
        if not concept_ids:
            return slim[:page_size]

        count_rows = await self.concept_record_count(
            source_key=source_key,
            concept_ids=concept_ids,
        )
        counts_by_concept = {row["conceptId"]: row for row in count_rows}
        for row in slim:
            concept_id = row.get("conceptId")
            counts = (
                counts_by_concept.get(concept_id, {})
                if isinstance(concept_id, int)
                else {}
            )
            row["recordCount"] = counts.get("recordCount")
            row["recordCountWithDescendants"] = counts.get(
                "recordCountWithDescendants"
            )
            row["personCount"] = counts.get("personCount")
            row["personCountWithDescendants"] = counts.get(
                "personCountWithDescendants"
            )
        slim.sort(
            key=lambda row: (
                row.get("personCountWithDescendants")
                if isinstance(row.get("personCountWithDescendants"), int)
                else -1
            ),
            reverse=True,
        )
        return slim[:page_size]

    async def concept_record_count(
        self,
        source_key: str,
        concept_ids: list[int],
    ) -> list[dict[str, int]]:
        url = f"/cdmresults/{source_key}/conceptRecordCount"
        r = await self._client.post(url, json=concept_ids, headers=self._headers())
        if r.status_code == 401:
            raise WebApiError(
                "WebAPI returned 401 (unauthorized). If your instance requires an "
                "API key, set `X-WebAPI-Key` in mcp.json. If you already set one, "
                "it may be disabled, expired, or unknown."
            )
        r.raise_for_status()

        rows = r.json()
        # WebAPI returns a list like [{"201826": [recordCount, ...]}].
        by_concept: dict[int, list[int]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            for concept_id, counts in row.items():
                try:
                    cid = int(concept_id)
                except (TypeError, ValueError):
                    continue
                if isinstance(counts, list) and len(counts) >= 4:
                    by_concept[cid] = counts

        result: list[dict[str, int]] = []
        for concept_id in concept_ids:
            counts = by_concept.get(concept_id)
            if not counts:
                continue
            result.append(
                {
                    "conceptId": concept_id,
                    "recordCount": int(counts[0]),
                    "recordCountWithDescendants": int(counts[1]),
                    "personCount": int(counts[2]),
                    "personCountWithDescendants": int(counts[3]),
                }
            )

        return result

    async def get_sources(self, source_name: str | None = None) -> list[dict[str, Any]]:
        # WebAPI exposes GET /source/sources with source metadata and daimons.
        r = await self._client.get("/source/sources", headers=self._headers())
        if r.status_code == 401:
            raise WebApiError(
                "WebAPI returned 401 (unauthorized). If your instance requires an "
                "API key, set `X-WebAPI-Key` in mcp.json. If you already set one, "
                "it may be disabled, expired, or unknown."
            )
        r.raise_for_status()

        rows = r.json()
        if not isinstance(rows, list):
            return []

        source_name_filter = source_name.lower() if source_name else None

        sources: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue

            row_source_name = row.get("sourceName")
            if source_name_filter and (
                not isinstance(row_source_name, str)
                or source_name_filter not in row_source_name.lower()
            ):
                continue

            sources.append(
                {
                    "sourceName": row_source_name,
                    "sourceKey": row.get("sourceKey")
                }
            )

        return sources

    async def search_cohort_definition(
        self,
        query: str,
        page_size: int,
    ) -> list[dict[str, Any]]:
        r = await self._client.get("/cohortdefinition", headers=self._headers())
        if r.status_code == 401:
            raise WebApiError(
                "WebAPI returned 401 (unauthorized). If your instance requires an "
                "API key, set `X-WebAPI-Key` in mcp.json. If you already set one, "
                "it may be disabled, expired, or unknown."
            )
        r.raise_for_status()

        rows = r.json()
        if not isinstance(rows, list):
            return []

        slim_rows: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue

            cohort_id = self._scalar(row.get("id"))
            try:
                cohort_id = int(cohort_id)
            except (TypeError, ValueError):
                continue

            created_by = row.get("createdBy")
            creator_name = None
            if isinstance(created_by, dict):
                creator_name = self._scalar(created_by.get("name"))

            slim_rows.append(
                {
                    "id": cohort_id,
                    "name": self._scalar(row.get("name")),
                    "dateCreated": self._iso_utc_from_millis(row.get("createdDate")),
                    "dateUpdated": self._iso_utc_from_millis(row.get("modifiedDate")),
                    "creatorName": creator_name,
                }
            )

        query_text = query.strip()
        if not query_text:
            return []

        try:
            query_id = int(query_text)
        except ValueError:
            query_id = None

        if query_id is not None:
            return [row for row in slim_rows if row["id"] == query_id][:page_size]

        return self._fts_search_rows(
            slim_rows,
            id_field="id",
            indexed_fields=["name", "creatorName"],
            query_text=query_text,
            page_size=page_size,
            table_name="cohort_fts",
        )

    async def search_concept_set(
        self,
        query: str,
        page_size: int,
    ) -> list[dict[str, Any]]:
        r = await self._client.get("/conceptset", headers=self._headers())
        if r.status_code == 401:
            raise WebApiError(
                "WebAPI returned 401 (unauthorized). If your instance requires an "
                "API key, set `X-WebAPI-Key` in mcp.json. If you already set one, "
                "it may be disabled, expired, or unknown."
            )
        r.raise_for_status()

        rows = r.json()
        if not isinstance(rows, list):
            return []

        slim_rows: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue

            concept_set_id = self._scalar(row.get("id"))
            try:
                concept_set_id = int(concept_set_id)
            except (TypeError, ValueError):
                continue

            created_by = row.get("createdBy")
            creator_name = None
            if isinstance(created_by, dict):
                creator_name = self._scalar(created_by.get("name"))

            slim_rows.append(
                {
                    "id": concept_set_id,
                    "name": self._scalar(row.get("name")),
                    "dateCreated": self._iso_utc_from_millis(row.get("createdDate")),
                    "dateUpdated": self._iso_utc_from_millis(row.get("modifiedDate")),
                    "creatorName": creator_name,
                }
            )

        query_text = query.strip()
        if not query_text:
            return []

        try:
            query_id = int(query_text)
        except ValueError:
            query_id = None

        if query_id is not None:
            return [row for row in slim_rows if row["id"] == query_id][:page_size]

        return self._fts_search_rows(
            slim_rows,
            id_field="id",
            indexed_fields=["name", "creatorName"],
            query_text=query_text,
            page_size=page_size,
            table_name="concept_set_fts",
        )

    async def get_cohort_definition(
        self,
        cohort_id: int,
    ) -> Any:
        r = await self._client.get(f"/cohortdefinition/{cohort_id}", headers=self._headers())
        if r.status_code == 401:
            raise WebApiError(
                "WebAPI returned 401 (unauthorized). If your instance requires an "
                "API key, set `X-WebAPI-Key` in mcp.json. If you already set one, "
                "it may be disabled, expired, or unknown."
            )
        r.raise_for_status()

        row = self._normalize_cohort_definition_payload(r.json())
        if not row:
            return {}
        return self._parse_expression(row.get("expression"))

    async def get_cohort_definition_meta_data(
        self,
        cohort_id: int,
    ) -> dict[str, Any]:
        r = await self._client.get(f"/cohortdefinition/{cohort_id}", headers=self._headers())
        if r.status_code == 401:
            raise WebApiError(
                "WebAPI returned 401 (unauthorized). If your instance requires an "
                "API key, set `X-WebAPI-Key` in mcp.json. If you already set one, "
                "it may be disabled, expired, or unknown."
            )
        r.raise_for_status()

        row = self._normalize_cohort_definition_payload(r.json())
        if not row:
            return {}

        row.pop("expression", None)
        return row

    async def get_concept_set_markdown(
        self,
        concept_set_id: int | None = None,
        concept_set_expression: Any | None = None,
    ) -> str:
        concept_sets = await self._resolve_concept_set_payload(
            concept_set_id=concept_set_id,
            concept_set_expression=concept_set_expression,
        )
        return await self._get_print_friendly_markdown(
            "/cohortdefinition/printfriendly/conceptsets?format=markdown",
            concept_sets,
        )

    async def get_cohort_definition_markdown(
        self,
        cohort_id: int | None = None,
        cohort_definition_expression: Any | None = None,
        include_concept_sets: bool = True,
    ) -> str:
        expression = await self._resolve_cohort_expression(
            cohort_id=cohort_id,
            cohort_definition_expression=cohort_definition_expression,
        )
        cohort_markdown = await self._get_print_friendly_markdown(
            "/cohortdefinition/printfriendly/cohort?format=markdown",
            expression,
        )

        if not include_concept_sets:
            return cohort_markdown

        concept_sets = expression.get("ConceptSets")
        if not isinstance(concept_sets, list) or not concept_sets:
            return cohort_markdown

        concept_set_markdown = await self._get_print_friendly_markdown(
            "/cohortdefinition/printfriendly/conceptsets?format=markdown",
            concept_sets,
        )
        if not concept_set_markdown:
            return cohort_markdown

        return f"{cohort_markdown.rstrip()}\n\n{concept_set_markdown.lstrip()}"

    async def get_concept_set(
        self,
        concept_set_id: int,
    ) -> Any:
        r = await self._client.get(f"/conceptset/{concept_set_id}", headers=self._headers())
        if r.status_code == 401:
            raise WebApiError(
                "WebAPI returned 401 (unauthorized). If your instance requires an "
                "API key, set `X-WebAPI-Key` in mcp.json. If you already set one, "
                "it may be disabled, expired, or unknown."
            )
        r.raise_for_status()

        row = self._normalize_cohort_definition_payload(r.json())
        if not row:
            return {}
        return self._parse_expression(row.get("expression"))

    async def get_concept_set_meta_data(
        self,
        concept_set_id: int,
    ) -> dict[str, Any]:
        r = await self._client.get(f"/conceptset/{concept_set_id}", headers=self._headers())
        if r.status_code == 401:
            raise WebApiError(
                "WebAPI returned 401 (unauthorized). If your instance requires an "
                "API key, set `X-WebAPI-Key` in mcp.json. If you already set one, "
                "it may be disabled, expired, or unknown."
            )
        r.raise_for_status()

        row = self._normalize_cohort_definition_payload(r.json())
        if not row:
            return {}

        row.pop("expression", None)
        return row


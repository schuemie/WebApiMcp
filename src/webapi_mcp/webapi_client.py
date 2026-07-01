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

        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE cohort_fts USING fts5(id UNINDEXED, name, creatorName)")
            conn.executemany(
                "INSERT INTO cohort_fts(id, name, creatorName) VALUES(?, ?, ?)",
                [
                    (
                        row["id"],
                        row["name"] if isinstance(row.get("name"), str) else "",
                        row["creatorName"] if isinstance(row.get("creatorName"), str) else "",
                    )
                    for row in slim_rows
                ],
            )

            tokens = [token for token in query_text.split() if token]
            if not tokens:
                return []

            fts_query = " ".join(f'"{token.replace("\"", "\"\"")}"*' for token in tokens)
            matched_ids = [
                int(row[0])
                for row in conn.execute(
                    "SELECT id FROM cohort_fts WHERE cohort_fts MATCH ? ORDER BY bm25(cohort_fts), id LIMIT ?",
                    (fts_query, page_size),
                )
            ]
        finally:
            conn.close()

        rank_by_id = {cohort_id: rank for rank, cohort_id in enumerate(matched_ids)}
        matched = [row for row in slim_rows if row["id"] in rank_by_id]
        matched.sort(key=lambda row: rank_by_id[row["id"]])
        return matched[:page_size]

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


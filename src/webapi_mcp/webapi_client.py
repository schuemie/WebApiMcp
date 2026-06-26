import httpx
from typing import Any

from .auth import current_api_key
from .config import settings


class WebApiError(Exception):
    pass


class WebApiClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.webapi_base_url.rstrip("/"),
            timeout=settings.request_timeout_s,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        key = current_api_key()
        if not key:
            raise WebApiError(
                "No WebAPI API key was supplied. Add `X-WebAPI-Key` to your "
                "mcp.json headers. See your internal docs for how to mint a key."
            )
        return {"X-API-KEY": key, "Accept": "application/json"}

    async def concept_search(
        self,
        query: str,
        source_key: str,
        domain: list[str] | None,
        vocabulary: list[str] | None,
        standard_concept: str | None,
        page_size: int,
    ) -> list[dict[str, Any]]:
        # WebAPI exposes POST /vocabulary/{sourceKey}/search with a filter body.
        # Field names mirror the WebAPI Vocabulary controller.
        body: dict[str, Any] = {"QUERY": query}
        if domain:
            body["DOMAIN_ID"] = domain
        if vocabulary:
            body["VOCABULARY_ID"] = vocabulary
        if standard_concept:
            body["STANDARD_CONCEPT"] = [standard_concept]

        url = f"/vocabulary/{source_key}/search"
        r = await self._client.post(url, json=body, headers=self._headers())
        if r.status_code == 401:
            raise WebApiError(
                "WebAPI rejected the API key (401). It may be disabled, expired, "
                "or unknown. Mint a new one and update mcp.json."
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
        return slim[:page_size]
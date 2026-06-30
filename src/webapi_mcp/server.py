import contextlib
import logging
from typing import Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from .auth import ApiKeyMiddleware
from .config import settings
from .webapi_client import WebApiClient, WebApiError

log = logging.getLogger("webapi-mcp")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

mcp = FastMCP(
    name="ohdsi-webapi",
    instructions=(
        "Tools for querying an OHDSI WebAPI instance. "
        "When provided, calls include the user's personal WebAPI API key from "
        "the MCP client."
    ),
)

_client: WebApiClient | None = None


def _get_client() -> WebApiClient:
    global _client
    if _client is None:
        _client = WebApiClient()
    return _client


@mcp.tool()
async def concept_search(
    query: str = Field(..., description="Free-text search term, e.g. 'metformin'"),
    source_key: str | None = Field(
        None,
        description=(
            "WebAPI source key for the vocabulary schema. "
            "Omit to use the server default."
        ),
    ),
    concept_class: list[str] | None = Field(
        None,
        description="Optional OMOP concept class filter, e.g. ['Ingredient'] or ['Clinical Finding'].",
    ),
    domain: list[str] | None = Field(
        None,
        description="Optional OMOP domain filter, e.g. ['Drug'] or ['Condition'].",
    ),
    vocabulary: list[str] | None = Field(
        None,
        description="Optional vocabulary filter, e.g. ['RxNorm'] or ['SNOMED'].",
    ),
    standard_concept: Literal["S", "C", "N"] | None = Field(
        "S",
        description="Restrict to Standard ('S'), Classification ('C'), or Non-standard ('N'). Default 'S'.",
    ),
    page_size: int = Field(
        25, ge=1, description="Maximum number of rows to return.",
    ),
) -> list[dict]:
    """
    Find OMOP concepts and their IDs by text with optional vocabulary/domain filters.
    The output is reverse sorted by person counts (including descendants).
    """
    page_size = min(page_size, settings.max_page_size)
    skey = source_key or settings.default_source_key
    try:
        rows = await _get_client().concept_search(
            query=query,
            source_key=skey,
            concept_class=concept_class,
            domain=domain,
            vocabulary=vocabulary,
            standard_concept=standard_concept,
            page_size=page_size,
        )
    except WebApiError as e:
        # Surface a clean, actionable error to the agent
        raise RuntimeError(str(e)) from e
    log.info(
        "concept_search query=%r source=%s returned=%d",
        query, skey, len(rows),
    )
    return rows


@mcp.tool()
async def concept_record_count(
    concept_ids: list[int] = Field(
        ...,
        description=(
            "List of OMOP concept IDs to retrieve counts for, "
            "e.g. [201826, 201820]."
        ),
    ),
    source_key: str | None = Field(
        None,
        description=(
            "WebAPI source key for counts. Omit to use the server default."
        ),
    ),
) -> list[dict]:
    """Return CDM concept counts for each requested OMOP concept ID.

    Each returned row includes:
    - recordCount
    - recordCountWithDescendants
    - personCount
    - personCountWithDescendants
    """
    skey = source_key or settings.default_source_key
    try:
        rows = await _get_client().concept_record_count(
            source_key=skey,
            concept_ids=concept_ids,
        )
    except WebApiError as e:
        # Surface a clean, actionable error to the agent
        raise RuntimeError(str(e)) from e
    log.info(
        "concept_record_count source=%s requested=%d returned=%d",
        skey,
        len(concept_ids),
        len(rows),
    )
    return rows


@mcp.tool()
async def get_sources() -> list[dict]:
    """List all configured WebAPI data sources and their attached daimons."""
    try:
        rows = await _get_client().get_sources()
    except WebApiError as e:
        # Surface a clean, actionable error to the agent
        raise RuntimeError(str(e)) from e
    log.info("get_sources returned=%d", len(rows))
    return rows


# ---- HTTP app wiring -----------------------------------------------------

async def healthz(_request):
    return JSONResponse({"status": "ok"})


def build_app() -> ApiKeyMiddleware:
    # FastMCP exposes a Streamable HTTP ASGI app at /mcp by default.
    mcp_app = mcp.streamable_http_app()

    @contextlib.asynccontextmanager
    async def lifespan(app):
        # Run the FastMCP streamable-HTTP session manager so its internal
        # task group is initialized; otherwise requests to /mcp fail with
        # "Task group is not initialized."
        async with mcp.session_manager.run():
            try:
                yield
            finally:
                if _client is not None:
                    await _client.aclose()

    starlette_app = Starlette(
        debug=False,
        lifespan=lifespan,
        routes=[
            Route("/healthz", healthz),
            # Mount MCP app at root so its internal /mcp route is reachable at /mcp
            Mount("/", app=mcp_app),
        ],
    )
    # Wrap with pure-ASGI middleware (BaseHTTPMiddleware breaks SSE streaming)
    return ApiKeyMiddleware(starlette_app)


def main() -> None:
    import uvicorn
    uvicorn.run(
        build_app(),
        host=settings.host,
        port=settings.port,
        log_level="info",
        proxy_headers=True,
        forwarded_allow_ips="*",  # trust the TLS-terminating reverse proxy
    )


if __name__ == "__main__":
    main()
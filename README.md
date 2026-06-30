# WebApi MCP Server

This project runs an MCP server that bridges MCP clients to an OHDSI WebAPI instance.

## Start the server

### 1) Install dependencies

```bash
python -m pip install -e .
```

### 2) Configure environment

Use `.env.example` as the template for your local env file.

```powershell
Copy-Item .env.example .env
```

Then edit `.env` and set at least:

- `WEBAPI_MCP_WEBAPI_BASE_URL=https://your-webapi-host/WebAPI`

All supported variables and defaults are documented in `.env.example`.

### 3) Run

Preferred (installed console command):

```bash
webapi-mcp
```

Equivalent Python module form:

```bash
python -m webapi_mcp.server
```

The server exposes:

- MCP endpoint: `http://localhost:8765/mcp`
- Health check: `http://localhost:8765/healthz`

## Reference this server in MCP.json

Start the server first (see above), then add this entry to your `mcp.json`.
`X-WebAPI-Key` is optional and only needed if your WebAPI instance requires API key auth:

```json
{
  "mcpServers": {
    "webapi-webapi": {
      "type": "http",
      "url": "http://localhost:8765/mcp",
      "headers": {}
    }
  }
}
```

If your WebAPI requires API key auth, include `X-WebAPI-Key`.

If you configured `WEBAPI_MCP_SHARED_GATEWAY_SECRET`, also include `X-Gateway-Secret` in the headers:

```json
{
  "mcpServers": {
    "webapi-webapi": {
      "type": "http",
      "url": "http://localhost:8765/mcp",
      "headers": {
        "X-WebAPI-Key": "<your-webapi-api-key>",
        "X-Gateway-Secret": "<your-shared-gateway-secret>"
      }
    }
  }
}
```




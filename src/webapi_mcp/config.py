from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WEBAPI_MCP_", env_file=".env")

    # WebAPI base URL, e.g. https://webapi.yourcorp.internal/WebAPI
    webapi_base_url: str

    # Default vocabulary source key (the WebAPI "source" that points at your
    # vocabulary schema). Users can override per-call.
    default_source_key: str = "OHDSI-CDMV5"

    # Bind address for the MCP server itself
    host: str = "0.0.0.0"
    port: int = 8765

    # Safety caps
    max_page_size: int = 100
    request_timeout_s: float = 30.0

    # TLS / SSL options for talking to WebAPI.
    # - verify_ssl=True (default): verify against system trust store.
    # - ca_bundle: path to a PEM file with your corporate/self-signed CA chain.
    #   When set, it takes precedence over verify_ssl and is used as the trust
    #   store. This is the recommended way to handle self-signed certs.
    # - verify_ssl=False: disable verification entirely (INSECURE; only use for
    #   local testing).
    verify_ssl: bool = True
    ca_bundle: str | None = None

    # Optional: require this shared secret in addition to per-user API key,
    # to keep random internal hosts from probing the MCP server.
    shared_gateway_secret: str | None = None


settings = Settings()  # raises if required env vars missing
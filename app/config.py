"""
Typed application configuration.

Per spec section 11: settings are validated at startup, loaded from
environment variables / .env, and never hardcode tenant-specific values.
"""
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # --- Microsoft Entra ID / app-only auth ---
    tenant_id: str
    client_id: str
    client_secret: str

    # --- SharePoint site / drive identity ---
    sharepoint_host: str = "auximotechgmbh.sharepoint.com"
    sharepoint_site_path: str = "/sites/AuximoKnowledgeHub"
    sharepoint_site_id: str
    sharepoint_drive_id: str

    # --- Microsoft Graph ---
    graph_base_url: str = "https://graph.microsoft.com/v1.0"
    graph_scope: str = "https://graph.microsoft.com/.default"

    # --- Persistence (Phase 2) ---
    database_url: str = "postgresql+asyncpg://knowledge_agent:knowledge_agent@localhost:5432/knowledge_agent"

    # --- Chunking (Phase 6) ---
    # Starting points, not permanent truths (spec section 33) -- benchmark
    # retrieval quality once embeddings/retrieval (Phases 7-9) exist, then
    # revisit.
    chunk_size: int = 800
    chunk_overlap: int = 120

    # --- Embeddings (Phase 7) ---
    # The model's output dimension must match EMBEDDING_DIMENSIONS in
    # app/db/models.py (the pgvector column type) -- switching to a model
    # with a different dimension needs a new migration, not just this value.
    embedding_model_name: str = "BAAI/bge-small-en-v1.5"
    embedding_batch_size: int = 32
    embedding_device: str = "cpu"
    # Run the embedding stage automatically at the end of every sync run.
    embed_after_sync: bool = True
    # bge models are trained with this instruction prepended to *queries*
    # only (documents are embedded as-is). Set to "" for models without one.
    embedding_query_instruction: str = "Represent this sentence for searching relevant passages: "

    # --- Search (Phase 8) ---
    search_default_top_k: int = 5

    # --- LLM / RAG answers (Phase 9) ---
    # Azure OpenAI v1 API (Responses API, no api-version needed). The full
    # ".../openai/v1/responses" URI is accepted too; the client normalizes it.
    azure_openai_endpoint: str = "https://auximoplayground-llm.openai.azure.com/openai/v1"
    # Optional so the rest of the app (sync, search) runs without it; the
    # ask command fails with a clear message if it's missing.
    azure_openai_api_key: SecretStr | None = None
    # The Azure *deployment* name, sent as "model".
    azure_openai_deployment: str = "gpt-4o"
    # 0 = least improvisation; answers should restate sources, not embellish them.
    llm_temperature: float = 0.0
    llm_max_output_tokens: int = 1200
    llm_timeout_seconds: float = 90.0
    llm_max_retries: int = 3
    # Chunks retrieved as context for one answer.
    rag_top_k: int = 8

    # --- Sync behavior ---
    sync_interval_hours: int = 24
    reconciliation_interval_days: int = 7

    # --- HTTP / retry behavior (Phase 1 needs this for the Graph client) ---
    http_timeout_seconds: float = 30.0
    http_max_retries: int = 5
    http_backoff_base_seconds: float = 1.0
    max_concurrent_graph_requests: int = 4

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("tenant_id", "client_id", "client_secret", "sharepoint_site_id", "sharepoint_drive_id")
    @classmethod
    def _not_blank(cls, v: str, info) -> str:
        if not v or not v.strip():
            raise ValueError(f"{info.field_name} must not be blank")
        return v.strip()

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}"


@lru_cache
def get_settings() -> Settings:
    """
    Cached settings accessor. Using a function (rather than a bare module-level
    instance) means tests can monkeypatch environment variables and call
    get_settings.cache_clear() to get a fresh, validated Settings object,
    instead of import order silently baking in whatever env existed first.
    """
    return Settings()

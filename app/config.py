"""
Typed application configuration.

Per spec section 11: settings are validated at startup, loaded from
environment variables / .env, and never hardcode tenant-specific values.
"""
import re
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # --- Microsoft Entra ID / app-only auth ---
    tenant_id: str
    client_id: str
    # SecretStr so it can't leak through repr()/logging of the settings object.
    client_secret: SecretStr

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
    # Prefix for document/chunk texts ("passage: " for e5 models, empty for
    # bge). Changing it -- without changing the model name -- needs a full
    # re-embed (POST /embeddings/run?reembed=all), since existing vectors were
    # computed without it.
    embedding_passage_prefix: str = ""

    # --- Search (Phase 8) ---
    search_default_top_k: int = 5

    # --- Reranking ---
    # Cross-encoder that reorders the retrieval candidate pool. Chosen by
    # measurement on our content (correct chunk at #1: 4/9 -> 6/9): the
    # multilingual mmarco-mMiniLMv2-L12 ranked German questions no better,
    # misranked a short English chunk from #2 to #27, was 2x slower, and its
    # scores didn't separate relevant from off-topic questions.
    rerank_enabled: bool = True
    reranker_model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    rerank_candidates: int = 30
    reranker_batch_size: int = 16
    # Drop reranked chunks below this relevance (0..1). If none pass, `ask`
    # answers "not found" without calling the LLM. None = never drop.
    # Off by default: on our test questions off-topic scored ~0.000 but a
    # relevant German question only 0.009 -- too thin a margin to cut on
    # yet, and the LLM already declines when the context doesn't answer.
    rerank_min_relevance: float | None = None

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

    # --- Scheduler (spec section 58) ---
    # Daily sync at sync_schedule_time (HH:MM, 24h) in sync_schedule_timezone
    # (an IANA name like "Europe/Berlin"; empty = the server's local time).
    sync_schedule_enabled: bool = True
    sync_schedule_time: str = "02:00"
    sync_schedule_timezone: str = ""
    # The scheduled library sync becomes a full reconciliation walk (spec
    # section 23) when the last one is older than this. 0 disables that.
    reconciliation_interval_days: int = 7
    # Also run page sync on the schedule (skipped cleanly if the capability
    # check fails -- it never escalates permissions, Rule 6).
    sync_pages_on_schedule: bool = True

    # --- API security (spec section 50) ---
    # When set, every endpoint except the liveness/metrics probes requires
    # an "X-API-Key: <value>" header. Leave unset only on a trusted network.
    api_key: SecretStr | None = None
    # /debug/* and /discovery/preview return raw Graph error bodies and
    # document content; off unless explicitly enabled.
    enable_debug_endpoints: bool = False

    # --- Chat API (Phase 11: POST /chat for the SharePoint chat bubble) ---
    # Entra ID audience(s) a user token must be issued for, comma-separated
    # -- normally "api://<CLIENT_ID>" (the backend app registration's
    # "Expose an API" URI). Unset = development mode: /chat falls back to
    # API_KEY auth and every caller shares one anonymous identity.
    chat_auth_audience: str = ""
    # Delegated scope the token must carry (the one SPFx requests).
    chat_required_scope: str = "user_impersonation"
    # Optional app role the user must hold (token "roles" claim), for
    # restricting chat to part of the tenant. Not used at Auximo: every
    # employee may use the Knowledge Hub and no guest users are invited, so
    # "a valid user of our tenant" is the access rule. If set, assign the role
    # in Entra (Enterprise applications -> Users and groups) BEFORE deploying:
    # users without it get 403. Only applies with CHAT_AUTH_AUDIENCE.
    chat_required_role: str = ""
    # Browser origins allowed to call the API (comma-separated). Exact
    # origins only -- never "*", since requests carry credentials.
    cors_allowed_origins: str = "https://auximotechgmbh.sharepoint.com"
    chat_max_message_chars: int = 2000
    # Prior user turns used to rewrite a follow-up into a standalone question.
    chat_history_turns: int = 6
    # Load the embedding + reranker models in the background at startup, so
    # the first chat request doesn't pay for it.
    chat_warmup_models: bool = True

    # --- Logging (spec section 46) ---
    log_level: str = "INFO"
    log_format: str = "text"  # "text" (key=value) or "json" (one object per line)

    # --- HTTP / retry behavior (Phase 1 needs this for the Graph client) ---
    http_timeout_seconds: float = 30.0
    http_max_retries: int = 5
    http_backoff_base_seconds: float = 1.0
    # Upper bound for one backoff sleep, including a server's Retry-After.
    http_max_backoff_seconds: float = 120.0
    max_concurrent_graph_requests: int = 4

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("tenant_id", "client_id", "sharepoint_site_id", "sharepoint_drive_id")
    @classmethod
    def _not_blank(cls, v: str, info) -> str:
        if not v or not v.strip():
            raise ValueError(f"{info.field_name} must not be blank")
        return v.strip()

    @field_validator("client_secret")
    @classmethod
    def _secret_not_blank(cls, v: SecretStr) -> SecretStr:
        value = v.get_secret_value().strip()
        if not value:
            raise ValueError("client_secret must not be blank")
        return SecretStr(value)

    @field_validator("api_key", mode="before")
    @classmethod
    def _empty_api_key_is_none(cls, v):
        # API_KEY= (empty) in .env means "no key", not "the empty string is the key".
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        return v

    @field_validator("sync_schedule_time")
    @classmethod
    def _valid_schedule_time(cls, v: str) -> str:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v.strip()):
            raise ValueError("sync_schedule_time must be HH:MM (24-hour)")
        return v.strip()

    @field_validator("sync_schedule_timezone")
    @classmethod
    def _valid_timezone(cls, v: str) -> str:
        v = v.strip()
        if v:
            try:
                ZoneInfo(v)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError(f"unknown timezone {v!r}") from exc
        return v

    @field_validator("cors_allowed_origins")
    @classmethod
    def _no_wildcard_origin(cls, v: str) -> str:
        if any(o.strip() == "*" for o in v.split(",")):
            raise ValueError("cors_allowed_origins must list exact origins, not '*'")
        return v

    @field_validator("log_format")
    @classmethod
    def _valid_log_format(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("text", "json"):
            raise ValueError("log_format must be 'text' or 'json'")
        return v

    @property
    def chat_audiences(self) -> list[str]:
        return [a.strip() for a in self.chat_auth_audience.split(",") if a.strip()]

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.cors_allowed_origins.split(",") if o.strip()]

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

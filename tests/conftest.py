import pytest

from app.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        tenant_id="00000000-0000-0000-0000-000000000000",
        client_id="11111111-1111-1111-1111-111111111111",
        client_secret="fake-secret-value",
        sharepoint_site_id="auximotechgmbh.sharepoint.com,fake-site-guid",
        sharepoint_drive_id="fake-drive-id",
        http_max_retries=2,
        http_backoff_base_seconds=0.01,  # keep tests fast
    )


class FakeGraphAuth:
    """Stands in for GraphAuth without touching MSAL or the network."""

    def get_access_token(self) -> str:
        return "fake-token"


class FakeEmbeddingProvider:
    """
    Deterministic stand-in for SentenceTransformerProvider: no torch, no
    model download. Same text -> same vector. Records every call's batch so
    tests can assert on batching, and raises for any text containing
    fail_on (when set).
    """

    def __init__(self, model_name: str = "fake-model", dimensions: int = 384, fail_on: str | None = None):
        self.model_name = model_name
        self._dimensions = dimensions
        self.fail_on = fail_on
        self.calls: list[list[str]] = []

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.fail_on is not None and any(self.fail_on in t for t in texts):
            raise RuntimeError(f"fake provider failure on '{self.fail_on}'")
        return [self._vector(t) for t in texts]

    async def embed_query(self, query: str) -> list[float]:
        return (await self.embed([query]))[0]

    def _vector(self, text: str) -> list[float]:
        import hashlib

        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [digest[i % len(digest)] / 255.0 for i in range(self._dimensions)]

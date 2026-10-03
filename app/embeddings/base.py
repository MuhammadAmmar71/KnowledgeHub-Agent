"""
Spec section 35: the embedding provider abstraction. Nothing upstream of
the embedding stage (sync, extraction, chunking) imports this -- Rule 11.
"""
from __future__ import annotations

from typing import Protocol


class EmbeddingProvider(Protocol):
    # Recorded on every chunk it embeds, so a model change is detectable
    # and triggers re-embedding (spec section 35).
    model_name: str

    @property
    def dimensions(self) -> int: ...

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embeds document/chunk texts."""
        ...

    async def embed_query(self, query: str) -> list[float]:
        """
        Embeds a search query. Separate from embed() because retrieval
        models like bge expect queries and passages to be encoded
        differently (an instruction prefix on the query side only).
        """
        ...

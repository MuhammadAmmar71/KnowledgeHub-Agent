"""
Local embedding provider backed by sentence-transformers. No API key, and
no document content leaves the machine.

The model is loaded lazily on first use rather than at import/startup: it
pulls in torch and (on first ever run) downloads weights, neither of which
/health or the sync endpoints should have to wait for.
"""
from __future__ import annotations

import asyncio
import threading


class SentenceTransformerProvider:
    def __init__(self, model_name: str, device: str = "cpu", batch_size: int = 32):
        self.model_name = model_name
        self._device = device
        self._batch_size = batch_size
        self._model = None
        self._load_lock = threading.Lock()

    def _get_model(self):
        if self._model is None:
            with self._load_lock:
                if self._model is None:
                    from sentence_transformers import SentenceTransformer

                    self._model = SentenceTransformer(self.model_name, device=self._device)
        return self._model

    @property
    def dimensions(self) -> int:
        return self._get_model().get_sentence_embedding_dimension()

    def _encode(self, texts: list[str]) -> list[list[float]]:
        vectors = self._get_model().encode(
            texts,
            batch_size=self._batch_size,
            # Unit-length vectors make cosine similarity equal to the dot
            # product, which is what pgvector's similarity search will rely on.
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return vectors.tolist()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # CPU-bound inference -- run it off the event loop so API requests
        # stay responsive while a large batch is embedding (Rule 13).
        return await asyncio.to_thread(self._encode, texts)

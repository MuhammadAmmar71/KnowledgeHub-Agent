"""
Reranking (spec section 37: "optional reranking").

Hybrid retrieval scores the query and each chunk *independently* (two
separate embeddings, or a keyword match), which is fast enough to scan the
whole index but coarse. A cross-encoder reads the query and one passage
*together* and scores how well that passage answers that query -- much
more accurate, but far too slow to run over every chunk. So retrieval
fetches a candidate pool and the cross-encoder only reorders that pool.

Runs locally via sentence-transformers (already a dependency for
embeddings): no extra service, no document text leaves the machine.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Protocol

logger = logging.getLogger("knowledge_agent.retrieval.rerank")


class Reranker(Protocol):
    model_name: str

    async def score(self, query: str, passages: list[str]) -> list[float]:
        """Relevance of each passage to the query, higher = more relevant."""
        ...


class CrossEncoderReranker:
    def __init__(self, model_name: str, device: str = "cpu", batch_size: int = 16, max_length: int = 512):
        self.model_name = model_name
        self._device = device
        self._batch_size = batch_size
        self._max_length = max_length
        self._model = None
        self._load_lock = threading.Lock()

    def _get_model(self):
        if self._model is None:
            with self._load_lock:
                if self._model is None:
                    self._model = self._load()
        return self._model

    def _load(self):
        import torch
        from sentence_transformers import CrossEncoder

        # Force a sigmoid so every model reports 0..1 relevance. Models
        # disagree on the default (ms-marco-MiniLM returns raw logits,
        # roughly -11..+10), and a min-relevance cut-off is only meaningful
        # on a fixed scale.
        kwargs = dict(
            device=self._device, max_length=self._max_length, default_activation_function=torch.nn.Sigmoid()
        )
        # Same cache-first policy as the embedding provider: once
        # downloaded, loading never depends on huggingface.co.
        try:
            return CrossEncoder(self.model_name, local_files_only=True, **kwargs)
        except Exception:  # noqa: BLE001 -- not cached (raised type varies by hub version)
            logger.info("reranker_model_not_cached_downloading model=%s", self.model_name)
            return CrossEncoder(self.model_name, **kwargs)

    def _predict(self, query: str, passages: list[str]) -> list[float]:
        scores = self._get_model().predict(
            [(query, passage) for passage in passages],
            batch_size=self._batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return [float(s) for s in scores]

    async def score(self, query: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        # CPU-bound -- keep it off the event loop (Rule 13).
        return await asyncio.to_thread(self._predict, query, passages)

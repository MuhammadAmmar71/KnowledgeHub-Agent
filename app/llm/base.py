"""
LLM provider abstraction, mirroring EmbeddingProvider: the RAG layer only
depends on this, so the model/vendor can change without touching it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class LLMResponse:
    text: str
    model: str | None = None
    # Set when generation stopped early (e.g. "max_output_tokens"), so the
    # caller can tell the user the answer was cut off.
    incomplete_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class LLMError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class LLMProvider(Protocol):
    async def generate(self, instructions: str, user_input: str) -> LLMResponse: ...

"""
Azure OpenAI client for the v1 Responses API:

    POST https://<resource>.openai.azure.com/openai/v1/responses
    api-key: <key>
    {"model": "<deployment name>", "instructions": ..., "input": ...}

Plain httpx rather than the openai SDK: it's one endpoint, and the project
already standardizes on httpx (with the same retry-on-429/5xx approach as
GraphClient). The API key is only ever placed in the request header -- it
is never logged or included in error messages.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from app.llm.base import LLMError, LLMResponse

logger = logging.getLogger("knowledge_agent.llm.azure_openai")

_RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
_MAX_BACKOFF_SECONDS = 30.0


def normalize_endpoint(endpoint: str) -> str:
    """Accepts the base URL or the full .../responses URI; returns the base."""
    base = endpoint.strip().rstrip("/")
    if base.endswith("/responses"):
        base = base[: -len("/responses")]
    return base


def _extract_text(data: dict) -> str:
    # Responses API output: a list of items; assistant text lives in
    # message items as "output_text" content parts. A "refusal" part
    # carries the model's refusal message instead.
    parts: list[str] = []
    for item in data.get("output") or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if content.get("type") == "output_text":
                parts.append(content.get("text", ""))
            elif content.get("type") == "refusal":
                parts.append(content.get("refusal", ""))
    return "".join(parts).strip()


class AzureOpenAIResponsesClient:
    def __init__(
        self,
        endpoint: str,
        api_key: str,
        deployment: str,
        temperature: float = 0.1,
        max_output_tokens: int = 1200,
        timeout_seconds: float = 90.0,
        max_retries: int = 3,
        backoff_base_seconds: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not api_key:
            raise LLMError("AZURE_OPENAI_API_KEY is not set (add it to .env)")
        self.deployment = deployment
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._max_retries = max_retries
        self._backoff_base = backoff_base_seconds
        self._client = httpx.AsyncClient(
            base_url=normalize_endpoint(endpoint),
            headers={"api-key": api_key},
            timeout=timeout_seconds,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _retry_delay(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after:
                try:
                    return min(float(retry_after), _MAX_BACKOFF_SECONDS)
                except ValueError:
                    pass
        return min(self._backoff_base * (2 ** attempt), _MAX_BACKOFF_SECONDS)

    async def generate(self, instructions: str, user_input: str) -> LLMResponse:
        payload = {
            "model": self.deployment,
            "instructions": instructions,
            "input": user_input,
            "temperature": self._temperature,
            "max_output_tokens": self._max_output_tokens,
        }

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.post("/responses", json=payload)
            except httpx.TransportError as exc:
                if attempt < self._max_retries:
                    delay = self._retry_delay(attempt, None)
                    logger.warning("llm_transport_error_retrying error=%s delay=%.1fs", type(exc).__name__, delay)
                    await asyncio.sleep(delay)
                    continue
                raise LLMError(f"could not reach Azure OpenAI: {type(exc).__name__}: {exc}") from exc

            if response.status_code in _RETRYABLE_STATUS_CODES and attempt < self._max_retries:
                delay = self._retry_delay(attempt, response)
                logger.warning("llm_retryable_status status=%s delay=%.1fs", response.status_code, delay)
                await asyncio.sleep(delay)
                continue

            if response.status_code >= 400:
                # Azure's error body describes the problem (bad deployment
                # name, auth failure, content filter) -- safe and useful to
                # surface; it never echoes the key.
                raise LLMError(
                    f"Azure OpenAI returned HTTP {response.status_code}: {response.text[:500]}",
                    status_code=response.status_code,
                )

            data = response.json()
            usage = data.get("usage") or {}
            incomplete = (data.get("incomplete_details") or {}).get("reason")
            return LLMResponse(
                text=_extract_text(data),
                model=data.get("model"),
                incomplete_reason=incomplete,
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
            )

        raise LLMError("Azure OpenAI request failed after retries")  # loop always returns/raises first

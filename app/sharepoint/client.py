"""
Reusable Microsoft Graph HTTP client.

Spec section 13/14: one client, handles auth headers, absolute Graph URLs,
timeouts, retries, 429 throttling, and pagination via @odata.nextLink.
Never logs Authorization headers or download URLs (section 50).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.auth.graph_auth import GraphAuth
from app.config import Settings
from app.observability import GRAPH_REQUESTS, GRAPH_RETRIES, GRAPH_THROTTLED

logger = logging.getLogger("knowledge_agent.graph_client")

_RETRYABLE_STATUS_CODES = {429, 502, 503, 504}


class GraphApiError(RuntimeError):
    """Raised for non-retryable or exhausted-retry Graph API failures."""

    def __init__(self, message: str, status_code: int | None = None, response_body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        # Graph's own error JSON (truncated) -- error bodies describe the
        # failure (bad request shape, permission denied, not found), they
        # don't carry secrets or user data, so this is safe to surface in
        # a dev/debug context even though production-facing endpoints in
        # this app deliberately keep their own error messages generic
        # (spec section 50).
        self.response_body = response_body


class GraphClient:
    """
    Thin async wrapper around httpx for calling Microsoft Graph.

    - get_json / get_bytes accept either a path relative to GRAPH_BASE_URL
      (e.g. "/sites/{id}") or an absolute URL (e.g. an @odata.nextLink or
      @odata.deltaLink), per spec section 13's requirement to handle
      absolute Graph URLs without reconstructing them.
    - get_paged follows @odata.nextLink until it is absent, returning the
      concatenated "value" arrays. It never manually builds the next URL.
    """

    def __init__(self, auth: GraphAuth, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self._auth = auth
        self._settings = settings
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_graph_requests)
        self._client = httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
            transport=transport,
            # GET /drives/{id}/items/{id}/content answers with a 302 to a
            # short-lived pre-authenticated download URL. Without this the
            # redirect body (empty) was being returned as the file content.
            # httpx drops the Authorization header on cross-origin
            # redirects, so our bearer token never goes to the download host.
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _resolve_url(self, endpoint: str) -> str:
        if endpoint.startswith("http://") or endpoint.startswith("https://"):
            return endpoint
        return f"{self._settings.graph_base_url}{endpoint}"

    async def _auth_headers(self) -> dict[str, str]:
        # get_access_token() calls MSAL's acquire_token_for_client(), which is
        # a blocking network call on first use (MSAL caches the token after
        # that). Running it via to_thread keeps a slow first token fetch from
        # freezing the whole async event loop -- including unrelated
        # in-flight requests like GET /sync/latest.
        token = await asyncio.to_thread(self._auth.get_access_token)
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def _backoff_delay(self, attempt: int) -> float:
        return min(
            self._settings.http_backoff_base_seconds * (2 ** (attempt - 1)),
            self._settings.http_max_backoff_seconds,
        )

    async def _request_with_retry(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        attempt = 0
        extra_headers = kwargs.pop("headers", {})
        while True:
            attempt += 1
            try:
                async with self._semaphore:
                    headers = {**(await self._auth_headers()), **extra_headers}
                    response = await self._client.request(method, url, headers=headers, **kwargs)
            except httpx.TransportError as exc:
                # Connection resets, DNS hiccups, timeouts (spec section 43:
                # transient). The exception text can include the URL, which
                # may be a pre-authenticated download URL -- log the type only.
                if attempt > self._settings.http_max_retries:
                    logger.warning(
                        "graph_request_transport_retries_exhausted method=%s error=%s attempts=%d",
                        method, type(exc).__name__, attempt,
                    )
                    raise GraphApiError(
                        f"Graph request failed after {attempt} attempts: {type(exc).__name__}"
                    ) from None
                delay = self._backoff_delay(attempt)
                GRAPH_RETRIES.labels(reason=type(exc).__name__).inc()
                logger.info(
                    "graph_request_retry method=%s error=%s attempt=%d delay_s=%.2f",
                    method, type(exc).__name__, attempt, delay,
                )
                await asyncio.sleep(delay)
                continue

            GRAPH_REQUESTS.labels(method=method, status=str(response.status_code)).inc()
            if response.status_code == 429:
                GRAPH_THROTTLED.inc()

            if response.status_code not in _RETRYABLE_STATUS_CODES:
                return response

            if attempt > self._settings.http_max_retries:
                logger.warning(
                    "graph_request_retries_exhausted method=%s status=%s attempts=%d",
                    method, response.status_code, attempt,
                )
                return response

            retry_after = response.headers.get("Retry-After")
            delay = self._backoff_delay(attempt)
            if retry_after is not None:
                try:
                    # Honor the server's value (spec section 44), but never
                    # sleep longer than the cap on a malformed/huge header.
                    delay = min(max(float(retry_after), 0.0), self._settings.http_max_backoff_seconds)
                except ValueError:
                    pass

            GRAPH_RETRIES.labels(reason=str(response.status_code)).inc()
            logger.info(
                "graph_request_retry method=%s status=%s attempt=%d delay_s=%.2f",
                method, response.status_code, attempt, delay,
            )
            await asyncio.sleep(delay)

    async def get_json(self, endpoint: str) -> dict:
        url = self._resolve_url(endpoint)
        response = await self._request_with_retry("GET", url)
        if response.status_code >= 400:
            logger.error("graph_get_json_failed url_host_only=%s status=%s", httpx.URL(url).host, response.status_code)
            raise GraphApiError(
                f"Graph GET failed with status {response.status_code}",
                status_code=response.status_code,
                response_body=response.text[:1000],
            )
        return response.json()

    async def get_bytes(self, endpoint: str) -> bytes:
        url = self._resolve_url(endpoint)
        response = await self._request_with_retry("GET", url)
        if response.status_code >= 400:
            logger.error("graph_get_bytes_failed url_host_only=%s status=%s", httpx.URL(url).host, response.status_code)
            raise GraphApiError(
                f"Graph content download failed with status {response.status_code}",
                status_code=response.status_code,
                response_body=response.text[:1000],
            )
        return response.content

    async def get_paged(self, endpoint: str) -> list[dict]:
        """
        Follows @odata.nextLink until exhausted, returning every item from
        every page's "value" array. Uses the server-provided nextLink
        verbatim -- never reconstructs it (spec section 14).
        """
        results: list[dict] = []
        url: str | None = self._resolve_url(endpoint)
        pages_fetched = 0

        while url:
            payload = await self.get_json(url)
            results.extend(payload.get("value", []))
            pages_fetched += 1
            url = payload.get("@odata.nextLink")

        logger.debug("graph_get_paged_complete items=%d pages=%d", len(results), pages_fetched)
        return results

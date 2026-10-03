"""
Microsoft Entra ID app-only authentication via MSAL.

Spec section 12: this is the ONLY place token acquisition logic lives.
Individual SharePoint/Graph services must never touch MSAL directly.

Security: never log the client secret, the access token, or any header
that carries it (spec section 5 / 50).
"""
from __future__ import annotations

import logging
import threading

import msal

from app.config import Settings

logger = logging.getLogger("knowledge_agent.auth")


class GraphAuthError(RuntimeError):
    """Raised when an application token cannot be acquired."""


class GraphAuth:
    """
    Wraps a single MSAL ConfidentialClientApplication and exposes a
    synchronous get_access_token() call.

    MSAL's ConfidentialClientApplication maintains its own in-memory token
    cache and will return a cached, non-expired token from
    acquire_token_for_client() without a network round-trip -- so callers
    should simply call get_access_token() per-request rather than trying to
    cache the token themselves.
    """

    def __init__(self, settings: Settings):
        self._settings = settings
        self._lock = threading.Lock()
        self._app = msal.ConfidentialClientApplication(
            client_id=settings.client_id,
            client_credential=settings.client_secret,
            authority=settings.authority,
        )

    def get_access_token(self) -> str:
        """
        Returns a valid app-only access token for Microsoft Graph.

        Raises GraphAuthError with a non-sensitive message on failure --
        the underlying MSAL error payload (which may echo back request
        details) is logged at debug level only, never included verbatim
        in an exception that might surface through an API response.
        """
        with self._lock:
            result = self._app.acquire_token_silent(
                scopes=[self._settings.graph_scope], account=None
            )
            if not result:
                result = self._app.acquire_token_for_client(
                    scopes=[self._settings.graph_scope]
                )

        if not result or "access_token" not in result:
            error_code = (result or {}).get("error", "unknown_error")
            logger.error(
                "graph_auth_failed error_code=%s correlation_id=%s",
                error_code,
                (result or {}).get("correlation_id", "n/a"),
            )
            raise GraphAuthError(
                f"Failed to acquire Graph app-only token (error={error_code}). "
                "Check TENANT_ID/CLIENT_ID/CLIENT_SECRET and that Sites.Selected "
                "has been granted and consented for this app registration."
            )

        return result["access_token"]

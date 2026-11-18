"""
Validates Entra ID user access tokens sent by the SharePoint chat bubble
(SPFx AadHttpClient, see SHAREPOINT_AGENT_INTEGRATION.md section 5).

A token is accepted only if ALL of these hold:
- RS256 signature by a current Microsoft signing key for our tenant (JWKS),
- issued by our tenant (v1.0 "sts.windows.net/<tid>/" or v2.0 issuer),
- audience is our API ("api://<client-id>" or the bare client id),
- not expired (60s clock-skew leeway),
- a *delegated* token carrying the required scope (scp) -- app-only tokens
  have no user and are rejected,
- (if configured) the user holds the required app role, e.g.
  "KnowledgeHub.Chat". Entra puts a user's app roles in the token's `roles`
  claim; the role is assigned to the Entra group that grants access to the
  Knowledge Hub, so only Knowledge Hub users can chat.

The user identity used everywhere downstream comes from these claims, never
from the request body.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable

import jwt

logger = logging.getLogger("knowledge_agent.auth.entra")

_LEEWAY_SECONDS = 60


class ChatAuthError(Exception):
    """Token rejected. The message is for logs; clients get a generic 401."""


class ChatForbiddenError(Exception):
    """
    Valid token for a real user, but without the required app role -> 403.
    Kept separate from ChatAuthError: "who are you?" (401) and "you may not
    use this" (403) need different answers to the user.
    """


@dataclass(frozen=True)
class ChatUser:
    # Entra object id: stable per user, used to own conversations.
    user_id: str
    name: str | None = None
    username: str | None = None


# Given a raw token, return the key to verify it with. Default: Microsoft's
# JWKS endpoint for the tenant; tests inject their own.
SigningKeyResolver = Callable[[str], Any]


def _jwks_resolver(tenant_id: str) -> SigningKeyResolver:
    # PyJWKClient caches keys and refetches when it sees an unknown key id
    # (Microsoft rotates signing keys), so rotation needs no restart.
    client = jwt.PyJWKClient(
        f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys",
        cache_keys=True,
        lifespan=3600,
        timeout=10,
    )
    return lambda token: client.get_signing_key_from_jwt(token).key


def _expand_audiences(audiences: list[str]) -> list[str]:
    # v1.0 tokens carry the App ID URI ("api://<id>"), v2.0 tokens the bare
    # client id -- accept either form of the same app.
    expanded: list[str] = []
    for audience in audiences:
        expanded.append(audience)
        if audience.startswith("api://"):
            expanded.append(audience[len("api://"):])
    return list(dict.fromkeys(expanded))


class EntraTokenValidator:
    def __init__(
        self,
        tenant_id: str,
        audiences: list[str],
        required_scope: str,
        signing_key_resolver: SigningKeyResolver | None = None,
        required_role: str = "",
    ):
        if not audiences:
            raise ValueError("at least one audience is required")
        self._tenant_id = tenant_id
        self._audiences = _expand_audiences(audiences)
        self._issuers = [
            f"https://sts.windows.net/{tenant_id}/",
            f"https://login.microsoftonline.com/{tenant_id}/v2.0",
        ]
        self._required_scope = required_scope
        self._required_role = required_role
        self._resolve_key = signing_key_resolver or _jwks_resolver(tenant_id)

    def _validate(self, token: str) -> ChatUser:
        try:
            key = self._resolve_key(token)
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self._audiences,
                issuer=self._issuers,
                leeway=_LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "aud", "iss"]},
            )
        except jwt.PyJWTError as exc:
            raise ChatAuthError(f"{type(exc).__name__}: {exc}") from exc

        if claims.get("tid") != self._tenant_id:
            raise ChatAuthError("token tenant does not match")
        scopes = (claims.get("scp") or "").split()
        if not scopes:
            raise ChatAuthError("not a delegated user token (no scp claim)")
        if self._required_scope and self._required_scope not in scopes:
            raise ChatAuthError(f"missing required scope '{self._required_scope}'")
        user_id = claims.get("oid")
        if not user_id:
            raise ChatAuthError("token has no oid claim")
        # Checked last, only once the token is otherwise fully valid, so a
        # forged or expired token is always a 401, never a 403.
        if self._required_role:
            roles = claims.get("roles") or []
            if not isinstance(roles, list) or self._required_role not in roles:
                raise ChatForbiddenError(f"user {user_id} lacks app role '{self._required_role}'")

        return ChatUser(
            user_id=user_id,
            name=claims.get("name"),
            username=claims.get("preferred_username") or claims.get("upn") or claims.get("unique_name"),
        )

    async def validate(self, token: str) -> ChatUser:
        # Key resolution may hit the network (JWKS fetch) -- keep it off the loop.
        return await asyncio.to_thread(self._validate, token)

"""
EntraTokenValidator against real RS256 tokens signed with a throwaway key.
Every rejection case here is a way a forged, misdirected or stale token
could otherwise get a user into /chat.
"""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth.entra import ChatAuthError, EntraTokenValidator

TENANT = "00000000-0000-0000-0000-000000000000"
CLIENT_ID = "11111111-1111-1111-1111-111111111111"
V1_ISSUER = f"https://sts.windows.net/{TENANT}/"
V2_ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def make_token(key=_KEY, algorithm="RS256", **overrides) -> str:
    now = int(time.time())
    claims = {
        "aud": f"api://{CLIENT_ID}",
        "iss": V1_ISSUER,
        "tid": TENANT,
        "oid": "user-oid-1",
        "name": "Ada Lovelace",
        "upn": "ada@auximo.example",
        "scp": "user_impersonation",
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=algorithm)


@pytest.fixture
def validator():
    return EntraTokenValidator(
        TENANT, [f"api://{CLIENT_ID}"], "user_impersonation",
        signing_key_resolver=lambda token: _KEY.public_key(),
    )


@pytest.mark.asyncio
async def test_valid_v1_token_yields_user_from_claims(validator):
    user = await validator.validate(make_token())
    assert user.user_id == "user-oid-1"
    assert user.name == "Ada Lovelace"
    assert user.username == "ada@auximo.example"


@pytest.mark.asyncio
async def test_valid_v2_token_with_bare_client_id_audience(validator):
    token = make_token(aud=CLIENT_ID, iss=V2_ISSUER, upn=None, preferred_username="ada@auximo.example")
    user = await validator.validate(token)
    assert user.username == "ada@auximo.example"


@pytest.mark.asyncio
async def test_extra_scopes_are_fine(validator):
    await validator.validate(make_token(scp="openid profile user_impersonation"))


@pytest.mark.parametrize("overrides, reason", [
    ({"aud": "api://some-other-app"}, "token for another API"),
    ({"iss": "https://sts.windows.net/99999999-9999-9999-9999-999999999999/"}, "another tenant's issuer"),
    ({"tid": "99999999-9999-9999-9999-999999999999"}, "another tenant id"),
    ({"exp": int(time.time()) - 3600}, "expired"),
    ({"nbf": int(time.time()) + 3600}, "not yet valid"),
    ({"scp": None}, "app-only token (no user)"),
    ({"scp": "Files.Read"}, "missing required scope"),
    ({"oid": None}, "no user object id"),
    ({"exp": None}, "no expiry"),
])
@pytest.mark.asyncio
async def test_rejected_tokens(validator, overrides, reason):
    with pytest.raises(ChatAuthError):
        await validator.validate(make_token(**overrides))


@pytest.mark.asyncio
async def test_token_signed_with_another_key_is_rejected(validator):
    with pytest.raises(ChatAuthError):
        await validator.validate(make_token(key=_OTHER_KEY))


@pytest.mark.asyncio
async def test_hs256_algorithm_confusion_is_rejected(validator):
    # Classic attack: sign with HMAC using something public as the "secret".
    with pytest.raises(ChatAuthError):
        await validator.validate(make_token(key="public-material-as-secret", algorithm="HS256"))


@pytest.mark.asyncio
async def test_garbage_and_key_resolution_failures_are_auth_errors():
    def failing_resolver(token):
        raise jwt.PyJWKClientError("unable to find a signing key that matches")

    validator = EntraTokenValidator(TENANT, [f"api://{CLIENT_ID}"], "user_impersonation", failing_resolver)
    with pytest.raises(ChatAuthError):
        await validator.validate(make_token())
    with pytest.raises(ChatAuthError):
        await validator.validate("not-a-jwt")


def test_requires_an_audience():
    with pytest.raises(ValueError):
        EntraTokenValidator(TENANT, [], "user_impersonation", lambda t: None)


# --- app role (only Knowledge Hub users may chat) ---------------------------------

from app.auth.entra import ChatForbiddenError  # noqa: E402

ROLE = "KnowledgeHub.Chat"


@pytest.fixture
def role_validator():
    return EntraTokenValidator(
        TENANT, [f"api://{CLIENT_ID}"], "user_impersonation",
        signing_key_resolver=lambda token: _KEY.public_key(), required_role=ROLE,
    )


@pytest.mark.asyncio
async def test_user_with_role_is_accepted(role_validator):
    user = await role_validator.validate(make_token(roles=["Other.Role", ROLE]))
    assert user.user_id == "user-oid-1"


@pytest.mark.parametrize("roles", [None, [], ["Other.Role"], "KnowledgeHub.Chat"])
@pytest.mark.asyncio
async def test_user_without_role_is_forbidden(role_validator, roles):
    # A bare string isn't a valid roles claim, even if it spells the role.
    with pytest.raises(ChatForbiddenError):
        await role_validator.validate(make_token(roles=roles))


@pytest.mark.asyncio
async def test_invalid_token_is_auth_error_even_without_role(role_validator):
    # Forged/expired tokens must never be reported as "authenticated but forbidden".
    with pytest.raises(ChatAuthError):
        await role_validator.validate(make_token(key=_OTHER_KEY))
    with pytest.raises(ChatAuthError):
        await role_validator.validate(make_token(exp=int(time.time()) - 3600))


@pytest.mark.asyncio
async def test_no_role_configured_means_any_tenant_user(validator):
    await validator.validate(make_token())

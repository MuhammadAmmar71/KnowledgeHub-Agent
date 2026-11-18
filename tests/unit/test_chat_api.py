"""
POST /chat over HTTP: authentication modes, error mapping, input limits
and CORS. The lifespan never runs (TestClient without its context manager),
so the chat service and token validator are placed on app.state directly.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

import app.main as main
from app.auth.entra import EntraTokenValidator
from app.chat.service import ChatReply, ConversationNotFoundError
from app.llm.base import LLMError
from tests.unit.test_entra_auth import CLIENT_ID, TENANT, _KEY, make_token

SHAREPOINT = "https://auximotechgmbh.sharepoint.com"


class FakeChatService:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    async def handle(self, user, message, conversation_id):
        self.calls.append((user, message, conversation_id))
        if self.fail:
            raise self.fail
        return ChatReply(
            reply="Use docker push [1].", conversation_id=conversation_id or uuid.uuid4(),
            sources=[{"number": 1, "title": "Guide.md", "url": "https://sp/g", "sections": ["Guide"],
                      "source_type": "file", "cited": True}],
            standalone_question=message, llm_called=True, project_numbers=[], project_scope_fell_back=False,
        )


@pytest.fixture
def make_client(settings, monkeypatch):
    def make(service=None, entra=True, **overrides):
        settings.tenant_id = TENANT
        settings.chat_auth_audience = f"api://{CLIENT_ID}" if entra else ""
        for name, value in overrides.items():
            setattr(settings, name, value)
        monkeypatch.setattr(main, "get_settings", lambda: settings)
        monkeypatch.setattr(main.app.state, "chat_service", service, raising=False)
        monkeypatch.setattr(
            main.app.state, "chat_token_validator",
            EntraTokenValidator(TENANT, [f"api://{CLIENT_ID}"], "user_impersonation", lambda t: _KEY.public_key()),
            raising=False,
        )
        return TestClient(main.app)
    return make


def _auth(token=None):
    return {"Authorization": f"Bearer {token or make_token()}"}


def test_valid_user_token_gets_an_answer_and_identity_comes_from_token(make_client):
    service = FakeChatService()
    client = make_client(service)
    # A spoofed "user" in the body must be ignored.
    response = client.post("/chat", json={"message": "How do I push?", "user": "i:0#.f|membership|ceo@x"}, headers=_auth())

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "Use docker push [1]."
    assert body["sources"][0]["title"] == "Guide.md"
    uuid.UUID(body["conversation_id"])
    user, message, _ = service.calls[0]
    assert user.user_id == "user-oid-1"
    assert message == "How do I push?"


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer not-a-jwt"}, {"X-API-Key": "whatever"}])
def test_missing_or_invalid_token_is_401(make_client, headers):
    client = make_client(FakeChatService())
    response = client.post("/chat", json={"message": "hi"}, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_expired_token_is_401(make_client):
    import time
    client = make_client(FakeChatService())
    response = client.post("/chat", json={"message": "hi"}, headers=_auth(make_token(exp=int(time.time()) - 600)))
    assert response.status_code == 401


def test_user_without_required_role_is_403_and_never_reaches_chat(make_client, monkeypatch):
    service = FakeChatService()
    client = make_client(service)
    monkeypatch.setattr(
        main.app.state, "chat_token_validator",
        EntraTokenValidator(TENANT, [f"api://{CLIENT_ID}"], "user_impersonation", lambda t: _KEY.public_key(),
                            required_role="KnowledgeHub.Chat"),
    )
    denied = client.post("/chat", json={"message": "hi"}, headers=_auth(make_token(roles=["Other"])))
    assert denied.status_code == 403
    assert "access" in denied.json()["detail"]
    assert service.calls == []

    allowed = client.post("/chat", json={"message": "hi"}, headers=_auth(make_token(roles=["KnowledgeHub.Chat"])))
    assert allowed.status_code == 200


def test_dev_mode_uses_api_key_and_shared_identity(make_client):
    service = FakeChatService()
    client = make_client(service, entra=False, api_key=SecretStr("dev-key"))
    assert client.post("/chat", json={"message": "hi"}).status_code == 401
    assert client.post("/chat", json={"message": "hi"}, headers={"X-API-Key": "dev-key"}).status_code == 200
    assert service.calls[0][0].user_id == "local-dev"


def test_conversation_id_is_passed_through(make_client):
    service = FakeChatService()
    conversation_id = uuid.uuid4()
    make_client(service).post("/chat", json={"message": "and then?", "conversation_id": str(conversation_id)}, headers=_auth())
    assert service.calls[0][2] == conversation_id


@pytest.mark.parametrize("payload", [{"message": ""}, {"message": "   "}, {}, {"message": "x" * 2001},
                                     {"message": "hi", "conversation_id": "not-a-uuid"}])
def test_invalid_requests_are_422(make_client, payload):
    service = FakeChatService()
    assert make_client(service).post("/chat", json=payload, headers=_auth()).status_code == 422
    assert service.calls == []


def test_unknown_conversation_is_404(make_client):
    client = make_client(FakeChatService(fail=ConversationNotFoundError()))
    assert client.post("/chat", json={"message": "hi"}, headers=_auth()).status_code == 404


def test_llm_failure_is_502_without_details(make_client):
    client = make_client(FakeChatService(fail=LLMError("HTTP 500: internal azure detail", status_code=500)))
    response = client.post("/chat", json={"message": "hi"}, headers=_auth())
    assert response.status_code == 502
    assert "azure" not in response.text.lower()


def test_chat_not_configured_is_503(make_client):
    assert make_client(None).post("/chat", json={"message": "hi"}, headers=_auth()).status_code == 503


def test_cors_preflight_allows_sharepoint_only(make_client):
    client = make_client(FakeChatService())
    preflight = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization,content-type"}

    allowed = client.options("/chat", headers={"Origin": SHAREPOINT, **preflight})
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == SHAREPOINT
    assert "authorization" in allowed.headers["access-control-allow-headers"].lower()

    denied = client.options("/chat", headers={"Origin": "https://evil.example", **preflight})
    assert "access-control-allow-origin" not in denied.headers


def test_cors_headers_on_actual_response(make_client):
    client = make_client(FakeChatService())
    response = client.post("/chat", json={"message": "hi"}, headers={**_auth(), "Origin": SHAREPOINT})
    assert response.headers["access-control-allow-origin"] == SHAREPOINT


def test_wildcard_cors_origin_is_rejected_by_settings(settings):
    from app.config import Settings
    with pytest.raises(ValueError):
        Settings(**{**settings.model_dump(), "cors_allowed_origins": "https://a.example, *"})

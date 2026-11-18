"""
ChatService against real Postgres: conversation storage and ownership,
turn ordering, and a full two-turn conversation through real retrieval
(fake embeddings and LLM, so no models or network).
"""
import uuid

import pytest
from sqlalchemy import func, select

from app.auth.entra import ChatUser
from app.chat.service import ChatService, ConversationNotFoundError
from app.chat.store import SqlAlchemyConversationRepository
from app.db.database import build_session_factory
from app.db.models import ChatMessage, Conversation
from app.llm.base import LLMError
from tests.conftest import FakeEmbeddingProvider
from tests.integration.test_project_search_postgres import _index
from tests.unit.test_chat_service import ScriptedLLM

ADA = ChatUser(user_id="oid-ada")


@pytest.fixture
def session_factory(engine):
    return build_session_factory(engine)


async def test_repository_ownership_and_turn_order(session_factory):
    async with session_factory() as session:
        repo = SqlAlchemyConversationRepository(session)
        conversation_id = await repo.create("oid-ada")
        for i in range(4):
            await repo.add_message(conversation_id, "user" if i % 2 == 0 else "assistant", f"m{i}")
        await repo.commit()

    async with session_factory() as session:
        repo = SqlAlchemyConversationRepository(session)
        assert await repo.owns(conversation_id, "oid-ada")
        assert not await repo.owns(conversation_id, "oid-bob")
        assert not await repo.owns(uuid.uuid4(), "oid-ada")
        assert [t.content for t in await repo.recent_turns(conversation_id, 3)] == ["m1", "m2", "m3"]


async def test_two_turn_conversation_through_real_retrieval(settings, session_factory):
    provider = FakeEmbeddingProvider()
    async with session_factory() as session:
        await _index(session, provider, "a", "Guide.md", "/Guide.md",
                     "# Required Permissions\nContributor and Container Registry Repository Contributor.")

    settings.rerank_enabled = False
    service = ChatService(settings=settings, session_factory=session_factory, embedding_provider=provider,
                          reranker=None, llm=ScriptedLLM("You need Contributor [1].",
                                                         "What permissions are needed for ACR?",
                                                         "Contributor and Repository Contributor [1]."))

    first = await service.handle(ADA, "What do I need for ACR?", None)
    assert first.sources and first.sources[0]["title"] == "Guide.md" and first.sources[0]["cited"]

    second = await service.handle(ADA, "and the exact roles?", first.conversation_id)
    assert second.conversation_id == first.conversation_id
    assert second.standalone_question == "What permissions are needed for ACR?"

    with pytest.raises(ConversationNotFoundError):
        await service.handle(ChatUser(user_id="oid-bob"), "hi", first.conversation_id)

    async with session_factory() as session:
        rows = (await session.execute(
            select(ChatMessage.role, ChatMessage.standalone_question)
            .where(ChatMessage.conversation_id == first.conversation_id)
            .order_by(ChatMessage.created_at)
        )).all()
    assert rows == [("user", None), ("assistant", None),
                    ("user", "What permissions are needed for ACR?"), ("assistant", None)]


async def test_failed_turn_leaves_nothing_behind(settings, session_factory):
    service = ChatService(settings=settings, session_factory=session_factory, embedding_provider=FakeEmbeddingProvider(),
                          reranker=None, llm=ScriptedLLM(LLMError("down", status_code=503)))
    async with session_factory() as session:
        await _index(session, FakeEmbeddingProvider(), "a", "Guide.md", "/Guide.md", "Some content about ACR.")

    with pytest.raises(LLMError):
        await service.handle(ADA, "ACR?", None)

    async with session_factory() as session:
        # Not even the new conversation row survives: nothing was committed.
        assert await session.scalar(select(func.count()).select_from(Conversation)) == 0

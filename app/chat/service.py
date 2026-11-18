"""
POST /chat logic, independent of FastAPI so it can be tested without HTTP.

One request:
1. resolve the conversation (new, or an existing one owned by this user),
2. rewrite a follow-up into a standalone question using recent turns,
3. answer that question with the same grounded RAG pipeline as `cli ask`
   (scoped hybrid search -> rerank -> GPT-4o with citations),
4. store both turns and commit.

Nothing is stored if answering fails, so a failed request never leaves a
half-written conversation behind.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.auth.entra import ChatUser
from app.chat.store import ConversationRepository, SqlAlchemyConversationRepository
from app.config import Settings
from app.embeddings.base import EmbeddingProvider
from app.llm.base import LLMProvider
from app.rag.answer import RagAnswer, answer_question
from app.rag.condense import condense_question
from app.retrieval.rerank import Reranker

logger = logging.getLogger("knowledge_agent.chat")


class ConversationNotFoundError(Exception):
    """Unknown conversation id, or one owned by another user (deliberately indistinguishable)."""


@dataclass
class ChatReply:
    reply: str
    conversation_id: uuid.UUID
    sources: list[dict]
    standalone_question: str
    llm_called: bool
    project_numbers: list[str]
    project_scope_fell_back: bool


def serialize_sources(answer: RagAnswer) -> list[dict]:
    """Cited sources first (citation order), then retrieved-but-uncited ones."""
    cited = set(answer.cited_numbers)
    ordered = answer.cited_sources + [s for s in answer.sources if s.number not in cited]
    return [
        {
            "number": s.number,
            "title": s.name,
            "url": s.sharepoint_url,
            "sections": s.sections,
            "source_type": s.source_type,
            "cited": s.number in cited,
        }
        for s in ordered
    ]


AnswerFn = Callable[..., Awaitable[RagAnswer]]


class ChatService:
    def __init__(
        self,
        settings: Settings,
        session_factory: Any,
        embedding_provider: EmbeddingProvider,
        reranker: Reranker | None,
        llm: LLMProvider,
        conversation_repo_factory: Callable[[Any], ConversationRepository] = SqlAlchemyConversationRepository,
        answer_fn: AnswerFn = answer_question,
    ):
        self._settings = settings
        self._session_factory = session_factory
        self._embedding_provider = embedding_provider
        self._reranker = reranker
        self._llm = llm
        self._repo_factory = conversation_repo_factory
        self._answer = answer_fn

    async def handle(self, user: ChatUser, message: str, conversation_id: uuid.UUID | None) -> ChatReply:
        settings = self._settings
        started = time.perf_counter()
        async with self._session_factory() as session:
            repo = self._repo_factory(session)
            if conversation_id is not None:
                if not await repo.owns(conversation_id, user.user_id):
                    raise ConversationNotFoundError()
                # Each turn is one user + one assistant message.
                history = await repo.recent_turns(conversation_id, settings.chat_history_turns * 2)
            else:
                conversation_id = await repo.create(user.user_id)
                history = []

            standalone = await condense_question(self._llm, history, message)
            answer = await self._answer(
                session, self._embedding_provider, self._llm, standalone,
                top_k=settings.rag_top_k,
                reranker=self._reranker,
                rerank_candidates=settings.rerank_candidates,
                min_relevance=settings.rerank_min_relevance if self._reranker is not None else None,
            )
            sources = serialize_sources(answer)

            await repo.add_message(
                conversation_id, "user", message,
                standalone_question=standalone if standalone != message else None,
            )
            await repo.add_message(conversation_id, "assistant", answer.answer, sources=sources)
            await repo.commit()

        scope = answer.project_scope
        # Never log the question or answer text: they can contain anything.
        logger.info(
            "chat_answered conversation_id=%s follow_up=%s sources=%d cited=%d llm_called=%s seconds=%.2f",
            conversation_id, bool(history), len(sources), len(answer.cited_numbers),
            answer.llm_called, time.perf_counter() - started,
        )
        return ChatReply(
            reply=answer.answer,
            conversation_id=conversation_id,
            sources=sources,
            standalone_question=standalone,
            llm_called=answer.llm_called,
            project_numbers=scope.project_numbers if scope else [],
            project_scope_fell_back=bool(scope and scope.fell_back),
        )

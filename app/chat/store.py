"""
Conversation persistence for POST /chat, behind the same repository
pattern as app/sync/state.py: a Protocol, a SQLAlchemy implementation, and
an in-memory test double.

Ownership is enforced here: every read takes the caller's user_id, and a
conversation that exists but belongs to someone else is indistinguishable
from one that doesn't exist.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ChatMessage, Conversation


@dataclass
class Turn:
    role: str  # "user" | "assistant"
    content: str


class ConversationRepository(Protocol):
    async def owns(self, conversation_id: uuid.UUID, user_id: str) -> bool: ...

    async def create(self, user_id: str) -> uuid.UUID: ...

    async def recent_turns(self, conversation_id: uuid.UUID, limit: int) -> list[Turn]:
        """The last `limit` messages, oldest first."""
        ...

    async def add_message(
        self,
        conversation_id: uuid.UUID,
        role: str,
        content: str,
        standalone_question: str | None = None,
        sources: list[dict] | None = None,
    ) -> None: ...

    async def commit(self) -> None: ...


class SqlAlchemyConversationRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def owns(self, conversation_id: uuid.UUID, user_id: str) -> bool:
        stmt = select(Conversation.id).where(Conversation.id == conversation_id, Conversation.user_id == user_id)
        return (await self._session.execute(stmt)).first() is not None

    async def create(self, user_id: str) -> uuid.UUID:
        conversation = Conversation(id=uuid.uuid4(), user_id=user_id)
        self._session.add(conversation)
        await self._session.flush()
        return conversation.id

    async def recent_turns(self, conversation_id: uuid.UUID, limit: int) -> list[Turn]:
        if limit <= 0:
            return []
        stmt = (
            select(ChatMessage.role, ChatMessage.content)
            .where(ChatMessage.conversation_id == conversation_id)
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()
        return [Turn(role=role, content=content) for role, content in reversed(rows)]

    async def add_message(
        self,
        conversation_id: uuid.UUID,
        role: str,
        content: str,
        standalone_question: str | None = None,
        sources: list[dict] | None = None,
    ) -> None:
        self._session.add(ChatMessage(
            id=uuid.uuid4(),
            conversation_id=conversation_id,
            role=role,
            content=content,
            standalone_question=standalone_question,
            sources=sources or [],
            # Explicit timestamp, not server_default: a user turn and its
            # answer land in one transaction, where now() is identical for
            # both -- ordering would then fall to the random uuid.
            created_at=datetime.now(timezone.utc),
        ))
        await self._session.execute(
            update(Conversation).where(Conversation.id == conversation_id).values(updated_at=datetime.now(timezone.utc))
        )
        await self._session.flush()

    async def commit(self) -> None:
        await self._session.commit()


@dataclass
class _StoredConversation:
    user_id: str
    messages: list[dict] = field(default_factory=list)


class InMemoryConversationRepository:
    """Test double -- mirrors SqlAlchemyConversationRepository without a database."""

    def __init__(self) -> None:
        self.conversations: dict[uuid.UUID, _StoredConversation] = {}

    async def owns(self, conversation_id: uuid.UUID, user_id: str) -> bool:
        stored = self.conversations.get(conversation_id)
        return stored is not None and stored.user_id == user_id

    async def create(self, user_id: str) -> uuid.UUID:
        conversation_id = uuid.uuid4()
        self.conversations[conversation_id] = _StoredConversation(user_id=user_id)
        return conversation_id

    async def recent_turns(self, conversation_id: uuid.UUID, limit: int) -> list[Turn]:
        if limit <= 0:
            return []
        messages = self.conversations[conversation_id].messages[-limit:]
        return [Turn(role=m["role"], content=m["content"]) for m in messages]

    async def add_message(
        self,
        conversation_id: uuid.UUID,
        role: str,
        content: str,
        standalone_question: str | None = None,
        sources: list[dict] | None = None,
    ) -> None:
        self.conversations[conversation_id].messages.append({
            "role": role, "content": content,
            "standalone_question": standalone_question, "sources": sources or [],
        })

    async def commit(self) -> None:
        pass

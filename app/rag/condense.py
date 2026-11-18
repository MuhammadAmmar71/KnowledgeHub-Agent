"""
Follow-up questions in a conversation ("and who can push to it?") can't be
searched as-is. This rewrites the latest message into a standalone question
using the conversation so far; retrieval and answering then use only that
question.

The conversation is used ONLY to resolve references -- the rewritten
question goes through normal retrieval, and the answer is grounded in
freshly retrieved excerpts. Earlier answers are never a source of facts
(spec section 38).
"""
from __future__ import annotations

import logging

from app.chat.store import Turn
from app.llm.base import LLMError, LLMProvider

logger = logging.getLogger("knowledge_agent.rag.condense")

CONDENSE_INSTRUCTIONS = """\
You rewrite the user's latest message in a conversation into one standalone question that can be \
understood without the conversation.

Rules:
- Resolve references like "it", "that project", "the second step" using the conversation.
- Keep names, technical terms and numbers exactly as written, especially 8-digit project numbers.
- Do not answer the question. Do not add facts, assumptions or details the conversation doesn't contain.
- Keep the language of the latest message.
- If the latest message is already standalone or starts a new topic, return it unchanged.
- Output only the question, with no preamble or quotes."""

# Earlier answers can be long; enough of each to resolve references.
_ASSISTANT_TURN_CHARS = 600


def build_condense_input(history: list[Turn], message: str) -> str:
    lines = ["Conversation:"]
    for turn in history:
        text = turn.content.strip()
        if turn.role == "assistant" and len(text) > _ASSISTANT_TURN_CHARS:
            text = text[:_ASSISTANT_TURN_CHARS].rstrip() + " …"
        lines.append(f"{'User' if turn.role == 'user' else 'Assistant'}: {text}")
    lines.append("")
    lines.append(f"Latest message: {message}")
    return "\n".join(lines)


async def condense_question(llm: LLMProvider, history: list[Turn], message: str) -> str:
    """Standalone version of `message`; `message` itself if there's no history or rewriting fails."""
    if not history:
        return message
    try:
        response = await llm.generate(CONDENSE_INSTRUCTIONS, build_condense_input(history, message))
    except LLMError as exc:
        # Degrade, don't fail: the original message is still a valid query,
        # just possibly a vaguer one.
        logger.warning("condense_failed status=%s -- using the original message", exc.status_code)
        return message
    rewritten = response.text.strip().strip('"').strip()
    # Guard against the model answering instead of rewriting.
    if not rewritten or len(rewritten) > 3 * len(message) + 300:
        logger.warning("condense_output_rejected length=%d -- using the original message", len(rewritten))
        return message
    return rewritten

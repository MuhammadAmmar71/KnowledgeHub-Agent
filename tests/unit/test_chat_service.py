import uuid
from contextlib import asynccontextmanager

import pytest

from app.auth.entra import ChatUser
from app.chat.service import ChatService, ConversationNotFoundError, serialize_sources
from app.chat.store import InMemoryConversationRepository, Turn
from app.llm.base import LLMError, LLMResponse
from app.rag.answer import RagAnswer, Source
from app.rag.condense import CONDENSE_INSTRUCTIONS, build_condense_input, condense_question
from app.retrieval.search import ProjectScope
from tests.conftest import FakeEmbeddingProvider

ADA = ChatUser(user_id="oid-ada", name="Ada")
BOB = ChatUser(user_id="oid-bob", name="Bob")


class ScriptedLLM:
    """Returns queued texts in order (or raises queued exceptions); records every call."""

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls: list[tuple[str, str]] = []

    async def generate(self, instructions, user_input):
        self.calls.append((instructions, user_input))
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return LLMResponse(text=output)


def _source(number, name, cited=False):
    return Source(number=number, document_id=uuid.uuid4(), name=name, sharepoint_url=f"https://sp/{name}",
                  path=None, source_type="file", sections=["Guide"])


class FakeAnswer:
    """Stands in for answer_question; records the question it was asked."""

    def __init__(self, text="Use docker push [1].", scope=None, fail=None):
        self.questions: list[str] = []
        self.text, self.scope, self.fail = text, scope, fail

    async def __call__(self, session, provider, llm, question, **kwargs):
        self.questions.append(question)
        if self.fail:
            raise self.fail
        sources = [_source(1, "Guide.md"), _source(2, "Other.md")]
        return RagAnswer(question=question, answer=self.text, sources=sources, cited_numbers=[1],
                         project_scope=self.scope)


@asynccontextmanager
async def _no_session():
    yield None


def _service(settings, llm, answer, repo):
    return ChatService(
        settings=settings, session_factory=_no_session, embedding_provider=FakeEmbeddingProvider(),
        reranker=None, llm=llm, conversation_repo_factory=lambda session: repo, answer_fn=answer,
    )


@pytest.fixture
def repo():
    return InMemoryConversationRepository()


@pytest.mark.asyncio
async def test_first_message_creates_conversation_and_stores_both_turns(settings, repo):
    llm, answer = ScriptedLLM(), FakeAnswer()
    reply = await _service(settings, llm, answer, repo).handle(ADA, "How do I push to ACR?", None)

    assert llm.calls == []  # no history -> no rewrite call
    assert answer.questions == ["How do I push to ACR?"]
    stored = repo.conversations[reply.conversation_id]
    assert stored.user_id == "oid-ada"
    assert [(m["role"], m["content"]) for m in stored.messages] == [
        ("user", "How do I push to ACR?"), ("assistant", "Use docker push [1]."),
    ]
    assert stored.messages[0]["standalone_question"] is None


@pytest.mark.asyncio
async def test_follow_up_is_rewritten_with_history_then_answered(settings, repo):
    answer = FakeAnswer()
    first = await _service(settings, ScriptedLLM(), answer, repo).handle(ADA, "How do I push to ACR?", None)

    llm = ScriptedLLM("Which permissions are needed to push images to ACR?")
    reply = await _service(settings, llm, answer, repo).handle(ADA, "and which permissions?", first.conversation_id)

    instructions, condense_input = llm.calls[0]
    assert instructions == CONDENSE_INSTRUCTIONS
    assert "User: How do I push to ACR?" in condense_input and "Latest message: and which permissions?" in condense_input
    # Retrieval/answering used the standalone question, not the fragment.
    assert answer.questions[-1] == "Which permissions are needed to push images to ACR?"
    assert reply.standalone_question == "Which permissions are needed to push images to ACR?"
    user_turn = repo.conversations[first.conversation_id].messages[2]
    assert user_turn["content"] == "and which permissions?"
    assert user_turn["standalone_question"] == "Which permissions are needed to push images to ACR?"


@pytest.mark.asyncio
async def test_another_users_conversation_is_not_found(settings, repo):
    first = await _service(settings, ScriptedLLM(), FakeAnswer(), repo).handle(ADA, "q", None)
    with pytest.raises(ConversationNotFoundError):
        await _service(settings, ScriptedLLM(), FakeAnswer(), repo).handle(BOB, "q2", first.conversation_id)
    with pytest.raises(ConversationNotFoundError):
        await _service(settings, ScriptedLLM(), FakeAnswer(), repo).handle(ADA, "q2", uuid.uuid4())


@pytest.mark.asyncio
async def test_failed_answer_stores_no_turns(settings, repo):
    first = await _service(settings, ScriptedLLM(), FakeAnswer(), repo).handle(ADA, "q", None)
    failing = FakeAnswer(fail=LLMError("down", status_code=503))
    with pytest.raises(LLMError):
        await _service(settings, ScriptedLLM("rewritten"), failing, repo).handle(ADA, "q2", first.conversation_id)
    assert len(repo.conversations[first.conversation_id].messages) == 2


@pytest.mark.asyncio
async def test_history_window_is_limited(settings, repo):
    settings.chat_history_turns = 1
    service_answer = FakeAnswer()
    conv = (await _service(settings, ScriptedLLM(), service_answer, repo).handle(ADA, "first question", None)).conversation_id
    await _service(settings, ScriptedLLM("second standalone"), service_answer, repo).handle(ADA, "second", conv)

    llm = ScriptedLLM("third standalone")
    await _service(settings, llm, service_answer, repo).handle(ADA, "third", conv)
    condense_input = llm.calls[0][1]
    assert "first question" not in condense_input  # only the last turn (2 messages) is used
    assert "User: second" in condense_input


@pytest.mark.asyncio
async def test_project_scope_is_reported(settings, repo):
    answer = FakeAnswer(scope=ProjectScope(["12345678"], explicit=False, fell_back=True))
    reply = await _service(settings, ScriptedLLM(), answer, repo).handle(ADA, "owner of 12345678?", None)
    assert reply.project_numbers == ["12345678"]
    assert reply.project_scope_fell_back is True


def test_serialize_sources_puts_cited_first():
    answer = RagAnswer(question="q", answer="x [2]", sources=[_source(1, "a.md"), _source(2, "b.md")], cited_numbers=[2])
    assert [(s["number"], s["cited"]) for s in serialize_sources(answer)] == [(2, True), (1, False)]


# --- condense --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_condense_without_history_never_calls_llm():
    llm = ScriptedLLM()
    assert await condense_question(llm, [], "standalone?") == "standalone?"
    assert llm.calls == []


@pytest.mark.asyncio
async def test_condense_falls_back_to_original_on_llm_error():
    llm = ScriptedLLM(LLMError("boom", status_code=500))
    assert await condense_question(llm, [Turn("user", "earlier")], "and that?") == "and that?"


@pytest.mark.asyncio
async def test_condense_rejects_an_answer_instead_of_a_question():
    llm = ScriptedLLM("Here is a long answer. " * 50)
    assert await condense_question(llm, [Turn("user", "earlier")], "and that?") == "and that?"


@pytest.mark.asyncio
async def test_condense_strips_quotes():
    llm = ScriptedLLM('"What permissions does ACR need?"')
    assert await condense_question(llm, [Turn("user", "earlier")], "and perms?") == "What permissions does ACR need?"


def test_condense_input_truncates_long_assistant_turns():
    text = build_condense_input([Turn("user", "q1"), Turn("assistant", "a" * 2000)], "q2")
    assert "a" * 600 in text and "a" * 700 not in text
    assert text.endswith("Latest message: q2")


def test_condense_prompt_forbids_answering_and_adding_facts():
    lowered = CONDENSE_INSTRUCTIONS.lower()
    assert "do not answer" in lowered and "do not add facts" in lowered and "project numbers" in lowered

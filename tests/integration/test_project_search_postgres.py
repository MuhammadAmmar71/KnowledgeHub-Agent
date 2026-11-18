"""
Project-number scoping against real Postgres: the column round-trip, the
array-overlap filter (&&), and the "does this project have any content"
check that decides between restricting and falling back.
"""
import pytest

from app.chunking.chunker import Chunker
from app.db.database import build_session_factory
from app.db.models import ProcessingStatus, SourceType
from app.retrieval.search import SearchFilters, scoped_search
from app.sync.state import DocumentUpsert, SqlAlchemyChunkRepository, SqlAlchemyDocumentRepository
from tests.conftest import FakeEmbeddingProvider


async def _index(session, provider, item_id: str, name: str, path: str, text: str) -> None:
    doc_repo, chunk_repo = SqlAlchemyDocumentRepository(session), SqlAlchemyChunkRepository(session)
    doc = await doc_repo.upsert(DocumentUpsert(
        source_type=SourceType.file, sharepoint_item_id=item_id, drive_id="d", site_id="s",
        name=name, extension=".md", path=path, sharepoint_url=f"https://sp/{item_id}",
        parent_sharepoint_item_id=None, modified_at=None, etag=None, size=len(text),
        content_hash=f"hash-{item_id}", processing_status=ProcessingStatus.indexed,
    ))
    chunks = Chunker().chunk(text, doc.id, doc.content_hash, {"name": name, "path": path})
    await chunk_repo.replace_chunks(doc.id, chunks)
    vectors = await provider.embed([c.content for c in chunks])
    await chunk_repo.set_embeddings(doc.id, {c.id: v for c, v in zip(chunks, vectors)}, provider.model_name)
    await doc_repo.commit()


@pytest.fixture
async def indexed(engine):
    provider = FakeEmbeddingProvider()
    session_factory = build_session_factory(engine)
    async with session_factory() as session:
        # Tagged through its folder path only -- the text never mentions it.
        await _index(session, provider, "a", "Guide.md", "/02_Dev/10000001---pulse/Guide.md",
                     "Deployment uses the container registry and a container app.")
        # Tagged through its text (glued to letters).
        await _index(session, provider, "b", "Infra.md", "/02_Dev/infra/Infra.md",
                     "Deployment uses registry 10035001infraacr and a container app.")
        await _index(session, provider, "c", "General.md", "/General.md",
                     "Deployment uses a container registry in general.")
    return provider, session_factory


async def test_project_numbers_round_trip(indexed):
    _, session_factory = indexed
    async with session_factory() as session:
        repo = SqlAlchemyChunkRepository(session)
        doc = await SqlAlchemyDocumentRepository(session).get_by_sharepoint_item_id(SourceType.file, "a")
        assert [c.project_numbers for c in await repo.load_chunks(doc.id)] == [["10000001"]]


async def test_question_with_project_number_only_returns_that_project(indexed):
    provider, session_factory = indexed
    async with session_factory() as session:
        found = await scoped_search(session, provider, "deployment registry for 10000001", top_k=10)
    assert found.scope.fell_back is False
    assert {r.name for r in found.results} == {"Guide.md"}


async def test_explicit_filter_matches_number_glued_to_text(indexed):
    provider, session_factory = indexed
    async with session_factory() as session:
        found = await scoped_search(
            session, provider, "deployment", top_k=10, filters=SearchFilters(project_numbers=["10035001"])
        )
    assert {r.name for r in found.results} == {"Infra.md"}


async def test_unknown_project_falls_back_to_all_content(indexed):
    provider, session_factory = indexed
    async with session_factory() as session:
        found = await scoped_search(session, provider, "deployment registry for 12345678", top_k=10)
    assert found.scope.fell_back is True
    assert {r.name for r in found.results} == {"Guide.md", "Infra.md", "General.md"}

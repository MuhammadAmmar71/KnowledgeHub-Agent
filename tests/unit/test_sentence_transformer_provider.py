"""
SentenceTransformerProvider against a stub model class -- verifies the
wiring (lazy load, normalization, off-loop execution, output shape)
without importing torch or downloading weights.
"""
import sys
import threading
import types

import numpy as np
import pytest

from app.embeddings.sentence_transformer import SentenceTransformerProvider


class _StubModel:
    instances = 0

    cached = True
    load_calls: list = []

    def __init__(self, model_name, device, local_files_only=False):
        _StubModel.load_calls.append(local_files_only)
        if local_files_only and not _StubModel.cached:
            raise OSError("not in local cache")
        _StubModel.instances += 1
        self.model_name = model_name
        self.device = device
        self.encode_kwargs = None
        self.encode_thread = None

    def get_sentence_embedding_dimension(self):
        return 4

    def encode(self, texts, **kwargs):
        self.encode_kwargs = kwargs
        self.encode_thread = threading.current_thread()
        return np.ones((len(texts), 4), dtype=np.float32)


@pytest.fixture
def stub_sentence_transformers(monkeypatch):
    _StubModel.instances = 0
    _StubModel.cached = True
    _StubModel.load_calls = []
    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = _StubModel
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    return _StubModel


@pytest.mark.asyncio
async def test_model_is_loaded_lazily_and_only_once(stub_sentence_transformers):
    provider = SentenceTransformerProvider("some/model", device="cpu", batch_size=8)
    assert stub_sentence_transformers.instances == 0

    await provider.embed(["a"])
    await provider.embed(["b"])
    assert provider.dimensions == 4
    assert stub_sentence_transformers.instances == 1


@pytest.mark.asyncio
async def test_embed_normalizes_runs_off_event_loop_and_returns_lists(stub_sentence_transformers):
    provider = SentenceTransformerProvider("some/model", batch_size=8)
    vectors = await provider.embed(["a", "b"])

    model = provider._get_model()
    assert vectors == [[1.0] * 4, [1.0] * 4]
    assert all(isinstance(v, list) for v in vectors)
    assert model.encode_kwargs["normalize_embeddings"] is True
    assert model.encode_kwargs["batch_size"] == 8
    assert model.encode_thread is not threading.main_thread()


@pytest.mark.asyncio
async def test_empty_input_does_not_load_model(stub_sentence_transformers):
    provider = SentenceTransformerProvider("some/model")
    assert await provider.embed([]) == []
    assert stub_sentence_transformers.instances == 0


@pytest.mark.asyncio
async def test_cached_model_loads_without_network(stub_sentence_transformers):
    await SentenceTransformerProvider("some/model").embed(["a"])
    assert stub_sentence_transformers.load_calls == [True]


@pytest.mark.asyncio
async def test_uncached_model_falls_back_to_download(stub_sentence_transformers):
    stub_sentence_transformers.cached = False
    await SentenceTransformerProvider("some/model").embed(["a"])
    assert stub_sentence_transformers.load_calls == [True, False]
    assert stub_sentence_transformers.instances == 1

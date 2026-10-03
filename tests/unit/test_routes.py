"""
Regression test for the /sync/latest vs /sync/{sync_id} route-ordering bug:
FastAPI/Starlette matches routes in declaration order, so a static path
segment must be registered before a parameterized sibling that would
otherwise swallow it. This doesn't hit the database -- it only inspects
the app's route table.
"""
from app.main import app


def _route_index(path: str) -> int:
    for i, route in enumerate(app.routes):
        if getattr(route, "path", None) == path:
            return i
    raise AssertionError(f"route {path} not registered")


def test_sync_latest_is_declared_before_sync_id_param_route():
    assert _route_index("/sync/latest") < _route_index("/sync/{sync_id}")


def test_embedding_routes_registered():
    _route_index("/embeddings/run")
    _route_index("/embeddings/status")

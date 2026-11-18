"""
Regression test for the /sync/latest vs /sync/{sync_id} route-ordering bug:
FastAPI/Starlette matches routes in declaration order, so a static path
segment must be registered before a parameterized sibling that would
otherwise swallow it. This doesn't hit the database -- it only inspects
the app's route table.
"""
from app.main import app


def _flat_routes(routes) -> list:
    # Newer FastAPI keeps an included router as one nested entry
    # (original_router) instead of copying its routes into the app; matching
    # still walks them in this same depth-first order.
    flat = []
    for route in routes:
        nested = getattr(route, "original_router", None)
        flat.extend(_flat_routes(nested.routes) if nested is not None else [route])
    return flat


def _route_index(path: str) -> int:
    for i, route in enumerate(_flat_routes(app.routes)):
        if getattr(route, "path", None) == path:
            return i
    raise AssertionError(f"route {path} not registered")


def test_sync_latest_is_declared_before_sync_id_param_route():
    assert _route_index("/sync/latest") < _route_index("/sync/{sync_id}")


def test_embedding_routes_registered():
    _route_index("/embeddings/run")
    _route_index("/embeddings/status")


def test_hardening_routes_registered():
    for path in ("/sync/reconcile", "/status", "/metrics", "/health"):
        _route_index(path)

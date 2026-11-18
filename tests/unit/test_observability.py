import json
import logging

from app.observability import _ContextFilter, _JsonFormatter, _RedactingTextFormatter, current_sync_id, redact


def test_redacts_bearer_tokens_jwts_url_queries_and_secrets():
    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJl"
    message = (
        f"Authorization: Bearer abc.def-ghi token={jwt} "
        "url=https://tenant.sharepoint.com/_layouts/download.aspx?UniqueId=1&tempauth=SECRETVALUE "
        "client_secret=hunter2 api-key: k3y"
    )
    out = redact(message)
    for leaked in ("abc.def-ghi", jwt, "SECRETVALUE", "UniqueId", "hunter2", "k3y"):
        assert leaked not in out
    assert "https://tenant.sharepoint.com/_layouts/download.aspx?[REDACTED]" in out


def test_redact_leaves_ordinary_messages_alone():
    message = "sync_run_completed sync_type=delta status=success added=3"
    assert redact(message) == message


def _record(msg: str) -> logging.LogRecord:
    record = logging.LogRecord("knowledge_agent.test", logging.INFO, __file__, 1, msg, None, None)
    _ContextFilter().filter(record)
    return record


def test_json_formatter_includes_sync_id_and_redacts():
    token = current_sync_id.set("run-123")
    try:
        line = _JsonFormatter().format(_record("download failed Bearer abc123"))
    finally:
        current_sync_id.reset(token)
    payload = json.loads(line)
    assert payload["sync_id"] == "run-123"
    assert payload["level"] == "INFO"
    assert "abc123" not in payload["message"]


def test_text_formatter_appends_sync_id_only_inside_a_run():
    formatter = _RedactingTextFormatter("%(message)s")
    assert formatter.format(_record("outside")) == "outside"
    token = current_sync_id.set("run-9")
    try:
        assert formatter.format(_record("inside")) == "inside sync_id=run-9"
    finally:
        current_sync_id.reset(token)

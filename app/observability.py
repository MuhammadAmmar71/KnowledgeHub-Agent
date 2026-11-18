"""
Logging and metrics (spec sections 46/47).

Logging: one configure_logging() call sets the format (key=value text or
one JSON object per line), stamps every record with the current sync_id
(a contextvar the job runner sets for the duration of a run, so lines from
deep inside the Graph client or a processor are still attributable), and
runs every line through a redaction filter as a last line of defense for
spec section 50 -- code should never log a secret in the first place, but
if a library or an exception message does, it gets scrubbed here.

Metrics: Prometheus collectors, exposed by GET /metrics. They're
process-local, so they describe what this server process has done; counts
that live in the database (documents, chunks, last sync) are read fresh at
scrape time by the endpoint instead.
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
from datetime import datetime, timezone

from prometheus_client import Counter, Gauge, Histogram

current_sync_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_sync_id", default=None)

# (pattern, replacement). Bearer tokens, JWT-shaped strings, the query
# string of any URL (Graph's pre-authenticated download URLs carry their
# credential there, e.g. tempauth=...), and key=value secrets.
_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"), "[REDACTED_JWT]"),
    (re.compile(r"(https?://[^\s?#\"']+)\?[^\s\"']*"), r"\1?[REDACTED]"),
    (
        re.compile(r"(?i)\b(client_secret|access_token|api[-_]?key|password|authorization)(\s*[=:]\s*)[^\s,;&\"']+"),
        r"\1\2[REDACTED]",
    ),
]


def redact(message: str) -> str:
    for pattern, replacement in _REDACTIONS:
        message = pattern.sub(replacement, message)
    return message


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.sync_id = current_sync_id.get()
        return True


class _RedactingTextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        sync_id = getattr(record, "sync_id", None)
        if sync_id:
            line = f"{line} sync_id={sync_id}"
        return redact(line)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        sync_id = getattr(record, "sync_id", None)
        if sync_id:
            payload["sync_id"] = sync_id
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, default=str))


def configure_logging(level: str = "INFO", fmt: str = "text") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(_ContextFilter())
    if fmt == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(_RedactingTextFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # httpx logs every request URL at INFO. Following the /content redirect
    # means the target is a temporary download URL carrying auth material in
    # its query string, which must never be logged (spec section 50).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


# --- Metrics ---------------------------------------------------------------

GRAPH_REQUESTS = Counter(
    "knowledge_agent_graph_requests_total", "Microsoft Graph HTTP responses received.", ["method", "status"]
)
GRAPH_THROTTLED = Counter(
    "knowledge_agent_graph_throttled_total", "Graph responses with HTTP 429 (throttling)."
)
GRAPH_RETRIES = Counter(
    "knowledge_agent_graph_retries_total", "Graph requests retried, by reason.", ["reason"]
)
SYNC_RUNS = Counter(
    "knowledge_agent_sync_runs_total", "Finished sync runs.", ["sync_type", "status"]
)
SYNC_DURATION = Histogram(
    "knowledge_agent_sync_duration_seconds", "Sync run wall-clock duration.", ["sync_type"],
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600, 7200),
)
SYNC_ITEMS = Counter(
    "knowledge_agent_sync_items_total", "Items handled by sync runs, by outcome.", ["sync_type", "outcome"]
)
EXTRACTION_FAILURES = Counter(
    "knowledge_agent_extraction_failures_total", "Documents whose text extraction failed.", ["extension"]
)
EMBEDDING_BATCH_SECONDS = Histogram(
    "knowledge_agent_embedding_batch_seconds", "Latency of one embedding provider call.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
EMBEDDING_DOCUMENTS = Counter(
    "knowledge_agent_embedding_documents_total", "Documents processed by the embedding stage.", ["outcome"]
)
RETRIEVAL_SECONDS = Histogram(
    "knowledge_agent_retrieval_seconds", "Latency of search_chunks.", ["mode"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
RERANK_SECONDS = Histogram(
    "knowledge_agent_rerank_seconds", "Latency of cross-encoder reranking of a candidate pool.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)
CHAT_REQUESTS = Counter(
    "knowledge_agent_chat_requests_total", "POST /chat requests by outcome.", ["outcome"]
)
CHAT_SECONDS = Histogram(
    "knowledge_agent_chat_seconds", "End-to-end latency of answered POST /chat requests.",
    buckets=(0.5, 1, 2, 3, 5, 8, 13, 20, 30, 60),
)
LLM_REQUEST_SECONDS = Histogram(
    "knowledge_agent_llm_request_seconds", "Latency of LLM answer generation calls.",
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120),
)

# Database-backed gauges, refreshed by GET /metrics at scrape time.
DOCUMENTS = Gauge(
    "knowledge_agent_documents", "Tracked (non-deleted) documents by processing status.", ["status"]
)
CHUNKS = Gauge("knowledge_agent_chunks", "Chunks of non-deleted documents, by whether they are embedded.", ["embedded"])
LAST_SUCCESSFUL_SYNC = Gauge(
    "knowledge_agent_last_successful_sync_timestamp_seconds",
    "Completion time of the last successful (or partial) sync run, by source.", ["source"],
)
SYNC_RUNNING = Gauge("knowledge_agent_sync_running", "Sync runs currently marked running, by source.", ["source"])

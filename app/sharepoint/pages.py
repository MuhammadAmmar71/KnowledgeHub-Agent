"""
SharePoint Site Pages -- a deliberately separate module from
sharepoint/drives.py (spec section 7): pages are a different Graph
resource (sitePage, reached via /sites/{id}/pages) with a different,
uncertain permission story. Current Microsoft Graph documentation lists
Sites.Read.All as the least-privileged permission for listing sitePage
resources, which is broader than the Sites.Selected this app otherwise
uses for the document library. This module must never assume that works;
callers MUST run check_page_capability() first and stop cleanly (not
auto-escalate permissions, per Rule 6) if it fails.
"""
from __future__ import annotations

import html as html_lib
import logging
import re
from dataclasses import dataclass

from app.sharepoint.client import GraphApiError, GraphClient

logger = logging.getLogger("knowledge_agent.pages")

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t]+")


@dataclass
class PageCapabilityResult:
    available: bool
    http_status: int | None
    detail: str


async def check_page_capability(client: GraphClient, site_id: str) -> PageCapabilityResult:
    """
    Spec section 7: a capability test, run before any page listing/sync is
    attempted. $top=1 keeps this cheap regardless of how many pages exist.
    Distinguishes a permission failure (401/403) -- the expected, documented
    risk -- from any other unexpected Graph error, so the caller can report
    a clear, specific reason rather than a generic failure.
    """
    try:
        await client.get_json(f"/sites/{site_id}/pages/microsoft.graph.sitePage?$top=1")
        return PageCapabilityResult(available=True, http_status=200, detail="Page listing succeeded.")
    except GraphApiError as exc:
        if exc.status_code in (401, 403):
            return PageCapabilityResult(
                available=False,
                http_status=exc.status_code,
                detail=(
                    "Permission denied listing SharePoint pages. Current Microsoft Graph "
                    "documentation lists Sites.Read.All as the least-privileged application "
                    "permission for sitePage listing, which is broader than the Sites.Selected "
                    "this app currently uses. Per the implementation spec (section 7), this is "
                    "not auto-escalated -- enabling page sync requires an explicit, approved "
                    "decision to grant Sites.Read.All (or to confirm a narrower permission "
                    "actually works, which current docs say it does not)."
                ),
            )
        return PageCapabilityResult(
            available=False, http_status=exc.status_code, detail=f"Unexpected Graph error: {exc}"
        )


async def list_pages(client: GraphClient, site_id: str) -> list[dict]:
    """Full listing only -- Graph has no documented delta endpoint for sitePage resources."""
    return await client.get_paged(f"/sites/{site_id}/pages/microsoft.graph.sitePage")


async def get_page_content(client: GraphClient, site_id: str, page_id: str) -> dict:
    """Fetches one page's full detail, including its canvasLayout (the web parts)."""
    return await client.get_json(f"/sites/{site_id}/pages/{page_id}/microsoft.graph.sitePage?expand=canvasLayout")


def _strip_html(fragment: str) -> str:
    text = _TAG_RE.sub(" ", fragment or "")
    text = html_lib.unescape(text)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()


def _extract_text_chunks(node: object) -> list[str]:
    """
    Walks canvasLayout generically rather than assuming one exact schema
    (horizontalSections -> columns -> webparts -> innerHtml, in the
    versions of the API seen so far) -- webPart shapes have drifted across
    Graph API versions, and a schema-strict walk risks silently extracting
    nothing if a section/column key is named differently than expected.
    Spec section 32: preserve meaningful textual content, never embed raw
    Graph JSON as the stored representation.
    """
    chunks: list[str] = []
    if isinstance(node, dict):
        inner_html = node.get("innerHtml")
        if isinstance(inner_html, str) and inner_html.strip():
            chunks.append(_strip_html(inner_html))
        for value in node.values():
            chunks.extend(_extract_text_chunks(value))
    elif isinstance(node, list):
        for item in node:
            chunks.extend(_extract_text_chunks(item))
    return chunks


def normalize_page_content(detail: dict) -> str:
    """
    Spec section 32's recommended representation. This is a first-pass,
    generic extraction (see _extract_text_chunks) -- good enough to prove
    the page-sync pipeline end-to-end and make pages searchable, but a
    schema-verified walk of canvasLayout (once its real shape is confirmed
    against this tenant) could produce cleaner section boundaries later.
    """
    title = detail.get("title") or ""
    description = detail.get("description") or ""
    web_url = detail.get("webUrl") or ""
    canvas = detail.get("canvasLayout") or {}

    text_chunks = [chunk for chunk in _extract_text_chunks(canvas) if chunk]
    text = "\n\n".join(text_chunks)

    return (
        f"TITLE: {title}\n\n"
        f"DESCRIPTION: {description}\n\n"
        f"SECTION:\n{title}\n\n"
        f"TEXT:\n{text}\n\n"
        f"WEB URL:\n{web_url}\n"
    )

"""
DriveItem delta fetching.

Spec section 19: delta is the change-tracking mechanism for the document
library. Section 22: delta pagination must follow every @odata.nextLink
and must NOT persist the final @odata.deltaLink until every change from
this batch has been processed successfully -- that persistence decision
is made by the caller (app/sync/manager.py), not here. This module's job
is purely: fetch, page, return everything plus the new cursor.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.sharepoint.client import GraphClient

logger = logging.getLogger("knowledge_agent.delta")


@dataclass
class DeltaFetchResult:
    items: list[dict] = field(default_factory=list)
    delta_link: str | None = None  # only populated once @odata.nextLink is exhausted


def initial_delta_endpoint(drive_id: str) -> str:
    """
    First-ever delta call for a drive (no saved cursor yet). Per Graph's
    own delta semantics this returns the drive's current full inventory,
    terminating in a deltaLink -- so it doubles as the mechanism for
    establishing a baseline cursor once an initial full sync has already
    populated document state some other way (spec section 18 vs 64: this
    codebase uses the recursive walk in drives.py for the very first
    inventory, and only uses this endpoint afterward to capture a cursor).
    """
    return f"/drives/{drive_id}/root/delta"


async def fetch_all_delta_changes(client: GraphClient, start_url: str) -> DeltaFetchResult:
    """
    Follows @odata.nextLink verbatim (never reconstructed, per section 19)
    until a page returns @odata.deltaLink instead, accumulating every item
    from every page along the way.
    """
    items: list[dict] = []
    url: str | None = start_url
    pages_fetched = 0

    while True:
        payload = await client.get_json(url)
        items.extend(payload.get("value", []))
        pages_fetched += 1

        next_link = payload.get("@odata.nextLink")
        if next_link:
            url = next_link
            continue

        delta_link = payload.get("@odata.deltaLink")
        logger.debug("delta_fetch_complete items=%d pages=%d", len(items), pages_fetched)
        return DeltaFetchResult(items=items, delta_link=delta_link)

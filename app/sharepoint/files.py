"""
File downloading.

Spec section 17: only items with a "file" facet get downloaded, via
GET /drives/{drive-id}/items/{item-id}/content. The temporary
@microsoft.graph.downloadUrl is never persisted as state.
"""
from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass

from app.sharepoint.client import GraphClient
from app.sharepoint.drives import DiscoveredFile


@dataclass
class DownloadedFile:
    item_id: str
    filename: str
    content: bytes
    content_type: str | None


async def download_file(client: GraphClient, drive_id: str, discovered: DiscoveredFile) -> DownloadedFile:
    """
    Downloads the binary content of a single discovered file.

    Callers must only pass DiscoveredFile instances (which, by construction
    in walk_drive(), only ever come from items carrying a "file" facet) --
    this function never calls /content on anything else.
    """
    content = await client.get_bytes(f"/drives/{drive_id}/items/{discovered.item_id}/content")
    content_type, _ = mimetypes.guess_type(discovered.name)
    return DownloadedFile(
        item_id=discovered.item_id,
        filename=discovered.name,
        content=content,
        content_type=content_type,
    )


def compute_content_hash(content: bytes) -> str:
    """SHA-256 hex digest, per spec section 28."""
    return hashlib.sha256(content).hexdigest()

"""
SharePoint site + document library (Drive) discovery.

Spec section 15/16: drive discovery, folder/file children, and recursive
traversal that is iterative (not recursive-call-based) to protect against
pathological folder nesting depth. No hardcoded folder tree (Rule 2).
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field

from app.config import Settings
from app.sharepoint.client import GraphClient

logger = logging.getLogger("knowledge_agent.drives")

# Extensions this pipeline knows how to process, per spec section 4.
# ".aspx" is excluded here deliberately -- pages come from the separate
# Site Pages API (section 31), not the document library.
SUPPORTED_EXTENSIONS = {
    ".md", ".pptx", ".json", ".py", ".png",
    ".html", ".docx", ".xlsx", ".pdf", ".drawio",
    # Not in spec section 4's original table; added because the live
    # library holds templates/instructions as .txt and diagrams as .svg.
    ".txt", ".svg",
}


@dataclass
class DiscoveredFile:
    item_id: str
    name: str
    extension: str
    size: int
    web_url: str
    parent_item_id: str | None
    path: str
    etag: str | None
    last_modified: str | None
    is_supported: bool


@dataclass
class DiscoveryResult:
    files: list[DiscoveredFile] = field(default_factory=list)
    skipped_unsupported: list[DiscoveredFile] = field(default_factory=list)
    folders_visited: int = 0


async def check_site_health(client: GraphClient, settings: Settings) -> dict:
    """GET /sites/{site-id} -- confirms Entra auth + site access are working."""
    site = await client.get_json(f"/sites/{settings.sharepoint_site_id}")
    return {
        "site_id": site.get("id"),
        "display_name": site.get("displayName"),
        "web_url": site.get("webUrl"),
    }


async def check_drive_health(client: GraphClient, settings: Settings) -> dict:
    """GET /drives/{drive-id} -- confirms the document library is reachable."""
    drive = await client.get_json(f"/drives/{settings.sharepoint_drive_id}")
    return {
        "drive_id": drive.get("id"),
        "name": drive.get("name"),
        "drive_type": drive.get("driveType"),
        "web_url": drive.get("webUrl"),
    }


def _extension_of(name: str) -> str:
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1].lower()


# Public alias -- delta processing (app/sync/manager.py) needs the same
# extension-classification logic used by the recursive walk, without
# reaching into a name-mangled "private" helper.
extension_of = _extension_of


async def discover_root_children(client: GraphClient, drive_id: str) -> list[dict]:
    return await client.get_paged(f"/drives/{drive_id}/root/children")


async def discover_folder_children(client: GraphClient, drive_id: str, folder_id: str) -> list[dict]:
    return await client.get_paged(f"/drives/{drive_id}/items/{folder_id}/children")


async def walk_drive(client: GraphClient, drive_id: str) -> DiscoveryResult:
    """
    Iterative (queue-based) recursive discovery of every file in the drive,
    starting from root/children. Deliberately avoids Python call-stack
    recursion so a deeply/pathologically nested folder tree cannot blow the
    stack (spec section 16).

    Never calls /content on a folder -- only items carrying the "file"
    facet are ever downloaded, and this function doesn't download at all,
    it only enumerates metadata (spec section 15/17).
    """
    result = DiscoveryResult()
    # Each queue entry: (folder_item_id_or_None_for_root, path_prefix)
    queue: deque[tuple[str | None, str]] = deque()
    queue.append((None, ""))

    while queue:
        folder_id, path_prefix = queue.popleft()
        result.folders_visited += 1

        if folder_id is None:
            children = await discover_root_children(client, drive_id)
        else:
            children = await discover_folder_children(client, drive_id, folder_id)

        for item in children:
            name = item.get("name", "")
            item_path = f"{path_prefix}/{name}" if path_prefix else f"/{name}"

            if "folder" in item:
                queue.append((item["id"], item_path))
                continue

            if "file" not in item:
                # Neither a folder nor a file facet (e.g. a package) --
                # skip rather than guess.
                continue

            extension = _extension_of(name)
            discovered = DiscoveredFile(
                item_id=item["id"],
                name=name,
                extension=extension,
                size=item.get("size", 0),
                web_url=item.get("webUrl", ""),
                parent_item_id=folder_id,
                path=item_path,
                etag=item.get("eTag"),
                last_modified=item.get("lastModifiedDateTime"),
                is_supported=extension in SUPPORTED_EXTENSIONS,
            )

            if discovered.is_supported:
                result.files.append(discovered)
            else:
                logger.info("file_skipped_unsupported name=%s extension=%s", name, extension)
                result.skipped_unsupported.append(discovered)

    return result

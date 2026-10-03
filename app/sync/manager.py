"""
Sync orchestration: initial full sync, delta sync, page sync, and (as of
Phase 6) extraction + chunking wired in right after a file/page's content
is confirmed new or changed.

Rule 10 (spec section 61): sync/Graph logic must not be coupled to
extraction logic. Rule 11: extraction must not be coupled to the embedding
provider. Both are respected here by keeping extraction+chunking behind
optional parameters (router/chunker/chunk_repo default to None): every
function in this module still works exactly as it did in Phases 2-4 if
those aren't supplied, and existing callers/tests are unaffected.
"""
from __future__ import annotations

import enum
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.chunking.chunker import Chunker
from app.db.models import Document, ProcessingStatus, SourceType
from app.processing.router import ProcessorRouter, UnsupportedExtensionError
from app.sharepoint.client import GraphApiError, GraphClient
from app.sharepoint.delta import fetch_all_delta_changes, initial_delta_endpoint
from app.sharepoint.drives import SUPPORTED_EXTENSIONS, DiscoveredFile, DiscoveryResult, extension_of, walk_drive
from app.sharepoint.files import DownloadedFile, compute_content_hash, download_file
from app.sharepoint.pages import get_page_content, list_pages, normalize_page_content
from app.sync.state import ChunkRepository, DocumentRepository, DocumentUpsert, SyncStateRepository

logger = logging.getLogger("knowledge_agent.sync.manager")

# Transaction boundaries: every item commits its own work as soon as it's
# done (repo.commit()), and a document row is committed BEFORE its chunks
# are written. A later failure's repo.rollback() therefore only discards
# that one item's uncommitted writes -- never items that already finished
# earlier in the run, and never the document row of the item whose
# chunking failed. Committing per item doesn't weaken Rule 8: the delta
# cursor still only advances via record_success after a clean pass, and
# reprocessing already-committed items on retry is idempotent (hash check).


class ProcessingOutcome(str, enum.Enum):
    added = "added"
    modified = "modified"
    unchanged = "unchanged"
    failed = "failed"


@dataclass
class SyncRunCounters:
    items_detected: int = 0
    items_added: int = 0
    items_modified: int = 0
    items_deleted: int = 0
    items_skipped: int = 0
    items_failed: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def has_failures(self) -> bool:
        return self.items_failed > 0


def _parse_graph_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # Graph returns RFC3339 like "2026-01-01T00:00:00Z"
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("unparseable_graph_datetime value=%s", value)
        return None


# Statuses meaning "content is tracked, but extraction/chunking never
# completed for it". An unchanged hash must not short-circuit these, or
# they'd be stuck forever: e.g. documents first synced before chunking was
# wired in (Phase 6) sit at PENDING with a matching hash on every later run.
# DOWNLOAD_FAILED is here because a failed download keeps the previous
# hash, so the next successful download of the same bytes looks unchanged.
_INCOMPLETE_PROCESSING_STATUSES = {
    ProcessingStatus.pending,
    ProcessingStatus.download_failed,
    ProcessingStatus.extraction_failed,
    ProcessingStatus.chunking,
    ProcessingStatus.chunking_failed,
}


async def _extract_and_chunk_file(
    document: Document,
    downloaded: DownloadedFile,
    extension: str,
    router: ProcessorRouter,
    chunker: Chunker,
    chunk_repo: ChunkRepository,
    doc_repo: DocumentRepository,
) -> None:
    """
    Runs after a file's download+hash+upsert already succeeded (outcome
    added/modified). Reuses the bytes already downloaded -- never
    re-fetches from Graph. A failure here never rolls back or invalidates
    the document row itself, which is already correctly tracked; it only
    means chunking didn't complete this round; spec section 56's failure
    states (EXTRACTION_FAILED) capture exactly that distinction.
    """
    # Read before anything can roll back: rollback expires ORM instances,
    # and touching an expired attribute under an async session raises.
    document_id = document.id
    document_name = document.name

    try:
        extracted = await router.extract(downloaded, extension)
    except UnsupportedExtensionError:
        # Shouldn't happen -- walk_drive/process_delta_item only classify
        # known extensions as supported in the first place -- but never
        # let a router/extension mismatch take down the sync run.
        logger.error("no_processor_for_supported_extension document_id=%s extension=%s", document_id, extension)
        return
    except Exception as exc:
        logger.exception("extraction_failed document_id=%s name=%s", document_id, document_name)
        try:
            await doc_repo.mark_processing_status(document_id, ProcessingStatus.extraction_failed, str(exc))
            await doc_repo.commit()
        except Exception:
            await doc_repo.rollback()
        return

    await _chunk_and_persist(
        document=document,
        text=extracted.text,
        content_hash=document.content_hash or "",
        chunker=chunker,
        chunk_repo=chunk_repo,
        doc_repo=doc_repo,
    )


async def _chunk_and_persist(
    document: Document,
    text: str,
    content_hash: str,
    chunker: Chunker,
    chunk_repo: ChunkRepository,
    doc_repo: DocumentRepository,
) -> None:
    base_metadata = {
        "source_type": document.source_type,
        "sharepoint_item_id": document.sharepoint_item_id,
        "name": document.name,
        "path": document.path,
        "sharepoint_url": document.sharepoint_url,
    }
    document_id = document.id
    chunks = chunker.chunk(text, document_id, content_hash, base_metadata)

    try:
        # Same transaction for the chunk swap and the status change (spec
        # section 57): either both land or neither does.
        await chunk_repo.replace_chunks(document_id, chunks)
        await doc_repo.mark_processing_status(document_id, ProcessingStatus.chunked)
        await doc_repo.commit()
    except Exception as exc:
        logger.exception("chunk_persist_failed document_id=%s", document_id)
        # The document row was committed before chunking started, so this
        # only discards the partial chunk swap -- old chunks stay intact.
        await doc_repo.rollback()
        try:
            await doc_repo.mark_processing_status(document_id, ProcessingStatus.chunking_failed, str(exc))
            await doc_repo.commit()
        except Exception:
            await doc_repo.rollback()




async def process_discovered_file(
    client: GraphClient,
    drive_id: str,
    site_id: str | None,
    discovered: DiscoveredFile,
    repo: DocumentRepository,
    router: ProcessorRouter | None = None,
    chunker: Chunker | None = None,
    chunk_repo: ChunkRepository | None = None,
) -> ProcessingOutcome:
    """
    Implements the New/Modified/Unchanged classification from spec section 20,
    keyed on sharepoint_item_id (never filename, per Rule 3) and gated on a
    SHA-256 content hash comparison (spec section 28) so an unchanged file
    never triggers reprocessing.

    Every persistence attempt below is individually guarded: if a write for
    THIS item fails (bad enum value, constraint violation, transient DB
    hiccup, whatever), we roll back and record it as a failed item rather
    than letting the exception propagate and poison the session's
    transaction for every item still queued behind it in this run (spec
    section 43: "continue processing other items").

    router/chunker/chunk_repo (Phase 6) are optional: when all three are
    supplied and this file is genuinely new or changed, its already-
    downloaded bytes get extracted and chunked immediately afterward --
    no second download. When any is None, behavior is identical to
    Phases 2-4 (download, hash, track -- nothing more).
    """
    existing = await repo.get_by_sharepoint_item_id(SourceType.file, discovered.item_id)

    try:
        downloaded = await download_file(client, drive_id, discovered)
    except GraphApiError as exc:
        logger.error(
            "file_download_failed item_id=%s name=%s status=%s",
            discovered.item_id, discovered.name, exc.status_code,
        )
        try:
            await repo.upsert(DocumentUpsert(
                source_type=SourceType.file,
                sharepoint_item_id=discovered.item_id,
                drive_id=drive_id,
                site_id=site_id,
                name=discovered.name,
                extension=discovered.extension,
                path=discovered.path,
                sharepoint_url=discovered.web_url,
                parent_sharepoint_item_id=discovered.parent_item_id,
                modified_at=_parse_graph_datetime(discovered.last_modified),
                etag=discovered.etag,
                size=discovered.size,
                content_hash=existing.content_hash if existing else None,
                processing_status=ProcessingStatus.download_failed,
                processing_error=str(exc),
                retry_count=(existing.retry_count + 1) if existing else 1,
            ))
            await repo.commit()
        except Exception:
            logger.exception(
                "failed_to_record_download_failure item_id=%s name=%s", discovered.item_id, discovered.name
            )
            await repo.rollback()
        return ProcessingOutcome.failed

    new_hash = compute_content_hash(downloaded.content)

    try:
        if existing is not None and not existing.is_deleted and existing.content_hash == new_hash:
            await repo.touch_metadata_only(existing, datetime.now(timezone.utc))
            await repo.commit()
            chunking_wired = router is not None and chunker is not None and chunk_repo is not None
            if chunking_wired and existing.processing_status in _INCOMPLETE_PROCESSING_STATUSES:
                logger.info(
                    "resuming_incomplete_processing item_id=%s name=%s status=%s",
                    discovered.item_id, discovered.name, existing.processing_status,
                )
                await _extract_and_chunk_file(
                    existing, downloaded, discovered.extension, router, chunker, chunk_repo, repo
                )
            return ProcessingOutcome.unchanged

        document = await repo.upsert(DocumentUpsert(
            source_type=SourceType.file,
            sharepoint_item_id=discovered.item_id,
            drive_id=drive_id,
            site_id=site_id,
            name=discovered.name,
            extension=discovered.extension,
            path=discovered.path,
            sharepoint_url=discovered.web_url,
            parent_sharepoint_item_id=discovered.parent_item_id,
            modified_at=_parse_graph_datetime(discovered.last_modified),
            etag=discovered.etag,
            size=discovered.size,
            content_hash=new_hash,
            # PENDING for now -- becomes CHUNKED below if extraction/chunking
            # is wired in and succeeds; otherwise stays PENDING, which is
            # still accurate (spec section 56's state machine), never a lie.
            processing_status=ProcessingStatus.pending,
            processing_error=None,
            retry_count=0,
        ))
        # Committed before chunking: a chunking failure below must not be
        # able to roll back the fact that this document exists/changed.
        await repo.commit()
        outcome = ProcessingOutcome.added if existing is None else ProcessingOutcome.modified
    except Exception:
        logger.exception(
            "failed_to_persist_document item_id=%s name=%s", discovered.item_id, discovered.name
        )
        await repo.rollback()
        return ProcessingOutcome.failed

    if router is not None and chunker is not None and chunk_repo is not None:
        await _extract_and_chunk_file(document, downloaded, discovered.extension, router, chunker, chunk_repo, repo)

    return outcome


async def run_initial_sync(
    client: GraphClient,
    drive_id: str,
    site_id: str | None,
    repo: DocumentRepository,
    discovery: DiscoveryResult | None = None,
    router: ProcessorRouter | None = None,
    chunker: Chunker | None = None,
    chunk_repo: ChunkRepository | None = None,
) -> SyncRunCounters:
    """
    Runs discovery (unless a pre-computed DiscoveryResult is passed, mainly
    for testing) and processes every supported file found. Unsupported
    files are counted as skipped, never as failures (spec section 4).

    This function does not create or finalize a SyncRun row itself -- the
    API layer owns that lifecycle (create -> run -> mark success/partial/
    failed) so this function stays a pure, testable unit of "process one
    drive's worth of discovered files".
    """
    counters = SyncRunCounters()

    result = discovery if discovery is not None else await walk_drive(client, drive_id)
    counters.items_skipped += len(result.skipped_unsupported)
    counters.items_detected = len(result.files) + len(result.skipped_unsupported)

    for discovered in result.files:
        outcome = await process_discovered_file(
            client, drive_id, site_id, discovered, repo, router=router, chunker=chunker, chunk_repo=chunk_repo
        )
        if outcome == ProcessingOutcome.added:
            counters.items_added += 1
        elif outcome == ProcessingOutcome.modified:
            counters.items_modified += 1
        elif outcome == ProcessingOutcome.failed:
            counters.items_failed += 1
            counters.errors.append(f"{discovered.name} ({discovered.item_id})")
        # unchanged -> no counter bump; it's neither new work nor a failure

    return counters


async def process_delta_item(
    client: GraphClient,
    drive_id: str,
    site_id: str | None,
    item: dict,
    repo: DocumentRepository,
    router: ProcessorRouter | None = None,
    chunker: Chunker | None = None,
    chunk_repo: ChunkRepository | None = None,
) -> tuple[str, ProcessingOutcome | None]:
    """
    Classifies a single Graph delta item per spec section 20:
    - "deleted" facet -> mark_deleted
    - folder facet, or no "file" facet, or an unsupported extension -> skipped
    - otherwise -> the same New/Modified/Unchanged/Failed path as the
      recursive walk uses, via process_discovered_file, so a file arriving
      through delta is handled identically to one arriving through the
      initial full walk (same hashing, same idempotency, same rollback
      safety, and now the same extraction+chunking wiring).

    Returns (category, outcome) where category is one of
    "deleted" | "skipped" | "processed", and outcome is only meaningful
    when category == "processed".
    """
    if "deleted" in item:
        try:
            marked = await repo.mark_deleted(SourceType.file, item.get("id", ""))
            await repo.commit()
        except Exception:
            logger.exception("failed_to_mark_deleted item_id=%s", item.get("id", ""))
            await repo.rollback()
            return "processed", ProcessingOutcome.failed
        return ("deleted", None) if marked else ("skipped", None)

    if "folder" in item or "file" not in item:
        return "skipped", None

    name = item.get("name", "")
    extension = extension_of(name)
    if extension not in SUPPORTED_EXTENSIONS:
        return "skipped", None

    parent_ref = item.get("parentReference") or {}
    discovered = DiscoveredFile(
        item_id=item.get("id", ""),
        name=name,
        extension=extension,
        size=item.get("size", 0),
        web_url=item.get("webUrl", ""),
        parent_item_id=parent_ref.get("id"),
        # Delta items don't reliably carry a materialized folder path the
        # way the recursive walk builds one -- webUrl remains the
        # authoritative source link either way (spec section 39).
        path=None,
        etag=item.get("eTag"),
        last_modified=item.get("lastModifiedDateTime"),
        is_supported=True,
    )
    outcome = await process_discovered_file(
        client, drive_id, site_id, discovered, repo, router=router, chunker=chunker, chunk_repo=chunk_repo
    )
    return "processed", outcome


async def run_delta_sync(
    client: GraphClient,
    drive_id: str,
    site_id: str | None,
    doc_repo: DocumentRepository,
    sync_state_repo: SyncStateRepository,
    source: str = "document_library",
    router: ProcessorRouter | None = None,
    chunker: Chunker | None = None,
    chunk_repo: ChunkRepository | None = None,
) -> SyncRunCounters:
    """
    Spec sections 19-22: the normal (non-initial) daily sync path.

    Critical ordering, per spec section 21: the new deltaLink is only ever
    persisted via sync_state_repo.record_success, and only when this
    function reaches the end with zero item failures. If the delta fetch
    itself fails (network/Graph error while paging), or any item fails to
    persist, the OLD cursor is left untouched -- so the next run refetches
    this same batch from Graph and retries. Idempotent processing (content
    hash comparison) makes that safe: already-succeeded items in the same
    batch just come back as "unchanged" on retry, not reprocessed.
    """
    counters = SyncRunCounters()
    await sync_state_repo.record_attempt(source)

    saved_link = await sync_state_repo.get_delta_link(source)
    start_url = saved_link or initial_delta_endpoint(drive_id)

    try:
        fetch_result = await fetch_all_delta_changes(client, start_url)
    except GraphApiError as exc:
        logger.error("delta_fetch_failed source=%s status=%s", source, exc.status_code)
        counters.items_failed += 1
        counters.errors.append(f"delta fetch failed: {exc}")
        return counters

    counters.items_detected = len(fetch_result.items)

    for item in fetch_result.items:
        category, outcome = await process_delta_item(
            client, drive_id, site_id, item, doc_repo, router=router, chunker=chunker, chunk_repo=chunk_repo
        )

        if category == "deleted":
            counters.items_deleted += 1
        elif category == "skipped":
            counters.items_skipped += 1
        elif category == "processed":
            if outcome == ProcessingOutcome.added:
                counters.items_added += 1
            elif outcome == ProcessingOutcome.modified:
                counters.items_modified += 1
            elif outcome == ProcessingOutcome.failed:
                counters.items_failed += 1
                counters.errors.append(f"{item.get('name', '?')} ({item.get('id', '?')})")
            # unchanged -> no counter bump, same as the initial-sync path

    if not counters.has_failures:
        # Only advance the cursor on a clean pass. If fetch_result.delta_link
        # is somehow None (shouldn't happen once nextLink is exhausted, but
        # defensively), leaving the old cursor in place is still correct.
        await sync_state_repo.record_success(source, fetch_result.delta_link)
    else:
        logger.warning(
            "delta_cursor_not_advanced source=%s items_failed=%d -- next run will retry this batch",
            source, counters.items_failed,
        )

    return counters


async def establish_delta_baseline(
    client: GraphClient,
    drive_id: str,
    sync_state_repo: SyncStateRepository,
    source: str = "document_library",
) -> str | None:
    """
    Called once, right after a successful initial full sync (the recursive
    walk in drives.py), to capture a starting deltaLink for future
    incremental runs -- without reprocessing every file a second time.
    This pages through the drive's full delta feed once, discarding the
    items (already handled by the walk) and keeping only the final cursor.
    """
    try:
        fetch_result = await fetch_all_delta_changes(client, initial_delta_endpoint(drive_id))
    except GraphApiError:
        logger.exception("failed_to_establish_delta_baseline source=%s", source)
        return None

    if fetch_result.delta_link:
        await sync_state_repo.record_success(source, fetch_result.delta_link)
    return fetch_result.delta_link


async def process_page(
    client: GraphClient,
    site_id: str,
    page_summary: dict,
    repo: DocumentRepository,
    chunker: Chunker | None = None,
    chunk_repo: ChunkRepository | None = None,
) -> ProcessingOutcome:
    """
    Same New/Modified/Unchanged/Failed shape as process_discovered_file,
    applied to a SharePoint page instead of a drive file. Reuses the
    existing Document model unchanged (source_type=page), which is exactly
    why Phase 2 designed source_type into the schema from the start.

    No router/UnsupportedExtensionError here unlike files: normalize_page_content
    (Phase 4) already IS a page's extraction step, there's nothing to route by
    extension for a single content type. chunker/chunk_repo (Phase 6) are
    optional, same contract as process_discovered_file.
    """
    page_id = page_summary.get("id", "")
    title = page_summary.get("title") or page_summary.get("name") or ""
    web_url = page_summary.get("webUrl") or ""
    last_modified = page_summary.get("lastModifiedDateTime")

    existing = await repo.get_by_sharepoint_item_id(SourceType.page, page_id)

    try:
        detail = await get_page_content(client, site_id, page_id)
        normalized_text = normalize_page_content(detail)
    except GraphApiError as exc:
        logger.error("page_content_fetch_failed page_id=%s title=%s status=%s", page_id, title, exc.status_code)
        try:
            await repo.upsert(DocumentUpsert(
                source_type=SourceType.page,
                sharepoint_item_id=page_id,
                drive_id=None,
                site_id=site_id,
                name=title,
                extension=".aspx",
                path=None,
                sharepoint_url=web_url,
                parent_sharepoint_item_id=None,
                modified_at=_parse_graph_datetime(last_modified),
                etag=None,
                size=existing.size if existing else None,
                content_hash=existing.content_hash if existing else None,
                processing_status=ProcessingStatus.extraction_failed,
                processing_error=str(exc),
                retry_count=(existing.retry_count + 1) if existing else 1,
            ))
            await repo.commit()
        except Exception:
            logger.exception("failed_to_record_page_failure page_id=%s title=%s", page_id, title)
            await repo.rollback()
        return ProcessingOutcome.failed

    new_hash = compute_content_hash(normalized_text.encode("utf-8"))

    try:
        if existing is not None and not existing.is_deleted and existing.content_hash == new_hash:
            await repo.touch_metadata_only(existing, datetime.now(timezone.utc))
            await repo.commit()
            if (
                chunker is not None and chunk_repo is not None
                and existing.processing_status in _INCOMPLETE_PROCESSING_STATUSES
            ):
                logger.info(
                    "resuming_incomplete_processing page_id=%s title=%s status=%s",
                    page_id, title, existing.processing_status,
                )
                await _chunk_and_persist(
                    document=existing, text=normalized_text, content_hash=new_hash,
                    chunker=chunker, chunk_repo=chunk_repo, doc_repo=repo,
                )
            return ProcessingOutcome.unchanged

        document = await repo.upsert(DocumentUpsert(
            source_type=SourceType.page,
            sharepoint_item_id=page_id,
            drive_id=None,
            site_id=site_id,
            name=title,
            extension=".aspx",
            path=None,
            sharepoint_url=web_url,
            parent_sharepoint_item_id=None,
            modified_at=_parse_graph_datetime(last_modified),
            etag=None,
            size=len(normalized_text),
            content_hash=new_hash,
            processing_status=ProcessingStatus.pending,
            processing_error=None,
            retry_count=0,
        ))
        # Committed before chunking: a chunking failure below must not be
        # able to roll back the fact that this document exists/changed.
        await repo.commit()
        outcome = ProcessingOutcome.added if existing is None else ProcessingOutcome.modified
    except Exception:
        logger.exception("failed_to_persist_page page_id=%s title=%s", page_id, title)
        await repo.rollback()
        return ProcessingOutcome.failed

    if chunker is not None and chunk_repo is not None:
        await _chunk_and_persist(
            document=document, text=normalized_text, content_hash=new_hash,
            chunker=chunker, chunk_repo=chunk_repo, doc_repo=repo,
        )

    return outcome


async def run_page_sync(
    client: GraphClient,
    site_id: str,
    repo: DocumentRepository,
    chunker: Chunker | None = None,
    chunk_repo: ChunkRepository | None = None,
) -> SyncRunCounters:
    """
    Full listing every run -- Graph has no documented sitePage delta
    endpoint, unlike driveItem (spec section 31 notes this asymmetry
    explicitly). Deletions are therefore detected by diffing the current
    full listing against previously-tracked page documents
    (mark_missing_deleted), not by a "deleted" facet.

    Callers MUST have already confirmed check_page_capability() succeeded
    -- this function does not re-check permissions itself, to keep it a
    plain, testable "process this listing" unit like run_initial_sync.
    """
    counters = SyncRunCounters()

    try:
        pages = await list_pages(client, site_id)
    except GraphApiError as exc:
        logger.error("page_listing_failed status=%s", exc.status_code)
        counters.items_failed += 1
        counters.errors.append(f"page listing failed: {exc}")
        return counters

    counters.items_detected = len(pages)
    seen_page_ids: set[str] = set()

    for page in pages:
        page_id = page.get("id", "")
        seen_page_ids.add(page_id)
        outcome = await process_page(client, site_id, page, repo, chunker=chunker, chunk_repo=chunk_repo)
        if outcome == ProcessingOutcome.added:
            counters.items_added += 1
        elif outcome == ProcessingOutcome.modified:
            counters.items_modified += 1
        elif outcome == ProcessingOutcome.failed:
            counters.items_failed += 1
            counters.errors.append(f"{page.get('title', '?')} ({page_id})")
        # unchanged -> no counter bump

    deleted_count = await repo.mark_missing_deleted(SourceType.page, seen_page_ids)
    await repo.commit()
    counters.items_deleted += deleted_count

    return counters

"""
Orchestrates a complete rebuild of the USEP Solr index.

It composes the shared source-preparation and per-inscription indexing operations, then reconciles
Solr with the complete filesystem corpus by removing stale document IDs.
"""

import logging
import time
from pathlib import Path

from django.conf import settings
from lxml import etree
from usep_indexer_app.lib import indexer, orphans, processor


log = logging.getLogger(__name__)


def build_inscription_filename(inscription_id: str) -> str:
    """
    Validates a bare inscription ID and returns its XML filename.

    Called by: process_single_reindex()
    """
    invalid_control_character = any(ord(character) < 32 or ord(character) == 127 for character in inscription_id)
    if not inscription_id or inscription_id != inscription_id.strip():
        raise ValueError('The inscription ID cannot be empty or begin or end with whitespace.')
    if inscription_id in {'.', '..'} or '/' in inscription_id or '\\' in inscription_id:
        raise ValueError('The inscription ID must be a bare ID, not a filesystem path.')
    if invalid_control_character:
        raise ValueError('The inscription ID cannot contain control characters.')
    if inscription_id.endswith('.xml'):
        raise ValueError('Provide the inscription ID without the .xml extension.')
    filename = f'{inscription_id}.xml'
    return filename


def build_inscription_filepaths(inscriptions_path: Path) -> list[str]:
    """
    Returns every XML inscription path in stable order.

    Called by: process_full_reindex()
    """
    return [str(path) for path in sorted(inscriptions_path.glob('*.xml'))]


def build_orphaned_ids(inscription_filepaths: list[str], solr_ids: list[str]) -> list[str]:
    """
    Returns Solr IDs absent from a full filesystem inscription list.

    Called by: process_full_reindex()
    """
    filesystem_ids = sorted(Path(file_path).stem for file_path in inscription_filepaths)
    return orphans.build_orphan_list(filesystem_ids, solr_ids)


def process_full_reindex() -> None:
    """
    Pulls and copies USEP data, then rebuilds the complete Solr index.

    Called by: spool.process_valid_events()
    """
    log.info('Full reindex processing started.')
    data_revision = processor.prepare_public_data(validate_source_xml=True)
    process_prepared_full_reindex(data_revision=data_revision)
    return


def process_prepared_full_reindex(*, data_revision: str = 'unavailable') -> None:
    """
    Rebuilds Solr from already prepared public data without another pull or copy.

    Called by: process_full_reindex(), processor.process_incremental()
    """
    rebuild_started = time.monotonic()
    inscriptions_path = settings.WEBSERVED_DATA_DIR_PATH / 'inscriptions'
    inscription_paths = [Path(file_path) for file_path in build_inscription_filepaths(inscriptions_path)]
    with indexer.IndexingResources.load(data_revision=data_revision) as resources:
        documents = indexer.build_complete_documents(inscription_paths, resources)
        solr_ids = resources.solr.get_ids()
        ids_to_remove = build_orphaned_ids([str(path) for path in inscription_paths], solr_ids)
        log.info(
            f'Full Solr indexing started; inscriptions_to_index_count, ``{len(documents)}``; '
            f'ids_to_remove_count, ``{len(ids_to_remove)}``; batch_size, ``{resources.batch_size}``; '
            f'data_revision, ``{data_revision}``'
        )
        document_batch_count, deletion_batch_count = update_all_index_entries(documents, ids_to_remove, resources)
        elapsed_seconds = time.monotonic() - rebuild_started
        log.info(
            f'Full Solr indexing completed; document_count, ``{len(documents)}``; '
            f'document_batch_count, ``{document_batch_count}``; deletion_count, ``{len(ids_to_remove)}``; '
            f'deletion_batch_count, ``{deletion_batch_count}``; request_count, ``{resources.solr.request_count}``; '
            f'elapsed_seconds, ``{elapsed_seconds:.3f}``; data_revision, ``{data_revision}``'
        )
    return


def process_single_reindex(inscription_id: str) -> Path:
    """
    Refreshes one inscription's published files and Solr document.

    Called by: management.commands.refresh_inscription.Command.handle()
    """
    filename = build_inscription_filename(inscription_id)
    log.info(f'Single-inscription refresh started; inscription_id, ``{inscription_id}``')
    data_revision = processor.prepare_public_data(validate_source_xml=False)
    inscriptions_path = settings.WEBSERVED_DATA_DIR_PATH / 'inscriptions'
    inscription_path = inscriptions_path / filename
    if not inscription_path.is_file():
        raise FileNotFoundError(f'No copied inscription XML exists for ID {inscription_id!r}: {inscription_path}')
    indexer.update_index_entry(filename, data_revision=data_revision)
    log.info(f'Single-inscription refresh completed; inscription_id, ``{inscription_id}``')
    return inscription_path


def update_all_index_entries(
    documents: list[etree._Element],
    ids_to_remove: list[str],
    resources: indexer.IndexingResources,
) -> tuple[int, int]:
    """
    Posts complete-document batches and bounded orphan-deletion batches.

    Called by: process_prepared_full_reindex()
    """
    document_batch_count = indexer.post_document_batches(documents, resources)
    deletion_batch_count = indexer.delete_id_batches(ids_to_remove, resources)
    return document_batch_count, deletion_batch_count

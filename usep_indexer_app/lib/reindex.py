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
from usep_indexer_app.lib import indexer, orphans, processor, xml_validation


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


def validate_inscription_corpus(inscriptions_path: Path) -> None:
    """
    Requires every source inscription XML file to be well-formed.

    Called by: process_full_reindex()
    """
    result: xml_validation.XMLDirectoryValidationResult = xml_validation.validate_xml_directory(inscriptions_path)
    log.info(
        f'Full-reindex XML validation completed; checked_count, ``{result.checked_count}``; '
        f'well_formed_count, ``{result.well_formed_count}``; failure_count, ``{len(result.failures)}``'
    )
    if result.failures:
        for failure in result.failures:
            log.error(
                f'Full-reindex XML validation failed; failure_path, ``{failure.path.as_posix()}``; '
                f'error, ``{failure.error}``'
            )
        failure_count: int = len(result.failures)
        file_label: str = 'file' if failure_count == 1 else 'files'
        verb: str = 'is' if failure_count == 1 else 'are'
        failure_details: str = '; '.join(f'{failure.path.as_posix()}: {failure.error}' for failure in result.failures)
        raise xml_validation.XMLNotWellFormedError(
            f'Found {failure_count} source XML {file_label} that {verb} not well-formed: {failure_details}'
        )
    return


def process_full_reindex() -> None:
    """
    Pulls and copies USEP data, then rebuilds the complete Solr index.

    Called by: spool.process_valid_events()
    """
    log.info('Full reindex processing started.')
    processor.call_git_pull(settings.USEP_DATA_GIT_CLONED_DIR_PATH)
    log.info(f'Git pull completed; git_clone_path, ``{settings.USEP_DATA_GIT_CLONED_DIR_PATH}``')
    data_revision = processor.read_git_revision(settings.USEP_DATA_GIT_CLONED_DIR_PATH)
    log.info(f'Public data revision selected; data_revision, ``{data_revision}``')
    source_inscriptions_path: Path = settings.USEP_DATA_GIT_CLONED_DIR_PATH / 'xml_inscriptions'
    validate_inscription_corpus(source_inscriptions_path)
    processor.copy_files(
        settings.USEP_DATA_GIT_CLONED_DIR_PATH,
        settings.TEMP_UNIFIED_INSCRIPTIONS_DIR_PATH,
        settings.WEBSERVED_DATA_DIR_PATH,
    )
    log.info(f'USEP data copy completed; webserved_data_path, ``{settings.WEBSERVED_DATA_DIR_PATH}``')
    inscriptions_path = settings.WEBSERVED_DATA_DIR_PATH / 'inscriptions'
    changed_file_count = processor.update_xinclude_references(inscriptions_path)
    log.info(
        f'XInclude normalization completed; inscriptions_path, ``{inscriptions_path}``; '
        f'changed_file_count, ``{changed_file_count}``'
    )
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
    processor.call_git_pull(settings.USEP_DATA_GIT_CLONED_DIR_PATH)
    log.info(f'Git pull completed; git_clone_path, ``{settings.USEP_DATA_GIT_CLONED_DIR_PATH}``')
    data_revision = processor.read_git_revision(settings.USEP_DATA_GIT_CLONED_DIR_PATH)
    log.info(f'Public data revision selected; data_revision, ``{data_revision}``')
    processor.copy_files(
        settings.USEP_DATA_GIT_CLONED_DIR_PATH,
        settings.TEMP_UNIFIED_INSCRIPTIONS_DIR_PATH,
        settings.WEBSERVED_DATA_DIR_PATH,
    )
    log.info(f'USEP data copy completed; webserved_data_path, ``{settings.WEBSERVED_DATA_DIR_PATH}``')
    inscriptions_path = settings.WEBSERVED_DATA_DIR_PATH / 'inscriptions'
    changed_file_count = processor.update_xinclude_references(inscriptions_path)
    log.info(
        f'XInclude normalization completed; inscriptions_path, ``{inscriptions_path}``; '
        f'changed_file_count, ``{changed_file_count}``'
    )
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

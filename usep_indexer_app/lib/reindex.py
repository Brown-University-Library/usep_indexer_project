"""
Orchestrates a complete rebuild of the USEP Solr index.

It composes the shared source-preparation and per-inscription indexing operations, then reconciles
Solr with the complete filesystem corpus by removing stale document IDs.
"""

import logging
from pathlib import Path

from django.conf import settings
from usep_indexer_app.lib import indexer, orphans, processor, solr_client, xml_validation


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
    filepaths = build_inscription_filepaths(inscriptions_path)
    ids_to_remove = build_orphaned_ids(filepaths, solr_client.get_ids(settings.SOLR_URL))
    log.info(
        f'Full Solr indexing started; inscriptions_to_index_count, ``{len(filepaths)}``; '
        f'ids_to_remove_count, ``{len(ids_to_remove)}``'
    )
    update_all_index_entries(filepaths, ids_to_remove)
    log.info('Full Solr indexing completed.')
    return


def process_single_reindex(inscription_id: str) -> Path:
    """
    Pulls and copies current USEP data, then strictly reindexes one inscription.

    Called by: management.commands.reindex_inscription.Command.handle()
    """
    filename = build_inscription_filename(inscription_id)
    log.info(f'Single-inscription reindex started; inscription_id, ``{inscription_id}``')
    processor.call_git_pull(settings.USEP_DATA_GIT_CLONED_DIR_PATH)
    log.info(f'Git pull completed; git_clone_path, ``{settings.USEP_DATA_GIT_CLONED_DIR_PATH}``')
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
    indexer.update_index_entry(filename, strict_enrichment=True)
    log.info(f'Single-inscription reindex completed; inscription_id, ``{inscription_id}``')
    return inscription_path


def update_all_index_entries(inscriptions_to_index: list[str], ids_to_remove: list[str]) -> None:
    """
    Applies all full-reindex updates and removals synchronously.

    Called by: process_full_reindex()
    """
    for inscription_id in ids_to_remove:
        log.debug(f'Removing Solr entry during full reindex; inscription_id, ``{inscription_id}``')
        indexer.remove_entry_via_id(inscription_id)
    for file_path in inscriptions_to_index:
        log.debug(f'Updating Solr entry during full reindex; file_path, ``{file_path}``')
        indexer.update_entry(file_path)
    return

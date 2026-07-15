"""
Orchestrates a complete rebuild of the USEP Solr index.

It composes the shared source-preparation and per-inscription indexing operations, then reconciles
Solr with the complete filesystem corpus by removing stale document IDs.
"""

from pathlib import Path

from django.conf import settings
from usep_indexer_app.lib import indexer, orphans, processor, solr_client


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
    processor.call_git_pull(settings.USEP_DATA_GIT_CLONED_DIR_PATH)
    processor.copy_files(
        settings.USEP_DATA_GIT_CLONED_DIR_PATH,
        settings.TEMP_UNIFIED_INSCRIPTIONS_DIR_PATH,
        settings.WEBSERVED_DATA_DIR_PATH,
    )
    processor.update_xinclude_references(settings.WEBSERVED_DATA_DIR_PATH / 'inscriptions')
    filepaths = build_inscription_filepaths(settings.WEBSERVED_DATA_DIR_PATH / 'inscriptions')
    ids_to_remove = build_orphaned_ids(filepaths, solr_client.get_ids(settings.SOLR_URL))
    update_all_index_entries(filepaths, ids_to_remove)
    return


def update_all_index_entries(inscriptions_to_index: list[str], ids_to_remove: list[str]) -> None:
    """
    Applies all full-reindex updates and removals synchronously.

    Called by: process_full_reindex()
    """
    for inscription_id in ids_to_remove:
        indexer.remove_entry_via_id(inscription_id)
    for file_path in inscriptions_to_index:
        indexer.update_entry(file_path)
    return

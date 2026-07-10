from pathlib import Path

from django.conf import settings
from usep_indexer_app.lib import orphans, processor, solr_client
from usep_indexer_app.lib.queue_support import enqueue_call


def build_inscription_filepaths(inscriptions_path: Path) -> list[str]:
    """
    Returns every XML inscription path in stable order.

    Called by: run_start_reindex_all()
    """
    return [str(path) for path in sorted(inscriptions_path.glob('*.xml'))]


def build_orphaned_ids(inscription_filepaths: list[str], solr_ids: list[str]) -> list[str]:
    """
    Returns Solr IDs absent from a full filesystem inscription list.

    Called by: run_build_solr_remove_list()
    """
    filesystem_ids = sorted(Path(file_path).stem for file_path in inscription_filepaths)
    return orphans.build_orphan_list(filesystem_ids, solr_ids)


def run_call_simple_git_pull() -> None:
    """
    Pulls the data clone and enqueues the full-copy stage.

    Called by: views.reindex_all()
    """
    processor.call_git_pull(settings.GIT_CLONED_DIR_PATH)
    enqueue_call('usep_indexer_app.lib.reindex.run_simple_copy_files', {})
    return


def run_simple_copy_files() -> None:
    """
    Copies and rewrites all data, then enqueues full reindex preparation.

    Called by: run_call_simple_git_pull()
    """
    processor.copy_files(
        settings.GIT_CLONED_DIR_PATH,
        settings.TEMP_DATA_DIR_PATH,
        settings.WEBSERVED_DATA_DIR_PATH,
    )
    processor.update_xinclude_references(settings.WEBSERVED_DATA_DIR_PATH / 'inscriptions')
    enqueue_call('usep_indexer_app.lib.reindex.run_start_reindex_all', {})
    return


def run_start_reindex_all() -> None:
    """
    Builds the full inscription list and enqueues orphan comparison.

    Called by: run_simple_copy_files()
    """
    filepaths = build_inscription_filepaths(settings.WEBSERVED_DATA_DIR_PATH / 'inscriptions')
    enqueue_call(
        'usep_indexer_app.lib.reindex.run_build_solr_remove_list',
        {'inscription_filepaths': filepaths},
    )
    return


def run_build_solr_remove_list(inscription_filepaths: list[str]) -> None:
    """
    Compares the full filesystem list with Solr and enqueues final fan-out.

    Called by: run_start_reindex_all()
    """
    ids_to_remove = build_orphaned_ids(inscription_filepaths, solr_client.get_ids(settings.SOLR_URL))
    enqueue_call(
        'usep_indexer_app.lib.reindex.run_enqueue_all_index_updates',
        {'inscriptions_to_index': inscription_filepaths, 'ids_to_remove': ids_to_remove},
    )
    return


def run_enqueue_all_index_updates(inscriptions_to_index: list[str], ids_to_remove: list[str]) -> None:
    """
    Enqueues one update or removal job per full-reindex target.

    Called by: run_build_solr_remove_list()
    """
    for file_path in inscriptions_to_index:
        enqueue_call(
            'usep_indexer_app.lib.indexer.run_update_entry',
            {'updated_file_path': file_path},
        )
    for inscription_id in ids_to_remove:
        enqueue_call(
            'usep_indexer_app.lib.indexer.run_remove_entry_via_id',
            {'id_to_remove': inscription_id},
        )
    return

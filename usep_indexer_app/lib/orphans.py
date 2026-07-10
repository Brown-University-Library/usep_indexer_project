import datetime
import logging
from pathlib import Path

from django.conf import settings
from usep_indexer_app.lib import solr_client


log = logging.getLogger(__name__)


def prep_orphan_list() -> list[str]:
    """
    Returns Solr IDs that have no matching web-served inscription.

    Called by: views.list_orphans()
    """
    directory_ids = build_directory_inscription_ids(settings.WEBSERVED_DATA_DIR_PATH / 'inscriptions')
    solr_ids = build_solr_inscription_ids(settings.SOLR_URL)
    return build_orphan_list(directory_ids, solr_ids)


def build_directory_inscription_ids(inscriptions_path: Path) -> list[str]:
    """
    Returns sorted inscription IDs from the flattened filesystem directory.

    Called by: prep_orphan_list(), reindex.build_orphaned_ids()
    """
    inscription_ids = sorted(path.stem for path in inscriptions_path.glob('*.xml'))
    return inscription_ids


def build_solr_inscription_ids(solr_url: str) -> list[str]:
    """
    Returns sorted inscription IDs from Solr.

    Called by: prep_orphan_list()
    """
    return solr_client.get_ids(solr_url)


def build_orphan_list(directory_ids: list[str], solr_ids: list[str]) -> list[str]:
    """
    Returns IDs present in Solr but absent from the filesystem.

    Called by: prep_orphan_list(), reindex.build_orphaned_ids()
    """
    return sorted(set(solr_ids) - set(directory_ids))


def prep_context(
    orphan_ids: list[str],
    orphan_handler_url: str,
    start_time: datetime.datetime,
) -> dict[str, object]:
    """
    Builds the HTML and JSON response context for orphan listing.

    Called by: views.list_orphans()
    """
    context: dict[str, object] = {
        'data': orphan_ids,
        'inscriptions_dir_path': str(settings.WEBSERVED_DATA_DIR_PATH / 'inscriptions'),
        'orphan_handler_url': orphan_handler_url,
        'solr_url': settings.SOLR_URL,
        'time_taken': str(datetime.datetime.now() - start_time),
    }
    return context


def run_deletes(ids_to_delete: list[str]) -> list[str]:
    """
    Deletes orphan IDs, continuing after individual failures.

    Called by: views.delete_orphans()
    """
    errors: list[str] = []
    for inscription_id in ids_to_delete:
        try:
            solr_client.delete_id(settings.SOLR_URL, inscription_id)
        except Exception:
            errors.append(inscription_id)
            log.exception('Unable to delete orphan ID %s; continuing.', inscription_id)
    return errors

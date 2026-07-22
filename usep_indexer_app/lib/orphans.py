"""
Finds and manages Solr inscriptions that no longer exist in the web-served data.

Filesystem/Solr comparison, response-context preparation, and best-effort deletion are grouped here
to support both the orphan administration flow and full reindexing.
"""

import dataclasses
import datetime
import logging
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from usep_indexer_app.lib import processor, solr_client


log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class OrphanReview:
    """
    Describes one freshly prepared manual orphan review.

    Called by: prepare_orphan_review(), views.list_orphans()
    """

    orphan_ids: list[str]
    data_revision: str


def prepare_orphan_review() -> OrphanReview:
    """
    Refreshes public data and compares its inscription IDs with Solr.

    Called by: views.list_orphans()
    """
    log.info('Manual orphan-review preparation started.')
    data_revision = processor.prepare_public_data(validate_source_xml=True)
    orphan_ids = prep_orphan_list()
    log.info(
        f'Manual orphan-review preparation completed; orphan_count, ``{len(orphan_ids)}``; '
        f'data_revision, ``{data_revision}``'
    )
    review = OrphanReview(orphan_ids=orphan_ids, data_revision=data_revision)
    return review


def prep_orphan_list() -> list[str]:
    """
    Returns Solr IDs that have no matching web-served inscription.

    Called by: prepare_orphan_review()
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


def build_solr_index_label(solr_url: str) -> str:
    """
    Builds a safe environment label from the first character of the Solr host.

    Called by: prep_context()
    """
    hostname = urlsplit(solr_url).hostname or ''
    hostname_initial = hostname[:1].lower()
    label = 'configured Solr index'
    if hostname_initial == 'd':
        label = 'configured dev Solr index'
    elif hostname_initial == 'p':
        label = 'configured prod Solr index'
    return label


def prep_context(
    orphan_ids: list[str],
    orphan_handler_url: str,
    start_time: datetime.datetime,
    data_revision: str,
) -> dict[str, object]:
    """
    Builds the orphan-list response context without infrastructure locations.

    Called by: views.list_orphans()
    """
    context: dict[str, object] = {
        'data': orphan_ids,
        'orphan_handler_url': orphan_handler_url,
        'solr_index_label': build_solr_index_label(settings.SOLR_URL),
        'data_revision': data_revision,
        'time_taken': str(datetime.datetime.now() - start_time),
    }
    return context


def run_deletes(ids_to_delete: list[str]) -> list[str]:
    """
    Deletes orphan IDs, continuing after individual failures.

    Called by: views.delete_orphans()
    """
    errors: list[str] = []
    with solr_client.SolrClient(
        settings.SOLR_URL,
        timeout=float(getattr(settings, 'SOLR_TIMEOUT_SECONDS', solr_client.DEFAULT_TIMEOUT)),
        commit_within_ms=int(getattr(settings, 'SOLR_COMMIT_WITHIN_MS', 500)),
    ) as client:
        for inscription_id in ids_to_delete:
            try:
                client.delete_ids([inscription_id])
            except Exception:
                errors.append(inscription_id)
                log.exception('Unable to delete orphan ID %s; continuing.', inscription_id)
    return errors

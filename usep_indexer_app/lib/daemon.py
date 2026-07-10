import logging

from django.conf import settings
from usep_indexer_app.lib import spool


log = logging.getLogger(__name__)


def validate_request_source(request_ip: str) -> bool:
    """
    Checks the perceived request IP against the configured allowlist.

    Called by: views.daemon_check()
    """
    is_valid = request_ip in settings.LEGIT_IPS
    if not is_valid:
        log.warning('Rejecting daemon check from IP %s.', request_ip)
    return is_valid


def check_daemon() -> dict[str, object]:
    """
    Reports filesystem-queue processor freshness and backlog state.

    Called by: views.daemon_check()
    """
    try:
        result = spool.get_processor_health(settings.SPOOL_ROOT_PATH, settings.SPOOL_HEALTH_MAX_AGE_SECONDS)
    except Exception:
        log.exception('Unable to inspect filesystem-queue processor health.')
        result = {
            'result': 'daemon_not_active',
            'processor_status': 'error',
            'pending_count': 0,
            'processing_count': 0,
            'failed_count': 0,
            'quarantine_count': 0,
            'oldest_pending_age_seconds': None,
            'last_started_at': None,
            'last_finished_at': None,
        }
    return result

import logging

from django.conf import settings
from rq import Worker
from usep_indexer_app.lib.queue_support import get_queue


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


def check_daemon() -> str:
    """
    Checks RQ's worker registry for a worker on the configured queue.

    Called by: views.daemon_check()
    """
    result = 'daemon_not_active'
    try:
        queue = get_queue()
        if Worker.all(queue=queue):
            result = 'daemon_active'
    except Exception:
        log.exception('Unable to inspect the RQ worker registry.')
    return result

"""
Supports the restricted endpoint that reports filesystem-queue processor health.

The request-source check and response-context assembly live here to keep processing-monitoring policy
out of the Django view layer.
"""

import datetime
import logging

from django.conf import settings
from django.http import HttpRequest
from usep_indexer_app.lib import spool


log = logging.getLogger(__name__)


def validate_request_source(request_ip: str) -> bool:
    """
    Checks the perceived request IP against the configured allowlist.

    Called by: views.processing_check()
    """
    is_valid = request_ip in settings.LEGIT_IPS
    if not is_valid:
        log.warning(f'Rejecting processing check; request_ip, ``{request_ip}``.')
    return is_valid


def check_processing() -> dict[str, object]:
    """
    Reports filesystem-queue processor freshness and backlog state.

    Called by: views.processing_check()
    """
    try:
        result = spool.get_processor_health(settings.SPOOL_ROOT_PATH, settings.SPOOL_HEALTH_MAX_AGE_SECONDS)
    except Exception:
        log.exception('Unable to inspect filesystem-queue processor health.')
        result = {
            'result': 'processing_not_active',
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


def make_context(
    request: HttpRequest,
    request_started: datetime.datetime,
    health: dict[str, object],
) -> dict[str, object]:
    """
    Assembles request metadata and processor-health data.

    Called by: views.processing_check()
    """
    context = {
        'request': {
            'url': '%s://%s%s'
            % (
                request.scheme,
                request.META.get('HTTP_HOST', '127.0.0.1'),
                request.META.get('REQUEST_URI', request.META['PATH_INFO']),
            ),
            'timestamp': str(request_started),
        },
        'response': {
            **health,
            'info': settings.README_URL,
            'timetaken': str(datetime.datetime.now() - request_started),
        },
    }
    return context

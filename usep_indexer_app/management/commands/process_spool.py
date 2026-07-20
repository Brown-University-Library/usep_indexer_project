import dataclasses
import json
import logging

from django.conf import settings
from django.core.mail import mail_admins
from django.core.management.base import BaseCommand, CommandError
from usep_indexer_app.lib import spool


log = logging.getLogger(__name__)
JOB_FAILURE_EMAIL_SUBJECT = 'USEP spool-processing job failed'


def email_job_failure(output: str) -> None:
    """
    Emails the configured Django admins once for a failed processor job.

    Called by: Command.handle()
    """
    message = f'The USEP spool-processing job failed.\n\nProcessor result:\n{output}'
    try:
        mail_admins(JOB_FAILURE_EMAIL_SUBJECT, message, fail_silently=False)
    except Exception:
        log.exception('Unable to email Django admins about the failed spool-processing job.')
    return


class Command(BaseCommand):
    """
    Processes one locked batch from the durable filesystem queue.

    Called by: Django management-command discovery
    """

    help = 'Process one batch from the durable USEP filesystem queue.'

    def handle(self, *args: object, **options: object) -> None:
        """
        Runs the filesystem-queue processor and reports structured counts.

        Called by: Django management-command runner
        """
        del args, options
        result = spool.process_spool(
            settings.SPOOL_ROOT_PATH,
            settings.SPOOL_BATCH_SIZE,
            settings.SPOOL_MAX_ATTEMPTS,
            settings.SPOOL_COMPLETED_RETENTION_DAYS,
        )
        output = json.dumps(dataclasses.asdict(result), sort_keys=True)
        if result.status == 'failed':
            email_job_failure(output)
            raise CommandError(output)
        self.stdout.write(output)
        return

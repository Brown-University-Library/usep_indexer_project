import dataclasses
import json
import logging

from django.conf import settings
from django.core.mail import mail_admins
from django.core.management.base import BaseCommand, CommandError

from usep_indexer_app.lib import data_issue_email, spool

log = logging.getLogger(__name__)
JOB_FAILURE_EMAIL_SUBJECT = 'USEP spool-processing job failed'


def email_job_failure(output: str) -> None:
    """
    Emails technical failures to administrators without sending them ordinary data-review reports.

    Called by: Command.handle()
    """
    message = f'The USEP spool-processing job failed.\n\nProcessor result:\n{output}'
    try:
        mail_admins(JOB_FAILURE_EMAIL_SUBJECT, message, fail_silently=False)
    except Exception:
        log.exception('Unable to email Django admins about the failed spool-processing job.')


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
        with data_issue_email.collect_report() as report:
            try:
                result = spool.process_spool(
                    settings.SPOOL_ROOT_PATH,
                    settings.SPOOL_BATCH_SIZE,
                    settings.SPOOL_MAX_ATTEMPTS,
                    settings.SPOOL_COMPLETED_RETENTION_DAYS,
                )
            except Exception as error:
                ## errors before a queue result exists cannot use the recorded attempt limit.
                email_job_failure(f'{type(error).__name__}: {error}')
                raise
        output = json.dumps(dataclasses.asdict(result), sort_keys=True)
        if result.status == 'failed':
            if result.failed > 0:
                data_issue_email.email_report(report)
            technical_failure = result.technical_failure or report.failed_count > len(report.issues)
            if technical_failure and (result.failed > 0 or result.retried == 0):
                email_job_failure(output)
            raise CommandError(output)
        self.stdout.write(output)

import dataclasses
import datetime
import json
import logging
import socket
from pathlib import Path
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.mail import mail_admins
from django.core.management.base import BaseCommand, CommandError

from usep_indexer_app.lib import console_logging, data_issue_email, spool

log = logging.getLogger(__name__)
JOB_FAILURE_EMAIL_SUBJECT = 'USEP indexer: queue-processing failure'


def build_job_failure_message(output: str, error: str, hostname: str) -> str:
    """
    Builds administrator troubleshooting guidance using the current deployment's paths.

    Called by: email_job_failure()
    """
    timestamp: str = datetime.datetime.now(ZoneInfo(settings.TIME_ZONE)).strftime('%d/%b/%Y %H:%M:%S %Z (%z)')
    project_path: Path = Path(settings.BASE_DIR).resolve()
    spool_path: Path = Path(settings.SPOOL_ROOT_PATH).resolve()
    cron_log_path: Path = project_path.parent / 'logs' / 'process_spool_cron.log'
    log_filename: str | Path | None = settings.LOGGING.get('handlers', {}).get('logfile', {}).get('filename')
    application_log: str = 'No application logfile is configured; check the cron output below.'
    if log_filename:
        application_log = str(Path(log_filename).resolve())

    message: str = f'''The USEP indexer could not finish a queue-processing run.

Server: {hostname}
Time: {timestamp}
Command: manage.py process_spool
Project directory: {project_path}
Error: {error}

WHERE TO START

1. Check the application log around the time above:
   {application_log}

   Look for the ERROR entry and its complete traceback. Read the
   preceding messages too: indexing may have succeeded before
   a later queue-file operation failed.

2. If the application log does not contain the relevant traceback,
   check the cron output (confirm the redirection in the crontab):
   {cron_log_path}

3. Inspect the queue directories:
   {spool_path / 'processing'}
   {spool_path / 'pending'}
   {spool_path / 'failed'}

   For a PermissionError, check the affected file's ownership and
   permissions, its parent directory permissions, and the account
   running the cron command. File and directory permissions serve
   different purposes.

RETRIES AND COUNTS

Events left in processing/ are picked up again by subsequent cron
runs. An unresolved failure there can produce repeated emails.
Events in failed/ have stopped retrying and require attention.

When present, the counts below describe queue events, not
individual inscriptions.
An unexpected exception can also cause these counts to fall back
to zero, even if some indexing already succeeded.

PROCESSOR RESULT

{output}'''
    return message


def email_job_failure(output: str, error: str | None = None) -> None:
    """
    Emails technical failures to administrators without sending them ordinary data-review reports.

    Called by: Command.handle()
    """
    try:
        hostname: str = socket.gethostname()
        error_summary: str = error if error is not None else output
        message: str = build_job_failure_message(output, error_summary, hostname)
        subject: str = f'{JOB_FAILURE_EMAIL_SUBJECT} on {hostname}'
        mail_admins(subject, message, fail_silently=False)
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
                error_message: str = f'{type(error).__name__}: {error}'
                self.stderr.write(console_logging.format_message(error_message, level='ERROR'))
                self.stderr.flush()
                email_job_failure(error_message)
                raise
        output = json.dumps(dataclasses.asdict(result), sort_keys=True)
        if result.status == 'failed':
            if result.failed > 0:
                data_issue_email.email_report(report)
            technical_failure = result.technical_failure or report.failed_count > len(report.issues)
            if technical_failure and (result.failed > 0 or result.retried == 0):
                email_job_failure(output, error=result.error)
            raise CommandError(console_logging.format_message(output, level='ERROR'))
        self.stdout.write(console_logging.format_message(output))
        self.stdout.flush()

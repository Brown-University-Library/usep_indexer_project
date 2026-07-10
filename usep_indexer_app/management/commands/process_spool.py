import dataclasses
import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from usep_indexer_app.lib import spool


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
            raise CommandError(output)
        self.stdout.write(output)
        return

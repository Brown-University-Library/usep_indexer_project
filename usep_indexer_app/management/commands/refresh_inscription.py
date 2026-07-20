from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from usep_indexer_app.lib import reindex, spool


class Command(BaseCommand):
    """
    Refreshes the public files and Solr data for one inscription.

    Called by: Django management-command discovery
    """

    help = (
        "Refreshes one inscription's public representation: pulls and copies current USEP XML/resources for the "
        'browser-rendered detail page, then updates its Solr-backed search, collection, publication, metadata, and '
        'transcription data.'
    )

    def add_arguments(self, parser: CommandParser) -> None:
        """
        Adds the bare inscription ID argument.

        Called by: Django management-command runner
        """
        parser.add_argument('inscription_id', help='Inscription ID without a path or .xml extension.')
        return

    def handle(self, *args: object, **options: object) -> None:
        """
        Runs a locked single-inscription refresh and reports its result.

        Called by: Django management-command runner
        """
        del args
        inscription_id = str(options['inscription_id'])
        with spool.processor_lock(settings.SPOOL_ROOT_PATH) as lock_acquired:
            if not lock_acquired:
                raise CommandError('Another processor is active; the inscription was not refreshed.')
            try:
                inscription_path = reindex.process_single_reindex(inscription_id)
            except Exception as error:
                raise CommandError(f'Unable to refresh inscription {inscription_id!r}: {error}') from error
        self.stdout.write(self.style.SUCCESS(f'Refreshed inscription {inscription_id}: {inscription_path}'))
        return

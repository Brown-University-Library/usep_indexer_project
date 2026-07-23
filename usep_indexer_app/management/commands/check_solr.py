from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from usep_indexer_app.lib import solr_check


class Command(BaseCommand):
    """
    Checks required Solr access or prints the active schema.

    Called by: Django management-command discovery
    """

    help = 'Check required Solr access or print the active schema.'

    def add_arguments(self, parser: CommandParser) -> None:
        """
        Adds the active-schema output options.

        Called by: Django management-command runner
        """
        parser.add_argument(
            '--schema',
            action='store_true',
            help='Print the active Solr schema to standard output.',
        )
        parser.add_argument(
            '--schema-format',
            choices=('json', 'schema.xml'),
            help='Schema output format; valid only with --schema (default: json).',
        )
        return

    def handle(self, *args: object, **options: object) -> None:
        """
        Runs the selected safe access or schema check.

        Called by: Django management-command runner
        """
        del args
        show_schema = bool(options['schema'])
        schema_format_option = options['schema_format']
        schema_format = str(schema_format_option) if schema_format_option is not None else 'json'
        if schema_format_option is not None and not show_schema:
            raise CommandError('--schema-format requires --schema.')

        timeout = float(settings.SOLR_TIMEOUT_SECONDS)
        if timeout <= 0:
            raise CommandError('SOLR_TIMEOUT_SECONDS must be greater than zero.')
        if show_schema:
            self.handle_schema(timeout, schema_format)
        else:
            self.handle_required_access(timeout)
        return

    def handle_required_access(self, timeout: float) -> None:
        """
        Runs and reports the safe query and empty-update checks.

        Called by: handle()
        """
        try:
            result = solr_check.check_required_access(settings.SOLR_URL, timeout)
        except solr_check.SolrCheckError as error:
            raise CommandError(str(error)) from error
        self.stdout.write(self.style.SUCCESS(f'Solr /select access: ok (documents: {result.document_count})'))
        self.stdout.write(
            self.style.SUCCESS('Solr /update access: ok (empty update accepted; indexed documents unchanged)')
        )
        return

    def handle_schema(self, timeout: float, schema_format: str) -> None:
        """
        Prints the active schema and requires its unique-key field to be id.

        Called by: handle()
        """
        try:
            result = solr_check.retrieve_active_schema(settings.SOLR_URL, timeout, schema_format)
        except solr_check.SolrCheckError as error:
            raise CommandError(str(error)) from error
        self.stdout.write(result.text, ending='')
        try:
            solr_check.validate_expected_unique_key(result.unique_key)
        except solr_check.SolrCheckError as error:
            raise CommandError(str(error)) from error
        return

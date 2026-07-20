from django.core.management.base import BaseCommand, CommandError, CommandParser
from usep_indexer_app.lib import xml_validation


class Command(BaseCommand):
    """
    Validates one local or remote XML document.

    Called by: Django management-command discovery
    """

    help = 'Validate that a local or remote XML document is well-formed.'

    def add_arguments(self, parser: CommandParser) -> None:
        """
        Adds the local file path or HTTP(S) URL argument.

        Called by: Django management-command runner
        """
        parser.add_argument('source', help='Local file path or HTTP(S) URL to an XML document.')
        return

    def handle(self, *args: object, **options: object) -> None:
        """
        Validates the requested document and reports its result.

        Called by: Django management-command runner
        """
        del args
        source = str(options['source'])
        try:
            xml_validation.validate_xml(source)
        except xml_validation.XMLSourceError as error:
            raise CommandError(f'Unable to read XML source {source!r}: {error}') from error
        except xml_validation.XMLNotWellFormedError as error:
            raise CommandError(f'XML is not well-formed: {source!r}: {error}') from error
        self.stdout.write(self.style.SUCCESS(f'XML is well-formed: {source}'))
        return

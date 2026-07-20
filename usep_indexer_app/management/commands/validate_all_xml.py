from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from usep_indexer_app.lib import xml_validation


class Command(BaseCommand):
    """
    Validates every XML file in the configured inscription source tree.

    Called by: Django management-command discovery
    """

    help = 'Validate all XML files in the configured usep-data inscription tree.'

    def handle(self, *args: object, **options: object) -> None:
        """
        Validates all inscription XML and reports counts and failures.

        Called by: Django management-command runner
        """
        del args, options
        inscriptions_path = settings.USEP_DATA_GIT_CLONED_DIR_PATH / 'xml_inscriptions'
        try:
            result = xml_validation.validate_xml_directory(inscriptions_path)
        except xml_validation.XMLSourceError as error:
            raise CommandError(f'Unable to validate inscription XML: {error}') from error

        self.stdout.write(f'XML directory: {inscriptions_path}')
        self.stdout.write(f'Files checked: {result.checked_count}')
        self.stdout.write(f'Well-formed: {result.well_formed_count}')
        self.stdout.write(f'Not well-formed: {len(result.failures)}')
        if result.failures:
            self.stdout.write('Not well-formed entries:')
            for failure in result.failures:
                self.stdout.write(f'- {failure.path.as_posix()}: {failure.error}')
            failure_count = len(result.failures)
            file_label = 'file' if failure_count == 1 else 'files'
            raise CommandError(f'Found {failure_count} XML {file_label} that are not well-formed.')
        self.stdout.write('Not well-formed entries: none')
        return

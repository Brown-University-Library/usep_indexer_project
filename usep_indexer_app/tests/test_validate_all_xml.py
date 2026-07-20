import io
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings


class ValidateAllXMLCommandTests(SimpleTestCase):
    """
    Checks recursive validation of the configured inscription XML tree.
    """

    def test_all_well_formed_xml_reports_success_counts(self) -> None:
        """
        Checks recursive success counts while ignoring non-XML files.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            usep_data_path = Path(temporary_directory) / 'usep-data'
            inscriptions_path = usep_data_path / 'xml_inscriptions'
            (inscriptions_path / 'bib_only').mkdir(parents=True)
            (inscriptions_path / 'transcribed').mkdir()
            (inscriptions_path / 'bib_only' / 'one.xml').write_bytes(b'<root />')
            (inscriptions_path / 'transcribed' / 'two.xml').write_bytes(b'<root><child /></root>')
            (inscriptions_path / 'README.md').write_text('Not XML.', encoding='utf-8')
            output = io.StringIO()

            with override_settings(USEP_DATA_GIT_CLONED_DIR_PATH=usep_data_path):
                call_command('validate_all_xml', stdout=output)

        command_output = output.getvalue()
        self.assertIn('Files checked: 2', command_output)
        self.assertIn('Well-formed: 2', command_output)
        self.assertIn('Not well-formed: 0', command_output)
        self.assertIn('Not well-formed entries: none', command_output)

    def test_malformed_xml_reports_all_failures_and_counts(self) -> None:
        """
        Checks that every malformed file is listed before the command fails.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            usep_data_path = Path(temporary_directory) / 'usep-data'
            inscriptions_path = usep_data_path / 'xml_inscriptions'
            (inscriptions_path / 'metadata_only').mkdir(parents=True)
            (inscriptions_path / 'transcribed').mkdir()
            (inscriptions_path / 'metadata_only' / 'one.xml').write_bytes(b'<root />')
            (inscriptions_path / 'metadata_only' / 'broken-one.xml').write_bytes(b'<root>')
            (inscriptions_path / 'transcribed' / 'broken-two.xml').write_bytes(b'<root><child></root>')
            output = io.StringIO()

            with override_settings(USEP_DATA_GIT_CLONED_DIR_PATH=usep_data_path):
                with self.assertRaisesMessage(CommandError, 'Found 2 XML files'):
                    call_command('validate_all_xml', stdout=output, stderr=io.StringIO())

        command_output = output.getvalue()
        self.assertIn('Files checked: 3', command_output)
        self.assertIn('Well-formed: 1', command_output)
        self.assertIn('Not well-formed: 2', command_output)
        self.assertIn('- metadata_only/broken-one.xml:', command_output)
        self.assertIn('- transcribed/broken-two.xml:', command_output)

    def test_missing_inscriptions_directory_fails(self) -> None:
        """
        Checks that a missing configured inscription directory fails clearly.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            usep_data_path = Path(temporary_directory) / 'usep-data'

            with override_settings(USEP_DATA_GIT_CLONED_DIR_PATH=usep_data_path):
                with self.assertRaisesMessage(CommandError, 'Directory does not exist'):
                    call_command('validate_all_xml', stderr=io.StringIO())

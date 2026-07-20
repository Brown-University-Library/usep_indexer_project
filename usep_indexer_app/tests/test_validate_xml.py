import io
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase


class ValidateXMLCommandTests(SimpleTestCase):
    """
    Checks local and remote XML validation through the management command.
    """

    def test_valid_local_xml_succeeds(self) -> None:
        """
        Checks that a well-formed local XML file succeeds.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            xml_path = Path(temporary_directory) / 'valid.xml'
            xml_path.write_bytes(b'<?xml version="1.0"?><root><child /></root>')
            output = io.StringIO()

            call_command('validate_xml', str(xml_path), stdout=output)

        self.assertEqual(f'XML is well-formed: {xml_path}\n', output.getvalue())

    def test_malformed_local_xml_fails(self) -> None:
        """
        Checks that a malformed local XML file raises a command error.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            xml_path = Path(temporary_directory) / 'malformed.xml'
            xml_path.write_bytes(b'<root><child></root>')

            with self.assertRaisesMessage(CommandError, 'XML is not well-formed'):
                call_command('validate_xml', str(xml_path), stderr=io.StringIO())

    @patch('usep_indexer_app.lib.xml_validation.httpx.get')
    def test_valid_remote_xml_succeeds(self, mock_get) -> None:
        """
        Checks that a well-formed remote XML response succeeds.
        """
        source = 'https://example.org/inscription.xml'
        mock_get.return_value = httpx.Response(
            200,
            content=b'<root />',
            request=httpx.Request('GET', source),
        )
        output = io.StringIO()

        call_command('validate_xml', source, stdout=output)

        self.assertEqual(f'XML is well-formed: {source}\n', output.getvalue())
        mock_get.assert_called_once_with(source, follow_redirects=True, timeout=30.0)

    @patch('usep_indexer_app.lib.xml_validation.httpx.get')
    def test_remote_http_error_fails(self, mock_get) -> None:
        """
        Checks that a failed remote request raises a command error.
        """
        source = 'https://example.org/missing.xml'
        mock_get.return_value = httpx.Response(
            404,
            request=httpx.Request('GET', source),
        )

        with self.assertRaisesMessage(CommandError, 'Unable to read XML source'):
            call_command('validate_xml', source, stderr=io.StringIO())

    @patch('usep_indexer_app.lib.xml_validation.httpx.get')
    def test_malformed_remote_xml_fails(self, mock_get) -> None:
        """
        Checks that a malformed remote XML response raises a command error.
        """
        source = 'https://example.org/malformed.xml'
        mock_get.return_value = httpx.Response(
            200,
            content=b'<root><child></root>',
            request=httpx.Request('GET', source),
        )

        with self.assertRaisesMessage(CommandError, 'XML is not well-formed'):
            call_command('validate_xml', source, stderr=io.StringIO())

    def test_missing_local_file_fails(self) -> None:
        """
        Checks that a missing local file raises a command error.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            xml_path = Path(temporary_directory) / 'missing.xml'

            with self.assertRaisesMessage(CommandError, 'Unable to read XML source'):
                call_command('validate_xml', str(xml_path), stderr=io.StringIO())

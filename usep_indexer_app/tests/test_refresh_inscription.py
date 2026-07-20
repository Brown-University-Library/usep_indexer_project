import fcntl
import io
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings
from usep_indexer_app.lib import spool
from usep_indexer_app.management.commands.refresh_inscription import Command


class RefreshInscriptionCommandTests(SimpleTestCase):
    """
    Checks the immediate single-inscription refresh command.
    """

    def test_help_briefly_explains_both_refresh_outputs(self) -> None:
        """
        Checks command help describes the public files and Solr data in at most 50 words.
        """
        self.assertLessEqual(len(Command.help.split()), 50)
        self.assertIn('browser-rendered detail page', Command.help)
        self.assertIn('Solr-backed search', Command.help)

    @patch('usep_indexer_app.management.commands.refresh_inscription.reindex.process_single_reindex')
    def test_command_refreshes_immediately_while_holding_processor_lock(self, mock_process_single_reindex) -> None:
        """
        Checks the command runs the requested refresh and reports success.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            spool_root = Path(temporary_directory) / 'spool'
            inscription_path = Path(temporary_directory) / 'webserved-data' / 'inscriptions' / 'one.xml'
            mock_process_single_reindex.return_value = inscription_path
            output = io.StringIO()
            with override_settings(SPOOL_ROOT_PATH=spool_root):
                call_command('refresh_inscription', 'one', stdout=output)

        mock_process_single_reindex.assert_called_once_with('one')
        self.assertIn(f'Refreshed inscription one: {inscription_path}', output.getvalue())

    @patch('usep_indexer_app.management.commands.refresh_inscription.reindex.process_single_reindex')
    def test_command_fails_without_refreshing_when_processor_lock_is_held(self, mock_process_single_reindex) -> None:
        """
        Checks an active processor prevents overlapping manual refreshing.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            spool_root = Path(temporary_directory) / 'spool'
            spool.ensure_spool_directories(spool_root)
            lock_path = spool_root / 'processor.lock'
            with lock_path.open('a+', encoding='utf-8') as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with override_settings(SPOOL_ROOT_PATH=spool_root):
                    with self.assertRaisesRegex(CommandError, 'Another processor is active'):
                        call_command('refresh_inscription', 'one')
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

        mock_process_single_reindex.assert_not_called()

    @patch(
        'usep_indexer_app.management.commands.refresh_inscription.reindex.process_single_reindex',
        side_effect=RuntimeError('Solr unavailable'),
    )
    def test_command_reports_any_refresh_failure(self, mock_process_single_reindex) -> None:
        """
        Checks failures from the refresh workflow produce a command error.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            with override_settings(SPOOL_ROOT_PATH=Path(temporary_directory) / 'spool'):
                with self.assertRaisesRegex(CommandError, "Unable to refresh inscription 'one': Solr unavailable"):
                    call_command('refresh_inscription', 'one')

        mock_process_single_reindex.assert_called_once_with('one')

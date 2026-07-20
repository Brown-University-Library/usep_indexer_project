import fcntl
import io
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings
from usep_indexer_app.lib import spool


class ReindexInscriptionCommandTests(SimpleTestCase):
    """
    Checks the immediate single-inscription management command.
    """

    @patch('usep_indexer_app.management.commands.reindex_inscription.reindex.process_single_reindex')
    def test_command_reindexes_immediately_while_holding_processor_lock(self, mock_process_single_reindex) -> None:
        """
        Checks the command runs the requested reindex and reports success.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            spool_root = Path(temporary_directory) / 'spool'
            inscription_path = Path(temporary_directory) / 'webserved-data' / 'inscriptions' / 'one.xml'
            mock_process_single_reindex.return_value = inscription_path
            output = io.StringIO()
            with override_settings(SPOOL_ROOT_PATH=spool_root):
                call_command('reindex_inscription', 'one', stdout=output)

        mock_process_single_reindex.assert_called_once_with('one')
        self.assertIn(f'Reindexed inscription one: {inscription_path}', output.getvalue())

    @patch('usep_indexer_app.management.commands.reindex_inscription.reindex.process_single_reindex')
    def test_command_fails_without_reindexing_when_processor_lock_is_held(self, mock_process_single_reindex) -> None:
        """
        Checks an active processor prevents overlapping manual reindexing.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            spool_root = Path(temporary_directory) / 'spool'
            spool.ensure_spool_directories(spool_root)
            lock_path = spool_root / 'processor.lock'
            with lock_path.open('a+', encoding='utf-8') as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with override_settings(SPOOL_ROOT_PATH=spool_root):
                    with self.assertRaisesRegex(CommandError, 'Another processor is active'):
                        call_command('reindex_inscription', 'one')
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

        mock_process_single_reindex.assert_not_called()

    @patch(
        'usep_indexer_app.management.commands.reindex_inscription.reindex.process_single_reindex',
        side_effect=RuntimeError('Solr unavailable'),
    )
    def test_command_reports_any_reindex_failure(self, mock_process_single_reindex) -> None:
        """
        Checks failures from the immediate workflow produce a command error.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            with override_settings(SPOOL_ROOT_PATH=Path(temporary_directory) / 'spool'):
                with self.assertRaisesRegex(CommandError, "Unable to reindex inscription 'one': Solr unavailable"):
                    call_command('reindex_inscription', 'one')

        mock_process_single_reindex.assert_called_once_with('one')

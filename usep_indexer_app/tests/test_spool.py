import datetime
import io
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings
from usep_indexer_app.lib import spool


class SpoolTests(SimpleTestCase):
    """
    Checks the durable filesystem queue and locked processor.
    """

    def test_atomic_event_write_creates_one_valid_pending_file(self) -> None:
        """
        Checks that a successful write creates a complete schema-valid event.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            spool_root = Path(temporary_directory)
            with self.assertLogs('usep_indexer_app.lib.spool', level='DEBUG') as captured_logs:
                event_path = spool.write_event(
                    spool_root,
                    'incremental',
                    files_updated=['xml_inscriptions/transcribed/one.xml'],
                    request_id='delivery-1',
                )
            event = spool.load_event(event_path)
            temporary_files = list((spool_root / 'pending').glob('*.tmp'))

        self.assertEqual('incremental', event.event_type)
        self.assertEqual('delivery-1', event.request_id)
        self.assertEqual(['xml_inscriptions/transcribed/one.xml'], event.files_updated)
        self.assertEqual([], temporary_files)
        joined_logs = '\n'.join(captured_logs.output)
        self.assertIn('event saved; event_type, ``incremental``', joined_logs)
        self.assertIn('request_id, ``delivery-1``', joined_logs)

    @override_settings(TIME_ZONE='America/New_York')
    @patch(
        'usep_indexer_app.lib.spool.utc_now',
        return_value=datetime.datetime(2026, 7, 14, 23, 33, 30, 123456, tzinfo=datetime.UTC),
    )
    def test_event_filename_uses_local_24_hour_timestamp(self, mock_utc_now) -> None:
        """
        Checks that event filenames show local 24-hour time with its UTC offset.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            event_path = spool.write_event(Path(temporary_directory), 'incremental')
            event = spool.load_event(event_path)

        self.assertTrue(event_path.name.startswith('20260714T193330.123456-0400_'))
        self.assertEqual('2026-07-14T23:33:30.123456+00:00', event.received_at)
        mock_utc_now.assert_called_once_with()

    @patch('usep_indexer_app.lib.spool.os.replace', side_effect=OSError('disk full'))
    def test_failed_atomic_write_removes_temporary_file(self, mock_replace) -> None:
        """
        Checks cleanup when an event cannot be atomically installed.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            spool_root = Path(temporary_directory)
            with self.assertRaises(OSError):
                spool.write_event(spool_root, 'incremental')
            pending_files = list((spool_root / 'pending').iterdir())

        self.assertEqual([], pending_files)
        mock_replace.assert_called_once()

    def test_coalescing_uses_newest_event_state(self) -> None:
        """
        Checks that newer updates and removals replace earlier path states.
        """
        older = self.build_test_event(
            event_id='00000000-0000-0000-0000-000000000001',
            received_at='2026-07-10T10:00:00+00:00',
            files_updated=['one.xml'],
            files_removed=['two.xml'],
        )
        newer = self.build_test_event(
            event_id='00000000-0000-0000-0000-000000000002',
            received_at='2026-07-10T10:01:00+00:00',
            files_updated=['two.xml'],
            files_removed=['one.xml'],
        )

        files_updated, files_removed, full_reindex = spool.coalesce_events([newer, older])

        self.assertEqual(['two.xml'], files_updated)
        self.assertEqual(['one.xml'], files_removed)
        self.assertFalse(full_reindex)

    @patch('usep_indexer_app.lib.spool.processor.process_incremental')
    def test_invalid_event_is_quarantined_while_valid_event_processes(self, mock_process) -> None:
        """
        Checks that one malformed event does not block a valid event batch.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            spool_root = Path(temporary_directory)
            spool.write_event(spool_root, 'incremental', files_updated=['one.xml'])
            (spool_root / 'pending' / 'bad.json').write_text('{bad json', encoding='utf-8')
            unsupported_document = spool.build_event_document('incremental')
            unsupported_document['schema_version'] = 99
            (spool_root / 'pending' / 'unsupported.json').write_text(
                json.dumps(unsupported_document),
                encoding='utf-8',
            )

            result = spool.process_spool(spool_root, batch_size=100, max_attempts=3, retention_days=30)

            completed_count = len(list((spool_root / 'completed').glob('*.json')))
            quarantine_count = len(list((spool_root / 'quarantine').glob('*.json')))

        self.assertEqual('success', result.status)
        self.assertEqual(1, result.processed)
        self.assertEqual(2, result.quarantined)
        self.assertEqual(1, completed_count)
        self.assertEqual(2, quarantine_count)
        mock_process.assert_called_once_with(['one.xml'], [])

    @patch('usep_indexer_app.lib.spool.reindex.process_full_reindex')
    @patch('usep_indexer_app.lib.spool.processor.process_incremental')
    def test_full_reindex_event_selects_full_workflow(self, mock_incremental, mock_full_reindex) -> None:
        """
        Checks that any full-reindex event controls the claimed batch workflow.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            spool_root = Path(temporary_directory)
            spool.write_event(spool_root, 'incremental', files_updated=['one.xml'])
            spool.write_event(spool_root, 'full_reindex')

            result = spool.process_spool(spool_root, batch_size=100, max_attempts=3, retention_days=30)

        self.assertEqual(2, result.processed)
        mock_full_reindex.assert_called_once_with()
        mock_incremental.assert_not_called()

    @patch('usep_indexer_app.lib.spool.processor.process_incremental', side_effect=RuntimeError('Solr down'))
    def test_processing_failure_returns_event_to_pending(self, mock_process) -> None:
        """
        Checks that a failed event below the attempt limit is retried later.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            spool_root = Path(temporary_directory)
            spool.write_event(spool_root, 'incremental')

            result = spool.process_spool(spool_root, batch_size=100, max_attempts=3, retention_days=30)

            pending_path = next((spool_root / 'pending').glob('*.json'))
            pending_document = json.loads(pending_path.read_text(encoding='utf-8'))

        self.assertEqual('failed', result.status)
        self.assertEqual(1, result.retried)
        self.assertEqual(1, pending_document['attempts'])
        self.assertIsNotNone(pending_document['last_attempt_at'])
        mock_process.assert_called_once_with([], [])

    @patch('usep_indexer_app.lib.spool.processor.process_incremental', side_effect=RuntimeError('Solr down'))
    def test_third_processing_failure_moves_event_to_failed(self, mock_process) -> None:
        """
        Checks retry metadata and the three-attempt terminal failure policy.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            spool_root = Path(temporary_directory)
            event_path = spool.write_event(spool_root, 'incremental')
            document = json.loads(event_path.read_text(encoding='utf-8'))
            document['attempts'] = 2
            spool.write_json_atomic(document, event_path)

            result = spool.process_spool(spool_root, batch_size=100, max_attempts=3, retention_days=30)

            failed_path = next((spool_root / 'failed').glob('*.json'))
            failed_document = json.loads(failed_path.read_text(encoding='utf-8'))

        self.assertEqual('failed', result.status)
        self.assertEqual(1, result.failed)
        self.assertEqual(3, failed_document['attempts'])
        self.assertIn('RuntimeError: Solr down', failed_document['last_error'])
        mock_process.assert_called_once_with([], [])

    @patch('usep_indexer_app.lib.spool.processor.process_incremental')
    def test_processing_file_is_replayed_after_crash(self, mock_process) -> None:
        """
        Checks that an event left in processing is recovered on the next run.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            spool_root = Path(temporary_directory)
            event_path = spool.write_event(spool_root, 'incremental', files_updated=['one.xml'])
            processing_path = spool_root / 'processing' / event_path.name
            os.replace(event_path, processing_path)

            result = spool.process_spool(spool_root, batch_size=100, max_attempts=3, retention_days=30)

            completed_count = len(list((spool_root / 'completed').glob('*.json')))

        self.assertEqual(1, result.processed)
        self.assertEqual(1, completed_count)
        mock_process.assert_called_once_with(['one.xml'], [])

    def test_completed_retention_removes_only_expired_files(self) -> None:
        """
        Checks 30-day cleanup while preserving newer completed events.
        """
        current_time = datetime.datetime(2026, 7, 10, tzinfo=datetime.UTC)
        with tempfile.TemporaryDirectory() as temporary_directory:
            spool_root = Path(temporary_directory)
            spool.ensure_spool_directories(spool_root)
            expired_path = spool_root / 'completed' / 'expired.json'
            current_path = spool_root / 'completed' / 'current.json'
            expired_path.write_text('{}', encoding='utf-8')
            current_path.write_text('{}', encoding='utf-8')
            expired_timestamp = current_time.timestamp() - (31 * 24 * 60 * 60)
            os.utime(expired_path, (expired_timestamp, expired_timestamp))

            cleaned_count = spool.clean_completed_events(spool_root, 30, current_time)

            expired_exists = expired_path.exists()
            current_exists = current_path.exists()

        self.assertEqual(1, cleaned_count)
        self.assertFalse(expired_exists)
        self.assertTrue(current_exists)

    @patch('usep_indexer_app.lib.spool.processor.process_incremental')
    def test_completion_age_starts_when_processing_succeeds(self, mock_process) -> None:
        """
        Checks that an old pending event receives a fresh completed-retention age.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            spool_root = Path(temporary_directory)
            event_path = spool.write_event(spool_root, 'incremental')
            old_timestamp = spool.utc_now().timestamp() - (31 * 24 * 60 * 60)
            os.utime(event_path, (old_timestamp, old_timestamp))

            result = spool.process_spool(spool_root, batch_size=100, max_attempts=3, retention_days=30)

            completed_count = len(list((spool_root / 'completed').glob('*.json')))

        self.assertEqual(1, result.processed)
        self.assertEqual(0, result.cleaned)
        self.assertEqual(1, completed_count)
        mock_process.assert_called_once_with([], [])

    @patch('usep_indexer_app.lib.spool.fcntl.flock', side_effect=BlockingIOError)
    @patch('usep_indexer_app.lib.spool.claim_events')
    def test_lock_contention_exits_without_claiming(self, mock_claim_events, mock_flock) -> None:
        """
        Checks that a concurrent invocation does not claim any work.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = spool.process_spool(Path(temporary_directory), 100, 3, 30)

        self.assertEqual('locked', result.status)
        mock_claim_events.assert_not_called()
        mock_flock.assert_called_once()

    def test_processor_health_distinguishes_fresh_and_stale_status(self) -> None:
        """
        Checks processing-health values and filesystem-queue backlog fields.
        """
        current_time = datetime.datetime(2026, 7, 10, 12, 0, tzinfo=datetime.UTC)
        with tempfile.TemporaryDirectory() as temporary_directory:
            spool_root = Path(temporary_directory)
            spool.ensure_spool_directories(spool_root)
            spool.write_processor_status(
                spool_root,
                {
                    'status': 'success',
                    'started_at': '2026-07-10T11:59:00+00:00',
                    'finished_at': '2026-07-10T11:59:01+00:00',
                },
            )
            spool.write_event(spool_root, 'incremental')

            fresh_health = spool.get_processor_health(spool_root, 300, current_time)
            stale_health = spool.get_processor_health(
                spool_root,
                30,
                current_time,
            )

        self.assertEqual('processing_active', fresh_health['result'])
        self.assertEqual(1, fresh_health['pending_count'])
        self.assertEqual('processing_not_active', stale_health['result'])

    def test_management_command_processes_configured_spool(self) -> None:
        """
        Checks that a successful cron-facing command emits output without email.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = io.StringIO()
            with override_settings(SPOOL_ROOT_PATH=Path(temporary_directory)):
                call_command('process_spool', stdout=output)

        result = json.loads(output.getvalue())
        self.assertEqual('success', result['status'])
        self.assertEqual(0, result['claimed'])
        self.assertEqual([], mail.outbox)

    @override_settings(ADMINS=[('Test Admin', 'admin@example.org')])
    @patch(
        'usep_indexer_app.management.commands.process_spool.spool.process_spool',
        return_value=spool.ProcessResult(status='failed', claimed=1, retried=1, error='XMLSyntaxError: malformed XML'),
    )
    def test_management_command_emails_admins_once_on_job_failure(self, mock_process) -> None:
        """
        Checks that one failed job sends one summary email before raising.
        """
        with self.assertRaises(CommandError):
            call_command('process_spool', stdout=io.StringIO(), stderr=io.StringIO())

        self.assertEqual(1, len(mail.outbox))
        self.assertIn('USEP spool-processing job failed', mail.outbox[0].subject)
        self.assertIn('"status": "failed"', mail.outbox[0].body)
        self.assertIn('XMLSyntaxError: malformed XML', mail.outbox[0].body)
        mock_process.assert_called_once()

    @patch(
        'usep_indexer_app.management.commands.process_spool.mail_admins',
        side_effect=RuntimeError('mail server unavailable'),
    )
    @patch(
        'usep_indexer_app.management.commands.process_spool.spool.process_spool',
        return_value=spool.ProcessResult(status='failed', claimed=1, retried=1, error='XMLSyntaxError: malformed XML'),
    )
    def test_email_delivery_failure_does_not_hide_job_failure(self, mock_process, mock_mail_admins) -> None:
        """
        Checks that an email-backend error is logged before the failed command raises.
        """
        with self.assertLogs('usep_indexer_app.management.commands.process_spool', level='ERROR') as captured_logs:
            with self.assertRaises(CommandError):
                call_command('process_spool', stdout=io.StringIO(), stderr=io.StringIO())

        self.assertIn('Unable to email Django admins', '\n'.join(captured_logs.output))
        mock_process.assert_called_once()
        mock_mail_admins.assert_called_once_with(
            'USEP spool-processing job failed',
            (
                'The USEP spool-processing job failed.\n\nProcessor result:\n'
                '{"claimed": 1, "cleaned": 0, "error": "XMLSyntaxError: malformed XML", "failed": 0, '
                '"processed": 0, "quarantined": 0, "retried": 1, "status": "failed"}'
            ),
            fail_silently=False,
        )

    def build_test_event(
        self,
        event_id: str,
        received_at: str,
        files_updated: list[str],
        files_removed: list[str],
    ) -> spool.SpoolEvent:
        """
        Checks construction of a validated event value for coalescing tests.

        Called by: test_coalescing_uses_newest_event_state()
        """
        event = spool.SpoolEvent(
            path=Path(f'{event_id}.json'),
            schema_version=1,
            event_id=event_id,
            event_type='incremental',
            received_at=received_at,
            request_id=event_id,
            files_updated=files_updated,
            files_removed=files_removed,
            attempts=0,
            last_attempt_at=None,
            last_error=None,
        )
        return event

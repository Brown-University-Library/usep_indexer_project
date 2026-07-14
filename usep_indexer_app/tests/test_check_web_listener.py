import pathlib
import sys
import tempfile
from unittest.mock import patch

from django.test import SimpleTestCase

import check_web_listener


class CheckWebListenerArgumentTests(SimpleTestCase):
    """
    Checks payload-path selection for the local HTTP listener script.
    """

    def test_default_payload_path_is_absolute(self) -> None:
        """
        Checks that omitting the option retains the bundled payload fixture.
        """
        with patch.object(sys, 'argv', ['check_web_listener.py']):
            payload_path, use_real_directory = check_web_listener.parse_arguments()

        self.assertEqual(check_web_listener.DEFAULT_PAYLOAD_PATH.resolve(), payload_path)
        self.assertTrue(payload_path.is_absolute())
        self.assertFalse(use_real_directory)

    def test_relative_payload_path_resolves_from_working_directory(self) -> None:
        """
        Checks that a project-root-relative custom payload becomes an absolute path.
        """
        relative_path = pathlib.Path('debug_payloads/real-push.json')
        project_root = pathlib.Path.cwd()
        with patch.object(sys, 'argv', ['check_web_listener.py', '--payload', str(relative_path)]):
            payload_path, use_real_directory = check_web_listener.parse_arguments()

        self.assertEqual((project_root / relative_path).resolve(), payload_path)
        self.assertTrue(payload_path.is_absolute())
        self.assertFalse(use_real_directory)

    def test_real_directory_flag_is_enabled(self) -> None:
        """
        Checks that the persistent event-directory mode requires its explicit flag.
        """
        with patch.object(sys, 'argv', ['check_web_listener.py', '--use-real-directory']):
            _, use_real_directory = check_web_listener.parse_arguments()

        self.assertTrue(use_real_directory)


class CheckWebListenerSpoolTests(SimpleTestCase):
    """
    Checks selection and inspection of the event spool.
    """

    @patch('check_web_listener.dotenv_values', return_value={'SPOOL_ROOT_PATH': 'var/usep-spool'})
    def test_real_spool_root_uses_only_outer_environment_value(self, mock_dotenv_values) -> None:
        """
        Checks that a relative configured spool path resolves from the project root.
        """
        spool_root = check_web_listener.load_real_spool_root()

        self.assertEqual((check_web_listener.PROJECT_ROOT_PATH / 'var/usep-spool').resolve(), spool_root)
        mock_dotenv_values.assert_called_once_with(check_web_listener.DOTENV_PATH)

    @patch('check_web_listener.dotenv_values', return_value={})
    def test_real_spool_root_requires_configuration(self, mock_dotenv_values) -> None:
        """
        Checks that real-directory mode fails clearly when the spool path is absent.
        """
        with self.assertRaisesRegex(RuntimeError, 'SPOOL_ROOT_PATH is not configured'):
            check_web_listener.load_real_spool_root()

        mock_dotenv_values.assert_called_once_with(check_web_listener.DOTENV_PATH)

    def test_new_request_event_isolated_from_other_pending_events(self) -> None:
        """
        Checks that existing and concurrently added events do not hide this script's event.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            spool_root = pathlib.Path(temporary_directory) / 'spool'
            existing_event = check_web_listener.spool.write_event(
                spool_root,
                'incremental',
                request_id='existing-delivery',
            )
            previous_pending_events = {existing_event}
            check_web_listener.spool.write_event(spool_root, 'incremental', request_id='other-new-delivery')
            expected_event = check_web_listener.spool.write_event(
                spool_root,
                'incremental',
                request_id='script-delivery',
            )

            matching_events = check_web_listener.find_new_request_events(
                spool_root,
                previous_pending_events,
                'script-delivery',
            )

        self.assertEqual([expected_event], matching_events)

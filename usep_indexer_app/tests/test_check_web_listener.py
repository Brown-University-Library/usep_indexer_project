import logging
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


class CheckWebListenerLoggingTests(SimpleTestCase):
    """
    Checks terminal and persistent logging for the local HTTP listener script.
    """

    def setUp(self) -> None:
        """
        Preserves the logging configuration replaced by each test.

        Called by: Django test runner
        """
        super().setUp()
        self.configured_loggers: list[logging.Logger] = [
            logging.getLogger(logger_name) for logger_name in (check_web_listener.__name__, 'usep_indexer_app')
        ]
        self.original_configurations: list[tuple[list[logging.Handler], int, bool]] = [
            (list(configured_logger.handlers), configured_logger.level, configured_logger.propagate)
            for configured_logger in self.configured_loggers
        ]
        return

    def tearDown(self) -> None:
        """
        Restores the logging configuration after each test.

        Called by: Django test runner
        """
        replacement_handlers = {
            handler for configured_logger in self.configured_loggers for handler in configured_logger.handlers
        }
        for configured_logger, original_configuration in zip(
            self.configured_loggers,
            self.original_configurations,
            strict=True,
        ):
            original_handlers, original_level, original_propagate = original_configuration
            configured_logger.handlers = original_handlers
            configured_logger.setLevel(original_level)
            configured_logger.propagate = original_propagate
        for handler in replacement_handlers:
            handler.close()
        super().tearDown()
        return

    def test_real_directory_mode_logs_script_and_application_messages_to_file(self) -> None:
        """
        Checks that real-directory mode adds the configured file handler.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            log_path = pathlib.Path(temporary_directory) / 'usep_indexer.log'
            environment_values = {'LOG_PATH': str(log_path), 'LOG_LEVEL': 'DEBUG'}
            with patch('check_web_listener.dotenv_values', return_value=environment_values):
                check_web_listener.configure_logging(use_real_directory=True)

            check_web_listener.log.info('script-file-log-check')
            logging.getLogger('usep_indexer_app.test').debug('application-file-log-check')
            for configured_logger in self.configured_loggers:
                for handler in configured_logger.handlers:
                    handler.flush()

            log_contents = log_path.read_text()

        self.assertIn('script-file-log-check', log_contents)
        self.assertIn('application-file-log-check', log_contents)

    @patch('check_web_listener.dotenv_values')
    def test_temporary_directory_mode_does_not_load_file_logging(self, mock_dotenv_values) -> None:
        """
        Checks that isolated mode retains terminal-only logging without requiring the outer environment file.
        """
        check_web_listener.configure_logging(use_real_directory=False)

        configured_handlers = {
            handler for configured_logger in self.configured_loggers for handler in configured_logger.handlers
        }

        self.assertEqual(1, len(configured_handlers))
        self.assertTrue(all(type(handler) is logging.StreamHandler for handler in configured_handlers))
        mock_dotenv_values.assert_not_called()

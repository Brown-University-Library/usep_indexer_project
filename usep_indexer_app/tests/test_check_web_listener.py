import pathlib
import sys
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
            payload_path = check_web_listener.parse_arguments()

        self.assertEqual(check_web_listener.DEFAULT_PAYLOAD_PATH.resolve(), payload_path)
        self.assertTrue(payload_path.is_absolute())

    def test_relative_payload_path_resolves_from_working_directory(self) -> None:
        """
        Checks that a project-root-relative custom payload becomes an absolute path.
        """
        relative_path = pathlib.Path('debug_payloads/real-push.json')
        project_root = pathlib.Path.cwd()
        with patch.object(sys, 'argv', ['check_web_listener.py', '--payload', str(relative_path)]):
            payload_path = check_web_listener.parse_arguments()

        self.assertEqual((project_root / relative_path).resolve(), payload_path)
        self.assertTrue(payload_path.is_absolute())

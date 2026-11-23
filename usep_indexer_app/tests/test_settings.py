import os
import pathlib
import runpy
import tempfile
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase
from dotenv import dotenv_values


class DataTeamSettingsTests(SimpleTestCase):
    """
    Checks required email configuration using isolated copies of the production settings module.
    """

    def setUp(self) -> None:
        """
        Checks settings read only a temporary environment and public example values.
        """
        temporary_directory: tempfile.TemporaryDirectory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root: pathlib.Path = pathlib.Path(temporary_directory.name)
        (root / '.env').write_text('', encoding='utf-8')
        self.settings_path: pathlib.Path = root / 'project' / 'config' / 'settings.py'
        self.settings_path.parent.mkdir(parents=True)
        config_directory: pathlib.Path = settings.BASE_DIR / 'config'
        self.settings_path.write_text((config_directory / 'settings.py').read_text(encoding='utf-8'), encoding='utf-8')
        self.environment: dict[str, str] = {
            name: value
            for name, value in dotenv_values(config_directory / 'example.env', interpolate=False).items()
            if value is not None
        }

    def load_settings(self, environment: dict[str, str]) -> dict[str, object]:
        """
        Checks settings declarations without initializing log handlers, mail, databases, or indexing.
        """
        with patch.dict(os.environ, environment, clear=True):
            namespace: dict[str, object] = runpy.run_path(str(self.settings_path))
        return namespace

    def test_missing_data_team_variables_fail_during_settings_load(self) -> None:
        """
        Checks each missing variable stops startup and identifies its name.
        """
        for name in ('USEP_DATA_TEAM_EMAILS_JSON', 'USEP_DATA_TEAM_CC_EMAILS_JSON', 'USEP_DATA_TEAM_EMAIL_LABEL'):
            environment: dict[str, str] = self.environment.copy()
            del environment[name]
            with self.subTest(setting=name):
                with self.assertRaises(KeyError) as captured:
                    self.load_settings(environment)
                self.assertEqual((name,), captured.exception.args)

    def test_explicit_empty_recipient_lists_disable_reporting(self) -> None:
        """
        Checks explicitly empty primary and CC lists remain valid configuration.
        """
        namespace: dict[str, object] = self.load_settings(self.environment)
        self.assertEqual([], namespace['USEP_DATA_TEAM_EMAILS'])
        self.assertEqual([], namespace['USEP_DATA_TEAM_CC_EMAILS'])
        self.assertEqual('Development', namespace['USEP_DATA_TEAM_EMAIL_LABEL'])

    def test_configured_recipients_and_label_load(self) -> None:
        """
        Checks configured primary recipients, CC recipients, and environment label are retained.
        """
        environment: dict[str, str] = self.environment | {
            'USEP_DATA_TEAM_EMAILS_JSON': '["editor@example.org"]',
            'USEP_DATA_TEAM_CC_EMAILS_JSON': '["reviewer@example.org"]',
            'USEP_DATA_TEAM_EMAIL_LABEL': 'Production',
        }
        namespace: dict[str, object] = self.load_settings(environment)
        self.assertEqual(['editor@example.org'], namespace['USEP_DATA_TEAM_EMAILS'])
        self.assertEqual(['reviewer@example.org'], namespace['USEP_DATA_TEAM_CC_EMAILS'])
        self.assertEqual('Production', namespace['USEP_DATA_TEAM_EMAIL_LABEL'])

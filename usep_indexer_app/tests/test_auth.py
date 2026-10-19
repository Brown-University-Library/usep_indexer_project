import json

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from usep_indexer_app.lib.auth import credentials_match, parse_remote_admin_auth


class RemoteAdminAuthTests(SimpleTestCase):
    """
    Checks required credential configuration and exact matching.
    """

    def test_invalid_configuration_has_clear_errors_without_credential_values(self) -> None:
        """
        Checks missing, malformed, and unusable credentials fail without revealing the supplied values.
        """
        invalid_values = [
            None,
            '',
            '{private-test-value',
            'null',
            '[]',
            '{}',
            json.dumps({'username': 'private-test-value'}),
            json.dumps({'username': 'admin', 'password': 123}),
            json.dumps({'username': None, 'password': 'private-test-value'}),
            json.dumps({'username': '', 'password': 'private-test-value'}),
            json.dumps({'username': 'admin', 'password': '  '}),
            json.dumps({'username': 'invalid:admin', 'password': 'private-test-value'}),
            json.dumps({'username': 'admin', 'password': 'private-test-value\n'}),
            json.dumps({'username': 'admin', 'password': 'private-test-value', 'extra': True}),
        ]
        for case_number, raw_value in enumerate(invalid_values):
            with self.subTest(case_number=case_number), self.assertRaises(ImproperlyConfigured) as captured:
                parse_remote_admin_auth(raw_value)
            self.assertIn('REMOTE_ADMIN_AUTH_JSON', str(captured.exception))
            self.assertNotIn('private-test-value', str(captured.exception))

    def test_long_credentials_are_preserved_and_require_both_values_to_match(self) -> None:
        """
        Checks long credentials remain unchanged and neither a username nor a password alone authorizes access.
        """
        credentials = {'username': 'a' * 30, 'password': 'b' * 64}
        parsed = parse_remote_admin_auth(json.dumps(credentials))
        self.assertEqual(credentials, parsed)
        with override_settings(REMOTE_ADMIN_AUTH=parsed):
            self.assertTrue(credentials_match(credentials['username'], credentials['password'], remote_admin=True))
            self.assertFalse(credentials_match('wrong', credentials['password'], remote_admin=True))
            self.assertFalse(credentials_match(credentials['username'], 'wrong', remote_admin=True))

    def test_unicode_credentials_are_compared_without_errors(self) -> None:
        """
        Checks UTF-8 credentials match exactly without character-type errors.
        """
        parsed = parse_remote_admin_auth(json.dumps({'username': 'éditeur', 'password': 'clé:example'}))
        with override_settings(REMOTE_ADMIN_AUTH=parsed):
            self.assertTrue(credentials_match('éditeur', 'clé:example', remote_admin=True))
            self.assertFalse(credentials_match('editor', 'clé:example', remote_admin=True))

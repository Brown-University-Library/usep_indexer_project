import base64
import json
import pathlib
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings


class ViewTests(SimpleTestCase):
    """
    Checks HTTP endpoint behavior.
    """

    def setUp(self) -> None:
        """
        Checks use of a reusable valid Basic Auth header.
        """
        credentials = base64.b64encode(b'test-user:test-password').decode('ascii')
        self.auth_header = {'HTTP_AUTHORIZATION': f'Basic {credentials}'}

    def test_protected_endpoint_rejects_missing_credentials(self) -> None:
        """
        Checks that the webhook endpoint requires HTTP Basic Auth.
        """
        with self.assertLogs('usep_indexer_app.lib.auth', level='INFO') as captured_logs:
            response = self.client.get('/')

        self.assertEqual(401, response.status_code)
        self.assertEqual('Basic realm="Login Required"', response['WWW-Authenticate'])
        self.assertIn('authorization_result, ``rejected``', captured_logs.output[0])

    @patch('usep_indexer_app.views.spool.write_event')
    def test_webhook_parses_all_commits_and_writes_event(self, mock_write_event) -> None:
        """
        Checks that added, modified, and removed paths are durably queued.
        """
        payload = {
            'commits': [
                {
                    'added': ['xml_inscriptions/bib_only/one.xml'],
                    'modified': ['xml_inscriptions/transcribed/two.xml'],
                    'removed': ['xml_inscriptions/metadata_only/three.xml'],
                },
                {
                    'added': [],
                    'modified': ['resources/titles.xml'],
                    'removed': [],
                },
            ],
        }
        with self.assertLogs('usep_indexer_app.views', level='DEBUG') as captured_logs:
            response = self.client.post(
                '/',
                data=json.dumps(payload),
                content_type='application/json',
                HTTP_X_GITHUB_DELIVERY='delivery-123',
                **self.auth_header,
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual(b'received', response.content)
        joined_logs = '\n'.join(captured_logs.output)
        self.assertIn('github request received; request_id, ``delivery-123``', joined_logs)
        self.assertIn("files_removed, ``['xml_inscriptions/metadata_only/three.xml']``", joined_logs)
        mock_write_event.assert_called_once()
        spool_root, event_type = mock_write_event.call_args.args
        kwargs = mock_write_event.call_args.kwargs
        self.assertEqual('/tmp/usep-indexer-spool-tests', str(spool_root))
        self.assertEqual('incremental', event_type)
        self.assertEqual(
            [
                'xml_inscriptions/bib_only/one.xml',
                'xml_inscriptions/transcribed/two.xml',
                'resources/titles.xml',
            ],
            kwargs['files_updated'],
        )
        self.assertEqual(['xml_inscriptions/metadata_only/three.xml'], kwargs['files_removed'])
        self.assertEqual('delivery-123', kwargs['request_id'])

    @patch('usep_indexer_app.views.spool.write_event')
    def test_root_get_does_not_write_event_without_a_body(self, mock_write_event) -> None:
        """
        Checks that a root GET without a request body does not trigger work.
        """
        response = self.client.get('/', **self.auth_header)
        self.assertEqual(200, response.status_code)
        self.assertEqual(b'received', response.content)
        mock_write_event.assert_not_called()

    @patch('usep_indexer_app.views.spool.write_event')
    def test_force_get_writes_empty_file_lists(self, mock_write_event) -> None:
        """
        Checks that a force GET without a request body queues empty file lists.
        """
        response = self.client.get('/force/', **self.auth_header)
        self.assertEqual(200, response.status_code)
        self.assertEqual([], mock_write_event.call_args.kwargs['files_updated'])
        self.assertEqual([], mock_write_event.call_args.kwargs['files_removed'])

    @patch('usep_indexer_app.views.spool.write_event')
    def test_reindex_all_writes_full_workflow_event(self, mock_write_event) -> None:
        """
        Checks that the admin reindex endpoint queues a full-workflow event.
        """
        response = self.client.get('/reindex_all/', **self.auth_header)
        self.assertEqual(200, response.status_code)
        mock_write_event.assert_called_once()
        self.assertEqual('full_reindex', mock_write_event.call_args.args[1])

    @patch('usep_indexer_app.views.spool.write_event', side_effect=OSError('disk full'))
    def test_reindex_returns_service_error_when_event_write_fails(self, mock_write_event) -> None:
        """
        Checks that a failed full-reindex write is not acknowledged as accepted.
        """
        response = self.client.get('/reindex_all/', **self.auth_header)
        self.assertEqual(503, response.status_code)
        mock_write_event.assert_called_once()

    @patch('usep_indexer_app.views.spool.write_event', side_effect=OSError('disk full'))
    def test_webhook_returns_service_error_when_event_write_fails(self, mock_write_event) -> None:
        """
        Checks that a failed durable write is not acknowledged as accepted.
        """
        response = self.client.post('/', data='{}', content_type='application/json', **self.auth_header)
        self.assertEqual(503, response.status_code)
        mock_write_event.assert_called_once()

    @patch('usep_indexer_app.views.orphans.prep_orphan_list', return_value=['orphan-1'])
    def test_list_orphans_supports_json_and_signed_cookie_session(self, mock_prep) -> None:
        """
        Checks JSON output and database-free confirmation state.
        """
        response = self.client.get('/list_orphans/?format=json', **self.auth_header)
        self.assertEqual(200, response.status_code)
        self.assertEqual(['orphan-1'], response.json()['data'])
        self.assertEqual(['orphan-1'], self.client.session['ids_to_delete'])
        mock_prep.assert_called_once_with()

    @override_settings(
        WEBSERVED_DATA_DIR_PATH=pathlib.Path('/private/deployment/usep-data'),
        SOLR_URL='https://dev-internal-solr.example.org/solr/private-core',
    )
    @patch('usep_indexer_app.views.orphans.prep_orphan_list', return_value=['orphan-1'])
    def test_list_orphans_omits_infrastructure_details(self, mock_prep) -> None:
        """
        Checks that HTML and JSON responses do not disclose configured locations.
        """
        json_response = self.client.get('/list_orphans/?format=json', **self.auth_header)
        json_data = json_response.json()
        self.assertNotIn('inscriptions_dir_path', json_data)
        self.assertNotIn('solr_url', json_data)
        self.assertEqual('configured dev Solr index', json_data['solr_index_label'])

        html_response = self.client.get('/list_orphans/', **self.auth_header)
        self.assertNotContains(html_response, '/private/deployment/usep-data')
        self.assertNotContains(html_response, 'dev-internal-solr.example.org')
        self.assertContains(html_response, 'configured dev Solr index')
        self.assertEqual(2, mock_prep.call_count)

    @patch('usep_indexer_app.views.orphans.run_deletes', return_value=[])
    def test_orphan_handler_deletes_ids_from_session(self, mock_run_deletes) -> None:
        """
        Checks that the GET confirmation endpoint deletes IDs stored in the session.
        """
        with patch('usep_indexer_app.views.orphans.prep_orphan_list', return_value=['orphan-1']):
            self.client.get('/list_orphans/?format=json', **self.auth_header)
        response = self.client.get('/orphan_handler/?action_button=Yes', **self.auth_header)
        self.assertEqual(200, response.status_code)
        self.assertEqual(b'all orphans deleted', response.content)
        mock_run_deletes.assert_called_once_with(['orphan-1'])

    def test_processing_check_hides_endpoint_from_unapproved_ip(self) -> None:
        """
        Checks the processing endpoint's source-IP restriction.
        """
        response = self.client.get('/processing_check/', REMOTE_ADDR='192.0.2.1')
        self.assertEqual(404, response.status_code)

    @override_settings(
        ALLOWED_HOSTS=['status.example.org'],
        README_URL='https://example.org/usep-indexer-readme',
    )
    @patch(
        'usep_indexer_app.views.processing_check_helper.check_processing',
        return_value={'result': 'processing_active', 'pending_count': 2, 'processor_status': 'success'},
    )
    def test_processing_check_reports_nested_context(self, mock_check_processing) -> None:
        """
        Checks processor health and request metadata use the version-style response shape.
        """
        response = self.client.get(
            '/processing_check/',
            REMOTE_ADDR='127.0.0.1',
            HTTP_HOST='status.example.org',
        )
        data = response.json()

        self.assertEqual(200, response.status_code)
        self.assertEqual({'timestamp', 'url'}, set(data['request']))
        self.assertEqual('http://status.example.org/processing_check/', data['request']['url'])
        self.assertTrue(data['request']['timestamp'])
        self.assertEqual('processing_active', data['response']['result'])
        self.assertEqual(2, data['response']['pending_count'])
        self.assertEqual('success', data['response']['processor_status'])
        self.assertEqual('https://example.org/usep-indexer-readme', data['response']['info'])
        self.assertTrue(data['response']['timetaken'])
        mock_check_processing.assert_called_once_with()

    def test_info_response_retains_legacy_keys(self) -> None:
        """
        Checks the metadata endpoint contract.
        """
        response = self.client.get('/info/')
        self.assertEqual(200, response.status_code)
        self.assertEqual({'datetime', 'info'}, set(response.json()))

    @override_settings(DEBUG=True)
    def test_error_check_raises_in_debug_mode(self) -> None:
        """
        Checks the template's intentional error behavior in development.
        """
        with self.assertRaisesRegex(Exception, 'Raising intentional exception'):
            self.client.get('/error_check/')

    def test_error_check_returns_404_in_production_mode(self) -> None:
        """
        Checks that the intentional error endpoint is hidden in production.
        """
        response = self.client.get('/error_check/')
        self.assertEqual(404, response.status_code)

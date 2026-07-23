import io
import json
from unittest.mock import Mock, patch

import httpx
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase
from lxml import etree
from usep_indexer_app.lib import solr_check
from usep_indexer_app.management.commands.check_solr import Command


def build_schema_response() -> dict[str, object]:
    """
    Builds a representative active-schema JSON response.

    Called by: SolrCheckLibraryTests methods
    """
    return {
        'responseHeader': {'status': 0, 'QTime': 1},
        'schema': {
            'name': 'usep-test',
            'version': 1.7,
            'uniqueKey': 'id',
            'fieldTypes': [],
            'fields': [
                {'name': 'id', 'type': 'string'},
                {'name': 'status', 'type': 'string'},
            ],
            'dynamicFields': [{'name': 'name_*', 'type': 'string'}],
            'copyFields': [],
        },
    }


def build_system_info_response() -> dict[str, object]:
    """
    Builds a representative Solr system-information response.

    Called by: SolrCheckLibraryTests methods
    """
    return {
        'responseHeader': {'status': 0, 'QTime': 1},
        'mode': 'std',
        'lucene': {
            'solr-spec-version': '9.8.1',
            'solr-impl-version': '9.8.1 test-build',
            'lucene-spec-version': '9.12.1',
        },
        'system': {'name': 'Linux'},
    }


class SolrCheckLibraryTests(SimpleTestCase):
    """
    Checks Solr request details, responses, and active-schema parsing.
    """

    def make_http_client(self, handler: Mock) -> httpx.Client:
        """
        Creates an in-memory HTTP client for one test.

        Called by: SolrCheckLibraryTests methods
        """
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return client

    def test_safe_access_check_sends_minimal_query_and_empty_update(self) -> None:
        """
        Checks exact safe requests and a successful result without indexed-data changes.
        """
        handler = Mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        'responseHeader': {'status': 0},
                        'response': {'numFound': 3, 'start': 0, 'docs': [{'id': 'one'}]},
                    },
                ),
                httpx.Response(200, json={'responseHeader': {'status': 0}}),
            ]
        )
        result = solr_check.check_required_access(
            'https://solr.example.org/solr/usep',
            7.5,
            http_client=self.make_http_client(handler),
        )

        self.assertEqual(3, result.document_count)
        self.assertEqual(2, handler.call_count)
        select_request: httpx.Request = handler.call_args_list[0].args[0]
        self.assertEqual('GET', select_request.method)
        self.assertEqual('/solr/usep/select', select_request.url.path)
        self.assertEqual(
            {'q': '*:*', 'fl': 'id', 'rows': '1', 'wt': 'json', 'omitHeader': 'false'},
            dict(select_request.url.params),
        )
        update_request: httpx.Request = handler.call_args_list[1].args[0]
        self.assertEqual('POST', update_request.method)
        self.assertEqual('/solr/usep/update', update_request.url.path)
        self.assertEqual({'wt': 'json', 'omitHeader': 'false'}, dict(update_request.url.params))
        self.assertEqual({'delete': []}, json.loads(update_request.content))
        self.assertEqual('application/json', update_request.headers['content-type'])

    def test_malformed_select_response_stops_before_update_check(self) -> None:
        """
        Checks a malformed normal-query response prevents a misleading update success.
        """
        handler = Mock(
            return_value=httpx.Response(
                200,
                json={'responseHeader': {'status': 0}, 'response': {'numFound': 0}},
            )
        )
        with self.assertRaisesRegex(solr_check.SolrCheckError, 'invalid docs'):
            solr_check.check_required_access(
                'https://solr.example.org/solr/usep',
                5,
                http_client=self.make_http_client(handler),
            )

        handler.assert_called_once()

    def test_update_denial_is_reported_as_update_failure(self) -> None:
        """
        Checks update authorization failure is distinct from successful read access.
        """
        handler = Mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        'responseHeader': {'status': 0},
                        'response': {'numFound': 0, 'start': 0, 'docs': []},
                    },
                ),
                httpx.Response(403, json={'error': {'code': 403}}),
            ]
        )
        with self.assertRaisesRegex(solr_check.SolrCheckError, 'Solr /update check returned HTTP 403'):
            solr_check.check_required_access(
                'https://solr.example.org/solr/usep',
                5,
                http_client=self.make_http_client(handler),
            )

        self.assertEqual(2, handler.call_count)

    def test_json_schema_output_is_redirect_safe_and_records_unique_key(self) -> None:
        """
        Checks JSON mode returns only the active schema object and its unique key.
        """
        response_document = build_schema_response()
        handler = Mock(return_value=httpx.Response(200, json=response_document))
        result = solr_check.retrieve_active_schema(
            'https://solr.example.org/solr/usep',
            5,
            'json',
            http_client=self.make_http_client(handler),
        )

        self.assertEqual(response_document['schema'], json.loads(result.text))
        self.assertEqual('id', result.unique_key)
        schema_request: httpx.Request = handler.call_args.args[0]
        self.assertEqual('/solr/usep/schema', schema_request.url.path)
        self.assertEqual({'wt': 'json', 'omitHeader': 'false'}, dict(schema_request.url.params))

    def test_schema_xml_output_is_preserved_and_records_unique_key(self) -> None:
        """
        Checks schema.xml mode returns parseable XML with exactly one final newline.
        """
        schema_xml = '<schema name="usep-test" version="1.7"><uniqueKey>id</uniqueKey></schema>'
        handler = Mock(return_value=httpx.Response(200, text=schema_xml))
        result = solr_check.retrieve_active_schema(
            'https://solr.example.org/solr/usep',
            5,
            'schema.xml',
            http_client=self.make_http_client(handler),
        )

        self.assertEqual(f'{schema_xml}\n', result.text)
        self.assertEqual('id', result.unique_key)
        etree.fromstring(result.text.encode('utf-8'))
        schema_request: httpx.Request = handler.call_args.args[0]
        self.assertEqual({'wt': 'schema.xml'}, dict(schema_request.url.params))

    def test_schema_denial_is_reported_as_schema_read_failure(self) -> None:
        """
        Checks Schema API authorization failure is distinct from query and update access.
        """
        handler = Mock(return_value=httpx.Response(403, json={'error': {'code': 403}}))

        with self.assertRaisesRegex(solr_check.SolrCheckError, 'Solr schema read returned HTTP 403'):
            solr_check.retrieve_active_schema(
                'https://solr.example.org/solr/usep',
                5,
                'json',
                http_client=self.make_http_client(handler),
            )

        handler.assert_called_once()

    def test_solr_version_output_is_redirect_safe_and_records_spec_version(self) -> None:
        """
        Checks version retrieval preserves the full response and extracts the clean Solr version.
        """
        response_document = build_system_info_response()
        handler = Mock(return_value=httpx.Response(200, json=response_document))
        result = solr_check.retrieve_solr_version(
            'https://solr.example.org/solr/usep',
            5,
            http_client=self.make_http_client(handler),
        )

        self.assertEqual('9.8.1', result.spec_version)
        self.assertEqual(response_document, json.loads(result.full_text))
        version_request: httpx.Request = handler.call_args.args[0]
        self.assertEqual('/solr/usep/admin/system', version_request.url.path)
        self.assertEqual({'wt': 'json', 'omitHeader': 'false'}, dict(version_request.url.params))

    def test_solr_version_denial_is_reported_as_version_read_failure(self) -> None:
        """
        Checks system-information authorization failure is distinct from other Solr access.
        """
        handler = Mock(return_value=httpx.Response(403, json={'error': {'code': 403}}))

        with self.assertRaisesRegex(solr_check.SolrCheckError, 'Solr version read returned HTTP 403'):
            solr_check.retrieve_solr_version(
                'https://solr.example.org/solr/usep',
                5,
                http_client=self.make_http_client(handler),
            )

        handler.assert_called_once()


class CheckSolrCommandTests(SimpleTestCase):
    """
    Checks command mode selection, output, and errors.
    """

    def test_help_briefly_describes_access_schema_and_version_modes(self) -> None:
        """
        Checks command help remains concise while naming all three concerns.
        """
        self.assertLessEqual(len(Command.help.split()), 15)
        self.assertIn('access', Command.help)
        self.assertIn('schema', Command.help)
        self.assertIn('version', Command.help)

    @patch('usep_indexer_app.management.commands.check_solr.solr_check.check_required_access')
    def test_default_command_reports_safe_access_success(self, mock_check_required_access) -> None:
        """
        Checks the default command reports both successful required-access checks.
        """
        mock_check_required_access.return_value = solr_check.SolrAccessCheck(document_count=42)
        output = io.StringIO()

        call_command('check_solr', stdout=output)

        mock_check_required_access.assert_called_once_with('http://solr.example.org/solr/usep', 30.0)
        self.assertIn('Solr /select access: ok (documents: 42)', output.getvalue())
        self.assertIn('Solr /update access: ok', output.getvalue())

    @patch('usep_indexer_app.management.commands.check_solr.solr_check.check_required_access')
    def test_default_command_reports_access_failure_as_command_error(self, mock_check_required_access) -> None:
        """
        Checks a diagnosed Solr access failure becomes a concise command error.
        """
        mock_check_required_access.side_effect = solr_check.SolrCheckError('Solr /update check returned HTTP 403.')

        with self.assertRaisesRegex(CommandError, 'Solr /update check returned HTTP 403'):
            call_command('check_solr', stderr=io.StringIO())

    @patch('usep_indexer_app.management.commands.check_solr.solr_check.retrieve_active_schema')
    def test_schema_command_writes_only_schema_to_stdout(self, mock_retrieve_active_schema) -> None:
        """
        Checks schema output has no status prose and uses the requested format.
        """
        schema_text = '{"uniqueKey": "id"}\n'
        mock_retrieve_active_schema.return_value = solr_check.ActiveSchemaOutput(
            text=schema_text,
            unique_key='id',
        )
        output = io.StringIO()

        call_command('check_solr', '--schema', stdout=output)

        mock_retrieve_active_schema.assert_called_once_with(
            'http://solr.example.org/solr/usep',
            30.0,
            'json',
        )
        self.assertEqual(schema_text, output.getvalue())

    @patch('usep_indexer_app.management.commands.check_solr.solr_check.retrieve_active_schema')
    def test_schema_command_outputs_incompatible_schema_before_failing(self, mock_retrieve_active_schema) -> None:
        """
        Checks an incompatible active schema remains inspectable while the command fails.
        """
        schema_text = '{"uniqueKey": "record_id"}\n'
        mock_retrieve_active_schema.return_value = solr_check.ActiveSchemaOutput(
            text=schema_text,
            unique_key='record_id',
        )
        output = io.StringIO()

        with self.assertRaisesRegex(CommandError, 'uniqueKey must be "id"'):
            call_command('check_solr', '--schema', stdout=output, stderr=io.StringIO())

        self.assertEqual(schema_text, output.getvalue())

    def test_schema_format_without_schema_fails(self) -> None:
        """
        Checks schema formatting cannot silently change another command mode.
        """
        with self.assertRaisesRegex(CommandError, '--schema-format requires --schema'):
            call_command('check_solr', '--schema-format=schema.xml', stderr=io.StringIO())

    @patch('usep_indexer_app.management.commands.check_solr.solr_check.retrieve_solr_version')
    def test_version_command_writes_only_spec_version(self, mock_retrieve_solr_version) -> None:
        """
        Checks --solr-version prints only the Solr release number.
        """
        mock_retrieve_solr_version.return_value = solr_check.SolrVersionOutput(
            spec_version='9.8.1',
            full_text='{"unused": true}\n',
        )
        output = io.StringIO()

        call_command('check_solr', '--solr-version', stdout=output)

        mock_retrieve_solr_version.assert_called_once_with(
            'http://solr.example.org/solr/usep',
            30.0,
        )
        self.assertEqual('9.8.1\n', output.getvalue())

    @patch('usep_indexer_app.management.commands.check_solr.solr_check.retrieve_solr_version')
    def test_version_all_command_writes_only_full_response(self, mock_retrieve_solr_version) -> None:
        """
        Checks --solr-version-all prints the complete formatted system-information response.
        """
        full_text = '{\n  "lucene": {\n    "solr-spec-version": "9.8.1"\n  }\n}\n'
        mock_retrieve_solr_version.return_value = solr_check.SolrVersionOutput(
            spec_version='9.8.1',
            full_text=full_text,
        )
        output = io.StringIO()

        call_command('check_solr', '--solr-version-all', stdout=output)

        self.assertEqual(full_text, output.getvalue())

    def test_information_modes_cannot_be_combined(self) -> None:
        """
        Checks each invocation selects only one redirect-safe information mode.
        """
        with self.assertRaisesRegex(CommandError, 'cannot be combined'):
            call_command('check_solr', '--schema', '--solr-version', stderr=io.StringIO())

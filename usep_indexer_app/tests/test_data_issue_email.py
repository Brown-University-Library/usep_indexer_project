import io
import json
import pathlib
import tempfile
from unittest.mock import Mock, patch

import httpx
from django.core import mail
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings
from lxml import etree

from usep_indexer_app.lib import data_issue_email, indexer, processor, spool
from usep_indexer_app.tests.test_indexing_refactor import FIXTURE_PATH, build_resources


class DataIssueEmailTests(SimpleTestCase):
    """
    Checks complete editor reports alongside real queue transitions and mocked indexing transport.
    """

    def setUp(self) -> None:
        """
        Checks reports use temporary public/source files and Django's in-memory email backend.
        """
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = pathlib.Path(temporary_directory.name)
        self.inscriptions = self.root / 'public' / 'inscriptions'
        self.inscriptions.mkdir(parents=True)
        self.clone = self.root / 'source'
        self.spool_root = self.root / 'spool'
        self.handler = Mock(return_value=httpx.Response(200, text='ok'))
        self.settings_override = override_settings(
            WEBSERVED_DATA_DIR_PATH=self.root / 'public',
            USEP_DATA_GIT_CLONED_DIR_PATH=self.clone,
            SPOOL_ROOT_PATH=self.spool_root,
            SPOOL_MAX_ATTEMPTS=3,
            ADMINS=[('Administrator', 'admin@example.org')],
            USEP_DATA_TEAM_EMAILS=['editor@example.org', 'reviewer@example.org'],
            USEP_DATA_TEAM_EMAIL_LABEL='Development',
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

    def add_inscription(self, filename: str, content: bytes, directory: str = 'transcribed') -> str:
        """
        Checks a synthetic source inscription is also present in the public copy.
        """
        relative_path = pathlib.Path('xml_inscriptions') / directory / filename
        source = self.clone / relative_path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(content)
        (self.inscriptions / filename).write_bytes(content)
        return relative_path.as_posix()

    def run_processor(self, *, fails: bool = True) -> None:
        """
        Checks one command uses new indexing resources and never contacts external services.
        """
        with httpx.Client(transport=httpx.MockTransport(self.handler)) as client:
            resources = build_resources(client)
            with (
                patch.object(processor, 'prepare_public_data', return_value='abc1234'),
                patch.object(indexer.IndexingResources, 'load', return_value=resources),
            ):
                if fails:
                    with self.assertRaises(CommandError):
                        call_command('process_spool', stdout=io.StringIO())
                else:
                    call_command('process_spool', stdout=io.StringIO())

    def team_messages(self) -> list[mail.EmailMessage]:
        """
        Checks editor messages can be inspected separately from administrator notifications.
        """
        outbox: list[mail.EmailMessage] = vars(mail)['outbox']
        return [message for message in outbox if 'editor@example.org' in message.to]

    def test_report_groups_every_failure_without_queue_summary_truncation(self) -> None:
        """
        Checks all failed filenames appear with revision links, counts, and safe explanations.
        """
        paths = [self.add_inscription(f'broken-{number:03}.xml', b'<TEI>') for number in range(45)]
        paths.append(self.add_inscription('wrong-id.xml', (FIXTURE_PATH / 'metadata.xml').read_bytes()))
        paths.append(self.add_inscription('transcribed.xml', (FIXTURE_PATH / 'transcribed.xml').read_bytes()))
        spool.write_event(self.spool_root, 'incremental', files_updated=paths)
        self.run_processor()

        messages = self.team_messages()
        self.assertEqual(1, len(messages))
        message = messages[0]
        self.assertEqual(['editor@example.org', 'reviewer@example.org'], message.to)
        self.assertEqual([], message.cc)
        self.assertIn('[Development]', message.subject)
        self.assertIn('XML could not be read (45)', message.body)
        self.assertIn('XML ID and filename differ (1)', message.body)
        self.assertIn('  "wrong-id" — Filename ID\n  "metadata" — XML-derived Solr ID', message.body)
        self.assertIn('xml:id attribute on publicationStmt/idno', message.body)
        self.assertIn('Inscriptions successfully accepted by Solr: 1', message.body)
        self.assertIn('Inscriptions with recorded failures: 46', message.body)
        self.assertIn('Data revision: abc1234', message.body)
        for number in range(45):
            self.assertIn(f'/blob/abc1234/xml_inscriptions/transcribed/broken-{number:03}.xml', message.body)
        self.assertNotIn(str(self.root), message.body)
        self.assertNotIn('Traceback', message.body)
        pending = json.loads(next((self.spool_root / 'pending').glob('*.json')).read_text())
        self.assertEqual(1, pending['attempts'])
        self.assertEqual(spool.MAX_ERROR_LENGTH, len(pending['last_error']))
        self.assertGreater(len(message.body), spool.MAX_ERROR_LENGTH)
        outbox: list[mail.EmailMessage] = vars(mail)['outbox']
        self.assertEqual(2, len(outbox))

    def test_retry_sends_a_fresh_report_and_success_does_not_repeat_it(self) -> None:
        """
        Checks repeated failures send once per attempt and a later successful run stays quiet.
        """
        path = self.add_inscription('metadata.xml', b'<TEI>')
        spool.write_event(self.spool_root, 'incremental', files_updated=[path])
        self.run_processor()
        self.run_processor()
        self.assertEqual(2, len(self.team_messages()))
        for message in self.team_messages():
            self.assertIn('Inscriptions with recorded failures: 1', message.body)
        self.add_inscription('metadata.xml', (FIXTURE_PATH / 'metadata.xml').read_bytes())
        self.run_processor(fails=False)
        self.run_processor(fails=False)
        self.assertEqual(2, len(self.team_messages()))
        self.assertEqual(1, len(list((self.spool_root / 'completed').glob('*.json'))))

    def test_mismatched_id_preserves_whitespace_without_adding_email_lines(self) -> None:
        """
        Checks stylesheet-generated ID differences remain visible without copying raw error messages.
        """
        document = etree.fromstring(b'<doc><field name="id">metadata</field></doc>')
        indexer.replace_field_values(document, 'id', [' metadata\n'])
        with httpx.Client(transport=httpx.MockTransport(self.handler)) as client:
            resources = build_resources(client)
            with (
                data_issue_email.collect_report() as report,
                patch.object(indexer, 'transform_to_document', return_value=document),
            ):
                indexer.build_complete_documents([FIXTURE_PATH / 'metadata.xml'], resources)
        body = data_issue_email.build_message(report)
        self.assertIn('  "metadata" — Filename ID', body)
        self.assertIn('  " metadata\\n" — XML-derived Solr ID\n', body)
        self.assertNotIn('SolrDocumentValidationError', body)
        self.assertNotIn(str(FIXTURE_PATH), body)

    def test_multiple_images_are_indexed_without_failure_email_or_retry(self) -> None:
        """
        Checks multiple image groups with empty entries complete normally in both queue workflows.
        """
        content = (FIXTURE_PATH / 'multiple-images.xml').read_bytes()
        path = self.add_inscription('multiple-images.xml', content)
        for event_type in ('incremental', 'full_reindex'):
            with self.subTest(event_type=event_type):
                self.handler.reset_mock()
                self.handler.side_effect = (
                    [httpx.Response(200, json={'response': {'docs': []}}), httpx.Response(200, text='ok')]
                    if event_type == 'full_reindex'
                    else [httpx.Response(200, text='ok')]
                )
                spool.write_event(self.spool_root, event_type, files_updated=[path] if event_type == 'incremental' else None)
                self.run_processor(fails=False)
                request: httpx.Request = self.handler.call_args.args[0]
                document = etree.fromstring(request.content)
                self.assertEqual(
                    ['https://images.example.org/first.jpg'], document.xpath('./doc/field[@name="graphic_name"]/text()')
                )
                self.assertEqual(content, (self.inscriptions / 'multiple-images.xml').read_bytes())
                self.assertEqual([], list((self.spool_root / 'pending').glob('*.json')))
        self.assertEqual(2, len(list((self.spool_root / 'completed').glob('*.json'))))
        self.assertEqual([], vars(mail)['outbox'])

    def test_multiple_images_do_not_hide_unrelated_inscription_failures(self) -> None:
        """
        Checks an ID mismatch still triggers the editor email and retry for a multiple-image inscription.
        """
        path = self.add_inscription('wrong-id.xml', (FIXTURE_PATH / 'multiple-images.xml').read_bytes())
        spool.write_event(self.spool_root, 'incremental', files_updated=[path])
        self.run_processor()
        body = self.team_messages()[0].body
        self.assertIn('XML ID and filename differ (1)', body)
        self.assertNotIn('More than one image value', body)
        self.assertEqual(1, len(list((self.spool_root / 'pending').glob('*.json'))))
        self.handler.assert_not_called()

    def test_disabled_recipients_leave_admin_email_and_retry_unchanged(self) -> None:
        """
        Checks an empty recipient list disables only the data-team notification.
        """
        path = self.add_inscription('broken.xml', b'<TEI>')
        spool.write_event(self.spool_root, 'incremental', files_updated=[path])
        with override_settings(USEP_DATA_TEAM_EMAILS=[], USEP_DATA_TEAM_CC_EMAILS=['observer@example.org']):
            self.run_processor()
        self.assertEqual([], self.team_messages())
        outbox: list[mail.EmailMessage] = vars(mail)['outbox']
        self.assertEqual(1, len(outbox))
        self.assertEqual(['admin@example.org'], outbox[0].to)

    def test_team_delivery_failure_keeps_admin_email_and_original_error(self) -> None:
        """
        Checks a failed team email does not replace the indexing error or prevent retry handling.
        """
        path = self.add_inscription('broken.xml', b'<TEI>')
        spool.write_event(self.spool_root, 'incremental', files_updated=[path])
        failed_message = Mock(spec=mail.EmailMessage)
        failed_message.send.side_effect = RuntimeError('mail unavailable')
        with (
            patch.object(data_issue_email, 'EmailMessage', return_value=failed_message),
            self.assertLogs('usep_indexer_app.lib.data_issue_email', level='ERROR') as captured,
        ):
            self.run_processor()
        self.assertIn('Unable to email the USEP data team', '\n'.join(captured.output))
        outbox: list[mail.EmailMessage] = vars(mail)['outbox']
        self.assertEqual(1, len(outbox))
        self.assertIn('InscriptionProcessingError', outbox[0].body)
        self.assertEqual(1, len(list((self.spool_root / 'pending').glob('*.json'))))

    def test_infrastructure_and_unknown_failures_are_only_sent_to_admins(self) -> None:
        """
        Checks outages, unknown Solr fields, and unexpected code errors do not reach editors.
        """
        for error in (RuntimeError('private server details'), OSError('private filesystem details')):
            with (
                self.subTest(error=type(error).__name__),
                patch.object(indexer, 'build_complete_document', side_effect=error),
            ):
                path = self.add_inscription('metadata.xml', (FIXTURE_PATH / 'metadata.xml').read_bytes())
                spool.write_event(self.spool_root, 'incremental', files_updated=[path])
                self.run_processor()
        self.assertEqual([], self.team_messages())
        for status in (400, 503):
            with self.subTest(status=status):
                self.handler.return_value = httpx.Response(status, text='unknown field; private server details')
                spool.write_event(self.spool_root, 'incremental', files_updated=[path])
                self.run_processor()
        self.assertEqual([], self.team_messages())

    def test_data_errors_are_reported_even_if_solr_later_becomes_unavailable(self) -> None:
        """
        Checks an interrupted run still reports collected XML problems without service details.
        """
        paths = [
            self.add_inscription('broken.xml', b'<TEI>'),
            self.add_inscription('metadata.xml', (FIXTURE_PATH / 'metadata.xml').read_bytes()),
        ]
        self.handler.return_value = httpx.Response(503, text='private server details')
        spool.write_event(self.spool_root, 'incremental', files_updated=paths)
        self.run_processor()
        message = self.team_messages()[0]
        self.assertIn('Processing stopped early', message.body)
        self.assertIn('Inscriptions successfully accepted by Solr: 0', message.body)
        self.assertIn('XML could not be read (1)', message.body)
        self.assertNotIn('private server details', message.body)

    def test_full_reindex_reports_batch_successes_and_individual_rejections(self) -> None:
        """
        Checks accepted counts after batch fallback and safe reporting of rejected numeric data.
        """
        self.add_inscription('metadata.xml', (FIXTURE_PATH / 'metadata.xml').read_bytes())
        self.add_inscription('transcribed.xml', (FIXTURE_PATH / 'transcribed.xml').read_bytes())
        self.handler.side_effect = [
            httpx.Response(200, json={'response': {'docs': []}}),
            httpx.Response(400, text='rejected batch'),
            httpx.Response(400, text="Error adding field 'notAfter': For input string: private server details"),
            httpx.Response(200, text='ok'),
        ]
        spool.write_event(self.spool_root, 'full_reindex')
        self.run_processor()
        message = self.team_messages()[0]
        self.assertIn('Date is not an integer year (1)', message.body)
        self.assertIn('Inscriptions successfully accepted by Solr: 1', message.body)
        self.assertNotIn('Processing stopped early', message.body)
        self.assertNotIn('private server details', message.body)
        self.assertEqual(4, self.handler.call_count)

    def test_full_reindex_counts_successful_batches_with_local_failures(self) -> None:
        """
        Checks the report counts documents rather than successful HTTP requests.
        """
        self.add_inscription('broken.xml', b'<TEI>')
        self.add_inscription('metadata.xml', (FIXTURE_PATH / 'metadata.xml').read_bytes())
        self.add_inscription('transcribed.xml', (FIXTURE_PATH / 'transcribed.xml').read_bytes())
        self.handler.side_effect = [
            httpx.Response(200, json={'response': {'docs': []}}),
            httpx.Response(200, text='ok'),
        ]
        spool.write_event(self.spool_root, 'full_reindex')
        self.run_processor()
        self.assertIn('Inscriptions successfully accepted by Solr: 2', self.team_messages()[0].body)

    def test_source_link_uses_winning_directory_and_escapes_filename(self) -> None:
        """
        Checks source links follow flattening precedence and encode special URL characters.
        """
        filename = 'sample space+#.xml'
        for directory in ('bib_only', 'metadata_only', 'transcribed'):
            self.add_inscription(filename, b'<TEI>', directory)
        spool.write_event(self.spool_root, 'full_reindex')
        self.handler.return_value = httpx.Response(200, json={'response': {'docs': []}})
        self.run_processor()
        body = self.team_messages()[0].body
        self.assertIn('/blob/abc1234/xml_inscriptions/transcribed/sample%20space%2B%23.xml', body)
        self.assertNotIn('/metadata_only/', body)

    def test_missing_source_and_revision_do_not_create_misleading_links(self) -> None:
        """
        Checks unavailable source information is explained without exposing server paths.
        """
        with data_issue_email.collect_report() as report:
            data_issue_email.start_indexing('/private/revision', 1)
            data_issue_email.record_failure('missing.xml', 'xml_parse')
        body = data_issue_email.build_message(report)
        self.assertIn('Data revision: unavailable', body)
        self.assertIn('An exact source link is unavailable', body)
        self.assertNotIn('/private/revision', body)

    def test_recipient_configuration_requires_a_list_of_addresses(self) -> None:
        """
        Checks optional recipient settings reject malformed values and remove repeated addresses.
        """
        self.assertEqual([], data_issue_email.parse_recipients('[]'))
        self.assertEqual(
            ['editor@example.org'],
            data_issue_email.parse_recipients('["editor@example.org", "editor@example.org"]'),
        )
        for value in ('null', '"editor@example.org"', '{}', '[3]', '["bad-address"]', 'invalid JSON'):
            with self.subTest(value=value), self.assertRaises(ImproperlyConfigured):
                data_issue_email.parse_recipients(value)

    def test_optional_cc_recipients_receive_the_same_report_in_the_cc_header(self) -> None:
        """
        Checks CC addresses are delivered with the team report without changing primary recipients.
        """
        path = self.add_inscription('broken.xml', b'<TEI>')
        spool.write_event(self.spool_root, 'incremental', files_updated=[path])
        with override_settings(USEP_DATA_TEAM_CC_EMAILS=['observer@example.org', 'maintainer@example.org']):
            self.run_processor()
        message = self.team_messages()[0]
        self.assertEqual(['editor@example.org', 'reviewer@example.org'], message.to)
        self.assertEqual(['observer@example.org', 'maintainer@example.org'], message.cc)
        self.assertEqual(message.to + message.cc, message.recipients())
        self.assertEqual('observer@example.org, maintainer@example.org', message.message()['Cc'])

    def test_invalid_cc_configuration_names_the_cc_setting(self) -> None:
        """
        Checks invalid CC addresses identify the correct environment setting without revealing values.
        """
        with self.assertRaisesRegex(ImproperlyConfigured, '^USEP_DATA_TEAM_CC_EMAILS_JSON must be'):
            data_issue_email.parse_recipients('["invalid-address"]', setting_name='USEP_DATA_TEAM_CC_EMAILS_JSON')

    def test_collector_clears_after_an_exception(self) -> None:
        """
        Checks a later command cannot inherit the preceding command's failures.
        """
        with self.assertRaises(RuntimeError), data_issue_email.collect_report():
            data_issue_email.record_failure('broken.xml', 'xml_parse')
            raise RuntimeError('stop')
        data_issue_email.record_failure('outside.xml', 'xml_parse')
        with data_issue_email.collect_report() as report:
            self.assertEqual([], report.issues)

    def test_known_validation_errors_have_separate_safe_categories(self) -> None:
        """
        Checks common source problems are grouped without copying operational error text.
        """
        cases = [
            ({'id': ['different']}, 'id_mismatch'),
            ({'id': []}, 'id_missing'),
            ({'notBefore': ['200'], 'notAfter': ['100']}, 'date_range'),
            ({'notAfter': ['unknown']}, 'invalid_year'),
            ({'msid_region': ['one', 'two']}, 'multiple_values'),
            ({'status': ['transcription']}, 'transcription_status'),
        ]
        with httpx.Client(transport=httpx.MockTransport(self.handler)) as client:
            resources = build_resources(client)
            for changed_fields, expected_category in cases:
                with self.subTest(category=expected_category):
                    document = etree.fromstring(
                        b'<doc><field name="id">metadata</field><field name="status">metadata</field></doc>'
                    )
                    for name, values in changed_fields.items():
                        indexer.replace_field_values(document, name, values)
                    with (
                        data_issue_email.collect_report() as report,
                        patch.object(indexer, 'transform_to_document', return_value=document),
                    ):
                        indexer.build_complete_documents([FIXTURE_PATH / 'metadata.xml'], resources)
                    self.assertEqual([expected_category], [issue.category for issue in report.issues])
            for error, category in (
                (etree.XSLTApplyError('private operational detail'), 'xml_transform'),
                (indexer.SolrDocumentValidationError('private operational detail'), 'document_validation'),
            ):
                with (
                    data_issue_email.collect_report() as report,
                    patch.object(indexer, 'build_complete_document', side_effect=error),
                ):
                    indexer.build_complete_documents([FIXTURE_PATH / 'metadata.xml'], resources)
                self.assertEqual(category, report.issues[0].category)
                self.assertNotIn('private operational detail', data_issue_email.build_message(report))

    def test_locked_and_quarantined_only_runs_send_no_team_email(self) -> None:
        """
        Checks a busy processor and invalid queue files do not generate editor notifications.
        """
        for result in (spool.ProcessResult(status='locked'), spool.ProcessResult(status='success', quarantined=1)):
            with patch.object(spool, 'process_spool', return_value=result):
                call_command('process_spool', stdout=io.StringIO())
        self.assertEqual([], self.team_messages())

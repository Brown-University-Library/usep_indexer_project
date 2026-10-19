import json
import pathlib
import tempfile
from unittest.mock import Mock, patch

import httpx
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings
from lxml import etree

from usep_indexer_app.lib import indexer, processor, reindex, spool
from usep_indexer_app.tests.test_indexing_refactor import FIXTURE_PATH, build_resources


class IndexingResilienceTests(SimpleTestCase):
    """
    Checks individual inscription failures leave other work available for processing.
    """

    def setUp(self) -> None:
        """
        Checks each test uses temporary public files and an in-memory Solr transport.
        """
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = pathlib.Path(temporary_directory.name)
        self.inscriptions_path = self.root / 'inscriptions'
        self.inscriptions_path.mkdir()
        self.handler = Mock(return_value=httpx.Response(200, text='ok'))
        self.client = httpx.Client(transport=httpx.MockTransport(self.handler))
        self.addCleanup(self.client.close)
        self.resources = build_resources(self.client)
        for filename in ('metadata.xml', 'transcribed.xml'):
            (self.inscriptions_path / filename).write_bytes((FIXTURE_PATH / filename).read_bytes())

    def test_incremental_run_reports_all_bad_files_after_posting_valid_files(self) -> None:
        """
        Checks malformed XML and invalid document IDs do not prevent later valid updates.
        """
        (self.inscriptions_path / 'broken.xml').write_bytes(b'<TEI>')
        (self.inscriptions_path / 'wrong-id.xml').write_bytes((FIXTURE_PATH / 'metadata.xml').read_bytes())
        filenames = ['broken.xml', 'metadata.xml', 'wrong-id.xml', 'transcribed.xml']
        with (
            override_settings(WEBSERVED_DATA_DIR_PATH=self.root),
            patch.object(indexer.IndexingResources, 'load', return_value=self.resources),
            self.assertLogs('usep_indexer_app.lib.indexer', level='WARNING') as captured,
            self.assertRaisesRegex(indexer.InscriptionProcessingError, '2 inscription'),
        ):
            indexer.update_index([f'xml_inscriptions/transcribed/{filename}' for filename in filenames], [])

        posted_ids: list[str] = []
        for recorded_call in self.handler.call_args_list:
            request: httpx.Request = recorded_call.args[0]
            posted_ids.extend(etree.fromstring(request.content).xpath('./doc/field[@name="id"]/text()'))
        self.assertEqual(['metadata', 'transcribed'], posted_ids)
        self.assertEqual(2, len(self.resources.document_failures))
        self.assertIn('broken', '\n'.join(captured.output))
        self.assertIn('wrong-id', '\n'.join(captured.output))

    def test_full_reindex_preserves_old_ids_when_some_documents_fail(self) -> None:
        """
        Checks valid full-reindex documents are posted but stale-ID deletion waits for a clean run.
        """
        (self.inscriptions_path / 'broken.xml').write_bytes(b'<TEI>')
        self.handler.side_effect = [
            httpx.Response(200, json={'response': {'docs': [{'id': 'broken'}, {'id': 'old'}]}}),
            httpx.Response(200, text='ok'),
        ]
        with (
            override_settings(WEBSERVED_DATA_DIR_PATH=self.root),
            patch.object(indexer.IndexingResources, 'load', return_value=self.resources),
            self.assertRaisesRegex(indexer.InscriptionProcessingError, 'broken'),
        ):
            reindex.process_prepared_full_reindex()

        self.assertEqual(2, self.handler.call_count)
        request: httpx.Request = self.handler.call_args_list[1].args[0]
        update = etree.fromstring(request.content)
        self.assertEqual('add', update.tag)
        self.assertEqual(['metadata', 'transcribed'], update.xpath('./doc/field[@name="id"]/text()'))

    def test_full_reindex_does_not_clear_solr_when_every_inscription_fails(self) -> None:
        """
        Checks an entirely invalid corpus cannot replace or delete existing Solr records.
        """
        for inscription_path in self.inscriptions_path.glob('*.xml'):
            inscription_path.write_bytes(b'<TEI>')
        self.handler.return_value = httpx.Response(200, json={'response': {'docs': [{'id': 'old'}]}})
        with (
            override_settings(WEBSERVED_DATA_DIR_PATH=self.root),
            patch.object(indexer.IndexingResources, 'load', return_value=self.resources),
            self.assertRaisesRegex(indexer.InscriptionProcessingError, '2 inscription'),
        ):
            reindex.process_prepared_full_reindex()
        self.handler.assert_called_once()
        request: httpx.Request = self.handler.call_args.args[0]
        self.assertEqual('GET', request.method)

    def test_failed_bulk_update_retries_documents_separately_and_keeps_valid_updates(self) -> None:
        """
        Checks a Solr data rejection is isolated to its inscription and prevents stale-ID deletion.
        """
        documents = indexer.build_complete_documents(
            [self.inscriptions_path / 'metadata.xml', self.inscriptions_path / 'transcribed.xml'], self.resources
        )
        self.handler.side_effect = [
            httpx.Response(400, text='invalid document'),
            httpx.Response(400, text='invalid document'),
            httpx.Response(200, text='ok'),
        ]
        counts = reindex.update_all_index_entries(documents, ['old'], self.resources)

        self.assertEqual((1, 0), counts)
        self.assertEqual(3, self.handler.call_count)
        self.assertEqual(1, len(self.resources.document_failures))
        self.assertIn('metadata', self.resources.document_failures[0])
        request: httpx.Request = self.handler.call_args.args[0]
        self.assertEqual(['transcribed'], etree.fromstring(request.content).xpath('./doc/field[@name="id"]/text()'))

    def test_incremental_solr_data_rejection_does_not_block_the_next_document(self) -> None:
        """
        Checks a rejected incremental document is reported after the following document succeeds.
        """
        self.handler.side_effect = [httpx.Response(400, text='invalid document'), httpx.Response(200, text='ok')]
        with (
            override_settings(WEBSERVED_DATA_DIR_PATH=self.root),
            patch.object(indexer.IndexingResources, 'load', return_value=self.resources),
            self.assertRaisesRegex(indexer.InscriptionProcessingError, 'metadata'),
        ):
            indexer.update_index(
                ['xml_inscriptions/metadata_only/metadata.xml', 'xml_inscriptions/transcribed/transcribed.xml'], []
            )
        self.assertEqual(2, self.handler.call_count)

    def test_solr_service_failure_stops_without_individual_document_retries(self) -> None:
        """
        Checks a service outage is not treated as a problem with individual XML files.
        """
        documents = indexer.build_complete_documents([self.inscriptions_path / 'metadata.xml'], self.resources)
        self.handler.return_value = httpx.Response(503, text='unavailable')
        with self.assertRaises(httpx.HTTPStatusError):
            indexer.post_document_batches(documents, self.resources)
        self.handler.assert_called_once()
        self.assertEqual([], self.resources.document_failures)

    def test_xinclude_rewriting_continues_after_invalid_utf8(self) -> None:
        """
        Checks a decoding problem is warned about without preventing later include rewrites.
        """
        broken_path = self.inscriptions_path / 'a-broken.xml'
        broken_path.write_bytes(b'<TEI>\xff</TEI>')
        valid_path = self.inscriptions_path / 'z-valid.xml'
        valid_path.write_text(next(iter(processor.XINCLUDE_REPLACEMENTS)), encoding='utf-8')
        with self.assertLogs('usep_indexer_app.lib.processor', level='WARNING') as captured:
            count = processor.update_xinclude_references(self.inscriptions_path)
        self.assertEqual(1, count)
        self.assertIn('a-broken.xml', '\n'.join(captured.output))
        self.assertIn('../resources/', valid_path.read_text(encoding='utf-8'))
        self.assertEqual(b'<TEI>\xff</TEI>', broken_path.read_bytes())

    def test_partial_queue_run_retains_retry_without_premature_email(self) -> None:
        """
        Checks a good inscription is posted before the failed event is retained for a silent retry.
        """
        (self.inscriptions_path / 'broken.xml').write_bytes(b'<TEI>')
        spool_root = self.root / 'spool'
        spool.write_event(
            spool_root,
            'incremental',
            files_updated=['xml_inscriptions/transcribed/broken.xml', 'xml_inscriptions/transcribed/transcribed.xml'],
        )
        with (
            override_settings(
                WEBSERVED_DATA_DIR_PATH=self.root,
                SPOOL_ROOT_PATH=spool_root,
                ADMINS=[('Example Admin', 'admin@example.org')],
            ),
            patch.object(processor, 'prepare_public_data', return_value='abc1234'),
            patch.object(indexer.IndexingResources, 'load', return_value=self.resources),
            self.assertRaisesRegex(CommandError, 'InscriptionProcessingError'),
        ):
            call_command('process_spool')

        self.handler.assert_called_once()
        pending_path = next((spool_root / 'pending').glob('*.json'))
        pending = json.loads(pending_path.read_text(encoding='utf-8'))
        self.assertEqual(1, pending['attempts'])
        self.assertIn('broken', pending['last_error'])
        outbox: list[mail.EmailMessage] = vars(mail)['outbox']
        self.assertEqual([], outbox)

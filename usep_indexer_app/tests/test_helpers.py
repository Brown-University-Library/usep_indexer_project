import json
import pathlib
import tempfile
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from usep_indexer_app.lib import bibliography, indexer, orphans, payloads, processor, reindex, transcription, xml_validation


class HelperTests(SimpleTestCase):
    """
    Checks pure helper behavior.
    """

    def test_malformed_payload_keeps_legacy_empty_lists(self) -> None:
        """
        Checks that malformed JSON produces empty changed-file lists.
        """
        files = payloads.prepare_files_to_process(b'{bad json')
        self.assertEqual([], files['files_updated'])
        self.assertEqual([], files['files_removed'])

    def test_xinclude_rewrite_updates_only_known_resource_urls(self) -> None:
        """
        Checks all three known XInclude replacements.
        """
        original = ' '.join(processor.XINCLUDE_REPLACEMENTS)
        updated = processor.rewrite_xinclude_text(original)
        for replacement in processor.XINCLUDE_REPLACEMENTS.values():
            self.assertIn(replacement, updated)
        self.assertNotIn('http://library.brown.edu/usep_data/resources/', updated)

    def test_update_xinclude_references_writes_changed_xml(self) -> None:
        """
        Checks filesystem rewriting and ignores non-XML files.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = pathlib.Path(temporary_directory)
            xml_path = directory / 'one.xml'
            xml_path.write_text(next(iter(processor.XINCLUDE_REPLACEMENTS)), encoding='utf-8')
            (directory / 'notes.txt').write_text('unchanged', encoding='utf-8')
            count = processor.update_xinclude_references(directory)
            self.assertEqual(1, count)
            self.assertIn('../resources/', xml_path.read_text(encoding='utf-8'))

    def test_index_filter_requires_an_inscription_source_directory(self) -> None:
        """
        Checks incremental filtering for inscription and resource paths.
        """
        self.assertTrue(indexer.should_index_path('xml_inscriptions/transcribed/one.xml'))
        self.assertFalse(indexer.should_index_path('resources/titles.xml'))

    def test_incremental_indexer_coalesces_flattened_filenames(self) -> None:
        """
        Checks duplicate source-directory paths become one flattened inscription update.
        """
        filenames = indexer.affected_filenames(
            ['resources/titles.xml', 'xml_inscriptions/transcribed/one.xml'],
            ['xml_inscriptions/metadata_only/one.xml', 'xml_inscriptions/bib_only/two.xml'],
        )
        self.assertEqual(['one.xml', 'two.xml'], filenames)

    def test_build_solr_document_applies_configured_xslt(self) -> None:
        """
        Checks the core inscription-to-Solr transformation happy path.
        """
        inscription_text = '<TEI><id>one</id></TEI>'
        xsl_text = """
            <xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
                <xsl:output method="xml"/>
                <xsl:template match="/">
                    <add><doc><field name="id"><xsl:value-of select="TEI/id"/></field></doc></add>
                </xsl:template>
            </xsl:stylesheet>
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = pathlib.Path(temporary_directory)
            inscription_path = directory / 'one.xml'
            xsl_path = directory / 'index.xsl'
            inscription_path.write_text(inscription_text, encoding='utf-8')
            xsl_path.write_text(xsl_text, encoding='utf-8')
            solr_document = indexer.build_solr_document(inscription_path, xsl_path)
        self.assertIn('<field name="id">one</field>', solr_document)

    def test_bibliography_reads_titles_xml_from_local_path(self) -> None:
        """
        Checks that bibliography relationships are built locally without Solr.
        """
        titles_xml = """
            <listBibl xmlns="http://www.tei-c.org/ns/1.0">
                <bibl xml:id="parent"/>
                <bibl xml:id="child"><title ref="#parent">Child</title></bibl>
            </listBibl>
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            titles_xml_path = pathlib.Path(temporary_directory) / 'titles.xml'
            titles_xml_path.write_text(titles_xml, encoding='utf-8')
            graph = bibliography.load_bibliography_graph(titles_xml_path)
        result, diagnostics = bibliography.resolve_bibliography_ids(['child'], graph)
        self.assertEqual(['child', 'parent'], result)
        self.assertEqual([], diagnostics)

    def test_bibliography_rejects_missing_titles_xml_path(self) -> None:
        """
        Checks that a missing local titles XML file raises a filesystem error before contacting Solr.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing_path = pathlib.Path(temporary_directory) / 'missing.xml'
            with self.assertRaises(OSError):
                bibliography.load_bibliography_graph(missing_path)

    def test_orphan_list_is_sorted_set_difference(self) -> None:
        """
        Checks orphan computation shared by admin and reindex flows.
        """
        result = orphans.build_orphan_list(['one', 'three'], ['three', 'two', 'one', 'four'])
        self.assertEqual(['four', 'two'], result)

    def test_solr_index_label_uses_hostname_environment_prefix(self) -> None:
        """
        Checks safe dev/prod labels and the neutral fallback without exposing a host.
        """
        test_cases = {
            'https://dev-solr.example.org/solr/usep': 'configured dev Solr index',
            'https://Prod-solr.example.org/solr/usep': 'configured prod Solr index',
            'http://127.0.0.1:9999/solr/usep': 'configured Solr index',
        }
        for solr_url, expected_label in test_cases.items():
            with self.subTest(solr_url=solr_url):
                self.assertEqual(expected_label, orphans.build_solr_index_label(solr_url))

    def test_full_reindex_orphan_ids_come_from_file_stems(self) -> None:
        """
        Checks full reindex filesystem-to-Solr comparison.
        """
        result = reindex.build_orphaned_ids(['/data/one.xml', '/data/two.xml'], ['one', 'old'])
        self.assertEqual(['old'], result)

    def test_single_reindex_filename_accepts_existing_id_characters_and_rejects_paths(self) -> None:
        """
        Checks single-reindex ID validation permits spaces and plus signs without accepting paths.
        """
        inscription_id = 'KY.Lou.SAM.L.1929.17.440A+B'
        self.assertEqual(f'{inscription_id}.xml', reindex.build_inscription_filename(inscription_id))
        invalid_ids = ['', '../one', 'directory/one', 'directory\\one', 'one.xml', ' one']
        for invalid_id in invalid_ids:
            with self.subTest(invalid_id=invalid_id):
                with self.assertRaises(ValueError):
                    reindex.build_inscription_filename(invalid_id)

    @patch('usep_indexer_app.lib.orphans.solr_client.SolrClient')
    def test_orphan_deletion_continues_after_one_failure(self, mock_client_class) -> None:
        """
        Checks the administrative delete flow reports failed IDs and continues.
        """
        mock_client = mock_client_class.return_value.__enter__.return_value
        mock_client.delete_ids.side_effect = [RuntimeError('Solr unavailable'), 'deleted']
        errors = orphans.run_deletes(['bad-id', 'good-id'])
        self.assertEqual(['bad-id'], errors)
        self.assertEqual(2, mock_client.delete_ids.call_count)

    def test_transcription_without_an_edition_returns_empty_text(self) -> None:
        """
        Checks the transcription edge case before loading the XSLT.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = pathlib.Path(temporary_directory)
            xml_path = directory / 'one.xml'
            xml_path.write_text('<TEI xmlns="http://www.tei-c.org/ns/1.0"/>', encoding='utf-8')
            result = transcription.build_transcription(indexer.parse_inscription(xml_path), Mock())
        self.assertEqual('', result)

    @patch('usep_indexer_app.lib.processor.subprocess.run')
    def test_git_pull_uses_argument_list_and_configured_working_directory(self, mock_run) -> None:
        """
        Checks that git invocation avoids shell-built commands.
        """
        clone_path = pathlib.Path('/tmp/clone with spaces')
        processor.call_git_pull(clone_path)
        mock_run.assert_called_once_with(['git', 'pull'], cwd=clone_path, check=True, text=True)

    @patch('usep_indexer_app.lib.processor.indexer.update_index')
    @patch('usep_indexer_app.lib.processor.update_xinclude_references', return_value=2)
    @patch('usep_indexer_app.lib.processor.copy_files')
    @patch('usep_indexer_app.lib.processor.call_git_pull')
    def test_incremental_processor_runs_synchronous_stages(
        self,
        mock_git_pull,
        mock_copy_files,
        mock_update_xinclude,
        mock_update_index,
    ) -> None:
        """
        Checks that incremental processing performs every required stage directly.
        """
        processor.process_incremental(['updated.xml'], ['removed.xml'])

        mock_git_pull.assert_called_once_with(pathlib.Path('/tmp/usep-data-clone'))
        mock_copy_files.assert_called_once_with(
            pathlib.Path('/tmp/usep-data-clone'),
            pathlib.Path('/tmp/temp_unified_inscriptions_dir'),
            pathlib.Path('/tmp/usep-webserved-data'),
        )
        mock_update_xinclude.assert_called_once_with(pathlib.Path('/tmp/usep-webserved-data/inscriptions'))
        mock_update_index.assert_called_once_with(['updated.xml'], ['removed.xml'], data_revision='unavailable')

    @patch('usep_indexer_app.lib.processor.indexer.update_index', side_effect=RuntimeError('Solr down'))
    @patch('usep_indexer_app.lib.processor.update_xinclude_references', return_value=2)
    @patch('usep_indexer_app.lib.processor.copy_files')
    @patch('usep_indexer_app.lib.processor.call_git_pull')
    def test_incremental_processor_logs_completed_file_stages_before_solr_failure(
        self,
        mock_git_pull,
        mock_copy_files,
        mock_update_xinclude,
        mock_update_index,
    ) -> None:
        """
        Checks that logs distinguish successful file preparation from a Solr failure.
        """
        with self.assertLogs('usep_indexer_app.lib.processor', level='INFO') as captured_logs:
            with self.assertRaisesRegex(RuntimeError, 'Solr down'):
                processor.process_incremental(['updated.xml'], ['removed.xml'])

        joined_logs = '\n'.join(captured_logs.output)
        self.assertIn('Git pull completed', joined_logs)
        self.assertIn('USEP data copy completed', joined_logs)
        self.assertIn('XInclude normalization completed', joined_logs)
        self.assertIn('changed_file_count, ``2``', joined_logs)
        self.assertIn('Incremental Solr indexing started', joined_logs)
        self.assertNotIn('Incremental Solr indexing completed', joined_logs)
        mock_git_pull.assert_called_once()
        mock_copy_files.assert_called_once()
        mock_update_xinclude.assert_called_once()
        mock_update_index.assert_called_once()

    @patch('usep_indexer_app.lib.reindex.process_prepared_full_reindex')
    @patch('usep_indexer_app.lib.processor.indexer.update_index')
    @patch('usep_indexer_app.lib.processor.index_affecting_resources_changed', return_value=True)
    @patch('usep_indexer_app.lib.processor.read_git_revision', return_value='abc1234')
    @patch('usep_indexer_app.lib.processor.update_xinclude_references', return_value=0)
    @patch('usep_indexer_app.lib.processor.copy_files')
    @patch('usep_indexer_app.lib.processor.call_git_pull')
    def test_incremental_indexing_resource_change_rebuilds_without_second_preparation(
        self,
        mock_git_pull,
        mock_copy_files,
        mock_update_xinclude,
        mock_read_revision,
        mock_resource_change,
        mock_update_index,
        mock_process_prepared_full_reindex,
    ) -> None:
        """
        Checks an indexing-XSL change promotes already copied data to a full rebuild.
        """
        processor.process_incremental(['resources/xsl/index-module.xsl'], [])

        mock_git_pull.assert_called_once()
        mock_copy_files.assert_called_once()
        mock_update_xinclude.assert_called_once()
        mock_read_revision.assert_called_once()
        mock_resource_change.assert_called_once_with(
            ['resources/xsl/index-module.xsl'],
            data_revision='abc1234',
        )
        mock_process_prepared_full_reindex.assert_called_once_with(data_revision='abc1234')
        mock_update_index.assert_not_called()

    def test_incremental_indexer_logs_ignored_paths_without_loading_resources(self) -> None:
        """
        Checks a display-only path is logged and causes no indexing-resource load.
        """
        with self.assertLogs('usep_indexer_app.lib.indexer', level='DEBUG') as captured_logs:
            indexer.update_index(['resources/xsl/display.xsl'], [])

        joined_logs = '\n'.join(captured_logs.output)
        self.assertIn('affected_filename_count, ``0``', joined_logs)
        self.assertIn('ignored_path_count, ``1``', joined_logs)

    @patch('usep_indexer_app.lib.reindex.indexer.delete_id_batches', return_value=1)
    @patch('usep_indexer_app.lib.reindex.indexer.post_document_batches', return_value=2)
    def test_full_reindex_uses_bounded_document_and_deletion_batches(self, mock_post_batches, mock_delete_batches) -> None:
        """
        Checks full-reindex mutations use the shared batching helpers.
        """
        resources = Mock()
        documents = [Mock(), Mock()]
        result = reindex.update_all_index_entries(documents, ['old'], resources)

        self.assertEqual((2, 1), result)
        mock_post_batches.assert_called_once_with(documents, resources)
        mock_delete_batches.assert_called_once_with(['old'], resources)

    @patch('usep_indexer_app.lib.reindex.indexer.build_complete_documents', side_effect=RuntimeError('bad XSL'))
    @patch('usep_indexer_app.lib.reindex.indexer.IndexingResources.load')
    def test_prepared_full_reindex_builds_everything_before_reading_solr(self, mock_load_resources, mock_build_documents) -> None:
        """
        Checks a local construction failure causes zero Solr requests during a full rebuild.
        """
        resources = mock_load_resources.return_value.__enter__.return_value
        with tempfile.TemporaryDirectory() as temporary_directory:
            public_data_path = pathlib.Path(temporary_directory) / 'public-data'
            inscriptions_path = public_data_path / 'inscriptions'
            inscriptions_path.mkdir(parents=True)
            (inscriptions_path / 'one.xml').write_text('<root/>', encoding='utf-8')
            with override_settings(WEBSERVED_DATA_DIR_PATH=public_data_path):
                with self.assertRaisesRegex(RuntimeError, 'bad XSL'):
                    reindex.process_prepared_full_reindex(data_revision='abc1234')

        mock_build_documents.assert_called_once()
        resources.solr.get_ids.assert_not_called()

    @patch('usep_indexer_app.lib.reindex.process_prepared_full_reindex')
    @patch('usep_indexer_app.lib.reindex.processor.update_xinclude_references')
    @patch('usep_indexer_app.lib.reindex.processor.copy_files')
    @patch('usep_indexer_app.lib.reindex.processor.call_git_pull')
    def test_full_reindex_validates_corpus_before_copying_or_contacting_solr(
        self,
        mock_git_pull,
        mock_copy_files,
        mock_update_xinclude,
        mock_process_prepared_full_reindex,
    ) -> None:
        """
        Checks malformed source XML stops the workflow before copying or contacting Solr.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            usep_data_path = pathlib.Path(temporary_directory) / 'usep-data'
            inscriptions_path = usep_data_path / 'xml_inscriptions' / 'transcribed'
            inscriptions_path.mkdir(parents=True)
            (inscriptions_path / 'broken.xml').write_bytes(b'<root><child></root>')

            with override_settings(USEP_DATA_GIT_CLONED_DIR_PATH=usep_data_path):
                with self.assertRaisesRegex(xml_validation.XMLNotWellFormedError, 'transcribed/broken.xml'):
                    reindex.process_full_reindex()

        mock_git_pull.assert_called_once_with(usep_data_path)
        mock_copy_files.assert_not_called()
        mock_update_xinclude.assert_not_called()
        mock_process_prepared_full_reindex.assert_not_called()

    @patch('usep_indexer_app.lib.reindex.process_prepared_full_reindex')
    @patch('usep_indexer_app.lib.reindex.processor.update_xinclude_references', return_value=0)
    @patch('usep_indexer_app.lib.reindex.processor.copy_files')
    @patch('usep_indexer_app.lib.reindex.processor.call_git_pull')
    def test_full_reindex_continues_after_successful_corpus_validation(
        self,
        mock_git_pull,
        mock_copy_files,
        mock_update_xinclude,
        mock_process_prepared_full_reindex,
    ) -> None:
        """
        Checks a well-formed source corpus reaches the Solr reconciliation stage.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            base_path = pathlib.Path(temporary_directory)
            usep_data_path = base_path / 'usep-data'
            source_inscriptions_path = usep_data_path / 'xml_inscriptions' / 'transcribed'
            source_inscriptions_path.mkdir(parents=True)
            (source_inscriptions_path / 'one.xml').write_bytes(b'<root />')
            webserved_data_path = base_path / 'webserved-data'
            copied_inscriptions_path = webserved_data_path / 'inscriptions'
            copied_inscriptions_path.mkdir(parents=True)
            copied_xml_path = copied_inscriptions_path / 'one.xml'
            copied_xml_path.write_bytes(b'<root />')

            with override_settings(
                USEP_DATA_GIT_CLONED_DIR_PATH=usep_data_path,
                TEMP_UNIFIED_INSCRIPTIONS_DIR_PATH=base_path / 'temporary-inscriptions',
                WEBSERVED_DATA_DIR_PATH=webserved_data_path,
            ):
                reindex.process_full_reindex()

        mock_git_pull.assert_called_once_with(usep_data_path)
        mock_copy_files.assert_called_once()
        mock_update_xinclude.assert_called_once_with(copied_inscriptions_path)
        mock_process_prepared_full_reindex.assert_called_once_with(data_revision='unavailable')

    @patch('usep_indexer_app.lib.reindex.indexer.update_index_entry')
    @patch('usep_indexer_app.lib.reindex.processor.update_xinclude_references', return_value=2)
    @patch('usep_indexer_app.lib.reindex.processor.copy_files')
    @patch('usep_indexer_app.lib.reindex.processor.call_git_pull')
    def test_single_reindex_refreshes_data_and_uses_complete_indexing(
        self,
        mock_git_pull,
        mock_copy_files,
        mock_update_xinclude,
        mock_update_index_entry,
    ) -> None:
        """
        Checks one-inscription reindexing pulls, copies, normalizes, and completely updates Solr.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            base_path = pathlib.Path(temporary_directory)
            usep_data_path = base_path / 'usep-data'
            temporary_inscriptions_path = base_path / 'temporary-inscriptions'
            webserved_data_path = base_path / 'webserved-data'
            copied_inscriptions_path = webserved_data_path / 'inscriptions'
            copied_inscriptions_path.mkdir(parents=True)
            copied_xml_path = copied_inscriptions_path / 'one.xml'
            copied_xml_path.write_bytes(b'<root />')

            with override_settings(
                USEP_DATA_GIT_CLONED_DIR_PATH=usep_data_path,
                TEMP_UNIFIED_INSCRIPTIONS_DIR_PATH=temporary_inscriptions_path,
                WEBSERVED_DATA_DIR_PATH=webserved_data_path,
            ):
                result = reindex.process_single_reindex('one')

        self.assertEqual(copied_xml_path, result)
        mock_git_pull.assert_called_once_with(usep_data_path)
        mock_copy_files.assert_called_once_with(usep_data_path, temporary_inscriptions_path, webserved_data_path)
        mock_update_xinclude.assert_called_once_with(copied_inscriptions_path)
        mock_update_index_entry.assert_called_once_with('one.xml', data_revision='unavailable')

    @patch('usep_indexer_app.lib.reindex.indexer.update_index_entry')
    @patch('usep_indexer_app.lib.reindex.processor.update_xinclude_references', return_value=0)
    @patch('usep_indexer_app.lib.reindex.processor.copy_files')
    @patch('usep_indexer_app.lib.reindex.processor.call_git_pull')
    def test_single_reindex_fails_when_copied_inscription_is_missing(
        self,
        mock_git_pull,
        mock_copy_files,
        mock_update_xinclude,
        mock_update_index_entry,
    ) -> None:
        """
        Checks a missing requested inscription fails after the source-data refresh.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            base_path = pathlib.Path(temporary_directory)
            usep_data_path = base_path / 'usep-data'
            temporary_inscriptions_path = base_path / 'temporary-inscriptions'
            webserved_data_path = base_path / 'webserved-data'
            with override_settings(
                USEP_DATA_GIT_CLONED_DIR_PATH=usep_data_path,
                TEMP_UNIFIED_INSCRIPTIONS_DIR_PATH=temporary_inscriptions_path,
                WEBSERVED_DATA_DIR_PATH=webserved_data_path,
            ):
                with self.assertRaisesRegex(FileNotFoundError, "ID 'missing'"):
                    reindex.process_single_reindex('missing')

        mock_git_pull.assert_called_once_with(usep_data_path)
        mock_copy_files.assert_called_once_with(usep_data_path, temporary_inscriptions_path, webserved_data_path)
        mock_update_xinclude.assert_called_once_with(webserved_data_path / 'inscriptions')
        mock_update_index_entry.assert_not_called()

    def test_version_response_uses_git_head(self) -> None:
        """
        Checks that the version endpoint reads branch and commit data from Git metadata.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            base_dir = pathlib.Path(temporary_directory)
            branch_dir = base_dir / '.git' / 'refs' / 'heads'
            branch_dir.mkdir(parents=True)
            (base_dir / '.git' / 'HEAD').write_text('ref: refs/heads/main\n', encoding='utf-8')
            (branch_dir / 'main').write_text('abc123\n', encoding='utf-8')
            with override_settings(BASE_DIR=base_dir):
                response = self.client.get('/version/')
        self.assertEqual('main abc123', json.loads(response.content)['response']['version'])


class DatabaseFreeConfigurationTests(SimpleTestCase):
    """
    Checks the explicit no-database architecture.
    """

    def test_database_and_database_apps_are_absent(self) -> None:
        """
        Checks that settings contain no database-dependent Django components.
        """
        from django.conf import settings

        self.assertEqual('django.db.backends.dummy', settings.DATABASES['default']['ENGINE'])
        self.assertNotIn('django.contrib.admin', settings.INSTALLED_APPS)
        self.assertNotIn('django.contrib.auth', settings.INSTALLED_APPS)
        self.assertNotIn('django.contrib.contenttypes', settings.INSTALLED_APPS)
        self.assertNotIn('django.contrib.sessions', settings.INSTALLED_APPS)
        self.assertEqual('django.contrib.sessions.backends.signed_cookies', settings.SESSION_ENGINE)

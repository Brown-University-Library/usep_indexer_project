import json
import pathlib
import tempfile
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from usep_indexer_app.lib import bibliography, indexer, orphans, payloads, processor, reindex, transcription


class HelperTests(SimpleTestCase):
    """
    Checks migrated pure helper behavior.
    """

    def test_malformed_payload_keeps_legacy_empty_lists(self) -> None:
        """
        Checks that malformed JSON does not prevent the legacy acknowledgement.
        """
        files = payloads.prepare_files_to_process(b'{bad json')
        self.assertEqual([], files['files_updated'])
        self.assertEqual([], files['files_removed'])

    def test_xinclude_rewrite_updates_only_known_resource_urls(self) -> None:
        """
        Checks all three legacy XInclude replacements.
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

    @patch('usep_indexer_app.lib.indexer.update_entry')
    @patch('usep_indexer_app.lib.indexer.remove_entry')
    def test_incremental_indexer_updates_only_inscription_paths(self, mock_remove_entry, mock_update_entry) -> None:
        """
        Checks synchronous updates and deletes while ignoring resource changes.
        """
        indexer.update_index(
            ['resources/titles.xml', 'xml_inscriptions/transcribed/one.xml'],
            ['xml_inscriptions/bib_only/two.xml'],
        )
        mock_remove_entry.assert_called_once_with('xml_inscriptions/bib_only/two.xml')
        mock_update_entry.assert_called_once_with('xml_inscriptions/transcribed/one.xml')

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

    @patch('usep_indexer_app.lib.indexer.build_solr_document', return_value='<add/>')
    @patch('usep_indexer_app.lib.indexer.solr_client.post_xml_update', side_effect=RuntimeError('Solr down'))
    def test_solr_post_failure_logs_source_file(self, mock_post_xml_update, mock_build_solr_document) -> None:
        """
        Checks a failed Solr post identifies the inscription file without hiding the original error.
        """
        with self.assertLogs('usep_indexer_app.lib.indexer', level='ERROR') as captured_logs:
            with self.assertRaisesRegex(RuntimeError, 'Solr down'):
                indexer.update_index_entry('one.xml')

        joined_logs = '\n'.join(captured_logs.output)
        expected_path = pathlib.Path('/tmp/usep-webserved-data/inscriptions/one.xml')
        self.assertIn('Solr XML update failed; filename, ``one.xml``', joined_logs)
        self.assertIn(f'inscription_path, ``{expected_path}``', joined_logs)
        mock_build_solr_document.assert_called_once()
        mock_post_xml_update.assert_called_once_with('http://solr.example.org/solr/usep', '<add/>')

    @patch('usep_indexer_app.lib.bibliography.solr_client.soft_commit')
    @patch('usep_indexer_app.lib.bibliography.solr_client.post_json_update')
    @patch('usep_indexer_app.lib.bibliography.solr_client.select_bibliography_ids', return_value=['child'])
    def test_bibliography_reads_titles_xml_from_local_path(
        self,
        mock_select_bibliography_ids,
        mock_post_json_update,
        mock_soft_commit,
    ) -> None:
        """
        Checks that bibliography enrichment reads ancestor IDs from a local titles XML file.
        """
        titles_xml = """
            <listBibl xmlns="http://www.tei-c.org/ns/1.0">
                <bibl xml:id="parent"><bibl xml:id="child"/></bibl>
            </listBibl>
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            titles_xml_path = pathlib.Path(temporary_directory) / 'titles.xml'
            titles_xml_path.write_text(titles_xml, encoding='utf-8')
            result = bibliography.add_bibliography('http://solr.example.org/solr/usep', titles_xml_path, 'one')

        self.assertTrue(result)
        mock_select_bibliography_ids.assert_called_once_with('http://solr.example.org/solr/usep', 'one')
        mock_post_json_update.assert_called_once_with(
            'http://solr.example.org/solr/usep',
            [{'id': 'one', 'bib_ids': {'add': ['parent']}}],
        )
        mock_soft_commit.assert_called_once_with('http://solr.example.org/solr/usep')

    @patch('usep_indexer_app.lib.bibliography.solr_client.select_bibliography_ids')
    def test_bibliography_rejects_missing_titles_xml_path(self, mock_select_bibliography_ids) -> None:
        """
        Checks that a missing local titles XML file raises a filesystem error before contacting Solr.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing_path = pathlib.Path(temporary_directory) / 'missing.xml'
            with self.assertRaises(OSError):
                bibliography.add_bibliography('http://solr.example.org/solr/usep', missing_path, 'one')
        mock_select_bibliography_ids.assert_not_called()

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

    @patch('usep_indexer_app.lib.orphans.solr_client.delete_id')
    def test_orphan_deletion_continues_after_one_failure(self, mock_delete_id) -> None:
        """
        Checks the administrative delete flow reports failed IDs and continues.
        """
        mock_delete_id.side_effect = [RuntimeError('Solr unavailable'), 'deleted']
        errors = orphans.run_deletes(['bad-id', 'good-id'])
        self.assertEqual(['bad-id'], errors)
        self.assertEqual(2, mock_delete_id.call_count)

    def test_transcription_without_an_edition_returns_empty_text(self) -> None:
        """
        Checks the transcription edge case before loading the XSLT.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = pathlib.Path(temporary_directory)
            xml_path = directory / 'one.xml'
            xml_path.write_text('<TEI xmlns="http://www.tei-c.org/ns/1.0"/>', encoding='utf-8')
            result = transcription.build_transcription(xml_path, directory / 'missing.xsl')
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
        Checks that incremental processing performs every stage without queue fan-out.
        """
        processor.process_incremental(['updated.xml'], ['removed.xml'])

        mock_git_pull.assert_called_once_with(pathlib.Path('/tmp/usep-data-clone'))
        mock_copy_files.assert_called_once_with(
            pathlib.Path('/tmp/usep-data-clone'),
            pathlib.Path('/tmp/temp_unified_inscriptions_dir'),
            pathlib.Path('/tmp/usep-webserved-data'),
        )
        mock_update_xinclude.assert_called_once_with(pathlib.Path('/tmp/usep-webserved-data/inscriptions'))
        mock_update_index.assert_called_once_with(['updated.xml'], ['removed.xml'])

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

    @patch('usep_indexer_app.lib.indexer.update_entry')
    @patch('usep_indexer_app.lib.indexer.remove_entry')
    def test_incremental_indexer_logs_actions_and_ignored_paths(self, mock_remove_entry, mock_update_entry) -> None:
        """
        Checks debug logs explain which incremental paths affect Solr.
        """
        with self.assertLogs('usep_indexer_app.lib.indexer', level='DEBUG') as captured_logs:
            indexer.update_index(
                ['resources/titles.xml', 'xml_inscriptions/transcribed/one.xml'],
                ['xml_inscriptions/bib_only/two.xml'],
            )

        joined_logs = '\n'.join(captured_logs.output)
        self.assertIn('indexable_updated_count, ``1``', joined_logs)
        self.assertIn('Removing Solr entry; removed_file_path, ``xml_inscriptions/bib_only/two.xml``', joined_logs)
        self.assertIn('Ignoring non-inscription update; updated_file_path, ``resources/titles.xml``', joined_logs)
        self.assertIn('Updating Solr entry; updated_file_path, ``xml_inscriptions/transcribed/one.xml``', joined_logs)

    @patch('usep_indexer_app.lib.reindex.indexer.update_entry')
    @patch('usep_indexer_app.lib.reindex.indexer.remove_entry_via_id')
    def test_full_reindex_updates_are_synchronous(self, mock_remove_entry, mock_update_entry) -> None:
        """
        Checks that full-reindex removals and updates run directly in stable order.
        """
        reindex.update_all_index_entries(['/data/one.xml', '/data/two.xml'], ['old'])

        mock_remove_entry.assert_called_once_with('old')
        self.assertEqual([('/data/one.xml',), ('/data/two.xml',)], [call.args for call in mock_update_entry.call_args_list])

    def test_version_response_uses_git_head(self) -> None:
        """
        Checks the template version helper in the migrated project.
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

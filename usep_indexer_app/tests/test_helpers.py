import json
import pathlib
import tempfile
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from usep_indexer_app.lib import indexer, orphans, payloads, processor, reindex, transcription


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

    @patch('usep_indexer_app.lib.indexer.enqueue_call')
    def test_incremental_indexer_queues_only_inscription_paths(self, mock_enqueue_call) -> None:
        """
        Checks update and delete fan-out while ignoring resource changes.
        """
        indexer.run_update_index(
            ['resources/titles.xml', 'xml_inscriptions/transcribed/one.xml'],
            ['xml_inscriptions/bib_only/two.xml'],
        )
        self.assertEqual(2, mock_enqueue_call.call_count)
        self.assertEqual(
            'usep_indexer_app.lib.indexer.run_remove_entry',
            mock_enqueue_call.call_args_list[0].args[0],
        )
        self.assertEqual(
            'usep_indexer_app.lib.indexer.run_update_entry',
            mock_enqueue_call.call_args_list[1].args[0],
        )

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

    def test_orphan_list_is_sorted_set_difference(self) -> None:
        """
        Checks orphan computation shared by admin and reindex flows.
        """
        result = orphans.build_orphan_list(['one', 'three'], ['three', 'two', 'one', 'four'])
        self.assertEqual(['four', 'two'], result)

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

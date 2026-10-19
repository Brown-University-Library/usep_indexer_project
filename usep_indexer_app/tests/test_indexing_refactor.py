import copy
import pathlib
import tempfile
from unittest.mock import Mock, patch

import httpx
from django.test import SimpleTestCase, override_settings
from lxml import etree

from usep_indexer_app.lib import bibliography, indexer, processor, solr_client, stylesheet_dependencies, transcription

FIXTURE_PATH = pathlib.Path(__file__).parent / 'fixtures' / 'indexing'


def build_resources(http_client: httpx.Client, *, batch_size: int = 100) -> indexer.IndexingResources:
    """
    Builds run-scoped indexing resources from repository fixtures.

    Called by: IndexingRefactorTests methods
    """
    resources = indexer.IndexingResources(
        base_transformer=indexer.load_transformer(FIXTURE_PATH / 'base.xsl'),
        transcription_transformer=transcription.load_transformer(FIXTURE_PATH / 'transcription.xsl'),
        bibliography_graph=bibliography.load_bibliography_graph(FIXTURE_PATH / 'titles.xml'),
        solr=solr_client.SolrClient(
            'https://solr.example.org/solr/usep',
            http_client=http_client,
            timeout=5,
            commit_within_ms=500,
        ),
        batch_size=batch_size,
        data_revision='abc1234',
    )
    return resources


def parse_xml_text(xml_text: str) -> etree._ElementTree:
    """
    Parses an in-memory XML fixture with the production parser restrictions.

    Called by: IndexingRefactorTests methods
    """
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    root = etree.fromstring(xml_text.encode('utf-8'), parser=parser)
    return etree.ElementTree(root)


class IndexingRefactorTests(SimpleTestCase):
    """
    Checks complete-document construction, transport, and resource invalidation.
    """

    def make_http_client(self, handler: Mock) -> httpx.Client:
        """
        Checks creation of a reusable in-memory HTTP client for one test.
        """
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return client

    def test_complete_builder_preserves_researcher_fields_for_all_statuses(self) -> None:
        """
        Checks representative bibliography-only, metadata, and transcription documents.
        """
        handler = Mock(return_value=httpx.Response(200, text='ok'))
        resources = build_resources(self.make_http_client(handler))
        expected_statuses = {
            'bib-only.xml': 'bib_only',
            'metadata.xml': 'metadata',
            'transcribed.xml': 'transcription',
        }
        for filename, expected_status in expected_statuses.items():
            with self.subTest(filename=filename):
                document = indexer.build_complete_document(FIXTURE_PATH / filename, resources)
                self.assertEqual([expected_status], indexer.field_values(document, 'status'))
                self.assertEqual(['first', 'second'], indexer.field_values(document, 'researcher_extension'))
                self.assertEqual([], document.xpath('./field[@name="transcription"]'))

        transcribed_document = indexer.build_complete_document(FIXTURE_PATH / 'transcribed.xml', resources)
        metadata_document = indexer.build_complete_document(FIXTURE_PATH / 'metadata.xml', resources)
        self.assertIn('arma Marcus virumque cano', indexer.field_values(transcribed_document, 'text'))
        self.assertEqual(['unicode-Δ', 'parent'], indexer.field_values(transcribed_document, 'bib_ids'))
        self.assertEqual([], indexer.field_values(metadata_document, 'transcription'))
        handler.assert_not_called()

    def test_complete_update_selects_first_nonempty_image_and_preserves_source(self) -> None:
        """
        Checks image layouts post at most one URL without changing XML or fetching images.
        """
        cases = [
            ('', []),
            ('<facsimile><surface><graphic/><graphic url=" "/></surface></facsimile>', []),
            ('<facsimile><surface><graphic url="only.jpg"/></surface></facsimile>', ['only.jpg']),
            (
                '<facsimile><surface><graphic url="first.jpg"/><graphic url="second.jpg"/></surface></facsimile>',
                ['first.jpg'],
            ),
            (
                (
                    '<facsimile><surface><graphic url="z.jpg"/></surface>'
                    '<surface><graphic url="a.jpg"/></surface></facsimile>'
                ),
                ['z.jpg'],
            ),
            (
                (
                    '<facsimile><surface><graphic url="first.jpg"/></surface></facsimile>'
                    '<facsimile><surface><graphic url="second.jpg"/></surface></facsimile>'
                ),
                ['first.jpg'],
            ),
            (
                '<facsimile><surface><graphic url=" first.jpg second.jpg "/></surface></facsimile>',
                ['first.jpg'],
            ),
        ]
        handler = Mock(return_value=httpx.Response(200, text='ok'))
        resources = build_resources(self.make_http_client(handler))
        with tempfile.TemporaryDirectory() as temporary_directory:
            public_path = pathlib.Path(temporary_directory)
            inscriptions_path = public_path / 'inscriptions'
            inscriptions_path.mkdir()
            for images, expected_urls in cases:
                with self.subTest(images=images):
                    handler.reset_mock()
                    content = (FIXTURE_PATH / 'metadata.xml').read_text().replace('<text>', f'{images}<text>')
                    inscription_path = inscriptions_path / 'metadata.xml'
                    inscription_path.write_text(content)
                    with override_settings(WEBSERVED_DATA_DIR_PATH=public_path):
                        indexer.update_index_entry('metadata.xml', resources=resources)
                    handler.assert_called_once()
                    request: httpx.Request = handler.call_args.args[0]
                    self.assertEqual('POST', request.method)
                    self.assertEqual('solr.example.org', request.url.host)
                    update_xml = etree.fromstring(request.content)
                    self.assertEqual(expected_urls, update_xml.xpath('./doc/field[@name="graphic_name"]/text()'))
                    self.assertEqual(content, inscription_path.read_text())

    def test_complete_builder_includes_nested_edition_blocks(self) -> None:
        """
        Checks nested edition blocks reach full-text search in source order without a separate field.
        """
        handler = Mock(return_value=httpx.Response(200, text='ok'))
        resources = build_resources(self.make_http_client(handler))
        document = indexer.build_complete_document(FIXTURE_PATH / 'nested-transcribed.xml', resources)

        expected_transcription = 'ARMA CANO arma cano'
        self.assertEqual(['transcription'], indexer.field_values(document, 'status'))
        self.assertEqual([], document.xpath('./field[@name="transcription"]'))
        self.assertEqual([expected_transcription], indexer.field_values(document, 'text'))
        handler.assert_not_called()

    def test_complete_builder_removes_stylesheet_transcription_and_preserves_text(self) -> None:
        """
        Checks stylesheet transcription fields are removed while existing search text is preserved.
        """
        handler = Mock(return_value=httpx.Response(200, text='ok'))
        resources = build_resources(self.make_http_client(handler))
        inscription_xml = indexer.parse_inscription(FIXTURE_PATH / 'transcribed.xml')
        base_document = indexer.transform_to_document(inscription_xml, resources.base_transformer)
        indexer.append_field_value(base_document, 'transcription', 'old transcription')
        etree.SubElement(base_document, 'field', name='transcription')
        indexer.append_field_value(base_document, 'text', 'existing search text')
        indexer.append_field_value(base_document, 'text', 'arma Marcus virumque cano')
        with patch.object(indexer, 'transform_to_document', return_value=base_document):
            document = indexer.build_complete_document(FIXTURE_PATH / 'transcribed.xml', resources)

        self.assertEqual([], document.xpath('./field[@name="transcription"]'))
        self.assertEqual(['existing search text', 'arma Marcus virumque cano'], indexer.field_values(document, 'text'))
        self.assertEqual(['transcription'], indexer.field_values(document, 'status'))
        handler.assert_not_called()

    def test_document_validation_requires_transcription_in_search_text(self) -> None:
        """
        Checks transcription validation still rejects missing search text and an inconsistent status.
        """
        document = etree.fromstring(
            b'<doc><field name="id">expected</field><field name="status">transcription</field></doc>'
        )
        with self.assertRaisesRegex(indexer.SolrDocumentValidationError, 'full-text field'):
            indexer.validate_complete_document(document, 'expected', 'arma cano')
        with self.assertRaisesRegex(indexer.SolrDocumentValidationError, 'Status/transcription mismatch'):
            indexer.validate_complete_document(document, 'expected', '')
        indexer.append_field_value(document, 'text', 'arma cano')
        indexer.validate_complete_document(document, 'expected', 'arma cano')

    def test_bibliography_graph_handles_flat_refs_unicode_recursion_and_cycles(self) -> None:
        """
        Checks fragment/bare parents, descendant titles, Unicode, multi-level traversal, and cycles.
        """
        titles_xml = parse_xml_text(
            """
            <listBibl xmlns="http://www.tei-c.org/ns/1.0">
              <bibl xml:id="top"><title>Top</title></bibl>
              <bibl xml:id="middle"><author><title ref="#top">Middle</title></author></bibl>
              <bibl xml:id="unicode-Δ"><title ref="middle">Child</title></bibl>
              <bibl xml:id="cycle-a"><title ref="#cycle-b">A</title></bibl>
              <bibl xml:id="cycle-b"><title ref="#cycle-a">B</title></bibl>
              <bibl xml:id="broken"><title ref="#missing">Broken</title><title ref="https://example.org/x">External</title></bibl>
            </listBibl>
            """
        )
        graph = bibliography.build_bibliography_graph(titles_xml)
        resolved_ids, diagnostics = bibliography.resolve_bibliography_ids(['unicode-Δ', 'unknown'], graph)

        self.assertEqual(['unicode-Δ', 'middle', 'top', 'unknown'], resolved_ids)
        self.assertTrue(any('unresolved local parent' in value for value in graph.diagnostics))
        self.assertTrue(any('nonlocal' in value for value in graph.diagnostics))
        self.assertTrue(any('cycle detected' in value for value in graph.diagnostics))
        self.assertEqual(["Direct bibliography ID 'unknown' is not present in titles.xml."], diagnostics)

    def test_duplicate_bibliography_ids_are_rejected(self) -> None:
        """
        Checks duplicate titles.xml IDs fail before any document can be posted.
        """
        root = etree.Element('{http://www.tei-c.org/ns/1.0}listBibl')
        for _ in range(2):
            bibliography_element = etree.SubElement(root, '{http://www.tei-c.org/ns/1.0}bibl')
            bibliography_element.set('{http://www.w3.org/XML/1998/namespace}id', 'same')
        titles_xml = etree.ElementTree(root)
        with self.assertRaisesRegex(bibliography.BibliographyValidationError, 'duplicate bibliography IDs'):
            bibliography.build_bibliography_graph(titles_xml)

    def test_direct_bibliography_normalization_omits_empty_external_and_malformed_refs(self) -> None:
        """
        Checks only valid local inscription publication pointers become bib_ids.
        """
        inscription_xml = parse_xml_text(
            """
            <TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader><fileDesc><sourceDesc><listBibl>
              <bibl><ptr target="#local"/></bibl>
              <bibl><ptr target="bare"/></bibl>
              <bibl><ptr target="#"/></bibl>
              <bibl><ptr target="https://example.org/publication"/></bibl>
              <bibl><ptr target="bad#fragment"/></bibl>
              <bibl><ptr/></bibl>
            </listBibl></sourceDesc></fileDesc></teiHeader></TEI>
            """
        )
        direct_ids, diagnostics = bibliography.extract_direct_bibliography_ids(inscription_xml)
        self.assertEqual(['local', 'bare'], direct_ids)
        self.assertEqual(4, len(diagnostics))

    def test_transcription_stylesheet_controls_choice_surplus_names_numbers_and_lines(self) -> None:
        """
        Checks the compiled XSL controls normalized content across multiple edition blocks.
        """
        inscription_xml = parse_xml_text(
            """
            <TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body>
              <div type="edition"><ab>
                <choice><sic>wrong</sic><corr>right</corr></choice>
                <surplus>extra</surplus>
                <name key="Marcus">M.</name><num value="12">XII</num><lb/>line
              </ab></div>
              <div type="edition"><ab>second block</ab></div>
            </body></text></TEI>
            """
        )
        transformer = transcription.load_transformer(FIXTURE_PATH / 'transcription.xsl')
        value = transcription.build_transcription(inscription_xml, transformer)

        for expected_token in ('right', 'wrong', 'extra', 'Marcus', '12', 'line', 'second block'):
            self.assertIn(expected_token, value)

    def test_transcription_ignores_blocks_outside_an_empty_edition(self) -> None:
        """
        Checks translation and commentary blocks do not supply missing edition text.
        """
        inscription_xml = parse_xml_text(
            """
            <TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body>
              <div type="edition"><div type="textpart"><ab> </ab></div></div>
              <div type="translation"><ab>translation excluded</ab></div>
              <div type="commentary"><div><ab>commentary excluded</ab></div></div>
            </body></text></TEI>
            """
        )
        transformer = transcription.load_transformer(FIXTURE_PATH / 'transcription.xsl')

        self.assertEqual('', transcription.build_transcription(inscription_xml, transformer))

    def test_complete_update_uses_one_post_and_no_read_atomic_update_or_commit(self) -> None:
        """
        Checks one inscription crosses the Solr boundary once as a complete XML document.
        """
        handler = Mock(return_value=httpx.Response(200, text='updated'))
        resources = build_resources(self.make_http_client(handler))
        with tempfile.TemporaryDirectory() as temporary_directory:
            public_data_path = pathlib.Path(temporary_directory) / 'public-data'
            inscriptions_path = public_data_path / 'inscriptions'
            inscriptions_path.mkdir(parents=True)
            (inscriptions_path / 'transcribed.xml').write_text(
                (FIXTURE_PATH / 'transcribed.xml').read_text(encoding='utf-8'),
                encoding='utf-8',
            )
            with override_settings(WEBSERVED_DATA_DIR_PATH=public_data_path):
                indexer.update_index_entry('transcribed.xml', resources=resources)

        handler.assert_called_once()
        request: httpx.Request = handler.call_args.args[0]
        self.assertEqual('POST', request.method)
        self.assertEqual('/solr/usep/update', request.url.path)
        update_xml = etree.fromstring(request.content)
        self.assertEqual('500', update_xml.get('commitWithin'))
        self.assertEqual(1, len(update_xml.xpath('./doc')))
        self.assertEqual(['unicode-Δ', 'parent'], update_xml.xpath('./doc/field[@name="bib_ids"]/text()'))
        self.assertEqual([], update_xml.xpath('./doc/field[@name="transcription"]'))
        self.assertEqual(['arma Marcus virumque cano'], update_xml.xpath('./doc/field[@name="text"]/text()'))

    def test_local_contract_failure_sends_zero_requests(self) -> None:
        """
        Checks an ID mismatch fails before the complete-document update.
        """
        handler = Mock(return_value=httpx.Response(200, text='updated'))
        resources = build_resources(self.make_http_client(handler))
        with tempfile.TemporaryDirectory() as temporary_directory:
            public_data_path = pathlib.Path(temporary_directory) / 'public-data'
            inscriptions_path = public_data_path / 'inscriptions'
            inscriptions_path.mkdir(parents=True)
            (inscriptions_path / 'wrong-filename.xml').write_text(
                (FIXTURE_PATH / 'metadata.xml').read_text(encoding='utf-8'),
                encoding='utf-8',
            )
            with (
                override_settings(WEBSERVED_DATA_DIR_PATH=public_data_path),
                self.assertRaisesRegex(indexer.SolrDocumentValidationError, 'does not match'),
            ):
                indexer.update_index_entry('wrong-filename.xml', resources=resources)

        handler.assert_not_called()

    def test_document_validation_rejects_mismatch_and_bad_dates_but_allows_unknown_fields(self) -> None:
        """
        Checks ID, dates, and status/transcription rules without a closed field allowlist.
        """
        document = etree.fromstring(
            b'<doc><field name="id">expected</field><field name="status">metadata</field>'
            b'<field name="condition"></field><field name="new_researcher_field">kept</field>'
            b'<field name="unknown_empty_field"></field></doc>'
        )
        indexer.omit_empty_optional_fields(document)
        indexer.validate_complete_document(document, 'expected', '')
        self.assertEqual([], indexer.field_values(document, 'condition'))
        self.assertEqual(['kept'], indexer.field_values(document, 'new_researcher_field'))
        self.assertEqual([''], indexer.field_values(document, 'unknown_empty_field'))

        mismatched_document = copy.deepcopy(document)
        indexer.replace_field_values(mismatched_document, 'id', ['other'])
        with self.assertRaisesRegex(indexer.SolrDocumentValidationError, 'does not match'):
            indexer.validate_complete_document(mismatched_document, 'expected', '')

        bad_date_document = copy.deepcopy(document)
        indexer.append_field_value(bad_date_document, 'notBefore', '200')
        indexer.append_field_value(bad_date_document, 'notAfter', '100')
        with self.assertRaisesRegex(indexer.SolrDocumentValidationError, 'notBefore'):
            indexer.validate_complete_document(bad_date_document, 'expected', '')

    def test_complete_update_posts_normalized_dates(self) -> None:
        """
        Checks posted years have no surrounding whitespace, extra sign, or leading zeroes.
        """
        handler = Mock(return_value=httpx.Response(200, text='updated'))
        resources = build_resources(self.make_http_client(handler))
        cases = [
            (' -0600\t', '-0450 ', ['-600'], ['-450']),
            ('\n+0010 ', ' 0200\n', ['10'], ['200']),
            ('-0000', '0000', ['0'], ['0']),
            (None, ' -0500 ', [], ['-500']),
            (' 0100 ', None, ['100'], []),
            (' \t', '\n ', [], []),
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            public_data_path = pathlib.Path(temporary_directory)
            inscriptions_path = public_data_path / 'inscriptions'
            inscriptions_path.mkdir()
            inscription_path = inscriptions_path / 'metadata.xml'
            inscription_path.write_bytes((FIXTURE_PATH / 'metadata.xml').read_bytes())
            for not_before, not_after, expected_before, expected_after in cases:
                with self.subTest(not_before=not_before, not_after=not_after):
                    handler.reset_mock()
                    base_document = indexer.transform_to_document(
                        indexer.parse_inscription(inscription_path), resources.base_transformer
                    )
                    for field_name, value in (('notBefore', not_before), ('notAfter', not_after)):
                        if value is not None:
                            indexer.append_field_value(base_document, field_name, value)
                    indexer.append_field_value(base_document, 'notBefore_desc', 'original date description')
                    with (
                        override_settings(WEBSERVED_DATA_DIR_PATH=public_data_path),
                        patch.object(indexer, 'transform_to_document', return_value=base_document),
                    ):
                        indexer.update_index_entry('metadata.xml', resources=resources)

                    handler.assert_called_once()
                    request: httpx.Request = handler.call_args.args[0]
                    update_xml = etree.fromstring(request.content)
                    self.assertEqual(expected_before, update_xml.xpath('./doc/field[@name="notBefore"]/text()'))
                    self.assertEqual(expected_after, update_xml.xpath('./doc/field[@name="notAfter"]/text()'))
                    self.assertEqual(
                        ['original date description'], update_xml.xpath('./doc/field[@name="notBefore_desc"]/text()')
                    )

    def test_date_normalization_still_rejects_invalid_years_and_reversed_ranges(self) -> None:
        """
        Checks invalid dates fail during document construction before any Solr request.
        """
        handler = Mock(return_value=httpx.Response(200, text='updated'))
        resources = build_resources(self.make_http_client(handler))
        inscription_path = FIXTURE_PATH / 'metadata.xml'
        cases = [
            ('unknown', '200', 'integer year'),
            ('100', '200.5', 'integer year'),
            (' -0450 ', ' -0600 ', 'notBefore cannot be greater'),
            (' 0200 ', ' 0100 ', 'notBefore cannot be greater'),
        ]
        for not_before, not_after, expected_error in cases:
            with self.subTest(not_before=not_before, not_after=not_after):
                base_document = indexer.transform_to_document(
                    indexer.parse_inscription(inscription_path), resources.base_transformer
                )
                indexer.append_field_value(base_document, 'notBefore', not_before)
                indexer.append_field_value(base_document, 'notAfter', not_after)
                with (
                    patch.object(indexer, 'transform_to_document', return_value=base_document),
                    self.assertRaisesRegex(indexer.SolrDocumentValidationError, expected_error),
                ):
                    indexer.build_complete_document(inscription_path, resources)
        handler.assert_not_called()

    def test_solr_rejection_propagates(self) -> None:
        """
        Checks a rejected complete update reaches the queue retry boundary.
        """
        handler = Mock(return_value=httpx.Response(503, text='unavailable'))
        resources = build_resources(self.make_http_client(handler))
        with tempfile.TemporaryDirectory() as temporary_directory:
            public_data_path = pathlib.Path(temporary_directory) / 'public-data'
            inscriptions_path = public_data_path / 'inscriptions'
            inscriptions_path.mkdir(parents=True)
            (inscriptions_path / 'transcribed.xml').write_text(
                (FIXTURE_PATH / 'transcribed.xml').read_text(encoding='utf-8'),
                encoding='utf-8',
            )
            with (
                override_settings(WEBSERVED_DATA_DIR_PATH=public_data_path),
                self.assertRaises(httpx.HTTPStatusError),
            ):
                indexer.update_index_entry('transcribed.xml', resources=resources)
        handler.assert_called_once()

    def test_batch_posting_and_deletion_have_bounded_request_counts(self) -> None:
        """
        Checks full-rebuild request counts scale with batches and reuse one client.
        """
        handler = Mock(return_value=httpx.Response(200, text='ok'))
        resources = build_resources(self.make_http_client(handler), batch_size=2)
        source_document = etree.fromstring(b'<doc><field name="id">one</field><field name="status">metadata</field></doc>')
        documents = [copy.deepcopy(source_document) for _ in range(5)]

        document_batch_count = indexer.post_document_batches(documents, resources)
        deletion_batch_count = indexer.delete_id_batches(['old-1', 'old-2', 'old-3'], resources)

        self.assertEqual(3, document_batch_count)
        self.assertEqual(2, deletion_batch_count)
        self.assertEqual(5, resources.solr.request_count)
        self.assertEqual(5, handler.call_count)

    def test_dependency_discovery_finds_transitive_imports_and_display_only_changes(self) -> None:
        """
        Checks new indexing modules trigger rebuilds while unrelated display XSL does not.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            public_data_path = pathlib.Path(temporary_directory) / 'public-data'
            xsl_path = public_data_path / 'resources' / 'xsl'
            xsl_path.mkdir(parents=True)
            main_path = xsl_path / 'main.xsl'
            module_path = xsl_path / 'module.xsl'
            transcription_path = xsl_path / 'transcription.xsl'
            main_path.write_text(
                '<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">'
                '<xsl:include href="module.xsl"/></xsl:stylesheet>',
                encoding='utf-8',
            )
            module_path.write_text(
                '<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"/>',
                encoding='utf-8',
            )
            transcription_path.write_text(
                '<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"/>',
                encoding='utf-8',
            )
            with override_settings(
                WEBSERVED_DATA_DIR_PATH=public_data_path,
                SOLR_XSL_PATH=main_path,
                TRANSCRIPTION_PARSER_XSL_PATH=transcription_path,
            ):
                self.assertTrue(processor.index_affecting_resources_changed(['resources/xsl/module.xsl']))
                self.assertFalse(processor.index_affecting_resources_changed(['resources/xsl/display-only.xsl']))
                self.assertTrue(processor.index_affecting_resources_changed(['resources/titles.xml']))

            module_path.unlink()
            with override_settings(
                WEBSERVED_DATA_DIR_PATH=public_data_path,
                SOLR_XSL_PATH=main_path,
                TRANSCRIPTION_PARSER_XSL_PATH=transcription_path,
            ):
                self.assertTrue(processor.index_affecting_resources_changed(['resources/xsl/module.xsl']))

    def test_dependency_discovery_rejects_nonlocal_imports(self) -> None:
        """
        Checks uncertain network dependencies activate the conservative fallback.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            stylesheet_path = pathlib.Path(temporary_directory) / 'main.xsl'
            stylesheet_path.write_text(
                '<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">'
                '<xsl:import href="https://example.org/module.xsl"/></xsl:stylesheet>',
                encoding='utf-8',
            )
            with self.assertRaises(stylesheet_dependencies.StylesheetDependencyError):
                stylesheet_dependencies.discover_stylesheet_dependencies([stylesheet_path])

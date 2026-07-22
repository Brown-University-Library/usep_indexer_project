"""
Builds complete USEP Solr documents locally and coordinates index mutations.

The configured researcher-owned base XSL remains the general field authority. This module preserves
its complete ``doc`` output, narrowly replaces cross-resource bibliography and transcription fields,
validates the public webapp's minimum contract, and only then sends an update to Solr.
"""

import copy
import logging
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx
from django.conf import settings
from lxml import etree
from usep_indexer_app.lib import bibliography, solr_client, transcription


log = logging.getLogger(__name__)
INDEXED_SOURCE_DIRECTORIES = {'bib_only', 'metadata_only', 'transcribed'}
REQUIRED_SINGLE_FIELDS = {'id', 'status'}
OPTIONAL_SINGLE_FIELDS = {
    'fake',
    'graphic_name',
    'msid_idno',
    'msid_institution',
    'msid_region',
    'msid_repository',
    'msid_settlement',
    'notAfter',
    'notBefore',
    'text_genre_desc',
    'transcription',
}
VALID_STATUSES = {'bib_only', 'metadata', 'transcription'}
FULL_TEXT_FIELD = 'text'
OPTIONAL_OMIT_IF_EMPTY_FIELDS = {
    'bib_ids',
    'char',
    'condition',
    'condition_desc',
    'decoration',
    'decoration_desc',
    'fake',
    'graphic_name',
    'language',
    'material',
    'material_desc',
    'msid_idno',
    'msid_institution',
    'msid_region',
    'msid_repository',
    'msid_settlement',
    'name',
    'notAfter',
    'notBefore',
    'notBefore_desc',
    'object_type',
    'text',
    'text_desc',
    'text_genre',
    'text_genre_desc',
    'title',
    'transcription',
    'writing',
}


class SolrDocumentValidationError(ValueError):
    """
    Identifies a locally built document that violates the minimum public-webapp contract.
    """


@dataclass
class IndexingResources:
    """
    Owns compiled transformations, bibliography data, HTTP state, and run options.
    """

    base_transformer: etree.XSLT
    transcription_transformer: etree.XSLT
    bibliography_graph: bibliography.BibliographyGraph
    solr: solr_client.SolrClient
    batch_size: int
    data_revision: str = 'unavailable'

    @classmethod
    def load(
        cls,
        *,
        data_revision: str = 'unavailable',
        http_client: httpx.Client | None = None,
    ) -> 'IndexingResources':
        """
        Loads all configured resources once after public data preparation.

        Called by: update_index_entry(), update_index(), reindex workflows
        """
        batch_size = int(getattr(settings, 'SOLR_INDEX_BATCH_SIZE', 100))
        if batch_size <= 0:
            raise ValueError('SOLR_INDEX_BATCH_SIZE must be greater than zero.')
        commit_within_setting = getattr(settings, 'SOLR_COMMIT_WITHIN_MS', 500)
        commit_within_ms = int(commit_within_setting) if commit_within_setting is not None else None
        timeout = float(getattr(settings, 'SOLR_TIMEOUT_SECONDS', solr_client.DEFAULT_TIMEOUT))
        try:
            base_transformer = load_transformer(settings.SOLR_XSL_PATH)
            transcription_transformer = transcription.load_transformer(settings.TRANSCRIPTION_PARSER_XSL_PATH)
            bibliography_graph = bibliography.load_bibliography_graph(settings.TITLES_XML_PATH)
        except Exception:
            log.error(f'Unable to load indexing resources; data_revision, ``{data_revision}``')
            raise
        for diagnostic in bibliography_graph.diagnostics:
            log.warning(f'Bibliography graph diagnostic; data_revision, ``{data_revision}``; diagnostic, ``{diagnostic}``')
        client = solr_client.SolrClient(
            settings.SOLR_URL,
            http_client=http_client,
            timeout=timeout,
            commit_within_ms=commit_within_ms,
        )
        resources = cls(
            base_transformer=base_transformer,
            transcription_transformer=transcription_transformer,
            bibliography_graph=bibliography_graph,
            solr=client,
            batch_size=batch_size,
            data_revision=data_revision,
        )
        log.info(
            f'Indexing resources loaded; batch_size, ``{batch_size}``; commit_within_ms, ``{commit_within_ms}``; '
            f'timeout_seconds, ``{timeout}``; data_revision, ``{data_revision}``'
        )
        return resources

    def __enter__(self) -> 'IndexingResources':
        """
        Returns the run-scoped resources for a context.

        Called by: indexing and reindex workflows
        """
        return self

    def __exit__(self, *exception_details: object) -> None:
        """
        Closes the run-scoped Solr client.

        Called by: context manager protocol
        """
        del exception_details
        self.close()
        return

    def close(self) -> None:
        """
        Releases the persistent HTTP client.

        Called by: __exit__()
        """
        self.solr.close()
        return


def load_transformer(xsl_path: Path) -> etree.XSLT:
    """
    Parses and compiles a configured XSL stylesheet with network access disabled.

    Called by: IndexingResources.load(), build_solr_document()
    """
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    xsl_document = etree.parse(xsl_path, parser=parser)
    access_control = etree.XSLTAccessControl(read_network=False, write_file=False, write_network=False)
    transformer = etree.XSLT(xsl_document, access_control=access_control)
    return transformer


def parse_inscription(inscription_path: Path) -> etree._ElementTree:
    """
    Parses one inscription with external entities and network access disabled.

    Called by: build_complete_document(), build_solr_document()
    """
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    inscription_xml = etree.parse(inscription_path, parser=parser)
    return inscription_xml


def transform_to_document(inscription_xml: etree._ElementTree, transformer: etree.XSLT) -> etree._Element:
    """
    Applies the base XSL and returns a detached, lossless Solr doc element.

    Called by: build_complete_document(), build_solr_document()
    """
    transformed_xml = transformer(inscription_xml)
    root_element = transformed_xml.getroot()
    document_elements: list[etree._Element] = []
    if root_element is not None and root_element.tag == 'doc':
        document_elements = [root_element]
    elif root_element is not None:
        document_elements = root_element.xpath('./doc')
    if len(document_elements) != 1:
        raise SolrDocumentValidationError(
            f'The configured base stylesheet must emit exactly one Solr doc element; found {len(document_elements)}.'
        )
    document = copy.deepcopy(document_elements[0])
    return document


def build_complete_document(inscription_path: Path, resources: IndexingResources) -> etree._Element:
    """
    Builds and validates one complete Solr document without contacting Solr.

    Called by: build_complete_documents(), update_index_entry()
    """
    build_started = time.monotonic()
    inscription_xml = parse_inscription(inscription_path)
    document = transform_to_document(inscription_xml, resources.base_transformer)
    validate_document_id(document, inscription_path.stem)

    direct_bibliography_ids, extraction_diagnostics = bibliography.extract_direct_bibliography_ids(inscription_xml)
    complete_bibliography_ids, resolution_diagnostics = bibliography.resolve_bibliography_ids(
        direct_bibliography_ids,
        resources.bibliography_graph,
    )
    for diagnostic in extraction_diagnostics + resolution_diagnostics:
        log.warning(
            f'Bibliography document diagnostic; inscription_id, ``{inscription_path.stem}``; '
            f'data_revision, ``{resources.data_revision}``; diagnostic, ``{diagnostic}``'
        )
    replace_field_values(document, 'bib_ids', complete_bibliography_ids)

    transcription_value = transcription.build_transcription(inscription_xml, resources.transcription_transformer)
    replace_field_values(document, 'transcription', [transcription_value] if transcription_value else [])
    if transcription_value:
        append_field_value(document, FULL_TEXT_FIELD, transcription_value, deduplicate=True)
    omit_empty_optional_fields(document)
    validate_complete_document(document, inscription_path.stem, transcription_value)
    elapsed_seconds = time.monotonic() - build_started
    log.debug(
        f'Complete Solr document built; inscription_id, ``{inscription_path.stem}``; '
        f'elapsed_seconds, ``{elapsed_seconds:.3f}``; data_revision, ``{resources.data_revision}``'
    )
    return document


def build_complete_documents(inscription_paths: Sequence[Path], resources: IndexingResources) -> list[etree._Element]:
    """
    Builds every supplied document locally before any caller mutates Solr.

    Called by: update_index(), reindex.process_prepared_full_reindex()
    """
    documents = [build_complete_document(inscription_path, resources) for inscription_path in inscription_paths]
    return documents


def build_solr_document(inscription_path: Path, xsl_path: Path) -> str:
    """
    Applies a configured XSL and serializes its unmodified Solr doc for diagnostics.

    Called by: focused transformation tests
    """
    inscription_xml = parse_inscription(inscription_path)
    transformer = load_transformer(xsl_path)
    document = transform_to_document(inscription_xml, transformer)
    document_bytes = etree.tostring(document, pretty_print=True, encoding='utf-8')
    return document_bytes.decode('utf-8')


def field_values(document: etree._Element, field_name: str) -> list[str]:
    """
    Returns direct field values in stylesheet order.

    Called by: document assembly, validation, and tests
    """
    values = [field_element.text or '' for field_element in document.xpath('./field[@name=$name]', name=field_name)]
    return values


def replace_field_values(document: etree._Element, field_name: str, values: Sequence[str]) -> None:
    """
    Narrowly replaces one named field while retaining all other field order and multiplicity.

    Called by: build_complete_document(), document-helper tests
    """
    matching_fields: list[etree._Element] = document.xpath('./field[@name=$name]', name=field_name)
    insertion_index = len(document)
    if matching_fields:
        insertion_index = document.index(matching_fields[0])
    for field_element in matching_fields:
        document.remove(field_element)
    for value in values:
        if not value:
            raise SolrDocumentValidationError(f'Cannot add an empty {field_name!r} field value.')
        field_element = etree.Element('field', name=field_name)
        field_element.text = value
        document.insert(insertion_index, field_element)
        insertion_index += 1
    return


def append_field_value(
    document: etree._Element,
    field_name: str,
    value: str,
    *,
    deduplicate: bool = False,
) -> None:
    """
    Appends one derived field without replacing researcher-owned values.

    Called by: build_complete_document()
    """
    if not value:
        raise SolrDocumentValidationError(f'Cannot append an empty {field_name!r} field value.')
    if not deduplicate or value not in field_values(document, field_name):
        field_element = etree.SubElement(document, 'field', name=field_name)
        field_element.text = value
    return


def omit_empty_optional_fields(document: etree._Element) -> None:
    """
    Omits empty values for documented optional fields without filtering unknown stylesheet fields.

    Called by: build_complete_document(), document-helper tests
    """
    field_elements: list[etree._Element] = document.xpath('./field')
    for field_element in field_elements:
        field_name = field_element.get('name') or ''
        is_dynamic_contract_field = field_name.startswith('c_') or field_name.startswith('name_')
        if (field_name in OPTIONAL_OMIT_IF_EMPTY_FIELDS or is_dynamic_contract_field) and not (
            field_element.text or ''
        ).strip():
            document.remove(field_element)
    return


def validate_document_id(document: etree._Element, expected_id: str) -> None:
    """
    Requires one nonempty ID equal to the flattened filename stem.

    Called by: build_complete_document(), validate_complete_document()
    """
    id_values = field_values(document, 'id')
    if len(id_values) != 1 or not id_values[0].strip():
        raise SolrDocumentValidationError('A complete Solr document must contain exactly one nonempty id field.')
    if id_values[0] != expected_id:
        raise SolrDocumentValidationError(
            f'Solr document id {id_values[0]!r} does not match inscription filename stem {expected_id!r}.'
        )
    return


def validate_complete_document(document: etree._Element, expected_id: str, transcription_value: str) -> None:
    """
    Validates the minimum active webapp contract while allowing additional fields.

    Called by: build_complete_document(), document-contract tests
    """
    validate_document_id(document, expected_id)
    field_elements: list[etree._Element] = document.xpath('./field')
    for field_element in field_elements:
        field_name = field_element.get('name')
        if not field_name:
            raise SolrDocumentValidationError('Every Solr field element must have a nonempty name attribute.')
        is_dynamic_contract_field = field_name.startswith('c_') or field_name.startswith('name_')
        if (
            field_name in OPTIONAL_OMIT_IF_EMPTY_FIELDS or is_dynamic_contract_field
        ) and not (field_element.text or '').strip():
            raise SolrDocumentValidationError(f'Solr field {field_name!r} cannot have an empty value.')

    for field_name in REQUIRED_SINGLE_FIELDS:
        values = field_values(document, field_name)
        if len(values) != 1:
            raise SolrDocumentValidationError(f'Solr field {field_name!r} must occur exactly once.')
    for field_name in OPTIONAL_SINGLE_FIELDS:
        if len(field_values(document, field_name)) > 1:
            raise SolrDocumentValidationError(f'Solr field {field_name!r} cannot occur more than once.')

    status = field_values(document, 'status')[0]
    if status not in VALID_STATUSES:
        raise SolrDocumentValidationError(f'Unsupported status value {status!r}.')
    if (status == 'transcription') != bool(transcription_value):
        raise SolrDocumentValidationError(
            f'Status/transcription mismatch for {expected_id!r}: status is {status!r} and '
            f'transcription_present is {bool(transcription_value)!r}.'
        )
    if transcription_value and transcription_value not in field_values(document, FULL_TEXT_FIELD):
        raise SolrDocumentValidationError('Normalized transcription must also be present in the full-text field.')

    validate_dates(document)
    bibliography_values = field_values(document, 'bib_ids')
    if len(bibliography_values) != len(set(bibliography_values)):
        raise SolrDocumentValidationError('The bib_ids field cannot contain duplicate values.')
    return


def validate_dates(document: etree._Element) -> None:
    """
    Requires numeric date bounds and a nondecreasing range when both are present.

    Called by: validate_complete_document()
    """
    date_values: dict[str, int] = {}
    for field_name in ('notBefore', 'notAfter'):
        values = field_values(document, field_name)
        if values:
            try:
                date_values[field_name] = int(values[0])
            except ValueError as error:
                raise SolrDocumentValidationError(f'Solr field {field_name!r} must contain an integer year.') from error
    if 'notBefore' in date_values and 'notAfter' in date_values and date_values['notBefore'] > date_values['notAfter']:
        raise SolrDocumentValidationError('notBefore cannot be greater than notAfter.')
    return


def update_index_entry(
    filename: str,
    *,
    resources: IndexingResources | None = None,
    data_revision: str = 'unavailable',
) -> None:
    """
    Builds and posts one complete inscription in exactly one update request.

    Called by: reindex.process_single_reindex(), complete-update tests
    """
    if resources is None:
        with IndexingResources.load(data_revision=data_revision) as loaded_resources:
            post_one_index_entry(filename, loaded_resources)
    else:
        post_one_index_entry(filename, resources)
    return


def post_one_index_entry(filename: str, resources: IndexingResources) -> None:
    """
    Posts one already resource-scoped complete inscription update.

    Called by: update_index_entry()
    """
    inscription_path = settings.WEBSERVED_DATA_DIR_PATH / 'inscriptions' / filename
    document = build_complete_document(inscription_path, resources)
    post_started = time.monotonic()
    try:
        resources.solr.post_documents([document])
    except Exception:
        log.error(
            f'Solr complete-document update failed; filename, ``{filename}``; '
            f'data_revision, ``{resources.data_revision}``'
        )
        raise
    elapsed_seconds = time.monotonic() - post_started
    log.info(
        f'Solr complete-document update completed; inscription_id, ``{inscription_path.stem}``; '
        f'document_count, ``1``; elapsed_seconds, ``{elapsed_seconds:.3f}``; request_count, ``1``'
    )
    return


def should_index_path(file_path: str) -> bool:
    """
    Checks whether a GitHub path belongs to an indexed inscription directory.

    Called by: affected_filenames(), update_index()
    """
    path_parts = set(Path(file_path).parts)
    is_index_path = bool(path_parts & INDEXED_SOURCE_DIRECTORIES) and Path(file_path).suffix == '.xml'
    return is_index_path


def affected_filenames(files_updated: Sequence[str], files_removed: Sequence[str]) -> list[str]:
    """
    Coalesces changed source paths into stable flattened inscription filenames.

    Called by: update_index(), incremental tests
    """
    filenames: list[str] = []
    for file_path in [*files_updated, *files_removed]:
        if should_index_path(file_path):
            filename = Path(file_path).name
            if filename not in filenames:
                filenames.append(filename)
    return filenames


def update_index(
    files_updated: list[str],
    files_removed: list[str],
    *,
    data_revision: str = 'unavailable',
) -> None:
    """
    Applies coalesced incremental complete-document changes through one shared client.

    Called by: processor.process_incremental()
    """
    filenames = affected_filenames(files_updated, files_removed)
    ignored_path_count = len(files_updated) + len(files_removed) - sum(
        should_index_path(file_path) for file_path in [*files_updated, *files_removed]
    )
    log.debug(
        f'Filtered incremental Solr changes; affected_filename_count, ``{len(filenames)}``; '
        f'ignored_path_count, ``{ignored_path_count}``'
    )
    if not filenames:
        return

    inscriptions_path = settings.WEBSERVED_DATA_DIR_PATH / 'inscriptions'
    paths_to_update = [inscriptions_path / filename for filename in filenames if (inscriptions_path / filename).is_file()]
    ids_to_remove = [Path(filename).stem for filename in filenames if not (inscriptions_path / filename).is_file()]
    with IndexingResources.load(data_revision=data_revision) as resources:
        documents = build_complete_documents(paths_to_update, resources)
        for document in documents:
            inscription_id = field_values(document, 'id')[0]
            post_started = time.monotonic()
            try:
                resources.solr.post_documents([document])
            except Exception:
                log.error(
                    f'Incremental complete-document update failed; inscription_id, ``{inscription_id}``; '
                    f'data_revision, ``{data_revision}``'
                )
                raise
            elapsed_seconds = time.monotonic() - post_started
            log.info(
                f'Incremental complete-document update completed; inscription_id, ``{inscription_id}``; '
                f'elapsed_seconds, ``{elapsed_seconds:.3f}``; commit_within_ms, '
                f'``{resources.solr.commit_within_ms}``'
            )
        delete_id_batches(ids_to_remove, resources)
        log.info(
            f'Incremental Solr indexing completed; document_count, ``{len(documents)}``; '
            f'deletion_count, ``{len(ids_to_remove)}``; request_count, ``{resources.solr.request_count}``'
        )
    return


def post_document_batches(documents: Sequence[etree._Element], resources: IndexingResources) -> int:
    """
    Posts complete documents in configured bounded batches.

    Called by: reindex.process_prepared_full_reindex(), batch tests
    """
    batch_count = 0
    for document_batch in iter_batches(documents, resources.batch_size):
        batch_started = time.monotonic()
        first_id = field_values(document_batch[0], 'id')[0]
        last_id = field_values(document_batch[-1], 'id')[0]
        try:
            resources.solr.post_documents(document_batch)
        except Exception:
            log.error(
                f'Solr document batch failed; document_count, ``{len(document_batch)}``; first_id, ``{first_id}``; '
                f'last_id, ``{last_id}``; data_revision, ``{resources.data_revision}``'
            )
            raise
        batch_count += 1
        elapsed_seconds = time.monotonic() - batch_started
        log.info(
            f'Solr document batch completed; batch_number, ``{batch_count}``; '
            f'document_count, ``{len(document_batch)}``; first_id, ``{first_id}``; last_id, ``{last_id}``; '
            f'elapsed_seconds, ``{elapsed_seconds:.3f}``; commit_within_ms, ``{resources.solr.commit_within_ms}``'
        )
    return batch_count


def delete_id_batches(inscription_ids: Sequence[str], resources: IndexingResources) -> int:
    """
    Deletes IDs in configured bounded batches without explicit commits.

    Called by: update_index(), reindex.process_prepared_full_reindex(), batch tests
    """
    batch_count = 0
    for id_batch in iter_batches(inscription_ids, resources.batch_size):
        batch_started = time.monotonic()
        try:
            resources.solr.delete_ids(id_batch)
        except Exception:
            log.error(
                f'Solr deletion batch failed; deletion_count, ``{len(id_batch)}``; first_id, ``{id_batch[0]}``; '
                f'last_id, ``{id_batch[-1]}``; data_revision, ``{resources.data_revision}``'
            )
            raise
        batch_count += 1
        elapsed_seconds = time.monotonic() - batch_started
        log.info(
            f'Solr deletion batch completed; batch_number, ``{batch_count}``; deletion_count, ``{len(id_batch)}``; '
            f'first_id, ``{id_batch[0]}``; last_id, ``{id_batch[-1]}``; '
            f'elapsed_seconds, ``{elapsed_seconds:.3f}``; commit_within_ms, ``{resources.solr.commit_within_ms}``'
        )
    return batch_count


def iter_batches[Value](values: Sequence[Value], batch_size: int) -> Iterator[Sequence[Value]]:
    """
    Yields stable bounded slices from a sequence.

    Called by: post_document_batches(), delete_id_batches()
    """
    if batch_size <= 0:
        raise ValueError('Batch size must be greater than zero.')
    for start_index in range(0, len(values), batch_size):
        yield values[start_index : start_index + batch_size]

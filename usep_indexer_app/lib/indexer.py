"""
Coordinates inscription-level changes to the USEP Solr index.

It transforms web-served TEI XML into Solr documents, applies incremental additions and removals,
and delegates the supplementary bibliography and transcription fields to their specialized modules.
"""

import logging
from pathlib import Path

from django.conf import settings
from lxml import etree
from usep_indexer_app.lib import bibliography, solr_client, transcription


log = logging.getLogger(__name__)
INDEXED_SOURCE_DIRECTORIES = {'bib_only', 'metadata_only', 'transcribed'}


def update_index_entry(filename: str) -> None:
    """
    Transforms and posts one inscription, then updates related fields.

    Called by: update_entry()
    """
    inscription_path = settings.WEBSERVED_DATA_DIR_PATH / 'inscriptions' / filename
    solr_xml = build_solr_document(inscription_path, settings.SOLR_XSL_PATH)
    solr_client.post_xml_update(settings.SOLR_URL, solr_xml)
    inscription_id = filename.removesuffix('.xml')
    update_bibliography(inscription_id)
    update_transcription(inscription_id, inscription_path)
    return


def build_solr_document(inscription_path: Path, xsl_path: Path) -> str:
    """
    Transforms an inscription XML document into a Solr XML update.

    Called by: update_index_entry()
    """
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    inscription_xml = etree.parse(inscription_path, parser=parser)
    xsl_document = etree.parse(xsl_path, parser=parser)
    transformer = etree.XSLT(xsl_document)
    transformed_xml = transformer(inscription_xml)
    transformed_bytes = etree.tostring(transformed_xml, pretty_print=True, encoding='utf-8')
    return transformed_bytes.decode('utf-8')


def update_bibliography(inscription_id: str) -> None:
    """
    Best-effort updates hierarchical bibliography data for an inscription.

    Called by: update_index_entry()
    """
    try:
        bibliography.add_bibliography(settings.SOLR_URL, settings.TITLES_XML_PATH, inscription_id)
    except Exception:
        log.exception('Unable to update bibliography for %s.', inscription_id)
    return


def update_transcription(inscription_id: str, inscription_path: Path) -> None:
    """
    Best-effort updates transcription data for an inscription.

    Called by: update_index_entry()
    """
    try:
        transcription.add_transcription(
            settings.SOLR_URL,
            settings.TRANSCRIPTION_PARSER_XSL_PATH,
            inscription_id,
            inscription_path,
        )
    except Exception:
        log.exception('Unable to update transcription for %s.', inscription_id)
    return


def remove_index_entry(filename: str | None = None, inscription_id: str | None = None) -> None:
    """
    Deletes a Solr entry by filename or explicit inscription ID.

    Called by: remove_entry(), remove_entry_via_id()
    """
    target_id = inscription_id
    if filename is not None:
        target_id = filename.removesuffix('.xml')
    if not target_id:
        raise ValueError('A filename or inscription_id is required.')
    solr_client.delete_id(settings.SOLR_URL, target_id)
    return


def should_index_path(file_path: str) -> bool:
    """
    Checks whether a GitHub path belongs to an indexed inscription directory.

    Called by: update_index()
    """
    path_parts = set(Path(file_path).parts)
    return bool(path_parts & INDEXED_SOURCE_DIRECTORIES)


def update_index(files_updated: list[str], files_removed: list[str]) -> None:
    """
    Applies per-inscription incremental Solr changes synchronously.

    Called by: processor.process_incremental()
    """
    for removed_file_path in files_removed:
        if should_index_path(removed_file_path):
            remove_entry(removed_file_path)
    for updated_file_path in files_updated:
        if should_index_path(updated_file_path):
            update_entry(updated_file_path)
    return


def update_entry(updated_file_path: str) -> None:
    """
    Updates one Solr entry from a source path.

    Called by: update_index(), reindex.update_all_index_entries()
    """
    update_index_entry(Path(updated_file_path).name)
    return


def remove_entry(removed_file_path: str) -> None:
    """
    Removes one Solr entry from a source path.

    Called by: update_index()
    """
    remove_index_entry(filename=Path(removed_file_path).name)
    return


def remove_entry_via_id(id_to_remove: str) -> None:
    """
    Removes one Solr entry from an explicit ID.

    Called by: reindex.update_all_index_entries()
    """
    remove_index_entry(inscription_id=id_to_remove)
    return

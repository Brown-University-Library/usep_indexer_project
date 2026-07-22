"""
Builds normalized searchable transcription without contacting Solr.

The configured researcher-owned stylesheet remains responsible for textual choices. A narrow
compatibility wrapper supplies only edition ``ab`` elements because the current stylesheet expects
its caller to make that selection; a future stylesheet can take over the selection directly.
"""

import copy
from pathlib import Path

from lxml import etree


TEI_NAMESPACE = {'tei': 'http://www.tei-c.org/ns/1.0'}


def load_transformer(xsl_path: Path) -> etree.XSLT:
    """
    Parses and compiles the configured transcription stylesheet.

    Called by: indexer.IndexingResources.load()
    """
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    xsl_document = etree.parse(xsl_path, parser=parser)
    access_control = etree.XSLTAccessControl(read_network=False, write_file=False, write_network=False)
    transformer = etree.XSLT(xsl_document, access_control=access_control)
    return transformer


def build_transcription(inscription_xml: etree._ElementTree, transformer: etree.XSLT) -> str:
    """
    Transforms the parsed inscription's edition blocks into normalized index text.

    Called by: indexer.build_complete_document()
    """
    edition_elements: list[etree._Element] = inscription_xml.xpath(
        "//tei:div[@type='edition']/tei:ab",
        namespaces=TEI_NAMESPACE,
    )
    transcription = ''
    if edition_elements:
        compatibility_document = etree.Element('edition-content')
        for edition_element in edition_elements:
            compatibility_document.append(copy.deepcopy(edition_element))
        transformed_text = str(transformer(compatibility_document))
        transcription = normalize_transcription(transformed_text)
    return transcription


def normalize_transcription(transcription: str) -> str:
    """
    Collapses layout whitespace emitted from source XML while preserving textual tokens.

    Called by: build_transcription()
    """
    normalized_transcription = ' '.join(transcription.split())
    return normalized_transcription

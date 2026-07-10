import re
from pathlib import Path

from lxml import etree
from usep_indexer_app.lib import solr_client


TEI_NAMESPACE = {'tei': 'http://www.tei-c.org/ns/1.0'}
LB_WHITESPACE = re.compile(r'(<lb.*/>)\s+(.*)')


def add_transcription(solr_url: str, xsl_path: Path, inscription_id: str, xml_path: Path) -> bool:
    """
    Builds and posts the transcription field for one inscription.

    Called by: indexer.update_transcription()
    """
    transcription = build_transcription(xml_path, xsl_path)
    update_data = {
        'add': {
            'doc': {
                'id': inscription_id,
                'transcription': {'set': transcription},
            },
        },
    }
    solr_client.post_json_update(solr_url, update_data)
    solr_client.soft_commit(solr_url)
    return True


def build_transcription(xml_path: Path, xsl_path: Path) -> str:
    """
    Extracts edition blocks and transforms them into index text.

    Called by: add_transcription()
    """
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    inscription_xml = etree.parse(xml_path, parser=parser)
    edition_elements = inscription_xml.xpath(
        "//tei:div[@type='edition']/tei:ab",
        namespaces=TEI_NAMESPACE,
    )
    if not edition_elements:
        return ''

    munged_text = munge_edition_elements(edition_elements)
    xsl_document = etree.parse(xsl_path, parser=parser)
    transformer = etree.XSLT(xsl_document)
    transformed_xml = transformer(etree.fromstring(munged_text.encode('utf-8'), parser=parser))
    return str(transformed_xml)


def munge_edition_elements(edition_elements: list[etree._Element]) -> str:
    """
    Joins serialized edition elements while removing post-lb whitespace.

    Called by: build_transcription()
    """
    munged_parts: list[str] = []
    for element in edition_elements:
        content = etree.tostring(element, encoding='unicode')
        for line in content.splitlines():
            stripped_line = line.strip()
            match = LB_WHITESPACE.match(stripped_line)
            if match:
                stripped_line = match.group(1) + match.group(2)
            munged_parts.append(stripped_line)
    return ''.join(munged_parts)

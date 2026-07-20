"""
Enriches indexed inscriptions with their inherited bibliography relationships.

This module bridges the TEI hierarchy in ``titles.xml`` and Solr's atomic-update interface so an
inscription's direct bibliography references also expose their ancestor references.
"""

from pathlib import Path

from lxml import etree
from usep_indexer_app.lib import solr_client


TEI_NAMESPACE = {'tei': 'http://www.tei-c.org/ns/1.0'}


def add_bibliography(solr_url: str, titles_xml_path: Path, inscription_id: str) -> bool:
    """
    Adds ancestor bibliography IDs from titles.xml to a Solr inscription.

    Called by: indexer.update_bibliography(), indexer.update_index_entry()
    """
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    titles_xml = etree.parse(titles_xml_path, parser=parser)

    direct_bib_ids = solr_client.select_bibliography_ids(solr_url, inscription_id)
    ancestor_ids: set[str] = set()
    for bibliography_id in direct_bib_ids:
        xpath = etree.XPath(
            '//tei:bibl[@xml:id=$bibliography_id]/ancestor::tei:bibl/@xml:id',
            namespaces=TEI_NAMESPACE,
        )
        ancestor_ids.update(xpath(titles_xml, bibliography_id=bibliography_id))

    update_data = [{'id': inscription_id, 'bib_ids': {'add': sorted(ancestor_ids)}}]
    solr_client.post_json_update(solr_url, update_data)
    solr_client.soft_commit(solr_url)
    return True

"""
Builds and resolves USEP bibliography relationships without contacting Solr.

The researcher-maintained ``titles.xml`` file expresses child-to-parent relationships through
``title/@ref`` values. This module normalizes those references once per indexing run and expands an
inscription's direct publication references in stable order.
"""

import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from lxml import etree


TEI_NAMESPACE = {'tei': 'http://www.tei-c.org/ns/1.0'}
XML_ID_ATTRIBUTE = '{http://www.w3.org/XML/1998/namespace}id'


class BibliographyValidationError(ValueError):
    """
    Identifies bibliography data that cannot produce an unambiguous relationship graph.
    """


@dataclass(frozen=True)
class BibliographyGraph:
    """
    Holds valid local IDs, their ordered parent relationships, and non-fatal diagnostics.
    """

    valid_ids: frozenset[str]
    parents_by_child: dict[str, tuple[str, ...]]
    diagnostics: tuple[str, ...]


def load_bibliography_graph(titles_xml_path: Path) -> BibliographyGraph:
    """
    Parses a titles.xml file and builds its bibliography graph.

    Called by: indexer.IndexingResources.load()
    """
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    titles_xml = etree.parse(titles_xml_path, parser=parser)
    graph = build_bibliography_graph(titles_xml)
    return graph


def build_bibliography_graph(titles_xml: etree._ElementTree) -> BibliographyGraph:
    """
    Builds ordered child-to-parent relationships from a parsed titles.xml tree.

    Called by: load_bibliography_graph(), bibliography tests
    """
    bibliography_elements: list[etree._Element] = titles_xml.xpath('//tei:bibl', namespaces=TEI_NAMESPACE)
    bibliography_ids: list[str] = []
    seen_ids: set[str] = set()
    duplicate_ids: set[str] = set()
    for bibliography_element in bibliography_elements:
        bibliography_id = (bibliography_element.get(XML_ID_ATTRIBUTE) or '').strip()
        if not bibliography_id:
            continue
        if bibliography_id in seen_ids:
            duplicate_ids.add(bibliography_id)
        bibliography_ids.append(bibliography_id)
        seen_ids.add(bibliography_id)
    if duplicate_ids:
        duplicate_text = ', '.join(sorted(duplicate_ids))
        raise BibliographyValidationError(f'titles.xml contains duplicate bibliography IDs: {duplicate_text}')

    valid_ids = frozenset(bibliography_ids)
    parents_by_child: dict[str, tuple[str, ...]] = {}
    diagnostics: list[str] = []
    for bibliography_element in bibliography_elements:
        child_id = (bibliography_element.get(XML_ID_ATTRIBUTE) or '').strip()
        if not child_id:
            diagnostics.append('titles.xml contains a bibliography entry without xml:id.')
            continue
        parent_ids: list[str] = []
        raw_references: list[str] = bibliography_element.xpath('.//tei:title/@ref', namespaces=TEI_NAMESPACE)
        for raw_reference in raw_references:
            parent_id, diagnostic = normalize_local_reference(raw_reference)
            if diagnostic:
                diagnostics.append(f'titles.xml bibliography {child_id!r}: {diagnostic}')
            if parent_id and parent_id in valid_ids and parent_id not in parent_ids:
                parent_ids.append(parent_id)
            elif parent_id and parent_id not in valid_ids:
                diagnostics.append(
                    f'titles.xml bibliography {child_id!r} refers to unresolved local parent {parent_id!r}.'
                )
        parents_by_child[child_id] = tuple(parent_ids)

    diagnostics.extend(find_cycle_diagnostics(parents_by_child))
    graph = BibliographyGraph(
        valid_ids=valid_ids,
        parents_by_child=parents_by_child,
        diagnostics=tuple(stable_unique(diagnostics)),
    )
    return graph


def extract_direct_bibliography_ids(inscription_xml: etree._ElementTree) -> tuple[list[str], list[str]]:
    """
    Extracts normalized local publication pointers from a parsed inscription.

    Called by: indexer.build_complete_document()
    """
    direct_ids: list[str] = []
    diagnostics: list[str] = []
    pointer_elements: list[etree._Element] = inscription_xml.xpath(
        '//tei:listBibl/tei:bibl/tei:ptr',
        namespaces=TEI_NAMESPACE,
    )
    for pointer_element in pointer_elements:
        raw_reference = pointer_element.get('target')
        if raw_reference is None:
            diagnostics.append('Inscription bibliography pointer is missing @target.')
            continue
        bibliography_id, diagnostic = normalize_local_reference(raw_reference)
        if diagnostic:
            diagnostics.append(f'Inscription bibliography pointer {raw_reference!r}: {diagnostic}')
        if bibliography_id and bibliography_id not in direct_ids:
            direct_ids.append(bibliography_id)
    return direct_ids, diagnostics


def normalize_local_reference(raw_reference: str) -> tuple[str | None, str | None]:
    """
    Normalizes one fragment or bare local ID and explains rejected references.

    Called by: build_bibliography_graph(), extract_direct_bibliography_ids()
    """
    reference = raw_reference.strip()
    normalized_reference: str | None = None
    diagnostic: str | None = None
    if not reference:
        diagnostic = 'reference is empty.'
    else:
        parsed_reference = urlsplit(reference)
        if parsed_reference.scheme or parsed_reference.netloc or reference.startswith('//'):
            diagnostic = 'reference is nonlocal and was ignored.'
        else:
            candidate = reference[1:] if reference.startswith('#') else reference
            if not candidate:
                diagnostic = 'reference contains no local ID.'
            elif not is_valid_local_id(candidate):
                diagnostic = 'reference is malformed and was ignored.'
            else:
                normalized_reference = candidate
    return normalized_reference, diagnostic


def is_valid_local_id(candidate: str) -> bool:
    """
    Checks the XML-ID-compatible character shape used by local bibliography IDs.

    Called by: normalize_local_reference()
    """
    is_valid = bool(candidate) and (candidate[0] == '_' or candidate[0].isalpha())
    if is_valid:
        for character in candidate[1:]:
            category = unicodedata.category(character)
            if not (character.isalnum() or character in {'_', '-', '.'} or category[0] in {'L', 'M', 'N'}):
                is_valid = False
                break
    return is_valid


def resolve_bibliography_ids(
    direct_ids: list[str],
    graph: BibliographyGraph,
) -> tuple[list[str], list[str]]:
    """
    Expands direct IDs through all known parents with stable deduplication.

    Called by: indexer.build_complete_document(), bibliography tests
    """
    resolved_ids: list[str] = []
    diagnostics: list[str] = []
    for direct_id in direct_ids:
        if direct_id not in resolved_ids:
            resolved_ids.append(direct_id)
        if direct_id not in graph.valid_ids:
            diagnostics.append(f'Direct bibliography ID {direct_id!r} is not present in titles.xml.')
            continue
        pending_ids: list[str] = list(reversed(graph.parents_by_child.get(direct_id, ())))
        while pending_ids:
            parent_id = pending_ids.pop()
            if parent_id in resolved_ids:
                continue
            resolved_ids.append(parent_id)
            parent_parents = graph.parents_by_child.get(parent_id, ())
            pending_ids.extend(reversed(parent_parents))
    return resolved_ids, diagnostics


def find_cycle_diagnostics(parents_by_child: dict[str, tuple[str, ...]]) -> list[str]:
    """
    Reports relationship cycles while leaving traversal safe and deterministic.

    Called by: build_bibliography_graph()
    """
    diagnostics: list[str] = []
    for start_id in parents_by_child:
        pending_paths: list[tuple[str, tuple[str, ...]]] = [(start_id, (start_id,))]
        while pending_paths:
            current_id, path = pending_paths.pop()
            for parent_id in parents_by_child.get(current_id, ()):
                if parent_id in path:
                    cycle_start = path.index(parent_id)
                    cycle = path[cycle_start:] + (parent_id,)
                    diagnostics.append(f'titles.xml bibliography cycle detected: {" -> ".join(cycle)}.')
                else:
                    pending_paths.append((parent_id, path + (parent_id,)))
    return stable_unique(diagnostics)


def stable_unique(values: list[str]) -> list[str]:
    """
    Deduplicates strings without changing their first-seen order.

    Called by: build_bibliography_graph(), find_cycle_diagnostics()
    """
    unique_values: list[str] = []
    for value in values:
        if value not in unique_values:
            unique_values.append(value)
    return unique_values

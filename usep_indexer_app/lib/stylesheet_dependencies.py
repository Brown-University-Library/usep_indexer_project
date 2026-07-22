"""
Discovers local XSL import and include dependencies for rebuild classification.

Dependency discovery is intentionally strict. If a configured indexing stylesheet cannot be fully
resolved, the processor conservatively treats stylesheet changes as index-affecting.
"""

from pathlib import Path
from urllib.parse import urlsplit

from lxml import etree


XSL_NAMESPACE = {'xsl': 'http://www.w3.org/1999/XSL/Transform'}


class StylesheetDependencyError(ValueError):
    """
    Identifies an indexing stylesheet dependency graph that cannot be resolved safely.
    """


def discover_stylesheet_dependencies(stylesheet_paths: list[Path]) -> set[Path]:
    """
    Returns configured stylesheets and every transitive local import/include.

    Called by: processor.index_affecting_resources_changed(), dependency tests
    """
    pending_paths = [path.resolve() for path in stylesheet_paths]
    discovered_paths: set[Path] = set()
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    while pending_paths:
        stylesheet_path = pending_paths.pop()
        if stylesheet_path in discovered_paths:
            continue
        if not stylesheet_path.is_file():
            raise StylesheetDependencyError(f'Stylesheet dependency does not exist: {stylesheet_path}')
        try:
            stylesheet_xml = etree.parse(stylesheet_path, parser=parser)
        except (OSError, etree.XMLSyntaxError) as error:
            raise StylesheetDependencyError(f'Unable to parse stylesheet dependency {stylesheet_path}: {error}') from error
        discovered_paths.add(stylesheet_path)
        href_values: list[str] = stylesheet_xml.xpath(
            '//xsl:import/@href | //xsl:include/@href',
            namespaces=XSL_NAMESPACE,
        )
        for href_value in href_values:
            dependency_path = resolve_local_dependency(stylesheet_path, href_value)
            if dependency_path not in discovered_paths:
                pending_paths.append(dependency_path)
    return discovered_paths


def resolve_local_dependency(stylesheet_path: Path, href_value: str) -> Path:
    """
    Resolves one filesystem-relative XSL dependency and rejects nonlocal references.

    Called by: discover_stylesheet_dependencies()
    """
    parsed_href = urlsplit(href_value)
    if not href_value.strip() or parsed_href.scheme or parsed_href.netloc or parsed_href.query or parsed_href.fragment:
        raise StylesheetDependencyError(
            f'Stylesheet {stylesheet_path} has a nonlocal or malformed import/include href: {href_value!r}'
        )
    dependency_path = (stylesheet_path.parent / parsed_href.path).resolve()
    return dependency_path


def relative_dependency_paths(dependency_paths: set[Path], public_data_path: Path) -> set[str]:
    """
    Converts discovered paths to normalized webhook-style paths under public data.

    Called by: processor.index_affecting_resources_changed()
    """
    public_data_path = public_data_path.resolve()
    relative_paths: set[str] = set()
    for dependency_path in dependency_paths:
        try:
            relative_path = dependency_path.relative_to(public_data_path)
        except ValueError as error:
            raise StylesheetDependencyError(
                f'Configured indexing stylesheet is outside the copied public data tree: {dependency_path}'
            ) from error
        relative_paths.add(relative_path.as_posix())
    return relative_paths

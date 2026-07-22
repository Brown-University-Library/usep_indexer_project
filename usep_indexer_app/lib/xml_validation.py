"""
Loads local or remote XML and checks whether it is well-formed.
"""

import dataclasses
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from lxml import etree


HTTP_TIMEOUT_SECONDS = 30.0


@dataclasses.dataclass(frozen=True)
class XMLValidationFailure:
    """
    Describes one XML file that could not be validated successfully.
    """

    path: Path
    error: str


@dataclasses.dataclass(frozen=True)
class XMLDirectoryValidationResult:
    """
    Summarizes validation of an XML directory tree.
    """

    checked_count: int
    well_formed_count: int
    failures: list[XMLValidationFailure]


class XMLSourceError(Exception):
    """
    Reports that XML could not be loaded from its source.
    """


class XMLNotWellFormedError(Exception):
    """
    Reports that loaded content is not well-formed XML.
    """


def is_http_url(source: str) -> bool:
    """
    Checks whether a source is an HTTP or HTTPS URL.

    Called by: read_xml_source()
    """
    scheme = urlsplit(source).scheme.lower()
    result = scheme in {'http', 'https'}
    return result


def read_xml_source(source: str) -> bytes:
    """
    Reads XML bytes from an HTTP(S) URL or local file path.

    Called by: validate_xml()
    """
    try:
        if is_http_url(source):
            response = httpx.get(source, follow_redirects=True, timeout=HTTP_TIMEOUT_SECONDS)
            response.raise_for_status()
            xml_content = response.content
        else:
            xml_content = Path(source).read_bytes()
    except (OSError, ValueError, httpx.HTTPError) as error:
        raise XMLSourceError(str(error)) from error
    return xml_content


def validate_xml(source: str) -> None:
    """
    Raises an error unless a local or remote XML document is well-formed.

    Called by: validate_xml_directory(), management.commands.validate_xml.Command.handle()
    """
    xml_content = read_xml_source(source)
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    try:
        etree.fromstring(xml_content, parser=parser)
    except etree.XMLSyntaxError as error:
        raise XMLNotWellFormedError(str(error)) from error
    return


def validate_xml_directory(directory_path: Path) -> XMLDirectoryValidationResult:
    """
    Validates every XML file below a directory and collects all failures.

    Called by: management.commands.validate_all_xml.Command.handle(), processor.validate_inscription_corpus()
    """
    if not directory_path.is_dir():
        raise XMLSourceError(f'Directory does not exist: {directory_path}')

    xml_paths = sorted(path for path in directory_path.rglob('*.xml') if path.is_file())
    failures: list[XMLValidationFailure] = []
    for xml_path in xml_paths:
        try:
            validate_xml(str(xml_path))
        except (XMLSourceError, XMLNotWellFormedError) as error:
            failures.append(
                XMLValidationFailure(
                    path=xml_path.relative_to(directory_path),
                    error=str(error),
                )
            )

    checked_count = len(xml_paths)
    result = XMLDirectoryValidationResult(
        checked_count=checked_count,
        well_formed_count=checked_count - len(failures),
        failures=failures,
    )
    return result

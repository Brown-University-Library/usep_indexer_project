"""
Loads local or remote XML and checks whether it is well-formed.
"""

from pathlib import Path
from urllib.parse import urlsplit

import httpx
from lxml import etree


HTTP_TIMEOUT_SECONDS = 30.0


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

    Called by: management.commands.validate_xml.Command.handle()
    """
    xml_content = read_xml_source(source)
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    try:
        etree.fromstring(xml_content, parser=parser)
    except etree.XMLSyntaxError as error:
        raise XMLNotWellFormedError(str(error)) from error
    return

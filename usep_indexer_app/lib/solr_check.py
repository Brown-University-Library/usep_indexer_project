"""
Verifies the configured Solr access and reports active Solr information.

The default check exercises the same query and update handlers required by the indexer without
changing indexed documents. The optional schema and version modes perform read-only information requests.
"""

import json
from dataclasses import dataclass

import httpx
from lxml import etree
from usep_indexer_app.lib import solr_client


@dataclass(frozen=True)
class SolrAccessCheck:
    """
    Summarizes the safe query and update-handler checks.
    """

    document_count: int


@dataclass(frozen=True)
class ActiveSchema:
    """
    Holds the active JSON schema and its declared unique key.
    """

    document: dict[str, object]
    unique_key: str | None


@dataclass(frozen=True)
class ActiveSchemaOutput:
    """
    Holds redirect-safe schema output and its declared unique key.
    """

    text: str
    unique_key: str | None


@dataclass(frozen=True)
class SolrVersionOutput:
    """
    Holds the clean Solr version and redirect-safe full system information.
    """

    spec_version: str
    full_text: str


class SolrCheckError(RuntimeError):
    """
    Identifies an access or active-schema verification failure.
    """


def describe_http_error(action: str, error: httpx.HTTPError | httpx.InvalidURL) -> str:
    """
    Builds a useful error without exposing the configured Solr location.

    Called by: check_required_access(), retrieve_active_schema(), retrieve_solr_version()
    """
    if isinstance(error, httpx.HTTPStatusError):
        description = f'{action} returned HTTP {error.response.status_code}.'
    else:
        description = f'{action} could not reach the configured Solr core ({type(error).__name__}).'
    return description


def require_success_response(response_document: object, action: str) -> dict[str, object]:
    """
    Requires a decoded Solr response with a successful response header.

    Called by: validate_select_response(), validate_update_response(), parse_schema_json()
    """
    if not isinstance(response_document, dict):
        raise SolrCheckError(f'{action} returned JSON that is not an object.')
    response_header = response_document.get('responseHeader')
    if not isinstance(response_header, dict):
        raise SolrCheckError(f'{action} response is missing responseHeader.')
    status = response_header.get('status')
    if not isinstance(status, int) or isinstance(status, bool):
        raise SolrCheckError(f'{action} response has an invalid status value.')
    if status != 0:
        raise SolrCheckError(f'{action} response reported status {status}.')
    return response_document


def validate_select_response(response_document: object) -> int:
    """
    Validates the minimal ID query response and returns the total document count.

    Called by: check_required_access()
    """
    response_document_dict = require_success_response(response_document, 'Solr /select check')
    response = response_document_dict.get('response')
    if not isinstance(response, dict):
        raise SolrCheckError('Solr /select check response is missing the response object.')
    document_count = response.get('numFound')
    documents = response.get('docs')
    if not isinstance(document_count, int) or isinstance(document_count, bool) or document_count < 0:
        raise SolrCheckError('Solr /select check response has an invalid numFound value.')
    if not isinstance(documents, list):
        raise SolrCheckError('Solr /select check response has an invalid docs value.')
    expected_returned_count = min(document_count, 1)
    if len(documents) != expected_returned_count:
        raise SolrCheckError('Solr /select check returned an unexpected number of documents.')
    if documents:
        first_document = documents[0]
        if not isinstance(first_document, dict):
            raise SolrCheckError('Solr /select check returned a document that is not an object.')
        inscription_id = first_document.get('id')
        if not isinstance(inscription_id, str) or not inscription_id:
            raise SolrCheckError('Solr /select check could not read a nonempty id field.')
    return document_count


def validate_update_response(response_document: object) -> None:
    """
    Validates the response to the empty update-handler command.

    Called by: check_required_access()
    """
    require_success_response(response_document, 'Solr /update check')
    return


def check_required_access(
    solr_url: str,
    timeout: float,
    *,
    http_client: httpx.Client | None = None,
) -> SolrAccessCheck:
    """
    Checks normal query and update-handler access without changing indexed documents.

    Called by: management.commands.check_solr.Command.handle_required_access()
    """
    with solr_client.SolrClient(
        solr_url,
        http_client=http_client,
        timeout=timeout,
        commit_within_ms=None,
    ) as client:
        try:
            select_response = client.check_read_access()
        except (httpx.HTTPError, httpx.InvalidURL) as error:
            raise SolrCheckError(describe_http_error('Solr /select check', error)) from error
        except ValueError as error:
            raise SolrCheckError('Solr /select check did not return valid JSON.') from error
        document_count = validate_select_response(select_response)

        try:
            update_response = client.check_update_access()
        except (httpx.HTTPError, httpx.InvalidURL) as error:
            raise SolrCheckError(describe_http_error('Solr /update check', error)) from error
        except ValueError as error:
            raise SolrCheckError('Solr /update check did not return valid JSON.') from error
        validate_update_response(update_response)
    return SolrAccessCheck(document_count=document_count)


def parse_schema_json(response_document: object) -> ActiveSchema:
    """
    Parses the active JSON schema response used by schema output.

    Called by: retrieve_active_schema()
    """
    response_document_dict = require_success_response(response_document, 'Solr schema read')
    schema_document = response_document_dict.get('schema')
    if not isinstance(schema_document, dict):
        raise SolrCheckError('Solr schema read response is missing the schema object.')
    unique_key_value = schema_document.get('uniqueKey')
    if unique_key_value is not None and (not isinstance(unique_key_value, str) or not unique_key_value):
        raise SolrCheckError('Active Solr schema has an invalid uniqueKey value.')
    return ActiveSchema(
        document=schema_document,
        unique_key=unique_key_value,
    )


def parse_schema_xml(schema_text: str) -> str | None:
    """
    Parses the unique-key field from Solr's active schema XML representation.

    Called by: retrieve_active_schema()
    """
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    try:
        schema_element = etree.fromstring(schema_text.encode('utf-8'), parser=parser)
    except (ValueError, etree.XMLSyntaxError) as error:
        raise SolrCheckError('Solr schema read did not return well-formed schema XML.') from error
    if etree.QName(schema_element).localname != 'schema':
        raise SolrCheckError('Solr schema XML does not have a schema root element.')
    unique_key_elements: list[etree._Element] = schema_element.xpath('./uniqueKey')
    if len(unique_key_elements) > 1:
        raise SolrCheckError('Solr schema XML contains more than one uniqueKey element.')
    unique_key = None
    if unique_key_elements:
        unique_key_text = (unique_key_elements[0].text or '').strip()
        if not unique_key_text:
            raise SolrCheckError('Solr schema XML contains an empty uniqueKey element.')
        unique_key = unique_key_text
    return unique_key


def validate_expected_unique_key(unique_key: str | None) -> None:
    """
    Requires the active schema to use the indexer's id field as its unique key.

    Called by: management.commands.check_solr.Command.handle_schema()
    """
    if unique_key != 'id':
        displayed_value = 'not defined' if unique_key is None else repr(unique_key)
        raise SolrCheckError(f'Active Solr schema uniqueKey must be "id"; found {displayed_value}.')
    return


def retrieve_active_schema(
    solr_url: str,
    timeout: float,
    output_format: str,
    *,
    http_client: httpx.Client | None = None,
) -> ActiveSchemaOutput:
    """
    Retrieves redirect-safe JSON or XML output for the active Solr schema.

    Called by: management.commands.check_solr.Command.handle_schema()
    """
    with solr_client.SolrClient(
        solr_url,
        http_client=http_client,
        timeout=timeout,
        commit_within_ms=None,
    ) as client:
        if output_format == 'json':
            try:
                schema_response = client.get_schema_json()
            except (httpx.HTTPError, httpx.InvalidURL) as error:
                raise SolrCheckError(describe_http_error('Solr schema read', error)) from error
            except ValueError as error:
                raise SolrCheckError('Solr schema read did not return valid JSON.') from error
            schema = parse_schema_json(schema_response)
            output_text = json.dumps(schema.document, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
            unique_key = schema.unique_key
        elif output_format == 'schema.xml':
            try:
                schema_text = client.get_schema_xml()
            except (httpx.HTTPError, httpx.InvalidURL) as error:
                raise SolrCheckError(describe_http_error('Solr schema read', error)) from error
            output_text = schema_text.rstrip('\n') + '\n'
            unique_key = parse_schema_xml(output_text)
        else:
            raise ValueError(f'Unsupported schema output format: {output_format}')
    return ActiveSchemaOutput(text=output_text, unique_key=unique_key)


def retrieve_solr_version(
    solr_url: str,
    timeout: float,
    *,
    http_client: httpx.Client | None = None,
) -> SolrVersionOutput:
    """
    Retrieves the Solr specification version and pretty-printed system information.

    Called by: management.commands.check_solr.Command.handle_version()
    """
    with solr_client.SolrClient(
        solr_url,
        http_client=http_client,
        timeout=timeout,
        commit_within_ms=None,
    ) as client:
        try:
            system_response = client.get_system_info()
        except (httpx.HTTPError, httpx.InvalidURL) as error:
            raise SolrCheckError(describe_http_error('Solr version read', error)) from error
        except ValueError as error:
            raise SolrCheckError('Solr version read did not return valid JSON.') from error
    system_document = require_success_response(system_response, 'Solr version read')
    lucene_document = system_document.get('lucene')
    if not isinstance(lucene_document, dict):
        raise SolrCheckError('Solr version read response is missing the lucene object.')
    spec_version = lucene_document.get('solr-spec-version')
    if not isinstance(spec_version, str) or not spec_version:
        raise SolrCheckError('Solr version read response has an invalid solr-spec-version value.')
    full_text = json.dumps(system_document, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
    return SolrVersionOutput(spec_version=spec_version, full_text=full_text)

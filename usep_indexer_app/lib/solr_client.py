"""
Provides the project's focused synchronous HTTP boundary to the USEP Solr core.

One ``SolrClient`` owns or receives one persistent ``httpx.Client``. It handles the indexer's bounded
query and update requests plus the management command's access, active-schema, and system-information
checks. Enrichment reads, atomic updates, and explicit commit requests do not belong at this boundary.
"""

import copy
from collections.abc import Sequence

import httpx
from lxml import etree


DEFAULT_TIMEOUT = 30.0


class SolrClient:
    """
    Sends bounded Solr operations through one reusable HTTP client.
    """

    def __init__(
        self,
        solr_url: str,
        *,
        http_client: httpx.Client | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        commit_within_ms: int | None = 500,
    ) -> None:
        """
        Configures the Solr URL, request policy, and persistent client.

        Called by: indexer.IndexingResources.load(), get_ids(), solr_check.check_required_access()
        """
        if timeout <= 0:
            raise ValueError('Solr timeout must be greater than zero.')
        if commit_within_ms is not None and commit_within_ms <= 0:
            raise ValueError('Solr commitWithin must be greater than zero when configured.')
        self.solr_url = solr_url.rstrip('/')
        self.timeout = timeout
        self.commit_within_ms = commit_within_ms
        self._owns_http_client = http_client is None
        self.http_client = http_client or httpx.Client(timeout=timeout)
        self.request_count = 0

    def __enter__(self) -> 'SolrClient':
        """
        Returns this client for a run-scoped context.

        Called by: context-managed SolrClient callers
        """
        return self

    def __exit__(self, *exception_details: object) -> None:
        """
        Closes an internally owned HTTP client after a run.

        Called by: context manager protocol
        """
        del exception_details
        self.close()
        return

    def close(self) -> None:
        """
        Closes the persistent HTTP client when this object created it.

        Called by: __exit__(), indexer.IndexingResources.close()
        """
        if self._owns_http_client:
            self.http_client.close()
        return

    def get_ids(self) -> list[str]:
        """
        Returns all IDs in the configured Solr core.

        Called by: reindex.process_prepared_full_reindex(), orphans.build_solr_inscription_ids()
        """
        params = {'q': '*:*', 'fl': 'id', 'rows': 100000, 'wt': 'json'}
        response = self.http_client.get(f'{self.solr_url}/select', params=params, timeout=self.timeout)
        self.request_count += 1
        response.raise_for_status()
        documents = response.json()['response']['docs']
        ids = sorted(document['id'] for document in documents)
        return ids

    def check_read_access(self) -> object:
        """
        Sends the smallest normal query that exercises the indexer's read path.

        Called by: solr_check.check_required_access()
        """
        params = {'q': '*:*', 'fl': 'id', 'rows': 1, 'wt': 'json', 'omitHeader': 'false'}
        response = self.http_client.get(f'{self.solr_url}/select', params=params, timeout=self.timeout)
        self.request_count += 1
        response.raise_for_status()
        return response.json()

    def check_update_access(self) -> object:
        """
        Sends an empty delete list through the normal update handler.

        Called by: solr_check.check_required_access()
        """
        response = self.http_client.post(
            f'{self.solr_url}/update',
            params={'wt': 'json', 'omitHeader': 'false'},
            json={'delete': []},
            timeout=self.timeout,
        )
        self.request_count += 1
        response.raise_for_status()
        return response.json()

    def get_schema_json(self) -> object:
        """
        Returns the active Solr schema response as decoded JSON.

        Called by: solr_check.retrieve_active_schema()
        """
        response = self.http_client.get(
            f'{self.solr_url}/schema',
            params={'wt': 'json', 'omitHeader': 'false'},
            timeout=self.timeout,
        )
        self.request_count += 1
        response.raise_for_status()
        return response.json()

    def get_schema_xml(self) -> str:
        """
        Returns Solr's XML representation of the active schema.

        Called by: solr_check.retrieve_active_schema()
        """
        response = self.http_client.get(
            f'{self.solr_url}/schema',
            params={'wt': 'schema.xml'},
            timeout=self.timeout,
        )
        self.request_count += 1
        response.raise_for_status()
        return response.text

    def get_system_info(self) -> object:
        """
        Returns Solr's system-information response as decoded JSON.

        Called by: solr_check.retrieve_solr_version()
        """
        response = self.http_client.get(
            f'{self.solr_url}/admin/system',
            params={'wt': 'json', 'omitHeader': 'false'},
            timeout=self.timeout,
        )
        self.request_count += 1
        response.raise_for_status()
        return response.json()

    def post_documents(self, documents: Sequence[etree._Element]) -> str:
        """
        Posts one complete-document XML batch through one update request.

        Called by: indexer.post_document_batches(), indexer.update_index_entry()
        """
        if not documents:
            raise ValueError('At least one complete Solr document is required.')
        update_element = etree.Element('add')
        self._set_commit_within(update_element)
        for document in documents:
            if document.tag != 'doc':
                raise ValueError('Complete Solr update elements must be doc elements.')
            update_element.append(copy.deepcopy(document))
        update_bytes = etree.tostring(update_element, encoding='utf-8', xml_declaration=True)
        response = self.http_client.post(
            f'{self.solr_url}/update',
            content=update_bytes,
            headers={'Content-Type': 'application/xml'},
            timeout=self.timeout,
        )
        self.request_count += 1
        response.raise_for_status()
        return response.text

    def delete_ids(self, inscription_ids: Sequence[str]) -> str:
        """
        Deletes one bounded ID batch without an explicit commit request.

        Called by: indexer.delete_id_batches(), orphans.run_deletes()
        """
        if not inscription_ids:
            return ''
        delete_element = etree.Element('delete')
        self._set_commit_within(delete_element)
        for inscription_id in inscription_ids:
            if not inscription_id:
                raise ValueError('Solr deletion IDs cannot be empty.')
            id_element = etree.SubElement(delete_element, 'id')
            id_element.text = inscription_id
        update_bytes = etree.tostring(delete_element, encoding='utf-8', xml_declaration=True)
        response = self.http_client.post(
            f'{self.solr_url}/update',
            content=update_bytes,
            headers={'Content-Type': 'application/xml'},
            timeout=self.timeout,
        )
        self.request_count += 1
        response.raise_for_status()
        return response.text

    def _set_commit_within(self, update_element: etree._Element) -> None:
        """
        Places the configured visibility option on an update command.

        Called by: post_documents(), delete_ids()
        """
        if self.commit_within_ms is not None:
            update_element.set('commitWithin', str(self.commit_within_ms))
        return


def get_ids(solr_url: str) -> list[str]:
    """
    Returns all IDs using a short-lived client outside an indexing run.

    Called by: orphans.build_solr_inscription_ids()
    """
    with SolrClient(solr_url) as client:
        ids = client.get_ids()
    return ids

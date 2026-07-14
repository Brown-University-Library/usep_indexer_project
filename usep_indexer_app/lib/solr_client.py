"""
Provides the project's focused HTTP boundary to the USEP Solr core.

Common select, update, commit, and deletion operations share timeout and error-handling behavior here
so the indexing modules can work in domain terms.
"""

import httpx


DEFAULT_TIMEOUT = 30.0


def get_ids(solr_url: str) -> list[str]:
    """
    Returns all IDs in the configured Solr core.

    Called by: orphans.build_solr_inscription_ids(), reindex.build_orphaned_ids()
    """
    params = {'q': '*:*', 'fl': 'id', 'rows': 100000, 'wt': 'json'}
    response = httpx.get(f'{solr_url}/select', params=params, timeout=DEFAULT_TIMEOUT)
    response.raise_for_status()
    documents = response.json()['response']['docs']
    ids = sorted(document['id'] for document in documents)
    return ids


def select_bibliography_ids(solr_url: str, inscription_id: str) -> list[str]:
    """
    Returns the direct bibliography IDs for one inscription.

    Called by: bibliography.add_bibliography()
    """
    params = {'q': f'id:"{inscription_id}"', 'fl': 'bib_ids', 'wt': 'json'}
    response = httpx.get(f'{solr_url}/select', params=params, timeout=DEFAULT_TIMEOUT)
    response.raise_for_status()
    documents = response.json()['response']['docs']
    bib_ids = documents[0].get('bib_ids', []) if documents else []
    return bib_ids


def post_xml_update(solr_url: str, solr_xml: str) -> str:
    """
    Posts an XML update to Solr.

    Called by: indexer.update_index_entry()
    """
    response = httpx.post(
        f'{solr_url}/update',
        content=solr_xml.encode('utf-8'),
        headers={'Content-Type': 'application/xml'},
        timeout=DEFAULT_TIMEOUT,
    )
    response.raise_for_status()
    return response.text


def post_json_update(solr_url: str, update_data: object) -> str:
    """
    Posts a JSON atomic update to Solr.

    Called by: bibliography.add_bibliography(), transcription.add_transcription()
    """
    response = httpx.post(f'{solr_url}/update', json=update_data, timeout=DEFAULT_TIMEOUT)
    response.raise_for_status()
    return response.text


def soft_commit(solr_url: str) -> str:
    """
    Requests a Solr soft commit.

    Called by: bibliography.add_bibliography(), transcription.add_transcription()
    """
    response = httpx.get(f'{solr_url}/update', params={'softCommit': 'true'}, timeout=DEFAULT_TIMEOUT)
    response.raise_for_status()
    return response.text


def delete_id(solr_url: str, inscription_id: str) -> str:
    """
    Deletes one Solr document and commits the change.

    Called by: indexer.remove_index_entry(), orphans.run_deletes()
    """
    update_data = {'delete': {'id': inscription_id}, 'commit': {}}
    response = httpx.post(f'{solr_url}/update', json=update_data, timeout=DEFAULT_TIMEOUT)
    response.raise_for_status()
    return response.text

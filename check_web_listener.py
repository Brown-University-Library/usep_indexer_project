"""
Checks the GitHub webhook listener through a real local HTTP connection.

The script starts an isolated loopback WSGI server, confirms that incorrect
Basic Auth is rejected, posts a sanitized GitHub push payload with valid local
credentials, and validates the durable event created in a temporary spool. Its
purpose is to verify, for development purposes, the listener's integrated HTTP
behavior without reading deployment settings or invoking the queue processor,
Git, rsync, or Solr.

Usage:
    uv run ./check_web_listener.py
"""

import argparse
import os
import pathlib
import tempfile
import threading
import uuid

os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings_run_tests'

import django
import httpx
from django.core.servers.basehttp import WSGIRequestHandler, WSGIServer
from django.core.wsgi import get_wsgi_application
from django.test import override_settings

from usep_indexer_app.lib import spool

DEFAULT_PAYLOAD_PATH = (
    pathlib.Path(__file__).parent / 'usep_indexer_app' / 'tests' / 'fixtures' / 'github_push_2026_07_11.json'
)
LOCAL_HOST = '127.0.0.1'
LOCAL_USERNAME = 'local-webhook-check'
LOCAL_PASSWORD = 'local-webhook-password'
EXPECTED_UPDATED_FILES = ['xml_inscriptions/transcribed/Auct.CA.Oak.priv.L.22.12.06.xml']
EXPECTED_REMOVED_FILES: list[str] = []
HTTP_TIMEOUT_SECONDS = 5.0


def parse_arguments() -> None:
    """
    Parses command-line help for the fixed-fixture check.

    Called by: main()
    """
    parser = argparse.ArgumentParser(
        description='Start a local HTTP server and verify the real GitHub webhook listener.',
    )
    parser.parse_args()
    return


def start_local_server() -> tuple[WSGIServer, threading.Thread, str]:
    """
    Starts Django's WSGI server on an available loopback port.

    Called by: run_http_check()
    """
    application = get_wsgi_application()
    server = WSGIServer((LOCAL_HOST, 0), WSGIRequestHandler)
    server.set_app(application)
    server_thread = threading.Thread(target=server.serve_forever, name='web-listener-check', daemon=True)
    server_thread.start()
    port = server.server_address[1]
    listener_url = f'http://{LOCAL_HOST}:{port}/'
    return server, server_thread, listener_url


def find_pending_events(spool_root: pathlib.Path) -> list[pathlib.Path]:
    """
    Returns the pending event files created by the listener.

    Called by: check_rejected_request(), check_accepted_request()
    """
    pending_directory = spool_root / 'pending'
    pending_events = sorted(pending_directory.glob('*.json')) if pending_directory.is_dir() else []
    return pending_events


def check_rejected_request(
    client: httpx.Client,
    listener_url: str,
    payload_body: bytes,
    headers: dict[str, str],
    spool_root: pathlib.Path,
) -> None:
    """
    Verifies that incorrect Basic Auth cannot create a spool event.

    Called by: run_http_check()
    """
    response = client.post(
        listener_url,
        content=payload_body,
        headers=headers,
        auth=httpx.BasicAuth('incorrect-user', 'incorrect-password'),
    )
    pending_events = find_pending_events(spool_root)
    if response.status_code != 401:
        raise RuntimeError(f'Incorrect Basic Auth returned HTTP {response.status_code}, not 401.')
    if pending_events:
        raise RuntimeError('The rejected request unexpectedly created a pending spool event.')
    return


def check_accepted_request(
    client: httpx.Client,
    listener_url: str,
    payload_body: bytes,
    headers: dict[str, str],
    spool_root: pathlib.Path,
    delivery_id: str,
) -> pathlib.Path:
    """
    Verifies that valid Basic Auth creates the expected durable event.

    Called by: run_http_check()
    """
    response = client.post(
        listener_url,
        content=payload_body,
        headers=headers,
        auth=httpx.BasicAuth(LOCAL_USERNAME, LOCAL_PASSWORD),
    )
    if response.status_code != 200 or response.text != 'received':
        raise RuntimeError(f'Authorized webhook returned HTTP {response.status_code} with body {response.text!r}.')

    pending_events = find_pending_events(spool_root)
    if len(pending_events) != 1:
        raise RuntimeError(f'Expected one pending spool event; found {len(pending_events)}.')

    event = spool.load_event(pending_events[0])
    if event.event_type != 'incremental':
        raise RuntimeError(f'Expected an incremental event; found {event.event_type!r}.')
    if event.request_id != delivery_id:
        raise RuntimeError(f'Expected request ID {delivery_id!r}; found {event.request_id!r}.')
    if event.files_updated != EXPECTED_UPDATED_FILES:
        raise RuntimeError(f'Unexpected updated files: {event.files_updated!r}.')
    if event.files_removed != EXPECTED_REMOVED_FILES:
        raise RuntimeError(f'Unexpected removed files: {event.files_removed!r}.')
    return pending_events[0]


def run_http_check() -> pathlib.Path:
    """
    Runs rejected and accepted requests against an isolated local listener.

    Called by: main()
    """
    payload_body = DEFAULT_PAYLOAD_PATH.read_bytes()
    delivery_id = f'local-check-{uuid.uuid4()}'
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'GitHub-Hookshot/local-web-listener-check',
        'X-GitHub-Delivery': delivery_id,
        'X-GitHub-Event': 'push',
    }
    django.setup()

    with tempfile.TemporaryDirectory(prefix='usep-web-listener-check-') as temporary_directory:
        spool_root = pathlib.Path(temporary_directory) / 'spool'
        with override_settings(
            ALLOWED_HOSTS=[LOCAL_HOST],
            BASIC_AUTH_USERNAME=LOCAL_USERNAME,
            BASIC_AUTH_PASSWORD=LOCAL_PASSWORD,
            SPOOL_ROOT_PATH=spool_root,
        ):
            server, server_thread, listener_url = start_local_server()
            try:
                with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as client:
                    check_rejected_request(client, listener_url, payload_body, headers, spool_root)
                    event_path = check_accepted_request(
                        client,
                        listener_url,
                        payload_body,
                        headers,
                        spool_root,
                        delivery_id,
                    )
            finally:
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=HTTP_TIMEOUT_SECONDS)

        checked_event_path = pathlib.Path('pending') / event_path.name
    return checked_event_path


def main() -> None:
    """
    Parses arguments, runs the HTTP check, and reports success.

    Called by: dundermain
    """
    parse_arguments()
    checked_event_path = run_http_check()
    print(f'web-listener HTTP check passed; validated temporary event {checked_event_path}')
    return


if __name__ == '__main__':
    main()

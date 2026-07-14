"""
Checks the GitHub webhook listener through a real local HTTP connection.

The script starts an isolated loopback WSGI server, confirms that incorrect
Basic Auth is rejected, posts a GitHub push payload with valid local
credentials, and validates the durable event created in a temporary spool or
the configured real event directory. Its purpose is to verify, for development
purposes, the listener's integrated HTTP behavior without invoking the queue
processor, Git, rsync, or Solr.

Usage:
    uv run ./check_web_listener.py [--payload PATH] [--use-real-directory]
"""

import argparse
import logging
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
from dotenv import dotenv_values

from usep_indexer_app.lib import spool


log = logging.getLogger(__name__)

DEFAULT_PAYLOAD_PATH = (
    pathlib.Path(__file__).parent / 'usep_indexer_app' / 'tests' / 'fixtures' / 'github_push_2026_07_11.json'
)
PROJECT_ROOT_PATH = pathlib.Path(__file__).resolve().parent
DOTENV_PATH = PROJECT_ROOT_PATH.parent / '.env'
LOCAL_HOST = '127.0.0.1'
LOCAL_USERNAME = 'local-webhook-check'
LOCAL_PASSWORD = 'local-webhook-password'
HTTP_TIMEOUT_SECONDS = 5.0


def configure_logging() -> None:
    """
    Displays development and server-flow messages for the local check.

    Called by: run_http_check()
    """
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(
        logging.Formatter(
            '[%(asctime)s] %(levelname)s [%(module)s-%(funcName)s()::%(lineno)d] %(message)s',
            datefmt='%d/%b/%Y %H:%M:%S',
        )
    )
    for logger_name in (__name__, 'usep_indexer_app'):
        configured_logger = logging.getLogger(logger_name)
        configured_logger.handlers.clear()
        configured_logger.addHandler(console_handler)
        configured_logger.setLevel(logging.DEBUG)
        configured_logger.propagate = False
    return


def parse_arguments() -> tuple[pathlib.Path, bool]:
    """
    Parses the payload path and event-directory selection.

    Called by: main()
    """
    parser = argparse.ArgumentParser(
        description='Start a local HTTP server and verify the real GitHub webhook listener.',
    )
    parser.add_argument(
        '--payload',
        type=pathlib.Path,
        default=DEFAULT_PAYLOAD_PATH,
        metavar='PATH',
        help='JSON payload file; relative paths are resolved from the project-root working directory.',
    )
    parser.add_argument(
        '--use-real-directory',
        action='store_true',
        help='Save the event under SPOOL_ROOT_PATH from the outer .env instead of a temporary directory.',
    )
    arguments = parser.parse_args()
    payload_path = arguments.payload.expanduser()
    if not payload_path.is_absolute():
        payload_path = pathlib.Path.cwd() / payload_path
    payload_path = payload_path.resolve()
    return payload_path, arguments.use_real_directory


def load_real_spool_root() -> pathlib.Path:
    """
    Loads only the configured spool root from the outer environment file.

    Called by: select_spool_root()
    """
    configured_spool_root = dotenv_values(DOTENV_PATH).get('SPOOL_ROOT_PATH')
    if not configured_spool_root:
        raise RuntimeError(f'SPOOL_ROOT_PATH is not configured in {DOTENV_PATH}.')
    spool_root = pathlib.Path(configured_spool_root).expanduser()
    if not spool_root.is_absolute():
        spool_root = PROJECT_ROOT_PATH / spool_root
    spool_root = spool_root.resolve()
    return spool_root


def select_spool_root(temporary_directory: str, use_real_directory: bool) -> pathlib.Path:
    """
    Selects either the isolated temporary spool or the configured real spool.

    Called by: run_http_check()
    """
    if use_real_directory:
        spool_root = load_real_spool_root()
    else:
        spool_root = pathlib.Path(temporary_directory) / 'spool'
    return spool_root


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
    log.info(f'local listener started; listener_url, ``{listener_url}``')
    return server, server_thread, listener_url


def find_pending_events(spool_root: pathlib.Path) -> list[pathlib.Path]:
    """
    Returns the pending event files created by the listener.

    Called by: find_new_request_events(), run_http_check()
    """
    pending_directory = spool_root / 'pending'
    pending_events = sorted(pending_directory.glob('*.json')) if pending_directory.is_dir() else []
    log.debug(f'pending_event_count, ``{len(pending_events)}``; pending_directory, ``{pending_directory}``')
    return pending_events


def find_new_request_events(
    spool_root: pathlib.Path,
    previous_pending_events: set[pathlib.Path],
    delivery_id: str,
) -> list[pathlib.Path]:
    """
    Returns newly saved pending events matching this script's delivery ID.

    Called by: check_rejected_request(), check_accepted_request()
    """
    current_pending_events = find_pending_events(spool_root)
    new_event_paths = [event_path for event_path in current_pending_events if event_path not in previous_pending_events]
    matching_event_paths = [
        event_path for event_path in new_event_paths if spool.load_event(event_path).request_id == delivery_id
    ]
    log.debug(
        f'new_event_count, ``{len(new_event_paths)}``; matching_event_count, ``{len(matching_event_paths)}``; '
        f'delivery_id, ``{delivery_id}``'
    )
    return matching_event_paths


def check_rejected_request(
    client: httpx.Client,
    listener_url: str,
    payload_body: bytes,
    headers: dict[str, str],
    spool_root: pathlib.Path,
    previous_pending_events: set[pathlib.Path],
    delivery_id: str,
) -> None:
    """
    Verifies that incorrect Basic Auth cannot create a spool event.

    Called by: run_http_check()
    """
    log.debug(f'request_action, ``send with incorrect credentials``; listener_url, ``{listener_url}``')
    response = client.post(
        listener_url,
        content=payload_body,
        headers=headers,
        auth=httpx.BasicAuth('incorrect-user', 'incorrect-password'),
    )
    matching_event_paths = find_new_request_events(spool_root, previous_pending_events, delivery_id)
    if response.status_code != 401:
        raise RuntimeError(f'Incorrect Basic Auth returned HTTP {response.status_code}, not 401.')
    if matching_event_paths:
        raise RuntimeError('The rejected request unexpectedly created a pending spool event.')
    log.info(f'authentication rejection confirmed; status_code, ``{response.status_code}``')
    return


def check_accepted_request(
    client: httpx.Client,
    listener_url: str,
    payload_body: bytes,
    headers: dict[str, str],
    spool_root: pathlib.Path,
    delivery_id: str,
    previous_pending_events: set[pathlib.Path],
) -> pathlib.Path:
    """
    Verifies that valid Basic Auth creates the expected durable event.

    Called by: run_http_check()
    """
    log.debug(f'request_action, ``send GitHub push``; listener_url, ``{listener_url}``; delivery_id, ``{delivery_id}``')
    response = client.post(
        listener_url,
        content=payload_body,
        headers=headers,
        auth=httpx.BasicAuth(LOCAL_USERNAME, LOCAL_PASSWORD),
    )
    if response.status_code != 200 or response.text != 'received':
        raise RuntimeError(f'Authorized webhook returned HTTP {response.status_code} with body {response.text!r}.')

    matching_event_paths = find_new_request_events(spool_root, previous_pending_events, delivery_id)
    if len(matching_event_paths) != 1:
        raise RuntimeError(
            f'Expected one new pending event for delivery ID {delivery_id}; found {len(matching_event_paths)}.'
        )

    event_path = matching_event_paths[0]
    event = spool.load_event(event_path)
    if event.event_type != 'incremental':
        raise RuntimeError(f'Expected an incremental event; found {event.event_type!r}.')
    if event.request_id != delivery_id:
        raise RuntimeError(f'Expected request ID {delivery_id!r}; found {event.request_id!r}.')
    log.info(
        f'queued event validated; delivery_id, ``{delivery_id}``; event_path, ``{event_path}``; '
        f'files_updated, ``{event.files_updated}``; files_removed, ``{event.files_removed}``'
    )
    return event_path


def run_http_check(payload_path: pathlib.Path, use_real_directory: bool) -> pathlib.Path:
    """
    Runs rejected and accepted requests against an isolated local listener.

    Called by: main()
    """
    django.setup()
    configure_logging()
    log.info('local HTTP listener check started')
    log.debug(f'payload_path, ``{payload_path}``')
    payload_body = payload_path.read_bytes()
    delivery_id = f'local-check-{uuid.uuid4()}'
    log.debug(f'payload_bytes, ``{len(payload_body)}``; delivery_id, ``{delivery_id}``')
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'GitHub-Hookshot/local-web-listener-check',
        'X-GitHub-Delivery': delivery_id,
        'X-GitHub-Event': 'push',
    }
    with tempfile.TemporaryDirectory(prefix='usep-web-listener-check-') as temporary_directory:
        spool_root = select_spool_root(temporary_directory, use_real_directory)
        directory_mode = 'real' if use_real_directory else 'temporary'
        log.info(f'directory_mode, ``{directory_mode}``; spool_root, ``{spool_root}``')
        with override_settings(
            ALLOWED_HOSTS=[LOCAL_HOST],
            BASIC_AUTH_USERNAME=LOCAL_USERNAME,
            BASIC_AUTH_PASSWORD=LOCAL_PASSWORD,
            SPOOL_ROOT_PATH=spool_root,
        ):
            server, server_thread, listener_url = start_local_server()
            try:
                with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as client:
                    pending_before_rejected_request = set(find_pending_events(spool_root))
                    check_rejected_request(
                        client,
                        listener_url,
                        payload_body,
                        headers,
                        spool_root,
                        pending_before_rejected_request,
                        delivery_id,
                    )
                    pending_before_accepted_request = set(find_pending_events(spool_root))
                    event_path = check_accepted_request(
                        client,
                        listener_url,
                        payload_body,
                        headers,
                        spool_root,
                        delivery_id,
                        pending_before_accepted_request,
                    )
            finally:
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=HTTP_TIMEOUT_SECONDS)
                log.debug(f'local listener stopped; listener_url, ``{listener_url}``')

        if use_real_directory:
            checked_event_path = event_path
        else:
            checked_event_path = pathlib.Path('pending') / event_path.name
    return checked_event_path


def main() -> None:
    """
    Parses arguments, runs the HTTP check, and reports success.

    Called by: dundermain
    """
    payload_path, use_real_directory = parse_arguments()
    checked_event_path = run_http_check(payload_path, use_real_directory)
    log.info(f'local HTTP listener check passed; checked_event_path, ``{checked_event_path}``')
    return


if __name__ == '__main__':
    main()

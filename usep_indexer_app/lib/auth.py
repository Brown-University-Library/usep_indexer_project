"""
Provides separate HTTP Basic Auth credentials for webhook and remote-admin views.
"""

import base64
import binascii
import json
import logging
import secrets
from collections.abc import Callable
from functools import update_wrapper

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest, HttpResponse

log = logging.getLogger(__name__)


class BasicAuthProtectedView:
    """
    Wraps a Django view with HTTP Basic Auth.
    """

    def __init__(self, view_function: Callable[..., HttpResponse], *, remote_admin: bool = False) -> None:
        """
        Stores the protected view while retaining its metadata.

        Called by: basic_auth_required(), remote_admin_auth_required()
        """
        self.view_function = view_function
        self.remote_admin = remote_admin
        update_wrapper(self, view_function)

    def __call__(self, request: HttpRequest, *args: object, **kwargs: object) -> HttpResponse:
        """
        Authorizes a request or returns the legacy 401 challenge.

        Called by: Django request dispatch
        """
        authorization_header: str = request.headers.get('Authorization', '')
        authorization_header_present: bool = 'Authorization' in request.headers
        basic_scheme: bool = authorization_header.partition(' ')[0].lower() == 'basic'
        credentials: tuple[str, str] | None = parse_basic_auth(authorization_header)
        credentials_parsed: bool = credentials is not None
        credential_source: str = 'REMOTE_ADMIN_AUTH' if self.remote_admin else 'BASIC_AUTH_USERNAME/BASIC_AUTH_PASSWORD'
        is_authorized = credentials is not None and credentials_match(*credentials, remote_admin=self.remote_admin)
        authorization_result = 'accepted' if is_authorized else 'rejected'
        log.info(
            f'authorization_result, ``{authorization_result}``; request_method, ``{request.method}``; '
            f'request_path, ``{request.path}``; credential_source, ``{credential_source}``; '
            f'authorization_header_present, ``{authorization_header_present}``; basic_scheme, ``{basic_scheme}``; '
            f'credentials_parsed, ``{credentials_parsed}``'
        )
        if is_authorized:
            response = self.view_function(request, *args, **kwargs)
        else:
            response = HttpResponse('Unauthorized', status=401)
            realm = 'USEP Remote Admin' if self.remote_admin else 'Login Required'
            response['WWW-Authenticate'] = f'Basic realm="{realm}"'
        return response


def basic_auth_required(view_function: Callable[..., HttpResponse]) -> BasicAuthProtectedView:
    """
    Protects a webhook view with the existing listener credentials.

    Called by: protected view decorators
    """
    return BasicAuthProtectedView(view_function)


def remote_admin_auth_required(view_function: Callable[..., HttpResponse]) -> BasicAuthProtectedView:
    """
    Protects a remote-admin view with its separate credentials.

    Called by: views.reindex_all(), views.list_orphans(), views.delete_orphans() decorators
    """
    return BasicAuthProtectedView(view_function, remote_admin=True)


def parse_remote_admin_auth(raw_value: str | None) -> dict[str, str]:
    """
    Validates the required remote-admin credentials without including their values in errors.

    Called by: config.settings
    """
    if not raw_value:
        raise ImproperlyConfigured('REMOTE_ADMIN_AUTH_JSON is required.')
    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError:
        raise ImproperlyConfigured('REMOTE_ADMIN_AUTH_JSON must contain valid JSON.') from None
    if not isinstance(value, dict) or set(value) != {'username', 'password'}:
        raise ImproperlyConfigured('REMOTE_ADMIN_AUTH_JSON must be an object with username and password fields.')
    credentials: dict[str, str] = {}
    for field_name in ('username', 'password'):
        field_value = value[field_name]
        if not isinstance(field_value, str) or not field_value.strip():
            raise ImproperlyConfigured(f'REMOTE_ADMIN_AUTH_JSON {field_name} must be a nonempty string.')
        if any(ord(character) < 32 or ord(character) == 127 for character in field_value):
            raise ImproperlyConfigured(f'REMOTE_ADMIN_AUTH_JSON {field_name} must not contain control characters.')
        credentials[field_name] = field_value
    if ':' in credentials['username']:
        raise ImproperlyConfigured('REMOTE_ADMIN_AUTH_JSON username must not contain a colon.')
    return credentials


def parse_basic_auth(authorization_header: str) -> tuple[str, str] | None:
    """
    Parses an HTTP Basic Authorization header.

    Called by: BasicAuthProtectedView.__call__()
    """
    credentials: tuple[str, str] | None = None
    scheme, separator, encoded_value = authorization_header.partition(' ')
    if separator and scheme.lower() == 'basic':
        try:
            decoded_value = base64.b64decode(encoded_value, validate=True).decode('utf-8')
            username, password_separator, password = decoded_value.partition(':')
            if password_separator:
                credentials = (username, password)
        except (binascii.Error, UnicodeDecodeError):
            credentials = None
    return credentials


def credentials_match(username: str, password: str, *, remote_admin: bool = False) -> bool:
    """
    Compares supplied Basic Auth credentials without timing-sensitive equality.

    Called by: BasicAuthProtectedView.__call__()
    """
    if remote_admin:
        expected_username = settings.REMOTE_ADMIN_AUTH['username']
        expected_password = settings.REMOTE_ADMIN_AUTH['password']
    else:
        expected_username = settings.BASIC_AUTH_USERNAME
        expected_password = settings.BASIC_AUTH_PASSWORD
    username_matches = secrets.compare_digest(username.encode('utf-8'), expected_username.encode('utf-8'))
    password_matches = secrets.compare_digest(password.encode('utf-8'), expected_password.encode('utf-8'))
    log.info(
        f'remote_admin, ``{remote_admin}``; received_username_length, ``{len(username)}``; '
        f'expected_username_length, ``{len(expected_username)}``; received_password_length, ``{len(password)}``; '
        f'expected_password_length, ``{len(expected_password)}``; username_matches, ``{username_matches}``; '
        f'password_matches, ``{password_matches}``'
    )
    return username_matches and password_matches

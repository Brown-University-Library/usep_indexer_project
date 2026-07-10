import base64
import binascii
import secrets
from collections.abc import Callable
from functools import update_wrapper

from django.conf import settings
from django.http import HttpRequest, HttpResponse


class BasicAuthProtectedView:
    """
    Wraps a Django view with HTTP Basic Auth.
    """

    def __init__(self, view_function: Callable[..., HttpResponse]) -> None:
        """
        Stores the protected view while retaining its metadata.

        Called by: basic_auth_required()
        """
        self.view_function = view_function
        update_wrapper(self, view_function)

    def __call__(self, request: HttpRequest, *args: object, **kwargs: object) -> HttpResponse:
        """
        Authorizes a request or returns the legacy 401 challenge.

        Called by: Django request dispatch
        """
        credentials = parse_basic_auth(request.headers.get('Authorization', ''))
        is_authorized = credentials is not None and credentials_match(*credentials)
        if is_authorized:
            response = self.view_function(request, *args, **kwargs)
        else:
            response = HttpResponse('Unauthorized', status=401)
            response['WWW-Authenticate'] = 'Basic realm="Login Required"'
        return response


def basic_auth_required(view_function: Callable[..., HttpResponse]) -> BasicAuthProtectedView:
    """
    Protects a view with the legacy HTTP Basic Auth contract.

    Called by: protected view decorators
    """
    return BasicAuthProtectedView(view_function)


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


def credentials_match(username: str, password: str) -> bool:
    """
    Compares supplied Basic Auth credentials without timing-sensitive equality.

    Called by: BasicAuthProtectedView.__call__()
    """
    username_matches = secrets.compare_digest(username, settings.BASIC_AUTH_USERNAME)
    password_matches = secrets.compare_digest(password, settings.BASIC_AUTH_PASSWORD)
    return username_matches and password_matches

"""
WSGI config.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/wsgi/
"""

import os
import pathlib
import sys

from django.core.wsgi import get_wsgi_application

PROJECT_DIR_PATH = pathlib.Path(__file__).resolve().parent.parent
# print( f'PROJECT_DIR_PATH, ``{PROJECT_DIR_PATH}``' )

sys.path.append(str(PROJECT_DIR_PATH))

os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings'  # so django can access its settings

application = get_wsgi_application()

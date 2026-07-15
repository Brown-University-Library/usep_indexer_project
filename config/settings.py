"""
Django settings for usep_indexer_project.
"""

import json
import os
import pathlib

from dotenv import find_dotenv, load_dotenv


dotenv_path = pathlib.Path(__file__).resolve().parent.parent.parent / '.env'
assert dotenv_path.exists(), f'file does not exist, ``{dotenv_path}``'
load_dotenv(find_dotenv(str(dotenv_path), raise_error_if_not_found=True), override=True)

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ['SECRET_KEY']
DEBUG = json.loads(os.environ['DEBUG_JSON'])
ADMINS = json.loads(os.environ['ADMINS_JSON'])
ALLOWED_HOSTS = json.loads(os.environ['ALLOWED_HOSTS_JSON'])
CSRF_TRUSTED_ORIGINS = json.loads(os.environ['CSRF_TRUSTED_ORIGINS_JSON'])

INSTALLED_APPS = [
    'django.contrib.staticfiles',
    'usep_indexer_app',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'usep_indexer_app' / 'usep_indexer_app_templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

## This service deliberately has no database or database-backed Django components.
DATABASES = {}
AUTH_PASSWORD_VALIDATORS = []

SESSION_ENGINE = 'django.contrib.sessions.backends.signed_cookies'
SESSION_COOKIE_HTTPONLY = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_SECURE = json.loads(os.environ.get('SESSION_COOKIE_SECURE_JSON', 'true'))
SESSION_COOKIE_SAMESITE = 'Lax'

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'America/New_York'
USE_I18N = True
USE_TZ = False

STATIC_URL = os.environ['STATIC_URL']
STATIC_ROOT = os.environ['STATIC_ROOT']

SERVER_EMAIL = os.environ['SERVER_EMAIL']
EMAIL_HOST = os.environ['EMAIL_HOST']
EMAIL_PORT = int(os.environ['EMAIL_PORT'])

CACHES = json.loads(os.environ['CACHES_JSON'])

BASIC_AUTH_USERNAME = os.environ['BASIC_AUTH_USERNAME']
BASIC_AUTH_PASSWORD = os.environ['BASIC_AUTH_PASSWORD']
USEP_DATA_GIT_CLONED_DIR_PATH = pathlib.Path(os.environ['USEP_DATA_GIT_CLONED_DIR_PATH'])
TEMP_DATA_DIR_PATH = pathlib.Path(os.environ['TEMP_DATA_DIR_PATH'])
WEBSERVED_DATA_DIR_PATH = pathlib.Path(os.environ['WEBSERVED_DATA_DIR_PATH'])
SOLR_URL = os.environ['SOLR_URL'].rstrip('/')
SOLR_XSL_PATH = pathlib.Path(os.environ['SOLR_XSL_PATH'])
TITLES_XML_PATH = WEBSERVED_DATA_DIR_PATH / 'resources' / 'titles.xml'
TRANSCRIPTION_PARSER_XSL_PATH = pathlib.Path(os.environ['TRANSCRIPTION_PARSER_XSL_PATH'])
LEGIT_IPS = json.loads(os.environ['LEGIT_IPS_JSON'])
SPOOL_ROOT_PATH = pathlib.Path(os.environ['SPOOL_ROOT_PATH'])
SPOOL_MAX_ATTEMPTS = int(os.environ.get('SPOOL_MAX_ATTEMPTS', '3'))
SPOOL_BATCH_SIZE = int(os.environ.get('SPOOL_BATCH_SIZE', '100'))
SPOOL_COMPLETED_RETENTION_DAYS = int(os.environ.get('SPOOL_COMPLETED_RETENTION_DAYS', '30'))
SPOOL_HEALTH_MAX_AGE_SECONDS = int(os.environ.get('SPOOL_HEALTH_MAX_AGE_SECONDS', '300'))
README_URL = os.environ.get(
    'README_URL',
    'https://github.com/Brown-University-Library/usep_indexer_project',
)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'standard': {
            'format': '[%(asctime)s] %(levelname)s [%(module)s-%(funcName)s()::%(lineno)d] %(message)s',
            'datefmt': '%d/%b/%Y %H:%M:%S',
        },
    },
    'handlers': {
        'mail_admins': {
            'level': 'ERROR',
            'class': 'django.utils.log.AdminEmailHandler',
            'include_html': True,
        },
        'logfile': {
            'level': os.environ.get('LOG_LEVEL', 'INFO'),
            'class': 'logging.FileHandler',
            'filename': os.environ['LOG_PATH'],
            'formatter': 'standard',
        },
        'console': {
            'level': 'DEBUG',
            'class': 'logging.StreamHandler',
            'formatter': 'standard',
        },
    },
    'loggers': {
        'django.request': {
            'handlers': ['mail_admins'],
            'level': 'ERROR',
            'propagate': False,
        },
        'usep_indexer_app': {
            'handlers': ['logfile'],
            'level': 'DEBUG',
            'propagate': False,
        },
    },
}

"""
Database-free Django settings for the test suite.
"""

import pathlib


BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
SECRET_KEY = 'test-only-secret-key'
DEBUG = False
ADMINS: list[tuple[str, str]] = []
ALLOWED_HOSTS = ['testserver']
CSRF_TRUSTED_ORIGINS: list[str] = []

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

DATABASES = {}
AUTH_PASSWORD_VALIDATORS: list[object] = []
SESSION_ENGINE = 'django.contrib.sessions.backends.signed_cookies'
SESSION_COOKIE_HTTPONLY = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = True

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'America/New_York'
USE_I18N = True
USE_TZ = False

STATIC_URL = '/static/'
STATIC_ROOT = '/tmp/usep-indexer-static'
SERVER_EMAIL = 'test@example.org'
EMAIL_HOST = 'localhost'
EMAIL_PORT = 1025
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'usep-indexer-tests',
    },
}

BASIC_AUTH_USERNAME = 'test-user'
BASIC_AUTH_PASSWORD = 'test-password'
USEP_DATA_GIT_CLONED_DIR_PATH = pathlib.Path('/tmp/usep-data-clone')
TEMP_UNIFIED_INSCRIPTIONS_DIR_PATH = pathlib.Path('/tmp/temp_unified_inscriptions_dir')
WEBSERVED_DATA_DIR_PATH = pathlib.Path('/tmp/usep-webserved-data')
SOLR_URL = 'http://solr.example.org/solr/usep'
SOLR_XSL_PATH = pathlib.Path('/tmp/USEp_to_Solr.xsl')
TITLES_XML_PATH = WEBSERVED_DATA_DIR_PATH / 'resources' / 'titles.xml'
TRANSCRIPTION_PARSER_XSL_PATH = pathlib.Path('/tmp/transcription_index_val.xsl')
LEGIT_IPS = ['127.0.0.1']
SPOOL_ROOT_PATH = pathlib.Path('/tmp/usep-indexer-spool-tests')
SPOOL_MAX_ATTEMPTS = 3
SPOOL_BATCH_SIZE = 100
SPOOL_COMPLETED_RETENTION_DAYS = 30
SPOOL_HEALTH_MAX_AGE_SECONDS = 300
README_URL = 'https://github.com/Brown-University-Library/usep_indexer_project'

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'level': 'CRITICAL',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'CRITICAL',
    },
}

TEST_RUNNER = 'django.test.runner.DiscoverRunner'

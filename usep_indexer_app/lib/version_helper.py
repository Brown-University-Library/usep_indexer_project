import datetime
import hashlib
import logging
import pathlib

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest

log = logging.getLogger(__name__)

BRANCH_AND_COMMIT_CACHE_SECONDS = 5


def get_branch_and_commit() -> tuple[str, str]:
    """
    Returns branch and commit data from cache or `.git/HEAD`.
    Called by: views.version()
    """
    log.debug('get_branch_and_commit()')
    base_dir = pathlib.Path(settings.BASE_DIR)
    cache_key = make_branch_and_commit_cache_key(base_dir)
    branch_and_commit = cache.get(cache_key)
    if branch_and_commit is None:
        branch_and_commit = read_branch_and_commit(base_dir)
        cache.set(cache_key, branch_and_commit, BRANCH_AND_COMMIT_CACHE_SECONDS)
    log.debug(f'branch_and_commit, ``{branch_and_commit}``')
    return branch_and_commit


def make_branch_and_commit_cache_key(base_dir: pathlib.Path) -> str:
    """
    Builds a cache key for branch and commit data.

    A simple key like `version_cache_key` should work for this endpoint, but including a hash of the
    full base path minimizes collision risk, which is useful in a template project, and lets tests use
    separate temporary project directories.

    `usedforsecurity=False` keeps python/openssl from blocking md5 usage in stricter environments.

    Called by: get_branch_and_commit()
    """
    full_base_dir = base_dir.expanduser().resolve(strict=False)
    base_dir_hash = hashlib.md5(str(full_base_dir).encode('utf-8'), usedforsecurity=False).hexdigest()
    cache_key = f'version_cache_key:{base_dir_hash}'
    return cache_key


def read_branch_and_commit(base_dir: pathlib.Path) -> tuple[str, str]:
    """
    Reads branch and commit data from `.git/HEAD`.
    Called by: get_branch_and_commit()
    """
    log.debug('read_branch_and_commit()')
    branch = 'branch_not_found'
    commit = 'commit_not_found'
    git_dir = base_dir / '.git'
    try:
        ## read the HEAD file to find the current branch ------------
        head_file: pathlib.Path = git_dir / 'HEAD'
        ref_line: str = head_file.read_text().strip()
        if ref_line.startswith('ref:'):
            ref_path = ref_line.split(' ', maxsplit=1)[1]
            branch = pathlib.Path(ref_path).name
            commit_file: pathlib.Path = git_dir / ref_path
            commit = commit_file.read_text().strip()
        else:
            branch = 'detached'
            commit = ref_line
    except FileNotFoundError:
        log.error('no `.git` directory, HEAD file, or commit ref file found.')
    except Exception:
        log.exception('other problem fetching branch and commit data')
    log.debug(f'branch, ``{branch}``; commit, ``{commit}``')
    return branch, commit


def make_context(
    request: HttpRequest,
    request_started: datetime.datetime,
    version_text: str,
) -> dict[str, object]:
    """
    Assembles data-dct.
    Called by views.version()
    """
    context = {
        'request': {
            'url': '%s://%s%s'
            % (
                request.scheme,
                request.META.get('HTTP_HOST', '127.0.0.1'),  # HTTP_HOST doesn't exist for client-tests
                request.META.get('REQUEST_URI', request.META['PATH_INFO']),
            ),
            'timestamp': str(request_started),
        },
        'response': {
            'ip': request.META.get('REMOTE_ADDR', 'unknown'),
            'version': version_text,
            'timetaken': str(datetime.datetime.now() - request_started),
        },
    }
    return context

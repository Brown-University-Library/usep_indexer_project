import datetime
import json
import logging


log = logging.getLogger(__name__)


def prepare_files_to_process(request_body: bytes) -> dict[str, object]:
    """
    Extracts added, modified, and removed paths from a GitHub push payload.

    Malformed payloads retain the legacy behavior of producing empty file lists.

    Called by: views.handle_github_push()
    """
    files_to_process: dict[str, object] = {
        'files_updated': [],
        'files_removed': [],
        'timestamp': str(datetime.datetime.now()),
    }
    if request_body:
        try:
            commit_info = json.loads(request_body)
            added, modified, removed = examine_commits(commit_info)
            files_to_process['files_updated'] = added + modified
            files_to_process['files_removed'] = removed
            log.debug(f'added, ``{added}``; modified, ``{modified}``; removed, ``{removed}``')
        except (json.JSONDecodeError, KeyError, TypeError, UnicodeDecodeError):
            log.exception('Unable to parse GitHub push payload; queuing empty file lists.')
    return files_to_process


def examine_commits(commit_info: dict[str, object]) -> tuple[list[str], list[str], list[str]]:
    """
    Collects changed paths across all commits in a push payload.

    Called by: prepare_files_to_process()
    """
    added: list[str] = []
    modified: list[str] = []
    removed: list[str] = []
    commits = commit_info['commits']
    if not isinstance(commits, list):
        raise TypeError('The commits value must be a list.')
    for commit in commits:
        if not isinstance(commit, dict):
            raise TypeError('Each commit must be an object.')
        added.extend(require_string_list(commit['added']))
        modified.extend(require_string_list(commit['modified']))
        removed.extend(require_string_list(commit['removed']))
    return added, modified, removed


def require_string_list(value: object) -> list[str]:
    """
    Validates and returns a list of strings from webhook JSON.

    Called by: examine_commits()
    """
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError('Expected a list of path strings.')
    return value

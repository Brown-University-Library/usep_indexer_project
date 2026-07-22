"""
Synchronizes USEP source data and prepares it for indexing.

This module groups the external Git and rsync steps with flattening the inscription sources,
normalizing their XInclude references, and dispatching incremental index changes. Full reindexing
reuses the same preparation operations.
"""

import logging
import subprocess
from pathlib import Path

from django.conf import settings
from usep_indexer_app.lib import indexer, stylesheet_dependencies, xml_validation


log = logging.getLogger(__name__)

XINCLUDE_REPLACEMENTS = {
    'http://library.brown.edu/usep_data/resources/include_publicationStmt.xml': '../resources/include_publicationStmt.xml',
    'http://library.brown.edu/usep_data/resources/include_taxonomies.xml': '../resources/include_taxonomies.xml',
    'http://library.brown.edu/usep_data/resources/titles.xml': '../resources/titles.xml',
}


def call_git_pull(git_clone_path: Path) -> None:
    """
    Runs git pull in the configured USEP data clone.

    Called by: prepare_public_data()
    """
    log.debug(f'Running Git pull; git_clone_path, ``{git_clone_path}``')
    subprocess.run(['git', 'pull'], cwd=git_clone_path, check=True, text=True)
    return


def read_git_revision(git_clone_path: Path) -> str:
    """
    Returns a short public data-repository revision for indexing diagnostics.

    Called by: prepare_public_data()
    """
    revision = 'unavailable'
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--short', 'HEAD'],
            cwd=git_clone_path,
            check=True,
            capture_output=True,
            text=True,
        )
        candidate_revision = result.stdout.strip()
        if candidate_revision:
            revision = candidate_revision
    except (OSError, subprocess.CalledProcessError):
        log.warning('Unable to read the public data-repository revision; using ``unavailable``.')
    return revision


def copy_files(git_clone_path: Path, temp_unified_inscriptions_dir_path: Path, webserved_data_path: Path) -> None:
    """
    Mirrors resources and flattens the three inscription source directories.

    Called by: prepare_public_data()
    """
    log.debug(
        f'Copying USEP data; git_clone_path, ``{git_clone_path}``; '
        f'temp_unified_inscriptions_dir_path, ``{temp_unified_inscriptions_dir_path}``; '
        f'webserved_data_path, ``{webserved_data_path}``'
    )
    run_rsync(git_clone_path / 'resources', webserved_data_path / 'resources', delete=True)
    run_rsync(git_clone_path / 'xml_inscriptions' / 'bib_only', temp_unified_inscriptions_dir_path, delete=True)
    run_rsync(git_clone_path / 'xml_inscriptions' / 'metadata_only', temp_unified_inscriptions_dir_path, delete=False)
    run_rsync(git_clone_path / 'xml_inscriptions' / 'transcribed', temp_unified_inscriptions_dir_path, delete=False)
    run_rsync(temp_unified_inscriptions_dir_path, webserved_data_path / 'inscriptions', delete=True)
    return


def run_rsync(source_path: Path, destination_path: Path, delete: bool) -> None:
    """
    Runs rsync with explicit arguments and legacy mirror semantics.

    Called by: copy_files()
    """
    source = f'{source_path}/'
    command = ['rsync', '-avz']
    if delete:
        command.append('--delete')
    command.extend([source, str(destination_path)])
    log.debug(
        f'Running rsync; source_path, ``{source_path}``; destination_path, ``{destination_path}``; delete, ``{delete}``'
    )
    subprocess.run(command, check=True, text=True)
    return


def update_xinclude_references(inscriptions_path: Path) -> int:
    """
    Rewrites absolute resource includes in each flattened inscription XML file.

    Called by: prepare_public_data()
    """
    changed_file_count = 0
    for inscription_path in sorted(inscriptions_path.glob('*.xml')):
        original_xml = inscription_path.read_text(encoding='utf-8')
        updated_xml = rewrite_xinclude_text(original_xml)
        if updated_xml != original_xml:
            inscription_path.write_text(updated_xml, encoding='utf-8')
            changed_file_count += 1
            log.debug(f'Updated XInclude references; inscription_path, ``{inscription_path}``')
    return changed_file_count


def rewrite_xinclude_text(xml_text: str) -> str:
    """
    Applies USEP resource URL replacements to XML text.

    Called by: update_xinclude_references()
    """
    updated_xml = xml_text
    for absolute_url, relative_path in XINCLUDE_REPLACEMENTS.items():
        updated_xml = updated_xml.replace(absolute_url, relative_path)
    return updated_xml


def validate_inscription_corpus(inscriptions_path: Path) -> None:
    """
    Requires every source inscription XML file to be well-formed.

    Called by: prepare_public_data()
    """
    result: xml_validation.XMLDirectoryValidationResult = xml_validation.validate_xml_directory(inscriptions_path)
    log.info(
        f'Source XML validation completed; checked_count, ``{result.checked_count}``; '
        f'well_formed_count, ``{result.well_formed_count}``; failure_count, ``{len(result.failures)}``'
    )
    if result.failures:
        for failure in result.failures:
            log.error(
                f'Source XML validation failed; failure_path, ``{failure.path.as_posix()}``; error, ``{failure.error}``'
            )
        failure_count: int = len(result.failures)
        file_label: str = 'file' if failure_count == 1 else 'files'
        verb: str = 'is' if failure_count == 1 else 'are'
        failure_details: str = '; '.join(f'{failure.path.as_posix()}: {failure.error}' for failure in result.failures)
        raise xml_validation.XMLNotWellFormedError(
            f'Found {failure_count} source XML {file_label} that {verb} not well-formed: {failure_details}'
        )
    return


def prepare_public_data(*, validate_source_xml: bool) -> str:
    """
    Pulls, optionally validates, and publishes the current USEP data.

    Called by: process_incremental(), orphans.prepare_orphan_review(), reindex workflows
    """
    call_git_pull(settings.USEP_DATA_GIT_CLONED_DIR_PATH)
    log.info(f'Git pull completed; git_clone_path, ``{settings.USEP_DATA_GIT_CLONED_DIR_PATH}``')
    data_revision = read_git_revision(settings.USEP_DATA_GIT_CLONED_DIR_PATH)
    log.info(f'Public data revision selected; data_revision, ``{data_revision}``')
    if validate_source_xml:
        source_inscriptions_path = settings.USEP_DATA_GIT_CLONED_DIR_PATH / 'xml_inscriptions'
        validate_inscription_corpus(source_inscriptions_path)
    copy_files(
        settings.USEP_DATA_GIT_CLONED_DIR_PATH,
        settings.TEMP_UNIFIED_INSCRIPTIONS_DIR_PATH,
        settings.WEBSERVED_DATA_DIR_PATH,
    )
    log.info(f'USEP data copy completed; webserved_data_path, ``{settings.WEBSERVED_DATA_DIR_PATH}``')
    inscriptions_path = settings.WEBSERVED_DATA_DIR_PATH / 'inscriptions'
    changed_file_count = update_xinclude_references(inscriptions_path)
    log.info(
        f'XInclude normalization completed; inscriptions_path, ``{inscriptions_path}``; '
        f'changed_file_count, ``{changed_file_count}``'
    )
    return data_revision


def normalize_changed_path(file_path: str) -> str:
    """
    Normalizes a webhook path for dependency comparison.

    Called by: index_affecting_resources_changed()
    """
    normalized_path = Path(file_path).as_posix()
    if normalized_path.startswith('./'):
        normalized_path = normalized_path.removeprefix('./')
    return normalized_path


def index_affecting_resources_changed(
    changed_paths: list[str],
    *,
    data_revision: str = 'unavailable',
) -> bool:
    """
    Detects titles.xml or configured indexing-XSL dependency changes.

    Called by: process_incremental(), resource-classification tests
    """
    normalized_paths = {normalize_changed_path(file_path) for file_path in changed_paths}
    if 'resources/titles.xml' in normalized_paths:
        return True
    resource_paths = {file_path for file_path in normalized_paths if file_path.startswith('resources/')}
    if not resource_paths:
        return False

    indexing_stylesheets = [settings.SOLR_XSL_PATH, settings.TRANSCRIPTION_PARSER_XSL_PATH]
    try:
        dependencies = stylesheet_dependencies.discover_stylesheet_dependencies(indexing_stylesheets)
        dependency_paths = stylesheet_dependencies.relative_dependency_paths(
            dependencies,
            settings.WEBSERVED_DATA_DIR_PATH,
        )
    except stylesheet_dependencies.StylesheetDependencyError as error:
        changed_stylesheet = any(Path(file_path).suffix.lower() in {'.xsl', '.xslt'} for file_path in resource_paths)
        if changed_stylesheet:
            log.warning(
                f'Indexing stylesheet dependency discovery was uncertain; promoting to a full rebuild; '
                f'data_revision, ``{data_revision}``; error, ``{error}``'
            )
        return changed_stylesheet
    return bool(resource_paths & dependency_paths)


def process_incremental(files_to_update: list[str], files_to_remove: list[str]) -> None:
    """
    Pulls and copies USEP data, then applies incremental Solr changes.

    Called by: spool.process_valid_events()
    """
    log.info(
        f'Incremental processing started; files_to_update_count, ``{len(files_to_update)}``; '
        f'files_to_remove_count, ``{len(files_to_remove)}``'
    )
    data_revision = prepare_public_data(validate_source_xml=False)
    changed_paths = [*files_to_update, *files_to_remove]
    if index_affecting_resources_changed(changed_paths, data_revision=data_revision):
        log.info(
            f'Incremental resource change promoted to full Solr rebuild; data_revision, ``{data_revision}``; '
            f'changed_path_count, ``{len(changed_paths)}``'
        )
        from usep_indexer_app.lib import reindex

        reindex.process_prepared_full_reindex(data_revision=data_revision)
    else:
        log.info(
            f'Incremental Solr indexing started; files_to_update_count, ``{len(files_to_update)}``; '
            f'files_to_remove_count, ``{len(files_to_remove)}``; data_revision, ``{data_revision}``'
        )
        indexer.update_index(files_to_update, files_to_remove, data_revision=data_revision)
        log.info('Incremental Solr indexing completed.')
    return

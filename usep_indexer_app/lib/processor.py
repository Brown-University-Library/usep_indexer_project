import logging
import subprocess
from pathlib import Path

from django.conf import settings
from usep_indexer_app.lib import indexer


log = logging.getLogger(__name__)

XINCLUDE_REPLACEMENTS = {
    'http://library.brown.edu/usep_data/resources/include_publicationStmt.xml': '../resources/include_publicationStmt.xml',
    'http://library.brown.edu/usep_data/resources/include_taxonomies.xml': '../resources/include_taxonomies.xml',
    'http://library.brown.edu/usep_data/resources/titles.xml': '../resources/titles.xml',
}


def call_git_pull(git_clone_path: Path) -> None:
    """
    Runs git pull in the configured USEP data clone.

    Called by: process_incremental(), reindex.process_full_reindex()
    """
    subprocess.run(['git', 'pull'], cwd=git_clone_path, check=True, text=True)
    return


def copy_files(git_clone_path: Path, temp_data_path: Path, webserved_data_path: Path) -> None:
    """
    Mirrors resources and flattens the three inscription source directories.

    Called by: process_incremental(), reindex.process_full_reindex()
    """
    run_rsync(git_clone_path / 'resources', webserved_data_path / 'resources', delete=True)
    run_rsync(git_clone_path / 'xml_inscriptions' / 'bib_only', temp_data_path, delete=True)
    run_rsync(git_clone_path / 'xml_inscriptions' / 'metadata_only', temp_data_path, delete=False)
    run_rsync(git_clone_path / 'xml_inscriptions' / 'transcribed', temp_data_path, delete=False)
    run_rsync(temp_data_path, webserved_data_path / 'inscriptions', delete=True)
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
    subprocess.run(command, check=True, text=True)
    return


def update_xinclude_references(inscriptions_path: Path) -> int:
    """
    Rewrites absolute resource includes in each flattened inscription XML file.

    Called by: process_incremental(), reindex.process_full_reindex()
    """
    changed_file_count = 0
    for inscription_path in sorted(inscriptions_path.glob('*.xml')):
        original_xml = inscription_path.read_text(encoding='utf-8')
        updated_xml = rewrite_xinclude_text(original_xml)
        if updated_xml != original_xml:
            inscription_path.write_text(updated_xml, encoding='utf-8')
            changed_file_count += 1
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


def process_incremental(files_to_update: list[str], files_to_remove: list[str]) -> None:
    """
    Pulls and copies USEP data, then applies incremental Solr changes.

    Called by: spool.process_valid_events()
    """
    call_git_pull(settings.GIT_CLONED_DIR_PATH)
    copy_files(
        settings.GIT_CLONED_DIR_PATH,
        settings.TEMP_DATA_DIR_PATH,
        settings.WEBSERVED_DATA_DIR_PATH,
    )
    inscriptions_path = settings.WEBSERVED_DATA_DIR_PATH / 'inscriptions'
    update_xinclude_references(inscriptions_path)
    indexer.update_index(files_to_update, files_to_remove)
    return

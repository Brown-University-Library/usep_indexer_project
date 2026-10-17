"""
Collects inscription problems during one processor command and emails a complete, grouped report.

Reports exist only in memory. Raw exceptions and server responses stay in the operational logs.
"""

import contextlib
import datetime
import json
import logging
import re
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.mail import EmailMessage
from django.core.validators import validate_email


log = logging.getLogger(__name__)
DATA_REPOSITORY_URL = 'https://github.com/Brown-University-Library/usep-data'
ISSUE_DESCRIPTIONS = {
    'xml_parse': ('XML could not be read', 'Check the XML syntax and character encoding.'),
    'xml_transform': ('XML transformation failed', 'Review the inscription and the indexing stylesheets together.'),
    'id_mismatch': (
        'XML ID and filename differ',
        'The filename without .xml must match the ID produced by the indexing stylesheet. '
        'The standard stylesheet reads the xml:id attribute on publicationStmt/idno. '
        'Compare the quoted values below; spaces and escaped characters are significant.',
    ),
    'id_missing': ('Missing or repeated document ID', 'The search document must contain exactly one nonempty ID.'),
    'date_range': ('Date range ends before it begins', 'Check the notBefore and notAfter years.'),
    'invalid_year': ('Date is not an integer year', 'Check the notBefore and notAfter values and their transformation.'),
    'multiple_graphics': ('More than one image value', 'The search document allows only one graphic_name value.'),
    'multiple_values': ('Repeated single-value field', 'A field that permits one value has been emitted more than once.'),
    'transcription_status': (
        'Transcription status and extracted text disagree',
        'Review the edition XML and transcription stylesheet; the source may contain unsupported structures.',
    ),
    'document_validation': (
        'Search document did not pass validation',
        'The XML or stylesheet output needs review with the indexer maintainer.',
    ),
}


@dataclass(frozen=True)
class DataIssue:
    """
    Records an inscription filename, category, source path, and optional mismatched document ID.
    """

    filename: str
    category: str
    source_path: str | None
    document_id: str | None = None


@dataclass
class RunReport:
    """
    Holds counts and data issues for one command, independently of queue retries and stored errors.
    """

    started_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.UTC).isoformat())
    data_revision: str = 'unavailable'
    selected_count: int = 0
    accepted_count: int = 0
    failed_count: int = 0
    indexing_finished: bool = False
    issues: list[DataIssue] = field(default_factory=list)


_active_report: ContextVar[RunReport | None] = ContextVar('usep_data_issue_report', default=None)


def parse_recipients(raw_value: str, *, setting_name: str = 'USEP_DATA_TEAM_EMAILS_JSON') -> list[str]:
    """
    Validates the optional JSON list of team addresses without exposing its values in errors.

    Called by: config.settings
    """
    try:
        parsed = json.loads(raw_value)
        if not isinstance(parsed, list) or any(not isinstance(address, str) for address in parsed):
            raise ValueError('Expected a list of addresses.')
        recipients: list[str] = []
        for address in parsed:
            validate_email(address)
            if address not in recipients:
                recipients.append(address)
    except (ValueError, ValidationError) as error:
        raise ImproperlyConfigured(f'{setting_name} must be a JSON list of email addresses.') from error
    return recipients


@contextlib.contextmanager
def collect_report() -> Iterator[RunReport]:
    """
    Collects a fresh report and clears it even when the command fails.

    Called by: management.commands.process_spool.Command.handle()
    """
    report = RunReport()
    token = _active_report.set(report)
    try:
        yield report
    finally:
        _active_report.reset(token)


def start_indexing(data_revision: str, selected_count: int) -> None:
    """
    Records the revision and inscription count before document construction starts.

    Called by: indexer.build_complete_documents()
    """
    report = _active_report.get()
    if report is not None:
        report.data_revision = data_revision if re.fullmatch(r'[0-9a-fA-F]{7,40}', data_revision) else 'unavailable'
        report.selected_count += selected_count
    return


def source_path_for_filename(filename: str) -> str | None:
    """
    Finds the winning source file using the same directory order as inscription copying.

    Called by: record_failure()
    """
    source_path = None
    if filename == Path(filename).name and filename.endswith('.xml'):
        for directory in ('transcribed', 'metadata_only', 'bib_only'):
            candidate = Path('xml_inscriptions') / directory / filename
            if (settings.USEP_DATA_GIT_CLONED_DIR_PATH / candidate).is_file():
                source_path = candidate.as_posix()
                break
    return source_path


def record_failure(filename: str, category: str | None, *, document_id: str | None = None) -> None:
    """
    Counts each failed inscription and retains safe details only for recognized data problems.

    Called by: indexer.build_complete_documents(), indexer.post_document_or_record_failure()
    """
    report = _active_report.get()
    if report is not None:
        report.failed_count += 1
        if category is not None and category in ISSUE_DESCRIPTIONS:
            try:
                source_path = source_path_for_filename(filename)
            except OSError:
                log.warning('Unable to locate the repository source file for a data-issue email.')
                source_path = None
            report.issues.append(
                DataIssue(
                    Path(filename).name,
                    category,
                    source_path,
                    document_id=document_id if category == 'id_mismatch' else None,
                )
            )
    return


def record_accepted(count: int) -> None:
    """
    Counts documents in successfully acknowledged Solr updates.

    Called by: indexer.post_document_or_record_failure(), indexer.post_document_batches()
    """
    report = _active_report.get()
    if report is not None:
        report.accepted_count += count
    return


def finish_indexing() -> None:
    """
    Marks that all selected document updates were attempted, before deletion or failure reporting.

    Called by: indexer.update_index(), reindex.update_all_index_entries()
    """
    report = _active_report.get()
    if report is not None:
        report.indexing_finished = True
    return


def build_message(report: RunReport) -> str:
    """
    Renders all recognized failures using fixed explanations and public source links.

    Called by: email_report()
    """
    lines = [
        'USEP inscription processing found problems for review.',
        '',
        f'Run started (UTC): {report.started_at}',
        f'Data revision: {report.data_revision}',
        f'Inscriptions selected: {report.selected_count}',
        f'Inscriptions successfully accepted by Solr: {report.accepted_count}',
        f'Inscriptions with recorded failures: {report.failed_count}',
        f'Inscriptions listed for data review: {len(report.issues)}',
        '',
        'These findings may require XML, stylesheet, or indexer changes; they do not assign responsibility.',
        'Retries report the same unresolved problems again. This email does not mean new problems were introduced.',
    ]
    if not report.indexing_finished:
        lines.append('Processing stopped early. These counts and findings cover only the work completed so far.')
    if report.failed_count > len(report.issues):
        lines.append('Other failures require technical review and are included only in the administrator report.')
    for category, (title, explanation) in ISSUE_DESCRIPTIONS.items():
        issues = sorted((issue for issue in report.issues if issue.category == category), key=lambda issue: issue.filename)
        if issues:
            lines.extend(['', f'{title} ({len(issues)})', explanation])
            for issue in issues:
                inscription_id = ''.join(
                    character if character.isprintable() else ' ' for character in Path(issue.filename).stem
                )
                lines.append(f'- {inscription_id}')
                if category == 'id_mismatch' and issue.document_id is not None:
                    filename_id = json.dumps(Path(issue.filename).stem, ensure_ascii=False)
                    document_id = json.dumps(issue.document_id, ensure_ascii=False)
                    lines.append(f'  {filename_id} — Filename ID')
                    lines.append(f'  {document_id} — XML-derived Solr ID')
                if issue.source_path and report.data_revision != 'unavailable':
                    source_url = f'{DATA_REPOSITORY_URL}/blob/{report.data_revision}/{quote(issue.source_path, safe="/")}'
                    lines.append(f'  XML: {source_url}')
                else:
                    lines.append('  An exact source link is unavailable; ask the indexer maintainer to locate this file.')
    return '\n'.join(lines) + '\n'


def email_report(report: RunReport) -> None:
    """
    Sends one optional team email without hiding indexing failures if email delivery fails.

    Called by: management.commands.process_spool.Command.handle()
    """
    recipients = getattr(settings, 'USEP_DATA_TEAM_EMAILS', [])
    if recipients and report.issues:
        try:
            label = ' '.join(str(getattr(settings, 'USEP_DATA_TEAM_EMAIL_LABEL', 'USEP indexer')).split())
            message = EmailMessage(
                subject=f'[{label}] USEP data review: {len(report.issues)} inscription(s)',
                body=build_message(report),
                from_email=settings.SERVER_EMAIL,
                to=recipients,
                cc=getattr(settings, 'USEP_DATA_TEAM_CC_EMAILS', []),
            )
            message.send(fail_silently=False)
        except Exception:
            log.exception('Unable to email the USEP data team about inscription problems.')
    return

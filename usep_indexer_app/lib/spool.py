"""
Implements the durable filesystem-backed queue between web requests and indexing work.

This module owns the event schema, atomic writes and lifecycle transitions, batch coalescing, retry
handling, validation and isolation of malformed event-data, non-overlapping processing, retention
cleanup, and processor-health data.
"""

import datetime
import fcntl
import json
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from django.conf import settings

from usep_indexer_app.lib import processor, reindex


log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
EVENT_TYPES = {'incremental', 'full_reindex'}
EVENT_KEYS = {
    'schema_version',
    'event_id',
    'event_type',
    'received_at',
    'request_id',
    'files_updated',
    'files_removed',
    'attempts',
    'last_attempt_at',
    'last_error',
}
LIFECYCLE_DIRECTORIES = ('pending', 'processing', 'completed', 'failed', 'quarantine')
MAX_ERROR_LENGTH = 2000


@dataclass(frozen=True)
class SpoolEvent:
    """
    Represents one validated filesystem-queue event.

    Called by: load_event(), coalesce_events(), record_event_failure()
    """

    path: Path
    schema_version: int
    event_id: str
    event_type: str
    received_at: str
    request_id: str
    files_updated: list[str]
    files_removed: list[str]
    attempts: int
    last_attempt_at: str | None
    last_error: str | None


@dataclass(frozen=True)
class ProcessResult:
    """
    Summarizes one filesystem-queue processor invocation.

    Called by: process_spool()
    """

    status: str
    claimed: int = 0
    processed: int = 0
    retried: int = 0
    failed: int = 0
    quarantined: int = 0
    cleaned: int = 0
    error: str | None = None


def utc_now() -> datetime.datetime:
    """
    Returns the current timezone-aware UTC time.

    Called by: build_event_document(), record_event_failure(), process_spool()
    """
    current_time = datetime.datetime.now(datetime.UTC)
    return current_time


def ensure_spool_directories(spool_root: Path) -> None:
    """
    Creates the filesystem-queue lifecycle directories when absent.

    Called by: write_event(), process_spool()
    """
    spool_root.mkdir(parents=True, exist_ok=True)
    for directory_name in LIFECYCLE_DIRECTORIES:
        (spool_root / directory_name).mkdir(exist_ok=True)
    return


def build_event_document(
    event_type: str,
    files_updated: list[str] | None = None,
    files_removed: list[str] | None = None,
    request_id: str | None = None,
) -> dict[str, object]:
    """
    Builds a versioned event document for a durable queue write.

    Called by: write_event()
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f'Unsupported event type: {event_type}')
    event_id = str(uuid.uuid4())
    received_at = utc_now().isoformat()
    document: dict[str, object] = {
        'schema_version': SCHEMA_VERSION,
        'event_id': event_id,
        'event_type': event_type,
        'received_at': received_at,
        'request_id': request_id or event_id,
        'files_updated': list(files_updated or []),
        'files_removed': list(files_removed or []),
        'attempts': 0,
        'last_attempt_at': None,
        'last_error': None,
    }
    validate_event_document(document, Path(f'{event_id}.json'))
    return document


def format_local_filename_timestamp(received_at: str) -> str:
    """
    Formats an event's UTC receipt time as a local 24-hour filename timestamp.

    Called by: write_event()
    """
    received_at_datetime = datetime.datetime.fromisoformat(received_at)
    local_received_at = received_at_datetime.astimezone(ZoneInfo(settings.TIME_ZONE))
    filename_timestamp = local_received_at.strftime('%Y%m%dT%H%M%S.%f%z')
    return filename_timestamp


def write_event(
    spool_root: Path,
    event_type: str,
    files_updated: list[str] | None = None,
    files_removed: list[str] | None = None,
    request_id: str | None = None,
) -> Path:
    """
    Atomically saves one event in the pending queue directory.

    Called by: views.handle_github_push(), views.reindex_all()
    """
    log.debug(
        f'spool_root, ``{spool_root}``; event_type, ``{event_type}``; request_id, ``{request_id or "not_provided"}``; '
        f'files_updated, ``{files_updated or []}``; files_removed, ``{files_removed or []}``'
    )
    ensure_spool_directories(spool_root)
    document = build_event_document(event_type, files_updated, files_removed, request_id)
    event_id = str(document['event_id'])
    received_at = str(document['received_at'])
    filename_timestamp = format_local_filename_timestamp(received_at)
    event_path = spool_root / 'pending' / f'{filename_timestamp}_{event_id}.json'
    write_json_atomic(document, event_path)
    log.info(
        f'event saved; event_type, ``{event_type}``; request_id, ``{document["request_id"]}``; event_path, ``{event_path}``'
    )
    return event_path


def write_json_atomic(document: dict[str, object], destination_path: Path) -> None:
    """
    Writes, synchronizes, and atomically replaces one JSON document.

    Called by: write_event(), record_event_failure(), write_processor_status()
    """
    temporary_path = destination_path.parent / f'.{destination_path.name}.{uuid.uuid4().hex}.tmp'
    try:
        with temporary_path.open('x', encoding='utf-8') as temporary_file:
            json.dump(document, temporary_file, indent=2, sort_keys=True)
            temporary_file.write('\n')
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, destination_path)
        sync_directory(destination_path.parent)
    finally:
        temporary_path.unlink(missing_ok=True)
    return


def sync_directory(directory_path: Path) -> None:
    """
    Synchronizes directory metadata after an atomic rename.

    Called by: write_json_atomic(), move_event()
    """
    directory_descriptor = os.open(directory_path, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    return


def validate_event_document(document: object, event_path: Path) -> SpoolEvent:
    """
    Validates an event document and returns its typed representation.

    Called by: build_event_document(), load_event()
    """
    if not isinstance(document, dict):
        raise ValueError('Event document must be a JSON object.')
    if set(document) != EVENT_KEYS:
        raise ValueError('Event document fields do not match the spool schema.')

    schema_version = document['schema_version']
    event_id = document['event_id']
    event_type = document['event_type']
    received_at = document['received_at']
    request_id = document['request_id']
    files_updated = document['files_updated']
    files_removed = document['files_removed']
    attempts = document['attempts']
    last_attempt_at = document['last_attempt_at']
    last_error = document['last_error']

    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version != SCHEMA_VERSION:
        raise ValueError(f'Unsupported spool schema version: {schema_version}')
    if not isinstance(event_id, str):
        raise ValueError('Event ID must be a string UUID.')
    uuid.UUID(event_id)
    if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
        raise ValueError(f'Unsupported event type: {event_type}')
    require_timestamp(received_at, 'received_at')
    if not isinstance(request_id, str) or not request_id:
        raise ValueError('Request ID must be a non-empty string.')
    validated_updated = require_path_list(files_updated, 'files_updated')
    validated_removed = require_path_list(files_removed, 'files_removed')
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
        raise ValueError('Attempts must be a non-negative integer.')
    if last_attempt_at is not None:
        require_timestamp(last_attempt_at, 'last_attempt_at')
    if last_error is not None and not isinstance(last_error, str):
        raise ValueError('Last error must be a string or null.')
    if isinstance(last_error, str) and len(last_error) > MAX_ERROR_LENGTH:
        raise ValueError('Last error exceeds the spool schema length limit.')
    if event_type == 'full_reindex' and (validated_updated or validated_removed):
        raise ValueError('Full-reindex events cannot contain incremental paths.')

    event = SpoolEvent(
        path=event_path,
        schema_version=schema_version,
        event_id=event_id,
        event_type=event_type,
        received_at=received_at,
        request_id=request_id,
        files_updated=validated_updated,
        files_removed=validated_removed,
        attempts=attempts,
        last_attempt_at=last_attempt_at,
        last_error=last_error,
    )
    return event


def require_timestamp(value: object, field_name: str) -> str:
    """
    Validates a timezone-aware ISO-8601 timestamp string.

    Called by: validate_event_document()
    """
    if not isinstance(value, str):
        raise ValueError(f'{field_name} must be a timestamp string.')
    parsed_timestamp = datetime.datetime.fromisoformat(value)
    if parsed_timestamp.utcoffset() is None:
        raise ValueError(f'{field_name} must include a timezone offset.')
    if parsed_timestamp.utcoffset() != datetime.timedelta(0):
        raise ValueError(f'{field_name} must use UTC.')
    return value


def require_path_list(value: object, field_name: str) -> list[str]:
    """
    Validates a spool event path list.

    Called by: validate_event_document()
    """
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f'{field_name} must contain path strings.')
    return list(value)


def load_event(event_path: Path) -> SpoolEvent:
    """
    Loads and validates one claimed event file.

    Called by: validate_claimed_events()
    """
    document = json.loads(event_path.read_text(encoding='utf-8'))
    event = validate_event_document(document, event_path)
    return event


def event_sort_key(event: SpoolEvent) -> tuple[datetime.datetime, str]:
    """
    Returns the chronological ordering key for one event.

    Called by: coalesce_events()
    """
    key = (datetime.datetime.fromisoformat(event.received_at), event.event_id)
    return key


def claim_events(spool_root: Path, batch_size: int) -> list[Path]:
    """
    Returns crash-recovery files and atomically claims pending events.

    Called by: process_locked_spool()
    """
    if batch_size < 1:
        raise ValueError('Spool batch size must be at least one.')
    processing_directory = spool_root / 'processing'
    pending_directory = spool_root / 'pending'
    claimed_paths = sorted(processing_directory.glob('*.json'))[:batch_size]
    remaining_capacity = batch_size - len(claimed_paths)
    for pending_path in sorted(pending_directory.glob('*.json'))[:remaining_capacity]:
        processing_path = processing_directory / pending_path.name
        os.replace(pending_path, processing_path)
        claimed_paths.append(processing_path)
    if claimed_paths:
        sync_directory(pending_directory)
        sync_directory(processing_directory)
    return claimed_paths


def validate_claimed_events(claimed_paths: list[Path], spool_root: Path) -> tuple[list[SpoolEvent], int]:
    """
    Loads valid claimed events and quarantines invalid documents.

    Called by: process_locked_spool()
    """
    valid_events: list[SpoolEvent] = []
    quarantined_count = 0
    for event_path in claimed_paths:
        try:
            valid_events.append(load_event(event_path))
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, OSError):
            log.exception('Quarantining invalid spool event %s.', event_path)
            move_event(event_path, spool_root / 'quarantine')
            quarantined_count += 1
    return valid_events, quarantined_count


def coalesce_events(events: list[SpoolEvent]) -> tuple[list[str], list[str], bool]:
    """
    Coalesces event paths with newest-event-wins semantics.

    Called by: process_locked_spool()
    """
    path_states: dict[str, str] = {}
    full_reindex = False
    for event in sorted(events, key=event_sort_key):
        if event.event_type == 'full_reindex':
            full_reindex = True
        for file_path in event.files_updated:
            path_states[file_path] = 'updated'
        for file_path in event.files_removed:
            path_states[file_path] = 'removed'
    files_updated = sorted(path for path, state in path_states.items() if state == 'updated')
    files_removed = sorted(path for path, state in path_states.items() if state == 'removed')
    return files_updated, files_removed, full_reindex


def process_valid_events(events: list[SpoolEvent]) -> None:
    """
    Runs the synchronous full or incremental workflow for a batch.

    Called by: process_locked_spool()
    """
    files_updated, files_removed, full_reindex = coalesce_events(events)
    workflow = 'full_reindex' if full_reindex else 'incremental'
    log.info(
        f'Filesystem-queue batch processing started; event_count, ``{len(events)}``; workflow, ``{workflow}``; '
        f'files_updated_count, ``{len(files_updated)}``; files_removed_count, ``{len(files_removed)}``'
    )
    log.debug(f'Coalesced filesystem-queue paths; files_updated, ``{files_updated}``; files_removed, ``{files_removed}``')
    if full_reindex:
        reindex.process_full_reindex()
    else:
        processor.process_incremental(files_updated, files_removed)
    log.info(f'Filesystem-queue batch processing completed; event_count, ``{len(events)}``; workflow, ``{workflow}``')
    return


def record_event_failure(event: SpoolEvent, spool_root: Path, max_attempts: int, error: Exception) -> str:
    """
    Records a failed attempt and moves an event for retry or operator action.

    Called by: process_locked_spool()
    """
    attempts = event.attempts + 1
    document: dict[str, object] = {
        'schema_version': event.schema_version,
        'event_id': event.event_id,
        'event_type': event.event_type,
        'received_at': event.received_at,
        'request_id': event.request_id,
        'files_updated': event.files_updated,
        'files_removed': event.files_removed,
        'attempts': attempts,
        'last_attempt_at': utc_now().isoformat(),
        'last_error': f'{type(error).__name__}: {error}'[:MAX_ERROR_LENGTH],
    }
    write_json_atomic(document, event.path)
    destination_name = 'failed' if attempts >= max_attempts else 'pending'
    move_event(event.path, spool_root / destination_name)
    return destination_name


def move_event(event_path: Path, destination_directory: Path) -> Path:
    """
    Atomically moves an event into a lifecycle directory.

    Called by: validate_claimed_events(), process_locked_spool(), record_event_failure()
    """
    destination_path = destination_directory / event_path.name
    os.replace(event_path, destination_path)
    sync_directory(event_path.parent)
    sync_directory(destination_directory)
    return destination_path


def clean_completed_events(spool_root: Path, retention_days: int, current_time: datetime.datetime | None = None) -> int:
    """
    Removes completed events older than the configured retention period.

    Called by: process_locked_spool()
    """
    if retention_days < 0:
        raise ValueError('Completed-event retention days cannot be negative.')
    comparison_time = current_time or utc_now()
    cutoff_timestamp = comparison_time.timestamp() - (retention_days * 24 * 60 * 60)
    cleaned_count = 0
    for completed_path in (spool_root / 'completed').glob('*.json'):
        if completed_path.stat().st_mtime < cutoff_timestamp:
            completed_path.unlink()
            cleaned_count += 1
    if cleaned_count:
        sync_directory(spool_root / 'completed')
    return cleaned_count


def write_processor_status(spool_root: Path, status_document: dict[str, object]) -> None:
    """
    Atomically saves the latest processor status document.

    Called by: process_spool()
    """
    write_json_atomic(status_document, spool_root / 'processor-status.json')
    return


def process_locked_spool(
    spool_root: Path,
    batch_size: int,
    max_attempts: int,
    retention_days: int,
) -> ProcessResult:
    """
    Claims, validates, processes, and transitions one event batch.

    Called by: process_spool()
    """
    claimed_paths = claim_events(spool_root, batch_size)
    valid_events, quarantined_count = validate_claimed_events(claimed_paths, spool_root)
    processed_count = 0
    retried_count = 0
    failed_count = 0
    processing_error: Exception | None = None

    if valid_events:
        try:
            process_valid_events(valid_events)
        except Exception as error:
            processing_error = error
            log.exception('Filesystem-queue batch processing failed.')
            for event in valid_events:
                destination_name = record_event_failure(event, spool_root, max_attempts, error)
                if destination_name == 'failed':
                    failed_count += 1
                else:
                    retried_count += 1
        else:
            for event in valid_events:
                os.utime(event.path)
                move_event(event.path, spool_root / 'completed')
                processed_count += 1

    cleaned_count = clean_completed_events(spool_root, retention_days)
    status = 'failed' if processing_error else 'success'
    error_summary = None
    if processing_error:
        error_summary = f'{type(processing_error).__name__}: {processing_error}'[:MAX_ERROR_LENGTH]
    result = ProcessResult(
        status=status,
        claimed=len(claimed_paths),
        processed=processed_count,
        retried=retried_count,
        failed=failed_count,
        quarantined=quarantined_count,
        cleaned=cleaned_count,
        error=error_summary,
    )
    return result


def process_spool(spool_root: Path, batch_size: int, max_attempts: int, retention_days: int) -> ProcessResult:
    """
    Runs one non-overlapping filesystem-queue processor invocation.

    Called by: management.commands.process_spool.Command.handle()
    """
    if max_attempts < 1:
        raise ValueError('Spool maximum attempts must be at least one.')
    ensure_spool_directories(spool_root)
    started_at = utc_now()
    lock_path = spool_root / 'processor.lock'
    result = ProcessResult(status='locked')
    with lock_path.open('a+', encoding='utf-8') as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info('Another filesystem-queue processor holds %s.', lock_path)
        else:
            write_processor_status(
                spool_root,
                {
                    'status': 'running',
                    'started_at': started_at.isoformat(),
                    'finished_at': None,
                    'claimed': 0,
                    'processed': 0,
                    'retried': 0,
                    'failed': 0,
                    'quarantined': 0,
                    'cleaned': 0,
                    'error': None,
                },
            )
            try:
                result = process_locked_spool(spool_root, batch_size, max_attempts, retention_days)
            except Exception as error:
                log.exception('Unexpected filesystem-queue processor failure.')
                result = ProcessResult(
                    status='failed',
                    error=f'{type(error).__name__}: {error}'[:MAX_ERROR_LENGTH],
                )
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            finished_at = utc_now()
            write_processor_status(
                spool_root,
                {
                    'status': result.status,
                    'started_at': started_at.isoformat(),
                    'finished_at': finished_at.isoformat(),
                    'claimed': result.claimed,
                    'processed': result.processed,
                    'retried': result.retried,
                    'failed': result.failed,
                    'quarantined': result.quarantined,
                    'cleaned': result.cleaned,
                    'error': result.error,
                },
            )
            elapsed_seconds = (finished_at - started_at).total_seconds()
            log.info(
                'Filesystem-queue processor status=%s claimed=%s processed=%s retried=%s failed=%s '
                'quarantined=%s cleaned=%s elapsed_seconds=%.3f.',
                result.status,
                result.claimed,
                result.processed,
                result.retried,
                result.failed,
                result.quarantined,
                result.cleaned,
                elapsed_seconds,
            )
    return result


def count_event_files(directory_path: Path) -> int:
    """
    Counts JSON event files in one lifecycle directory.

    Called by: get_processor_health()
    """
    count = len(list(directory_path.glob('*.json'))) if directory_path.is_dir() else 0
    return count


def get_oldest_pending_age(spool_root: Path, current_time: datetime.datetime) -> float | None:
    """
    Returns the age in seconds of the oldest pending event.

    Called by: get_processor_health()
    """
    pending_paths = list((spool_root / 'pending').glob('*.json')) if (spool_root / 'pending').is_dir() else []
    oldest_age: float | None = None
    if pending_paths:
        oldest_timestamp = min(path.stat().st_mtime for path in pending_paths)
        oldest_age = max(0.0, current_time.timestamp() - oldest_timestamp)
    return oldest_age


def get_processor_health(
    spool_root: Path,
    maximum_age_seconds: int,
    current_time: datetime.datetime | None = None,
) -> dict[str, object]:
    """
    Reports recent processor status and filesystem-queue backlog counts.

    Called by: daemon.check_daemon()
    """
    comparison_time = current_time or utc_now()
    status_path = spool_root / 'processor-status.json'
    processor_status = 'missing'
    started_at: str | None = None
    finished_at: str | None = None
    is_fresh_success = False
    if status_path.is_file():
        try:
            status_document = json.loads(status_path.read_text(encoding='utf-8'))
            if not isinstance(status_document, dict):
                raise ValueError('Processor status must be a JSON object.')
            processor_status = str(status_document.get('status', 'invalid'))
            started_at_value = status_document.get('started_at')
            finished_at_value = status_document.get('finished_at')
            started_at = started_at_value if isinstance(started_at_value, str) else None
            finished_at = finished_at_value if isinstance(finished_at_value, str) else None
            reference_timestamp = finished_at or started_at
            if reference_timestamp:
                status_time = datetime.datetime.fromisoformat(reference_timestamp)
                status_age = (comparison_time - status_time).total_seconds()
                is_fresh_success = processor_status in {'running', 'success'} and status_age <= maximum_age_seconds
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError, OSError):
            log.exception('Unable to read filesystem-queue processor status.')
            processor_status = 'invalid'

    health: dict[str, object] = {
        'result': 'daemon_active' if is_fresh_success else 'daemon_not_active',
        'processor_status': processor_status,
        'last_started_at': started_at,
        'last_finished_at': finished_at,
        'pending_count': count_event_files(spool_root / 'pending'),
        'processing_count': count_event_files(spool_root / 'processing'),
        'failed_count': count_event_files(spool_root / 'failed'),
        'quarantine_count': count_event_files(spool_root / 'quarantine'),
        'oldest_pending_age_seconds': get_oldest_pending_age(spool_root, comparison_time),
    }
    return health

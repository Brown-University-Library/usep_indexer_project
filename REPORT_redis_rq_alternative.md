# Implementation plan: replace Redis/RQ with a filesystem-backed work queue

Updated: 2026-07-10

## Table of contents

- [Decision summary](#decision-summary)
- [Terminology](#terminology)
- [Confirmed deployment and operating assumptions](#confirmed-deployment-and-operating-assumptions)
- [Target architecture](#target-architecture)
- [Event schema](#event-schema)
- [Event lifecycle](#event-lifecycle)
- [Processing workflows](#processing-workflows)
- [Failure recovery and retention](#failure-recovery-and-retention)
- [Health and observability](#health-and-observability)
- [Configuration and operations](#configuration-and-operations)
- [Implementation work](#implementation-work)
- [Verification and acceptance criteria](#verification-and-acceptance-criteria)
- [Deployment handoff checklist](#deployment-handoff-checklist)
- [Issues and decisions to consider in the future](#issues-and-decisions-to-consider-in-the-future)
- [Tradeoffs](#tradeoffs)

## Decision summary

Redis and RQ will be replaced directly rather than retained for a shadow period or emergency fallback. GitHub webhook events and full-reindex requests will be saved to a durable, filesystem-backed work queue. A Django management command, invoked by cron every other minute, will claim and process the queued work synchronously.

This is a good fit because the workload is low-frequency and batch-oriented, while the shared git clone, staging tree, web-served tree, and Solr core already require serialized access. Closely spaced incremental events can be coalesced into one git pull, copy, XInclude rewrite, and set of Solr changes.

The deployment is a single production host. Django and cron run under the same account and use the same local POSIX filesystem, which supports atomic rename and `flock`. Those confirmed constraints are part of the design, not incidental implementation details.

## Terminology

In this plan, **spool** means the durable, filesystem-backed work queue. It is not a temporary scratch area.

- **Spool directory:** the directory tree containing queued event files and their lifecycle directories.
- **Spool schema:** the expected JSON structure and validation rules for each event file.
- **Spool writes:** saving new webhook or administrative events atomically into `pending/`.
- **Spool processor:** the cron-invoked Django management command that claims and processes event files.
- **Spool loss:** losing queued event files before they have been processed successfully.
- **Event:** one JSON file representing an accepted incremental webhook/force request or a requested full reindex.
- **Claim:** an atomic rename of an event from `pending/` to `processing/`, assigning it to the locked processor invocation.

## Confirmed deployment and operating assumptions

- One production host runs both the Django service and the cron command.
- Both processes use the same local filesystem and the same operating-system account.
- Atomic rename is available when source and destination are inside the spool directory tree.
- Advisory `flock` locking is available and reliable on that filesystem.
- Cron invokes the processor every other minute. This intentionally provides a short coalescing window during bursts of updates.
- Processing is serial. Per-inscription parallelism will not be recreated.
- An event receives at most three processing attempts before it is moved to `failed/`.
- Completed events are retained for 30 days.
- Failed and quarantined events are retained until an operator resolves them.
- These cadence, retry, and retention values are configuration defaults so they can be tuned after testing and production measurement.

## Target architecture

The configured spool directory has this layout:

```text
spool/
    pending/
    processing/
    completed/
    failed/
    quarantine/
    processor.lock
    processor-status.json
```

One event per file is safer than a shared append-only JSONL file. It avoids coordination around partial lines and file rotation and makes individual events easy to inspect, replay, or quarantine.

The request path is:

1. The view parses and minimally validates request input.
2. A library helper constructs the versioned event document.
3. The helper writes a uniquely named temporary file in `pending/`, flushes and `fsync`s it, atomically renames it to its final `.json` name, and synchronizes the directory.
4. Only after the durable write succeeds does the endpoint return its normal acknowledgement. A failed spool write returns a service error so GitHub or an operator can retry.

The processing path is:

1. Cron invokes `uv run ./manage.py process_spool` every other minute.
2. The command attempts a non-blocking exclusive `flock` on `processor.lock`. Lock contention exits cleanly without starting a second processor.
3. The locked processor includes recoverable files already in `processing/`, then atomically claims a bounded batch from `pending/`.
4. It validates each file against the spool schema. Malformed or unsupported files move to `quarantine/` without blocking valid events.
5. It coalesces valid incremental paths in event order. The newest event determines whether each path is updated or removed.
6. If the batch contains a full-reindex event, the batch uses the full-reindex workflow; otherwise it uses the coalesced incremental workflow.
7. Successful files move to `completed/`. A failed batch is retried or moved to `failed/` according to its attempt count.
8. The invocation updates processor status, logs structured counts and timings, and removes completed files older than the configured retention period.

## Event schema

Schema version 1 contains:

```json
{
  "schema_version": 1,
  "event_id": "UUID",
  "event_type": "incremental",
  "received_at": "UTC ISO-8601 timestamp",
  "request_id": "GitHub delivery ID or generated UUID",
  "files_updated": ["xml_inscriptions/transcribed/example.xml"],
  "files_removed": [],
  "attempts": 0,
  "last_attempt_at": null,
  "last_error": null
}
```

`event_type` is either `incremental` or `full_reindex`. Full-reindex events have empty path lists. The identity, type, received timestamp, request ID, and path lists are the immutable event payload. Retry bookkeeping fields may be updated through the same atomic file-replacement procedure.

Validation rejects unsupported schema versions, unknown event types, invalid IDs or timestamps, non-list path fields, non-string paths, negative attempt counts, and invalid retry metadata. Invalid event files go to `quarantine/`; they do not consume retry attempts because they cannot safely enter a processing workflow.

## Event lifecycle

```text
request -> pending -> processing -> completed
                         |             |
                         |             +-> deleted after retention period
                         |
                         +-> pending (attempts remain below limit)
                         +-> failed  (attempt limit reached)
                         +-> quarantine (invalid schema)
```

Files left in `processing/` after a process crash are included in the next locked invocation. Because a crash may occur after some external side effects but before lifecycle moves finish, all processing operations must remain convergent when repeated.

## Processing workflows

For an incremental batch, the processor:

1. Coalesces updated and removed paths, with each newer event replacing the earlier state for the same path.
2. Runs one `git pull` in the configured USEP clone.
3. Runs one full resource/inscription copy and one XInclude rewrite pass.
4. Removes the coalesced deleted inscription IDs from Solr.
5. Synchronously indexes each coalesced updated inscription.

For a batch containing a full-reindex event, the processor:

1. Runs one `git pull`, one full copy, and one XInclude rewrite pass.
2. Builds the complete web-served inscription list.
3. Queries Solr for IDs absent from that list and removes them.
4. Synchronously indexes every inscription in stable order.

The existing pure git, copy, rewrite, transform, and Solr helpers remain reusable. RQ-specific chaining and fan-out are replaced with explicit synchronous orchestration in library modules; Django views remain thin.

## Failure recovery and retention

- One failure of the shared workflow applies to every valid event in that batch because the events were coalesced into one unit of work.
- Failed events are atomically rewritten with an incremented `attempts`, a UTC `last_attempt_at`, and a bounded error summary.
- Events below the three-attempt limit return to `pending/`; events reaching the limit move to `failed/`.
- Invalid JSON and invalid-schema documents move to `quarantine/` and are logged.
- Existing files in `processing/` are replayed on the next invocation after a crash.
- Completed-event cleanup uses file age and the 30-day default retention setting.
- Failed and quarantined files are never automatically deleted.
- Total spool loss remains an operational data-loss event. GitHub redelivery or manual/full reindex is the recovery source; backup requirements remain an operator decision.

## Health and observability

The processor maintains an atomically written `processor-status.json` containing invocation timestamps, status, counts, and an error summary when applicable. The existing source-IP-protected `/daemon_check/` endpoint retains its legacy top-level result values while deriving health from recent processor status rather than an RQ worker registry. It also reports backlog information such as pending count and oldest-pending age.

Logs include claimed, quarantined, completed, retried, failed, and retention-cleanup counts. Operational monitoring should alert on stale processor status, oldest-pending age, failed/quarantined events, and spool disk usage.

## Configuration and operations

The Django settings and dotenv example define:

- `SPOOL_ROOT_PATH`
- `SPOOL_MAX_ATTEMPTS` with default `3`
- `SPOOL_BATCH_SIZE` with a conservative default that can be tuned
- `SPOOL_COMPLETED_RETENTION_DAYS` with default `30`
- `SPOOL_HEALTH_MAX_AGE_SECONDS` allowing more than two cron intervals

The production cron entry should run every other minute:

```cron
*/2 * * * * cd /path/to/usep_indexer_project && uv run ./manage.py process_spool
```

The non-blocking processor lock makes overlapping cron invocations safe. Redis settings, the worker runner, and the `redis` and `rq` dependencies are removed as part of the direct cutover.

## Implementation work

1. Add a filesystem-queue library module for directory setup, schema creation and validation, atomic writes, claims, coalescing, retries, quarantine, status, health, and retention.
2. Add synchronous incremental and full-reindex orchestrators that reuse existing business-logic helpers.
3. Add the `process_spool` Django management command and non-blocking lock behavior.
4. Change webhook, force, and full-reindex views to create durable events instead of RQ jobs.
5. Change the daemon-check helper to inspect processor status and backlog.
6. Add spool configuration to normal and test settings and to the dotenv example.
7. Remove Redis/RQ imports, queue helpers, worker runner, dependencies, and documentation.
8. Add focused Django tests and update existing RQ-oriented tests to assert filesystem-queue behavior.
9. Run the repository test runner and Ruff checks, then reconcile documentation with the implemented behavior.

## Verification and acceptance criteria

- Atomic event creation produces a complete schema-valid file only in `pending/` and leaves no temporary file after success.
- A spool-write failure does not return a successful webhook or reindex acknowledgement.
- Concurrent processor invocation cannot acquire the lock and exits without claiming work.
- Multiple events coalesce with newest-event-wins semantics.
- A full-reindex event selects the full workflow for its batch.
- Malformed and unsupported event files move to `quarantine/` while valid files continue.
- A workflow failure retries events and the third failure moves them to `failed/`.
- Files left in `processing/` are replayed by a later invocation.
- Successful events move to `completed/`, and expired completed files are removed.
- Health output reflects fresh/stale processor status and includes pending backlog data.
- No application import, setting, dependency, command, or documentation still requires Redis or RQ.
- `uv run ./run_tests.py` passes.
- Ruff formatting and lint checks pass for the changed Python files.

## Deployment handoff checklist

1. Provision `SPOOL_ROOT_PATH` on durable local storage with capacity monitoring and ownership that allows the Django and cron processes to create, rename, synchronize, and delete files.
2. Add the spool settings to the production environment, retaining the defaults initially except for any deployment-specific batch or health thresholds.
3. Drain or otherwise account for every job in the old RQ queue before switching the web code; the direct-replacement code does not read legacy Redis jobs.
4. Deploy the locked dependencies and application code with `uv sync --locked`.
5. Install the every-other-minute cron entry under the same account as Django and confirm that its working directory, settings, environment, and log destination are correct.
6. Stop the RQ worker when the legacy queue is drained. Redis can remain available during deployment verification, but the new application has no Redis/RQ dependency or fallback path.
7. Send a controlled webhook or use `/force/`, confirm that one event appears in `pending/`, allow cron to process it, and confirm its move to `completed/`.
8. Check `/daemon_check/` after the first cron invocation and verify fresh processor status, expected backlog counts, and no failed or quarantined events.
9. Trigger `/reindex_all/` if a final convergence pass is desired, then verify the Solr result and processor status before considering the cutover complete.

## Issues and decisions to consider in the future

- Tune the two-minute cadence, batch-size limit, three-attempt limit, 30-day retention, and health-age threshold using observed production traffic and duration.
- Decide whether completed events need backup or whether their 30-day audit window is sufficient.
- Decide whether the spool directory itself needs backup. Without it, spool loss requires GitHub redelivery or an operator-triggered full reindex.
- Establish alert thresholds and the mechanism used to monitor stale status, oldest-pending age, failed/quarantined counts, and disk capacity.
- Add operator commands for listing, inspecting, replaying, and explicitly discarding failed or quarantined events if direct file operations prove too error-prone.
- Decide whether resource-only changes should trigger a full Solr reindex. The current incremental contract copies resource changes but indexes only changed inscription paths.
- Revisit webhook authentication and validation, including GitHub signature verification, independently of the queue replacement.
- Revisit legacy GET support and the query-driven orphan deletion flow in a later API revision.
- Reconsider this architecture before moving to multiple web hosts, separate Unix accounts, shared/network storage, containers with ephemeral filesystems, or active-active processing.
- Measure whether sequential full reindexing is operationally acceptable; if not, add deliberately bounded concurrency without weakening the global workflow lock.

## Tradeoffs

Advantages:

- Removes the Redis service, continuously running RQ worker, and the production Redis-version dependency constraint.
- Produces inspectable queue state using ordinary files.
- Fits the serialized, batch-oriented workflow and coalesces bursts naturally.
- Keeps the service database-free and reduces Python dependency coupling.

Costs:

- Processing latency is governed by the cron interval.
- Retry, quarantine, health, retention, locking, and disk monitoring become application responsibilities.
- Disk-full or permission failures prevent durable acceptance and must surface as request failures.
- Per-inscription parallelism is removed.
- The design is intentionally single-host and relies on the confirmed local-filesystem guarantees.

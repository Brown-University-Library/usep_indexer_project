# Feasibility report: replacing Redis/RQ with a filesystem spool and cron

Generated: 2026-07-09

## Table of contents

- [Executive assessment](#executive-assessment)
- [Why this is a good candidate](#why-this-is-a-good-candidate)
- [Recommended design](#recommended-design)
- [Why not one append-only queue file](#why-not-one-append-only-queue-file)
- [Reliability requirements](#reliability-requirements)
- [Tradeoffs compared with Redis/RQ](#tradeoffs-compared-with-redisrq)
- [Suggested migration path](#suggested-migration-path)
- [Recommendation](#recommendation)
- [Dependency compatibility note](#dependency-compatibility-note)

## Executive assessment

Replacing Redis/RQ is feasible for this service. The workload is naturally batch-oriented, low-frequency, and dominated by global operations: one `git pull`, a complete rebuild of the flattened web-served tree, an XInclude rewrite pass, and then per-inscription Solr updates. A cron-driven processor can perform those steps without a database or a continuously running queue worker.

The recommended filesystem design is a spool directory containing one immutable JSON file per accepted webhook—not multiple writers appending to one shared file. Per-event files make atomic writes, crash recovery, retries, inspection, and quarantine substantially simpler.

Redis/RQ should remain for the initial Django deployment, as requested. The new business logic is separated from Django views and RQ orchestration so a later management command can call the same functions synchronously.

## Why this is a good candidate

- GitHub pushes are expected to be infrequent relative to typical job-queue workloads.
- Processing already needs serialization around the shared git clone, staging directory, web-served directory, and Solr core.
- Copying always rebuilds the complete resource and inscription trees, so combining several closely spaced webhook events is safe before incremental indexing.
- A delay up to the cron interval is likely acceptable because the current listener is already asynchronous.
- The service does not need a database for application state.

## Recommended design

Use directories such as:

```text
spool/
    pending/
    processing/
    completed/
    failed/
    quarantine/
```

The listener would:

1. Parse and minimally validate the GitHub payload.
2. Create a unique event document containing a schema version, received timestamp, request identifier, updated paths, removed paths, and optionally a hash of the raw payload.
3. Write it to a temporary file in `pending/`, flush and `fsync` it, then atomically rename it to its final `.json` name.
4. Return the same immediate HTTP acknowledgement used today.

The cron command would:

1. Acquire a non-blocking operating-system lock so two cron invocations cannot process the shared checkout simultaneously.
2. Atomically move a bounded batch of pending files to `processing/`.
3. Validate each document against the current spool schema.
4. Coalesce updated and removed paths across the batch, resolving a path's final state by the newest event.
5. Run one git pull, one full copy, and one XInclude rewrite for the batch.
6. Apply the combined incremental Solr updates and removals.
7. Move successful event files to `completed/`, or move failed files back to `pending/` with attempt metadata and eventually to `failed/` or `quarantine/`.
8. Emit structured counts and timing logs and enforce retention limits for completed events.

An alternative is to perform a full reindex for every batch. That is easier to reason about but may create unnecessary Solr traffic, so incremental path coalescing is preferable unless operational measurements show that full reindexing is inexpensive.

## Why not one append-only queue file

A shared JSONL file can work if every writer uses `flock`, appends exactly one newline-terminated record, flushes, and `fsync`s before releasing the lock. The reader then needs a safe checkpoint or atomic file rotation. It also needs recovery rules for a partial final line and careful coordination with writers during rotation.

One-file-per-event avoids most of those edge cases and makes manual recovery obvious: an administrator can inspect, replay, move, or quarantine an individual event without editing a shared log.

## Reliability requirements

The filesystem spool must not be treated as a casual temporary directory. It needs:

- A local filesystem whose rename operation is atomic within the spool.
- Adequate permissions and disk monitoring.
- A uniqueness strategy such as timestamp plus UUID.
- A single-processor lock, preferably `flock` on a dedicated lock file.
- A documented retry limit and poison-event quarantine behavior.
- Idempotent processing. Re-running git pull, the copy, XInclude replacements, and Solr updates should converge on the same result.
- Retention and cleanup policies for completed and failed events.
- Alerting for oldest-pending age, repeated failures, malformed events, and disk usage.
- Backup or explicit acceptance that GitHub webhook redelivery is the recovery source after total spool loss.

## Tradeoffs compared with Redis/RQ

Advantages:

- Removes the Redis service and the continuously running RQ worker.
- Avoids the production constraint imposed by Redis 3.2.10.
- Produces directly inspectable queue state.
- Fits the serial, batch-oriented nature of the workflow.
- Reduces Python package compatibility coupling.

Costs:

- Processing latency becomes the cron interval.
- Retry, quarantine, monitoring, retention, and locking become application responsibilities.
- Per-inscription parallelism is lost unless deliberately rebuilt; that is probably acceptable and may be safer for Solr.
- Disk-full and permission failures can prevent the listener from durably accepting an event.
- A filesystem spool is ordinarily single-host. Shared or active-active web deployments would need shared storage with verified locking semantics or a different durable queue.

## Suggested migration path

1. Run the Django/RQ port in production and gather event frequency, processing duration, failure rate, and queue-depth data.
2. Add a `process_spool` Django management command that calls the existing helper functions directly.
3. Add unit tests for atomic event creation, lock contention, coalescing, retry limits, and crash recovery.
4. Exercise the spool processor in shadow mode using copied payloads without posting to production Solr.
5. Switch the listener from RQ enqueueing to atomic spool writes while retaining RQ as an emergency fallback for one release.
6. Add cron, monitoring, retention cleanup, and an operator replay command.
7. Remove Redis/RQ only after successful failure-recovery testing.

## Recommendation

Proceed with Redis/RQ for this first migration. Treat a per-event filesystem spool plus a locked cron processor as a strong follow-up option. It is likely simpler operationally for this particular workload, but only after its durability, observability, and replay behavior are implemented explicitly.

## Dependency compatibility note

RQ 1.16.2 documents support for Redis servers 3.0 and newer and declares Python 3.12 support. Current RQ documentation requires Redis 5 or newer. The project therefore pins RQ to 1.16.2 while the server remains on Redis 3.2.10.

- [RQ 1.16.2 README](https://github.com/rq/rq/blob/v1.16.2/README.md)
- [RQ 1.16.2 package metadata](https://github.com/rq/rq/blob/v1.16.2/pyproject.toml)
- [Current RQ documentation](https://python-rq.org/docs/)

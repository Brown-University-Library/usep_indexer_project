# USEP indexer project

---

UNDER-CONSTRUCTION -- not yet deployed

---

This Django 5.2 service replaces the legacy Flask `usep_gh_handler_app`. It accepts USEP GitHub push notifications, saves work to a durable filesystem-backed queue, and provides the legacy administrative endpoints. A cron-invoked Django management command processes queued work synchronously.

The project intentionally has no database. It omits Django admin, auth, contenttypes, models, migrations, and database-backed sessions. The orphan confirmation flow uses a signed-cookie session.

_(Terms: the term `spool`, used below and in the project, refers to the filesystem-backed work queue where accepted requests are stored as event files until the processor handles them.)_


## Table of contents

- [Requirements](#requirements)
- [Setup](#setup)
- [Endpoints](#endpoints)
- [Local HTTP listener check](#local-http-listener-check)
- [Tests](#tests)

## Requirements

- `uv` -- [installation](https://docs.astral.sh/uv/getting-started/installation/)
- `git` and `rsync`
- Access to the USEP data clone, web-served data directory, and Solr core
- A local POSIX filesystem supporting atomic rename and `flock` for the durable queue

## Setup

```bash
cd /path/to/usep_indexer_project_stuff/
cp ./usep_indexer_project/config/dotenv_example_file.txt ./.env
mkdir -p ./logs ./cache_dir
git clone git@github.com:Brown-University-Library/usep_indexer_project.git
cd ./usep_indexer_project
uv sync --upgrade
```

Update `.env` with deployment-specific credentials, filesystem paths, spool path, and Solr settings. The spool must not use temporary or ephemeral storage. The old shell variables map to the similarly named variables in the example file, without the `usep_gh__` prefix.

Run the web service:

```bash
uv run ./manage.py runserver
```

Process one batch of queued events:

```bash
uv run ./manage.py process_spool
```

The production processor is intended to run every other minute. The command takes a non-blocking lock, so an overlapping invocation exits safely:

```cron
*/2 * * * * cd /path/to/usep_indexer_project && uv run ./manage.py process_spool
```

## Endpoints

| Path | Methods | Protection | Purpose |
| --- | --- | --- | --- |
| `/` | GET, POST | Basic Auth | GitHub push listener |
| `/force/` | GET, POST | Basic Auth | Legacy manual listener trigger |
| `/reindex_all/` | GET | Basic Auth | Enqueue full pull, copy, and reindex |
| `/list_orphans/` | GET | Basic Auth | Compare filesystem and Solr IDs; add `?format=json` for JSON |
| `/orphan_handler/` | GET | Basic Auth | Confirm or cancel orphan deletion |
| `/daemon_check/` | GET | Source-IP allowlist | Report processor freshness and queue backlog |
| `/info/` | GET | Public | Service metadata |
| `/version/` | GET | Public | Git branch and commit metadata |
| `/error_check/` | GET | Public | Raise in debug mode; return 404 otherwise |

GET support and the query-driven orphan deletion flow are retained for initial compatibility. They should be tightened in a later API revision.

## Local HTTP listener check

Run the black-box listener check from the project root:

```bash
uv run ./check_web_listener.py
```

The script starts a real WSGI HTTP server on an available loopback port, sends rejected and accepted Basic Auth requests with the sanitized GitHub payload fixture, and validates the durable event written to an isolated temporary spool. It uses the test settings, does not read `.env`, and does not run the spool processor, Git, rsync, or Solr.

## Tests

The test settings are database-free and do not require `.env`, Solr, or a USEP checkout.

```bash
uv run ./run_tests.py -v
```

See [REPORT_redis_rq_alternative.md](REPORT_redis_rq_alternative.md) for the filesystem-queue architecture, operating assumptions, implementation plan, and future decisions.

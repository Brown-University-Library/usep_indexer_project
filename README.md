# USEP indexer project

This Django 5.2 service replaces the legacy Flask `usep_gh_handler_app`. It accepts USEP GitHub push notifications, delegates filesystem and Solr work to the existing Redis/RQ architecture, and provides the legacy administrative endpoints.

The project intentionally has no database. It omits Django admin, auth, contenttypes, models, migrations, and database-backed sessions. The orphan confirmation flow uses a signed-cookie session.

## Table of contents

- [Requirements](#requirements)
- [Setup](#setup)
- [Endpoints](#endpoints)
- [Tests](#tests)

## Requirements

- Python 3.12
- `uv`
- `git` and `rsync`
- Redis 3.2.10 or newer
- Access to the USEP data clone, web-served data directory, and Solr core

RQ is deliberately pinned to 1.16.2. That release supports Python 3.12 and Redis servers 3.0 or newer. Current RQ releases require Redis 5 or newer and cannot be adopted until the production Redis server is upgraded.

## Setup

From `usep_indexer_project_stuff`:

```bash
cp ./usep_indexer_project/config/dotenv_example_file.txt ./.env
mkdir -p ./logs ./cache_dir
cd ./usep_indexer_project
uv sync --upgrade
```

Update `.env` with deployment-specific credentials, filesystem paths, Redis URL, and Solr settings. The old shell variables map to the similarly named variables in the example file, without the `usep_gh__` prefix.

Run the web service:

```bash
uv run ./manage.py runserver
```

Run the RQ worker in another terminal:

```bash
uv run ./run_worker.py
```

For a one-time drain of the current queue, add `--burst`.

## Endpoints

| Path | Methods | Protection | Purpose |
| --- | --- | --- | --- |
| `/` | GET, POST | Basic Auth | GitHub push listener |
| `/force/` | GET, POST | Basic Auth | Legacy manual listener trigger |
| `/reindex_all/` | GET | Basic Auth | Enqueue full pull, copy, and reindex |
| `/list_orphans/` | GET | Basic Auth | Compare filesystem and Solr IDs; add `?format=json` for JSON |
| `/orphan_handler/` | GET | Basic Auth | Confirm or cancel orphan deletion |
| `/daemon_check/` | GET | Source-IP allowlist | Report RQ worker availability |
| `/info/` | GET | Public | Service metadata |
| `/version/` | GET | Public | Git branch and commit metadata |
| `/error_check/` | GET | Public | Raise in debug mode; return 404 otherwise |

GET support and the query-driven orphan deletion flow are retained for initial compatibility. They should be tightened in a later API revision.

## Tests

The test settings are database-free and do not require `.env`, Redis, Solr, or a USEP checkout.

```bash
uv run ./run_tests.py -v
```

See [REPORT_redis_rq_alternative.md](REPORT_redis_rq_alternative.md) for an assessment of replacing Redis/RQ with a durable filesystem spool and cron-driven processor.

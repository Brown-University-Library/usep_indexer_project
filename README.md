# USEP indexer project

---

UNDER-CONSTRUCTION -- not yet deployed

---

This Django 5.2 service replaces the legacy Flask `usep_gh_handler_app`. It accepts USEP GitHub push notifications, saves work to a durable filesystem-backed queue, and provides the legacy administrative endpoints. A cron-invoked Django management command processes queued work synchronously.

The project intentionally has no database. It omits Django admin, auth, contenttypes, models, migrations, and database-backed sessions. The orphan confirmation flow uses a signed-cookie session.

_(Terms: the term `spool`, used below and in the project, refers to the filesystem-backed work queue where accepted requests are stored as event files until the processor handles them.)_


## Overveiw

There are three main parts to this webapp:

- A listener that receives GitHub push webhooks and saves them to a file-based queue.
- A processor that is run on a cron schedule, reads events from the queue, pulls the latest `usep_data` from GitHub, and reorganizes the files.
- An indexer, invoked by the processor, that prepares the Solr documents and posts the updates to Solr.


## Table of contents

- [Overveiw](#overveiw)
- [Requirements](#requirements)
- [Local installation](#local-installation)
- [Endpoints](#endpoints)
- [Local HTTP listener check](#local-http-listener-check)
- [Tests](#tests)

## Requirements

- `uv` -- [installation](https://docs.astral.sh/uv/getting-started/installation/)
- `git` and `rsync`
- Access to the [usep_data](https://github.com/Brown-University-Library/usep-data) clone, web-served data directory, and Solr core
- A local POSIX filesystem supporting atomic rename and `flock` for the durable file-queue

## Local installation

The listener, the queue processor, and the indexer have different dependencies. 

The listener can accept a webhook once Django, the log, and the spool (the 'event'-directory) are configured. 

Processing that queued event also requires a local `usep_data` clone, and writable data directories.

Indexing requires resources from that usep_data clone, and an accessible Solr core.

### Install the application

```bash
cd /path/to/usep_indexer_stuff/
git clone git@github.com:Brown-University-Library/usep_indexer_project.git
cp ./usep_indexer_project/config/dotenv_example_file.txt ./.env
mkdir -p ./logs ./cache_dir ./spool_dir
cd ./usep_indexer_project
uv sync --upgrade
```

The application deliberately reads `.env` from the directory above the repository. This keeps local or deployment settings outside the Git checkout.

### Prepare the data and services

Before processing queued work:

- Clone the `usep_data` repository:

    ```bash
    cd /path/to/usep_indexer_stuff/
    git clone git@github.com:Brown-University-Library/usep-data.git
    ```

    Confirm that `git pull` works in that clone with the account that will run the processor. Set the `USEP_DATA_GIT_CLONED_DIR_PATH` `.env` entry to the clone's absolute path.

- Create writable directories for `TEMP_UNIFIED_INSCRIPTIONS_DIR_PATH` and `WEBSERVED_DATA_DIR_PATH`.

    ```bash
    cd /path/to/usep_indexer_stuff/
    mkdir -p ./temp_unified_inscriptions_dir
    mkdir -p ./webserved_data
    ```

    The processor uses `rsync` to flatten the three `usep_data` inscription directories into the temporary directory and mirror the resulting inscriptions and resources into the web-served directory.

    Note that for getting this webapp running, the `webserved_data` does not actually need to be served via http -- but on the dev and prod servers it must be, because the front-end webapp will make http calls to it.

- Start or otherwise obtain access to a compatible USEP Solr core and set `SOLR_URL`. Or create an ssh-tunnel:

    (Assumes Solr is locked down to only allow access from a dev or prod server via IP.)

    In a separate terminal tab, run:

    `ssh -N -L 9999:solr-server.domain.edu:1234 username@dev-server.domain.edu`

    The -N flag tells SSH not to run a remote command, appropriate because we're just port-forwarding.
    
    The -L 9999:solr-server.domain.edu:1234 flag forwards local port 9999 through dev-server.domain.edu to Solr at solr-server.domain.edu:1234.

    This, then, allows you to set this `.env` entry: `SOLR_URL="http://127.0.0.1:9999/solr/us_epigraphy"`.

    Running that ssh command won't show any output, but you can confirm the tunnel is working by opening <http://127.0.0.1:9999/solr/#/> in a browser.

    That connection will stay open as long as the terminal tab is open.

- Point the `SOLR_XSL_PATH` and `TRANSCRIPTION_PARSER_XSL_PATH` `.env` entries to the corresponding XSL files under `WEBSERVED_DATA_DIR_PATH/resources/xsl`. The processor reads `titles.xml` directly from `WEBSERVED_DATA_DIR_PATH/resources`. These files appear after the processor's initial copy from `usep_data` and must be readable when indexing begins.

### Configure the environment

Update the outer `.env` using `config/dotenv_example_file.txt` as the checklist. In particular:

- Replace the Django secret, Basic Auth credentials, hosts, trusted origins, and IP allowlist. For ordinary local HTTP development, keep `DEBUG_JSON="true"` and `SESSION_COOKIE_SECURE_JSON="false"`.
- Set `USEP_DATA_GIT_CLONED_DIR_PATH`, `TEMP_UNIFIED_INSCRIPTIONS_DIR_PATH`, `WEBSERVED_DATA_DIR_PATH`, `SPOOL_ROOT_PATH`, and `LOG_PATH` to real locations. Absolute paths are recommended. The log's parent directory must already exist and all data paths must be writable by the user running the `./manage.py` process-command, described below.
- Keep `SPOOL_ROOT_PATH` on durable, non-ephemeral local storage that supports atomic rename and `flock`. The application creates the queue's lifecycle subdirectories automatically.
- Set `SOLR_URL`, `SOLR_XSL_PATH`, and `TRANSCRIPTION_PARSER_XSL_PATH` to the appropriate local values.
- Review the file-based cache, static-file, email, queue-retention, and queue-health settings. The email server is only used for admin error notifications, but its settings are required when Django loads.

The old shell variables map to the similarly named variables in the example file, without the `usep_gh__` prefix.

Validate the Django configuration before starting the service:

```bash
cd /path/to/usep_indexer_stuff/usep_indexer_project/
uv run ./manage.py check
uv run ./run_tests.py -v
```

### Run the listener and processor

Run the web service:

```bash
uv run ./manage.py runserver
```

The web process only validates requests and writes queue files; it does not pull data or update Solr. In another terminal, process one batch of queued events:

```bash
uv run ./manage.py process_spool
```

For a new local installation, enqueue a full reindex through the authenticated endpoint after starting the web service, then run the processor:

```bash
curl --user 'replace_me:replace_me' http://127.0.0.1:8000/reindex_all/
uv run ./manage.py process_spool
```

Replace the example credentials with `BASIC_AUTH_USERNAME` and `BASIC_AUTH_PASSWORD` from `.env`. A successful full reindex pulls `usep_data`, populates the temporary and web-served directories, rewrites local XInclude references, and updates Solr. If `process_spool` reports a missing path, correct the corresponding `.env` value before retrying; queued events are retained and retried up to `SPOOL_MAX_ATTEMPTS`.

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
| `/list_orphans/` | GET | Basic Auth | Compare filesystem and Solr IDs, identify the index as dev/prod without exposing configured locations; add `?format=json` for JSON |
| `/orphan_handler/` | GET | Basic Auth | Confirm or cancel orphan deletion |
| `/processing_check/` | GET | Source-IP allowlist | Report processor freshness and queue backlog |
| `/info/` | GET | Public | Service metadata |
| `/version/` | GET | Public | Git branch and commit metadata |
| `/error_check/` | GET | Public | Raise in debug mode; return 404 otherwise |

GET support and the query-driven orphan deletion flow are retained for initial compatibility. They should be tightened in a later API revision.

### Processing-check response

The `/processing_check/` response contains:

- `request.timestamp`: local timestamp at which the application began handling the request.
- `request.url`: full URL used to request the endpoint.
- `response.result`: `processing_active` when the latest `running` or `success` status is fresh; otherwise `processing_not_active`.
- `response.processor_status`: latest recorded processor-run status, such as `running`, `success`, or `failed`.
- `response.last_started_at` and `response.last_finished_at`: UTC timestamps recorded for the latest processor run.
- `response.pending_count`, `response.processing_count`, `response.failed_count`, and `response.quarantine_count`: current event-file counts in those queue directories; these are snapshots, not cumulative totals.
- `response.oldest_pending_age_seconds`: age of the oldest pending event, or `null` when no event is pending.
- `response.info`: URL of this README.
- `response.timetaken`: time spent assembling the endpoint response.

## Local HTTP listener check

Run the black-box listener check from the project root:

```bash
uv run ./check_web_listener.py
uv run ./check_web_listener.py --payload path/to/github-payload.json
uv run ./check_web_listener.py --payload path/to/github-payload.json --use-real-directory
```

The script starts a real WSGI HTTP server on an available loopback port, sends rejected and accepted Basic Auth requests, and confirms that a durable event is written. It uses the sanitized GitHub payload fixture and an isolated temporary spool by default. Pass `--payload` to send another JSON payload; relative paths are resolved from the project-root working directory and logged as absolute paths. Pass `--use-real-directory` to read `SPOOL_ROOT_PATH`, `LOG_PATH`, and `LOG_LEVEL` from the outer `.env`, leave the locally timestamped event in that spool's `pending/` directory for subsequent processing, and write the script and application messages to the configured log file. The remaining test settings and local credentials stay in effect. The script does not run the spool processor, Git, rsync, or Solr.

## Tests

The test settings are database-free and do not require `.env`, Solr, or a USEP checkout.

```bash
uv run ./run_tests.py -v
```

See [REPORT_redis_rq_alternative.md](REPORT_redis_rq_alternative.md) for the filesystem-queue architecture, operating assumptions, implementation plan, and future decisions.

# AGENTS.md — Repository Agent Instructions (Source of Truth)

This file defines the canonical coding directives for this repository.

If other instruction files exist (Copilot, IDE rules, contributor docs) and conflict with this file, follow this file and treat the others as stale.


## Table of contents

- [Project basics](#project-basics)
- [Agent project index](#agent-project-index)
- [How to run code](#how-to-run-code)
- [Coding directives (Python)](#coding-directives-python)
- [Django architecture conventions](#django-architecture-conventions)
- [Front-end change guidance](#front-end-change-guidance)
- [Tests](#tests)
- [Change workflow expectations](#change-workflow-expectations)
- [If instructions are missing or ambiguous](#if-instructions-are-missing-or-ambiguous)


## Project basics

- Primary language: Python
- Target runtime: Python 3.12 -- unless a `pyproject.toml` specifies a different version
- Dependency / execution tool: `uv`
- Project-root is the directory containing this file (and `.git/`, and `.gitignore`).


## Agent project index

Use this section as a map, not as a substitute for reading the relevant code. Paths in this section are relative to the project root unless they begin with `../`. The normal local checkout has an outer “stuff” directory, but configured paths are authoritative and may be elsewhere in another environment.

### System shape

- This is a small Django 5.2 WSGI service with three jobs: accept GitHub push notifications, durably queue work on the filesystem, and synchronize/index USEP TEI XML into Solr.
- The web listener and the processor are deliberately separate. A successful listener response means an event was saved, not that Git, file copying, or Solr work finished.
- There is intentionally no database. Do not add models, migrations, Django admin/auth/contenttypes, or database-backed sessions without an explicit architecture change. The only session use is the signed-cookie orphan-confirmation flow.
- `config/urls.py` is the complete endpoint map; every endpoint is implemented directly in `usep_indexer_app/views.py`. Domain work belongs in `usep_indexer_app/lib/`.
- `README.md` is the operator setup guide and endpoint summary. Its final link to `REPORT_redis_rq_alternative.md` currently names a file that is not in this repository, so do not rely on that report being available.

### Find code by concern

| Concern | Start here | Follow into |
| --- | --- | --- |
| URL, HTTP method, response, or protection | `config/urls.py`, `usep_indexer_app/views.py` | `lib/auth.py`, then the concern-specific module |
| Basic Auth | `usep_indexer_app/lib/auth.py` | `config/settings.py` for setting names |
| GitHub payload parsing | `usep_indexer_app/lib/payloads.py` | `views.handle_github_push()` and the sanitized test fixture |
| Durable queue schema and lifecycle | `usep_indexer_app/lib/spool.py` | `management/commands/process_spool.py`, `lib/daemon.py` |
| Git pull, `rsync`, and XInclude rewriting | `usep_indexer_app/lib/processor.py` | `lib/reindex.py` for the full workflow |
| Main XML-to-Solr transformation | `usep_indexer_app/lib/indexer.py` | runtime XSL configured by `SOLR_XSL_PATH`; source copy is normally under `../usep-data/resources/xsl/` |
| Solr HTTP requests | `usep_indexer_app/lib/solr_client.py` | callers in `indexer.py`, `bibliography.py`, `transcription.py`, and `orphans.py` |
| Bibliography inheritance | `usep_indexer_app/lib/bibliography.py` | runtime `resources/titles.xml` and direct `bib_ids` already in Solr |
| Searchable transcription | `usep_indexer_app/lib/transcription.py` | runtime XSL configured by `TRANSCRIPTION_PARSER_XSL_PATH` |
| Full rebuild and stale-ID removal | `usep_indexer_app/lib/reindex.py` | `processor.py`, `indexer.py`, `orphans.py` |
| Manual orphan listing/deletion | `usep_indexer_app/lib/orphans.py`, `views.py` | `usep_indexer_app_templates/orphan_list.html` |
| Processor health | `usep_indexer_app/lib/daemon.py` | `spool.get_processor_health()` |
| Public version metadata | `usep_indexer_app/lib/version_helper.py` | `.git/HEAD` and Django cache behavior |
| Tests and supported test entry point | `run_tests.py` | `usep_indexer_app/tests/` |
| Integrated local listener check | `check_web_listener.py` | `tests/test_check_web_listener.py` |
| Environment setting names | `config/dotenv_example_file.txt` | `config/settings.py`; never copy values from a real outer `.env` |

### Request-to-index flows

1. `POST /` or a forced `GET`/`POST /force/` passes HTTP Basic Auth, then `payloads.prepare_files_to_process()` collects added, modified, and removed paths across every commit in the JSON body.
2. `spool.write_event()` writes a strict schema-version-1 JSON document to `pending/` using a synchronized temporary file and atomic rename. `X-GitHub-Delivery` becomes `request_id`; when absent, the generated event UUID is used.
3. `uv run ./manage.py process_spool` takes a non-blocking `flock`, recovers files left in `processing/`, claims a batch, quarantines malformed events, and coalesces valid events with newest-event-wins path state.
4. Any full-reindex event in a claimed batch selects the full workflow for that entire batch. Otherwise, one incremental workflow handles the coalesced changed paths.
5. Both workflows run `git pull`, rebuild the flattened data directories with `rsync`, and rewrite three known absolute XInclude URLs in the web-served inscription copies.
6. Incremental indexing only acts on paths containing a source-directory component in `{'bib_only', 'metadata_only', 'transcribed'}`. Full reindexing indexes every flattened `*.xml` file and removes Solr IDs absent from that filesystem set.
7. The main XSLT produces the Solr document. Bibliography ancestors and searchable transcription are then sent as separate atomic updates.

### Queue lifecycle and behavior

- `SPOOL_ROOT_PATH` contains `pending/`, `processing/`, `completed/`, `failed/`, and `quarantine/`, plus `processor.lock` and `processor-status.json`. The app creates the lifecycle directories.
- The event schema uses an exact key set. Unknown/missing keys, unsupported schema versions or event types, invalid UUIDs/timestamps/path lists, and malformed JSON go to `quarantine/` rather than blocking other valid events.
- A processing exception applies to the whole coalesced valid batch: every valid event gets the same failed attempt, then returns to `pending/` or moves to `failed/` at `SPOOL_MAX_ATTEMPTS`.
- Files already under `processing/` are replayed before new pending files after a crash. Processing must therefore be safe to repeat.
- Completed retention is based on completion-time file modification time, not original receipt time.
- A busy lock returns status `locked` without claiming work. The management command only raises `CommandError` for status `failed`.
- Health is based on whether the last `running`/`success` timestamp is fresh enough. Backlog counts are reported, but a backlog by itself does not change `daemon_active` to `daemon_not_active`.
- Queue correctness assumes a durable local POSIX filesystem with atomic rename, directory synchronization, and `flock`; do not move it to an arbitrary network/object filesystem without revisiting those assumptions.

### Source data and generated data

- `../usep-data/` is normally a separate Git clone and the source of truth for TEI XML and resource files. It has `xml_inscriptions/bib_only/`, `metadata_only/`, and `transcribed/`, plus `resources/` containing `titles.xml` and the indexing XSL files. Do not edit that sibling repository unless the task explicitly includes it.
- `../temp_unified_inscriptions_dir/` and `../webserved_data/` are generated runtime trees, not source code. Do not treat local contents or counts as stable fixtures, and do not hand-edit them as a lasting fix.
- Flattening order is significant: `bib_only` first resets the temporary directory with `--delete`; `metadata_only` overlays it; `transcribed` overlays last; the flattened result then mirrors to `webserved_data/inscriptions` with `--delete`. When source directories contain the same basename, later sources win: `transcribed` over `metadata_only` over `bib_only`.
- Resources mirror directly from the data clone to `webserved_data/resources` with `--delete`. The runtime XSL and `titles.xml` settings should point into that copied resource tree.
- XInclude replacement is literal and limited to the three URLs in `processor.XINCLUDE_REPLACEMENTS`. It rewrites only the web-served inscription copies after flattening; it does not modify the source clone or temporary flattened copies.
- Incremental resource-only changes are copied but ignored by `indexer.update_index()`. A change to `titles.xml`, an indexing XSL, or another resource may therefore require an explicitly queued full reindex to affect existing Solr documents.

### Failure boundaries and compatibility gotchas

- Failure of the main XML parse/XSLT/Solr post propagates and retries the whole queue batch. Bibliography and transcription enrichment are best-effort: their exceptions are logged but do not fail the main inscription update or retry the event.
- Incremental paths are reduced to their basename when locating a flattened inscription and deriving the Solr ID. The three source directories therefore share one filename/ID namespace.
- Malformed GitHub JSON is intentionally acknowledged and queued as an incremental event with empty path lists. An empty-body request to `/` queues nothing, while `/force/` queues even without a body.
- The listener validates Basic Auth but does not validate a GitHub HMAC signature. Its security depends on strong credentials and deployment behind HTTPS. Preserve compatibility unless a task explicitly changes this contract.
- `/reindex_all/` and orphan deletion are state-changing GET flows retained for legacy compatibility. Orphan deletion relies on the preceding `/list_orphans/` response putting all candidate IDs into a signed browser cookie.
- `/daemon_check/` compares `REMOTE_ADDR` directly with `LEGIT_IPS`; it is not proxy-header aware. `/info/`, `/version/`, and production `/error_check/` are public.
- `/list_orphans/` deliberately omits configured filesystem and Solr locations from its HTML/JSON context. It exposes only a safe index label: Solr hostnames beginning with `d` display as dev, those beginning with `p` display as prod, and other hostnames use a neutral label. Preserve that information-disclosure boundary when changing the response context or template.
- The main settings module asserts that `../.env` exists during import and loads it with `override=True`. JSON-suffixed values must be valid JSON, required email/log/cache values must exist even when not central to a command, and the log directory must already exist. Prefer absolute configured paths because unresolved relative paths depend on the process working directory.
- `USE_TZ` is false for Django-facing times, while spool document timestamps are timezone-aware UTC and queue filenames are rendered in `settings.TIME_ZONE`.
- The version endpoint reads loose `.git/HEAD` and branch-ref files directly. A deployment without `.git`, with packed refs, or with a detached head can return fallback/detached metadata; results are cached briefly.

### Safe ways to verify changes

- `uv run ./run_tests.py -v` uses `config.settings_run_tests`, requires no outer `.env`, database, Solr, data clone, Git pull, or `rsync`.
- Pass dotted Django test targets after `run_tests.py` for focused work, for example `uv run ./run_tests.py -v usep_indexer_app.tests.test_spool`.
- `uv run ./check_web_listener.py` starts a loopback WSGI server and uses a temporary queue by default. It checks rejected and accepted Basic Auth requests without invoking the processor or external services.
- `check_web_listener.py --use-real-directory` is not isolated: it reads selected outer `.env` values, writes the configured log, and intentionally leaves a real pending event. Use that flag only when the task calls for it.
- Running `process_spool`, calling `/reindex_all/`, or exercising the full processor can pull the sibling Git clone, delete/mirror generated files via `rsync --delete`, consume real queued work, and modify Solr. Do not use these as routine verification without explicit authorization and a known-safe environment.
- Test locations follow responsibility: endpoint/auth/session behavior in `test_views.py`; XML, processor, indexer, and helper behavior in `test_helpers.py`; queue durability/retry/health in `test_spool.py`; local HTTP-check behavior in `test_check_web_listener.py`.

### Public-repository privacy boundary

- This repository is public. Never add secrets, credentials, real webhook payloads, private URLs/hosts/IP allowlists, personal data, production filesystem paths, log excerpts, Solr responses, or queue event/status contents.
- In the normal outer directory, `.env`, `backups/`, `logs/`, `spool_dir/`, and ad hoc payload/report files are local operational material. They may be inspected only when a task truly requires it and must not be copied into this repository or summarized with identifying values.
- `temp_unified_inscriptions_dir/` and `webserved_data/` are generated data trees and can be large. Refer to their roles and configured setting names, not machine-specific contents.
- `config/dotenv_example_file.txt` and `config/settings_run_tests.py` contain placeholders/test-only values and are the safe sources for documenting configuration shape.


## How to run code

- Assume user is in the project-root directory.
- Do not use `python` to run scripts.
- Run a script via: `uv run ./path_to_script.py --help`
- Run tests via:
    - `uv run ./run_tests.py`
        - Note that `run_tests.py` has usage instructions about how to run more granular tests.
- Run django management scripts via: `uv run ./manage.py THE-COMMAND`


## Coding directives (Python)

### Type hints and imports

- Use Python 3.12 type hints everywhere (functions and important variables). (Unless a `pyproject.toml` specifies a different version.)
- Prefer builtin generics (e.g., `list[str]`, `dict[str, int]`) over `typing.List` / `typing.Dict`.
- Prefer PEP 604 unions (e.g., `str | None`) over `Optional[str]`.
- Avoid `typing` and `annotations` imports unless strictly necessary.

### Script structure

- Structure runnable modules as:
  - `def main() -> None: ...`
  - `if __name__ == '__main__': main()`
- Keep `main()` simple: parse args / orchestrate calls only.
- Put real logic into top-level helper functions and modules (no nested function definitions).
- Rarely use more than three levels of hierarchy: main() can call helper_A() which can call helper(B) which can, if necessary, can call helper(C) -- but that's it.

### Functions and control flow

- Prefer single-return functions (use local variables and a final return).
- Do not define functions inside other functions.
- Favor clarity and explicitness over cleverness.

### Logging

- When adding a log statement, when possible, format variable values as a label, followed by a comma and a space, with the value enclosed in double backticks.
- Prefer a label that matches the variable name. For example: ```log.debug(f'branch_and_commit, ``{branch_and_commit}``')```

### HTTP and networking

- Use `httpx` for all HTTP calls.
- Do not introduce alternate HTTP libraries (e.g., `requests`, `aiohttp`) unless the repository already depends on them and there is a documented reason.

### Docstrings

- Use triple-quoted docstrings.
- Write docstrings in present tense, with triple-quotes on their own lines.
  - Good: 
    ```
    """
    Parses ...
    """
    ```
  - Avoid: `"""Parse ..."""`
- The last line of non-test function-docstrings should be: `Called by: the_caller_function()` (or, if in another class/module, `Called by: module.Class.the_caller_function()`)
- Start test-function docstring-text with "Checks..."
- For header-comments, in functions, start the comment with two hashes (e.g., `## does this`).

### Additonal coding directives

- inspect the `/ruff.toml` for additional coding directives, such as `max-line-length` and `quote-style`.

### Markdown formatting

- Do not use hard line-breaks in markdown files; let paragraphs wrap naturally.
- When creating a Markdown file with more than three top-level `##` headings, add a table of contents near the top with links to those `##` headings.


## Django architecture conventions

### View-layer responsibilities

- `project/app/views.py` should contain **only** view functions that directly handle URL endpoints.
- Every view function in `project/app/views.py` should correspond to an entry in `project/config/urls.py`.
- Views should act as **manager/orchestrator** functions:
  - Parse request input (query params, POST body, files)
  - Perform minimal validation and shaping of inputs
  - Delegate substantive work to modules under `project/app/lib/`
  - Convert returned results into the appropriate `HttpResponse` (HTML, JSON, redirects)

### Business logic placement

- Put domain logic, integrations, and reusable operations in `project/app/lib/` (not in `views.py`).
- If multiple endpoints share logic, move that shared logic into `project/app/lib/` and keep each view thin.
- Prefer pure, testable functions in `project/app/lib/` that accept plain Python values (not Django request objects)
  unless passing the request is necessary for a narrow, well-justified reason.

### Imports and dependencies

- `views.py` should primarily import:
  - Django primitives (`HttpRequest`, `HttpResponse`, `render`, `redirect`, etc.)
  - The minimal set of functions/classes from `project/app/lib/` needed for each endpoint
- Avoid creating a secondary abstraction layer inside `views.py` (no view-helper utilities); place helpers in `project/app/lib/`.


## Front-end change guidance

- When front-end changes are required, use JavaScript only where it is truly required.
- Prefer updates in CSS, Python code, or Django template code when those can satisfy the behavior or presentation need.


## Tests

- Use the standard library `unittest` framework (not pytest) for non-Django projects.
- Use Django's test framework for Django projects.
- New behavior should usually come with a focused test covering:
  - the happy path
  - at least one failure / edge case


## Change workflow expectations

When implementing a change (especially from an issue/task):

1. Read relevant surrounding code and match existing conventions.
2. Make the smallest correct change that satisfies the request.
3. Update tests and run: `uv run ./run_tests.py`
4. If you cannot run tests in your environment, still write/adjust tests and state what you would run.


## If instructions are missing or ambiguous

- Do not ask questions unless absolutely necessary to proceed.
- Make reasonable assumptions, state them explicitly, then implement.
- If blocked, provide:
  - what you tried
  - what you found in the repo
  - a concrete next step (command, file to edit, or minimal decision needed)

---

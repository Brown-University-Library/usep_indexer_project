# USEP processing and indexing flows

This report describes the current implementation. In these diagrams, the queue is the set of safely saved job files, Solr is the search index, and public files are the copied XML and resources under `webserved_data`. The `/force/` endpoint follows the same incremental queue path as a GitHub push. XML-validation commands and health or information endpoints are not shown because they do not publish or index data.

## Table of contents

- [Shared publishing outcome](#shared-publishing-outcome)
- [Typical GitHub push for existing updated inscriptions](#typical-github-push-for-existing-updated-inscriptions)
- [Full reindex](#full-reindex)
- [Individual inscription refresh](#individual-inscription-refresh)
- [Resource-file changes](#resource-file-changes)
- [Removed, moved, or reclassified inscription](#removed-moved-or-reclassified-inscription)
- [Queue recovery and failures](#queue-recovery-and-failures)
- [Manual orphan cleanup](#manual-orphan-cleanup)
- [Implementation landmarks](#implementation-landmarks)

## Shared publishing outcome

```text
usep-data inscription XML and resources
                |
                v
Copy resources, merge inscriptions, and normalize known links
                |
                v
webserved_data/inscriptions and resources
                |
                +--> browser XML/XSL rendering
                |                |
                |                v
                |       inscription detail pages
                |
                +--> local indexing transformations
                                 |
                                 v
                      complete search documents
                                 |
                                 v
                         Solr search index
                                 |
                                 v
               search, collection, and publication pages
```

**Narrative.** The main publishing workflows maintain two distinct outputs. The copied XML and display files let a browser assemble an inscription's detail page. Separately, the service turns the same prepared material into complete Solr records for search and listing pages. Updating one output does not automatically update the other, which is why the workflows below show file publication and search indexing as separate stages.

## Typical GitHub push for existing updated inscriptions

```text
Researcher pushes updates to existing inscription XML on GitHub
                |
                v
GitHub sends a push message to the authenticated POST /
                |
                v
Listener collects added, modified, and removed paths
from every commit in the push
                |
                v
Listener safely writes one incremental job file to pending/
                |
                v
HTTP "received" returns to GitHub
(no pull, file copy, or Solr work has happened yet)
                |
                |  later: scheduled process_spool run
                v
Processor tries to reserve the shared processing slot
                |
                +--> busy --> leave queued work for a later run
                |
                v
Recover interrupted work, claim a limited-size queue batch,
check it, and combine repeated path changes
                |
                v
Pull the latest usep-data repository
                |
                v
Copy resources and merge the inscription folders
  bib_only --> metadata_only --> transcribed
  later folders win when filenames match
                |
                v
Publish webserved_data and rewrite known resource links
                |
                +--> copied XML and resources
                |              |
                |              v
                |     browser-rendered detail pages
                |
                v
For this typical case, each affected public XML file still exists
                |
                v
Build and check all affected search documents locally
                |
                v
Send one complete Solr update for each affected existing inscription
                |
                v
Move successfully handled job files to completed/
```

**Narrative.** A successful webhook response confirms that the request was safely saved, not that publication is complete. The listener quickly records paths; a scheduled process later pulls the authoritative repository and republishes the public data tree. Repeated notices are combined before work begins. For each affected inscription whose public XML still exists, the service checks a complete Solr record locally, then sends one update. The copied files feed the detail page, while Solr feeds search and listing pages. Removals and resource changes take the separate paths below.

## Full reindex

```text
Authorized operator requests GET /reindex_all/
                |
                v
Save one full_reindex job file to pending/
                |
                v
Return "pull and reindex initiated"
(the work is queued, not finished)
                |
                |  later: scheduled process_spool run
                v
Try to reserve the shared processing slot
                |
                +--> busy --> leave the job queued for a later run
                |
                v
Claim a queue batch
                |
                v
Any valid full_reindex job makes the whole claimed batch use this flow
                |
                v
Pull the latest usep-data repository
                |
                v
Check every source inscription XML file
                |
                +--> any malformed XML
                |          |
                |          v
                |    stop before copying or contacting Solr
                |    and follow the queue-failure flow
                |
                v
Copy resources, merge inscriptions, publish webserved_data,
and rewrite known resource links
                |
                v
Load current indexing rules, titles.xml, and one reusable Solr client
(no Solr request has happened yet)
                |
                v
Build and check every complete search document locally
                |
                +--> any local build fails
                |          |
                |          v
                |    stop before the first Solr request
                |    and follow the queue-failure flow
                |
                v
Read all current Solr IDs once
                |
                v
Post all rebuilt documents in configured-size groups
                |
                v
Delete IDs with no matching public XML in configured-size groups
                |
                v
Move successfully handled job files to completed/

A normally reported Git, copy, build, Solr-read, posting,
or deletion failure follows the queue-failure flow.
Because public files are copied first and Solr is updated
in batches, a failure can occur after some changes are
already live. The retry starts from the beginning and
safely writes the current data again.
```

**Narrative.** `/reindex_all/` saves a job; the web request does not rebuild. The processor checks all source XML before replacing public copies, then prepares every search record before contacting Solr. A local conversion problem therefore leaves Solr intact, although copied public files may already be newer. The index is updated in place: documents are posted in groups, then IDs with no matching XML are removed. A later Solr failure can leave earlier groups accepted; retrying safely repeats the workflow. Any valid full-reindex job selects this path for its claimed group.

## Individual inscription refresh

```text
Operator runs refresh_inscription INSCRIPTION_ID
                |
                v
Reserve the shared processing slot
                |
                +--> busy --> stop; no work starts
                |
                v
Check that the value is an ID only,
not a path or an .xml filename
                |
                v
Pull the latest usep-data repository
                |
                v
Copy all resources, merge all inscription folders,
publish webserved_data, and rewrite known resource links
                |
                +--> copied XML and resources --> browser detail pages
                |
                v
Does INSCRIPTION_ID.xml exist in the new public copy?
                |
                +--> no --> report an error; do not post to Solr
                |
                v
Build one complete search document
  base transformation
  + publication relationships
  + searchable transcription
                |
                +--> build or check fails
                |          |
                |          v
                |    report an error; old Solr record remains
                |
                v
Post one complete document to Solr
                |
                v
Report success directly to the operator
(no queue job was created)
```

**Narrative.** Unlike webhook and full-reindex requests, this command starts immediately and never enters the queue. It uses the shared processing slot, pulls current data, and republishes the complete XML and resource set, but it rebuilds only the requested Solr record. If the ID is absent or the record cannot be prepared, no Solr update is sent; the public copies may already be newer. The command reports the error directly and has no automatic retry.

## Resource-file changes

```text
Resource-only GitHub push changes a file under resources/
                |
                v
Follow the normal webhook queue and scheduled-processing path
                |
                v
Pull and copy the newest complete resource tree
                |
                v
The changed resource is now published in webserved_data
                |
                v
Can it affect how search documents are built?
                |
                +--> yes, or a changed XSL/XSLT file
                |    cannot be classified safely
                |          |
                |          |  examples:
                |          |  titles.xml
                |          |  an indexing transformation
                |          |  one of its imported or included files
                |          v
                |    rebuild every Solr document from the
                |    already prepared files; do not pull or copy again
                |
                +--> no, proven display-only resource
                           |
                           v
                     finish without changing Solr
```

**Narrative.** Resource changes matter even when no inscription XML changes. The newest resource tree is always copied first, so browser-facing improvements are published either way. If the changed file can alter bibliography, transcription, or other search fields, every Solr record is rebuilt from that prepared copy. If the service cannot confidently classify a changed XSL/XSLT file, it chooses the safe full rebuild. A resource proven to affect display only causes no Solr work. The rebuild reuses the existing pull and copy.

## Removed, moved, or reclassified inscription

```text
Queued GitHub push reports a removed or moved inscription path
                |
                v
Pull, merge, and publish the latest source files
                |
                v
Reduce changed inscription paths to their shared filename
                |
                v
Classify each filename from the final merged public folder
                |
                +--> file still exists
                |          |
                |          v
                |    add its current surviving XML to the rebuild list
                |
                +--> file no longer exists
                           |
                           v
                     add its ID to the deletion list
                |
                v
Build and check every surviving search document locally
                |
                v
Post the surviving documents one at a time
                |
                v
Delete absent IDs in configured-size groups

A move between source folders with the same filename
uses one surviving-file update.

A filename change follows both branches:
  old filename --> delete its old ID
  new filename --> build and post its new ID
```

**Narrative.** The processor does not delete a search record merely because GitHub reports one removed path. It first rebuilds the merged public folder, where `transcribed` takes priority over `metadata_only`, which takes priority over `bib_only` for the same filename. If another version still supplies that filename, the surviving version is indexed. Only when no public XML remains is the Solr ID deleted. A rename naturally becomes deletion of the old ID and creation or replacement of the new one.

## Queue recovery and failures

```text
Scheduled processor starts
                |
                v
Reserve the shared processing slot
                |
                +--> busy --> leave queued work untouched
                |
                v
Recover job files left in processing/ after an interruption,
then claim new files from pending/
                |
                v
Check each claimed job file
                |
                +--> unreadable or invalid
                |          |
                |          v
                |    move that file to quarantine/
                |    and continue with valid jobs
                |
                v
Combine valid jobs and run one processing workflow
                |
                +--> success --> move valid jobs to completed/
                |
                +--> workflow reports an error
                           |
                           v
                     record the same failed attempt
                     on every valid job in the batch
                           |
                           v
                     choose each job's destination
                       attempts remain --> pending/
                       limit reached   --> failed/
                           |
                           v
                     return a failed processor result
                           |
                           v
                     command attempts one summary email
                |
                +--> process terminates abruptly
                           |
                           v
                     jobs may remain in processing/
                     for recovery on the next run;
                     no attempt count or email is guaranteed
```

**Narrative.** Queue files make processing restartable. Work abandoned in `processing/` after an interruption is tried before new work. Badly formed queue files are isolated rather than blocking good ones. A reported Git, copying, XML, transformation, or Solr error fails the combined workflow; each valid job records the attempt and is retried until the limit. The command then attempts one summary email. An abrupt termination can leave jobs for recovery without an attempt count or email. Solr updates accepted before a later failure remain, so a retry safely repeats the work.

## Manual orphan cleanup

```text
Operator opens /list_orphans/
                |
                v
Read IDs from public inscription filenames
                |
                v
Read IDs currently held in Solr
                |
                v
IDs found only in Solr become orphan candidates
                |
                v
Show the list and remember it for confirmation
                |
                v
Operator confirms deletion?
                |
                +--> no --> change nothing
                |
                +--> yes
                           |
                           v
                     try each deletion and continue if one fails
                           |
                           v
                     report complete or partial success
```

**Narrative.** A full reindex automatically removes Solr records that no longer have matching public XML. This separate legacy tool lets an operator inspect and confirm the same kind of mismatch without running a rebuild. It compares the current public files with Solr, remembers the proposed list in the browser session, and deletes only after confirmation. It does not pull data or reserve the processor lock, so it should be used only when the public copy is current and no indexing work is competing.

## Implementation landmarks

| Concern | Main files |
| --- | --- |
| Webhook and full-reindex requests | [`config/urls.py`](config/urls.py), [`usep_indexer_app/views.py`](usep_indexer_app/views.py), [`usep_indexer_app/lib/payloads.py`](usep_indexer_app/lib/payloads.py) |
| Durable queue, locking, combining, retry, and recovery | [`usep_indexer_app/lib/spool.py`](usep_indexer_app/lib/spool.py), [`usep_indexer_app/management/commands/process_spool.py`](usep_indexer_app/management/commands/process_spool.py) |
| Git pull, copied public data, merging, and resource classification | [`usep_indexer_app/lib/processor.py`](usep_indexer_app/lib/processor.py), [`usep_indexer_app/lib/stylesheet_dependencies.py`](usep_indexer_app/lib/stylesheet_dependencies.py) |
| Complete search-document construction | [`usep_indexer_app/lib/indexer.py`](usep_indexer_app/lib/indexer.py), [`usep_indexer_app/lib/bibliography.py`](usep_indexer_app/lib/bibliography.py), [`usep_indexer_app/lib/transcription.py`](usep_indexer_app/lib/transcription.py) |
| Full and single-inscription rebuilds | [`usep_indexer_app/lib/reindex.py`](usep_indexer_app/lib/reindex.py), [`usep_indexer_app/management/commands/refresh_inscription.py`](usep_indexer_app/management/commands/refresh_inscription.py) |
| Solr operations and manual orphan handling | [`usep_indexer_app/lib/solr_client.py`](usep_indexer_app/lib/solr_client.py), [`usep_indexer_app/lib/orphans.py`](usep_indexer_app/lib/orphans.py) |

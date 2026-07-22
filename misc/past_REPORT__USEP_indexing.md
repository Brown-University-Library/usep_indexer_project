# Why reindexing one USEP inscription currently takes six HTTP calls

## Executive summary

The current indexer makes exactly six sequential HTTP requests to Solr for every inscription it updates successfully. Those six requests are real, but six is not an inherent requirement of Solr or of USEP's data. It is a consequence of dividing one logical document into a base update, a bibliography enrichment, a transcription enrichment, and two explicit visibility requests.

The logical work is necessary: create the main search document, derive bibliography relationships, derive searchable transcription text, and make the result visible to searches. The present request boundaries are mostly not necessary. The indexer already has the inscription XML and `titles.xml` locally, so it can calculate all fields before contacting Solr and send one complete document. If the deployed Solr configuration requires an explicit visibility operation, the result could instead be one document update followed by one soft commit.

There is also a more important issue to fix before optimizing the calls. The bibliography enrichment looks for `<bibl>` elements nested inside other `<bibl>` elements in `titles.xml`. The current `titles.xml` contains 1,285 `<bibl>` elements and none is nested inside another. The hierarchy was flattened in the data repository in 2021; relationships are now represented by references such as `ref="#AA"`. Consequently, the current “inherited bibliography” calculation returns no inherited IDs, and three of the six requests query, post, and commit an enrichment that does not add anything.

Review of the webapp and live site confirms that these parent relationships are still required. The Publications page constructs parent-journal and child-publication links from the flat `titles.xml`; the selected publication page then finds inscriptions using an exact `bib_ids:<publication-id>` Solr query. At the time of review, the live AJA and AJP parent pages each returned zero inscriptions, while representative child-publication pages returned 43 and 39. This is the visible consequence of the indexer retaining direct citation IDs but failing to add their referenced parent IDs.

The recommended direction is therefore:

1. Replace XML-ancestor bibliography discovery with relationship discovery from the current flat `titles.xml` references, then rebuild the affected Solr data.
2. Build a complete Solr document locally, including transcription and the intended bibliography IDs.
3. Post that complete document once, using one consistent visibility policy.
4. Separately optimize full-reindex batching, parser reuse, HTTP connection reuse, and—only if worthwhile—the filesystem preparation steps.

## Table of contents

- [Scope and terminology](#scope-and-terminology)
- [The two outputs produced by indexing](#the-two-outputs-produced-by-indexing)
- [End-to-end flow for one inscription](#end-to-end-flow-for-one-inscription)
- [The six Solr HTTP requests](#the-six-solr-http-requests)
- [Why the current sequence is sequential](#why-the-current-sequence-is-sequential)
- [What is actually necessary](#what-is-actually-necessary)
- [What the front end requires from titles.xml](#what-the-front-end-requires-from-titlesxml)
- [Findings and risks](#findings-and-risks)
- [Forthright simplification options](#forthright-simplification-options)
- [Recommended implementation sequence](#recommended-implementation-sequence)
- [Verification criteria](#verification-criteria)
- [Code and data reviewed](#code-and-data-reviewed)

## Scope and terminology

This report describes the code in the current `usep_indexer_project` checkout and the current `usepweb_project` checkout. It distinguishes three kinds of activity that can otherwise be conflated:

- An inbound webhook or manual web request tells the indexer that work exists. The listener saves a queue file; it does not update Solr.
- Filesystem preparation pulls `usep-data`, flattens the three source inscription directories, copies resources and inscriptions to the web-served tree, and rewrites three XInclude URLs. These are local Git/subprocess/filesystem operations from the application's point of view, not the six Solr HTTP calls.
- Solr indexing transforms and sends one inscription's searchable representation. This is the part that currently makes six HTTP requests.

There are two ways to initiate work for a single inscription. An ordinary GitHub-driven incremental event is accepted by the listener, saved in the spool, and later processed by `manage.py process_spool`. The newer `manage.py refresh_inscription ID` command immediately performs the same pull/copy preparation and indexes the selected inscription while holding the shared processor lock. The ordinary path treats bibliography and transcription failures as best-effort; the manual command reports either enrichment failure as a command failure. Both paths use the same six-request Solr implementation.

## The two outputs produced by indexing

The preparation/indexing process serves two related but distinct consumers:

```text
usep-data source XML and resources
                 |
                 +--> flattened webserved_data --> browser loads XML/XSL --> inscription detail page
                 |
                 +--> local XSL transformations --> Solr document --> search, collection, and publication pages
```

This explains why updating Solr alone is not the complete publishing operation. The public webapp's inscription-detail view does not retrieve the inscription from Solr. It gives the browser URLs for the copied inscription XML and display stylesheets; client-side Saxon code loads the XML and performs the display transformation. By contrast, search results, collection listings, and publication-to-inscription listings query Solr.

A single-inscription reindex therefore still makes conceptual sense as a targeted way to repair or refresh the Solr-backed parts of the site:

- Search results and facets.
- Collection listings.
- Publication-to-inscription relationships.
- Status, image, date, language, and other indexed metadata.
- Searchable transcription text.

It is not needed to render the inscription-detail page itself. The `refresh_inscription` command performs both sides of publication: it refreshes the copied XML/resources and then updates the selected Solr document. The capability is useful as a recovery and troubleshooting tool. A more explicitly separated design could provide a data-sync operation, a Solr-only single-inscription reindex that works from prepared files, and the existing combined refresh operation that orchestrates both.

The filesystem copying and XInclude rewriting therefore have a real purpose: they publish the XML and resources consumed by the detail page. They are conceptually separate from the six Solr requests and can be optimized independently.

## End-to-end flow for one inscription

For a GitHub-driven incremental update, the current flow is:

1. `POST /` receives an authenticated GitHub payload.
2. The listener extracts added, modified, and removed paths and atomically saves an event in the filesystem spool.
3. A later `manage.py process_spool` run acquires the processor lock, claims queued events, and coalesces their paths.
4. The processor runs `git pull` in the configured `usep-data` clone.
5. Four `rsync` operations copy resources, flatten `bib_only`, `metadata_only`, and `transcribed` with defined overlay precedence, and mirror the flattened result into `webserved_data/inscriptions`.
6. Every copied inscription is scanned for three known absolute XInclude URLs, which are rewritten to relative paths in the web-served copy.
7. For each changed path in one of the three inscription source directories, the filename is used to select the corresponding flattened XML file.
8. The indexer transforms that XML with `USEp_to_Solr.xsl` and begins the six-request Solr sequence described below.

The direct `refresh_inscription` management command starts at step 4 after validating the requested bare ID and acquiring the same lock. It also verifies that the selected copied XML exists before indexing it. It does not queue the request.

The full-copy preparation is deliberately conservative. A source file may move among the three status directories, duplicate basenames are resolved by overlay order, resources can change, and deleted files must disappear from the published tree. Rebuilding the flattened tree makes those rules simple and repeatable, although it means that “reindex one” currently scans and copies far more than one file.

## The six Solr HTTP requests

All six calls target the configured Solr core through `usep_indexer_app/lib/solr_client.py`. They are synchronous, use a 30-second timeout, and use top-level `httpx` functions rather than a shared client.

| # | Request | What it does | Why it exists in the current design | Is a separate request inherently necessary? |
| --- | --- | --- | --- | --- |
| 1 | `POST {SOLR_URL}/update`, XML body | Sends the main Solr document created by `USEp_to_Solr.xsl`. It includes the ID, descriptive/search fields, status, and direct `bib_ids`. The XSL emits `<add commitWithin="500">`. | Establishes or replaces the document that later atomic updates modify. | A document update is necessary, but it could already contain every derived field. |
| 2 | `GET {SOLR_URL}/select` for the ID, returning only `bib_ids` | Reads the just-posted document's direct bibliography IDs back from Solr. | The bibliography module accepts only an inscription ID, so it uses Solr as an intermediate data store instead of receiving IDs from the local XML/transformation. | No. The same IDs are already available locally in the TEI XML and in the generated Solr XML. |
| 3 | `POST {SOLR_URL}/update`, JSON atomic update | Adds bibliography ancestor IDs to the existing `bib_ids` multivalued field. | The intended publication hierarchy lives in `titles.xml`, outside the inscription XML, and is calculated after the main transform. | The derived data may be necessary, but a separate atomic update is not. It can be placed in the complete document before request 1. |
| 4 | `GET {SOLR_URL}/update?softCommit=true` | Opens a new Solr searcher so the bibliography update is searchable. | Gives the bibliography phase its own explicit visibility boundary. It also makes the earlier base update visible if it was not already committed. | No intermediate visibility is needed by the transcription calculation, which reads local XML. One final visibility policy is enough. |
| 5 | `POST {SOLR_URL}/update`, JSON atomic update | Sets the `transcription` field to text produced by a second, transcription-specific XSL transformation. It sends an empty string when no edition `<ab>` exists. | Transcription normalization is implemented in a separate module and transform, after the base document has already been sent. | The transformation is necessary for searchable text, but the field can be calculated before request 1 and included in the complete document. |
| 6 | `GET {SOLR_URL}/update?softCommit=true` | Makes the transcription atomic update searchable. | Provides prompt visibility for the final enrichment. | Some visibility mechanism is necessary. A separate request may not be: the update can use the deployment's chosen commit/autocommit mechanism. |

The count is unconditionally six on a successful current update, even for an inscription with no transcription and even when no bibliography ancestors are found. An earlier version of the newly ported indexer fetched `titles.xml` over HTTP as well and would therefore have made a seventh request; current code correctly reads the copied `titles.xml` from disk.

This count applies to adding or updating an inscription. Removing an inscription follows a different path: one JSON `POST` contains both the delete command and a hard commit.

## Why the current sequence is sequential

The sequence reflects how the implementation is divided, rather than six independent business requirements:

- The bibliography and transcription updates are Solr atomic updates, so the base document must exist before they can modify it.
- The bibliography calculation currently takes its direct inputs from request 2, so request 1 must happen first.
- Each function waits for `raise_for_status()` before continuing, giving failures a clear local boundary.
- Bibliography finishes—including its own commit—before transcription begins because `update_index_entry()` simply calls the modules in that order.

Only part of this ordering is a genuine dependency. The base document must precede an atomic modification of that document, but neither bibliography nor transcription actually needs to be calculated after the base post. Both can be calculated entirely from local files. Transcription does not depend on bibliography, and it does not need the bibliography soft commit.

The comment in the README that each request “must finish before the next begins” accurately describes the code, but should not be read as a Solr requirement that these exact six operations must remain separate.

## What is actually necessary

The necessary final-state work is:

- Produce the base Solr fields from the inscription XML.
- Preserve the direct bibliography IDs contained in the inscription.
- If USEP still wants inherited publication relationships, derive them correctly from the current `titles.xml` representation.
- Produce normalized searchable transcription text.
- Send a complete update to Solr and make it searchable according to an explicit durability/visibility policy.
- Publish the current XML and resources for the browser-rendered inscription page.

None of those requirements mandates six Solr requests. The cleanest representation is one fully assembled document sent once. A second request is defensible only if an explicit soft commit cannot or should not be coupled to that update and prompt visibility is required.

## What the front end requires from titles.xml

### Collections and Publications use different Solr relationships

The requested review began with the public [Collections page](https://library.brown.edu/projects/usep/collections/). That part of the site does not establish a requirement for bibliography parents. The collections overview is built from the webapp's collection records, and a collection-detail page retrieves inscriptions with an ID-prefix Solr query such as `id:CA.Malibu.JPGM*`. It does not query `titles.xml` or `bib_ids`.

The [Publications page](https://library.brown.edu/projects/usep/publications/) is the relevant consumer. Its current path is:

```text
titles.xml + pubs.xsl
        |
        +--> browser creates publication links
                       |
                       +--> /publication/<bibliography-id>/
                                      |
                                      +--> Solr query: bib_ids:<bibliography-id>
```

The publications overview is transformed in the browser from the current flat `titles.xml`. Its stylesheet was explicitly rewritten for that representation in 2021. It groups journal articles using `title[@level='j']/@ref`, groups corpus volumes using `title[@level='s']/@ref`, and links monographs directly by their own `xml:id`. The active publication-result view does not interpret the bibliography hierarchy itself; it asks Solr for documents whose `bib_ids` exactly contains the selected ID.

The older `Publications` model class, which expects fields such as `bib_ids_types` and `bib_titles_all`, is not used by the active publications view. The current public interface therefore needs one essential indexed field for this purpose: a correct, multivalued `bib_ids` containing the directly cited bibliography entry and every parent under which that citation should be found.

### The failure is visible on the live site

The source analysis is consistent with the live result pages observed on 2026-07-20:

| Publication link | Relationship in `titles.xml` | Live inscription count |
| --- | --- | ---: |
| [AJA](https://library.brown.edu/projects/usep/publication/AJA/) | Parent journal | 0 |
| [AJA_Dennison](https://library.brown.edu/projects/usep/publication/AJA_Dennison/) | Child article | 43 |
| [AJP](https://library.brown.edu/projects/usep/publication/AJP/) | Parent journal | 0 |
| [AJP_Wilson4](https://library.brown.edu/projects/usep/publication/AJP_Wilson4/) | Child article | 39 |

The Publications overview makes the parent journal names clickable. A parent page returning zero while its child pages return inscriptions is not an intentional absence of private or hidden data; it is a broken public aggregation. Direct IDs were indexed, but the referenced parent IDs were not.

The corpus display currently links individual volumes but has its parent-corpus link commented out in `pubs.xsl`. Parent-corpus IDs should still be derived: they were part of the older nested semantics, they make the index internally consistent, and they allow the parent link to be restored later without another indexing-model change.

### What the flat file actually represents

The current `titles.xml` has 1,285 bibliography entries and zero nested bibliography entries. It contains 666 `title/@ref` relationships. Each child has at most one such parent, and every valid relationship is one level deep in the current file.

The references are not perfectly uniform:

- 641 values are fragment-style references such as `#AA`.
- 25 are bare local IDs such as `TEAD_PR`.
- After removing an optional leading `#`, 665 references resolve to an existing `bibl/@xml:id`.
- `ObjectBiographies_Powers` refers to the nonexistent `HCD`; the apparent intended parent is `ObjectBiographies`.

The front-end stylesheet assumes a leading `#` for the journal and corpus groups it renders, so the 25 bare IDs are also a data/front-end consistency problem. The indexer should nevertheless accept both forms. It can improve Solr's relationships without waiting for every source-data inconsistency to be corrected.

### The indexer can meet the requirement

Yes: the indexer can be updated to provide what the active webapp needs, without changing the webapp's publication-result query. The replacement rule should be:

1. Build a lookup from each `bibl/@xml:id` to the local IDs named by its descendant `title/@ref` attributes.
2. Treat `#ID` and `ID` as the same local target, but accept the target only if that `xml:id` exists in `titles.xml`.
3. For every direct `bib_id` extracted from an inscription, retain that ID and add all valid parent IDs reached through the lookup.
4. Follow references recursively with a visited-ID set, even though current data has only one-level relationships. This handles future multi-level data and prevents a malformed cycle from looping forever.
5. Deduplicate the completed list before sending it to Solr.

Examples from current data include `Brueckner1926 -> AA`, `AJA_Dennison -> AJA`, `MAAR_GiganteHouston -> MAAR`, `CIL_VI -> CIL`, and `TEAD_PR_1 -> TEAD_PR`. The lookup must search descendant titles rather than direct child titles only, because some entries, including `CEG_1` and `CEG_2`, wrap the parent-bearing title inside another title.

The smallest repair is to replace the ancestor XPath in `bibliography.add_bibliography()` with this lookup and retain the existing atomic update. The preferable implementation is still to derive direct and parent IDs locally, put their complete value into the main Solr document, and send that document once. A complete-field `set` or full-document replacement is safer than an isolated atomic `add`, because a changed `titles.xml` relationship must also be able to remove a stale parent ID.

After this correction, existing documents require a full Solr rebuild; refreshing only subsequently edited inscriptions would leave the rest of the public parent-publication pages incomplete. Future changes to `titles.xml` should likewise be treated as an indexing dependency and trigger an appropriate bibliography rebuild, because the relationship can change even when no inscription XML changes.

## Findings and risks

### 1. The inherited-bibliography code no longer matches `titles.xml`

`bibliography.add_bibliography()` locates a direct bibliography entry with this XPath and adds the XML IDs of enclosing bibliography elements:

```text
//tei:bibl[@xml:id=$bibliography_id]/ancestor::tei:bibl/@xml:id
```

That made sense with the older hierarchical data. For example, in the pre-flattened file, `Brueckner1926` was nested under `AA_1926`, which was nested under `AA`. The current file is flat: `Brueckner1926` has a title with `ref="#AA"`, but it has no `<bibl>` ancestor. The current `titles_old.xml` still illustrates the former shape with 1,329 total bibliography elements, 810 of them nested; current `titles.xml` has 1,285 total and zero nested.

The data history shows the flattening entering `titles.xml` in commit `269e53044` on 2021-08-05 and becoming the merged mainline shape in November 2021. The new Django indexer already contained the ancestor-XPath behavior in its initial commit in July 2026. This strongly suggests a legacy assumption survived the data-model change.

On current data, request 3 is therefore expected to send an atomic `add` of an empty list. Requests 2 and 3 do not create the intended inherited relationship, and request 4 merely commits the state. Direct bibliography IDs still come from request 1, so publication pages that query an exact directly cited ID work; parent-journal queries are incomplete.

The webapp and live-site investigation resolves the earlier product question: parent relationships are required. The code should follow the current reference representation rather than XML ancestry. Removing the enrichment without replacing its intended result would preserve the live defect.

### 2. The read-after-write bibliography query has a visibility race

Request 1 uses `commitWithin="500"`, then request 2 immediately uses the normal Solr `/select` handler. A successful update response means Solr accepted the update; it does not by itself guarantee that a normal searcher can already see the new document. The explicit soft commit does not occur until request 4.

Consequently, request 2 can observe the previous document or no document, especially for a newly created inscription. `commitWithin="500"` makes eventual visibility likely but does not make this immediate read deterministic. This problem would have caused intermittent missing ancestors when `titles.xml` was still hierarchical; with the current flat file, the calculation is empty regardless.

Reading direct bibliography IDs from the local source removes both the extra request and this race.

### 3. One logical update can be partially visible

The base document, bibliography, and transcription are posted separately. A later failure does not roll back an earlier success. In ordinary queued processing, bibliography and transcription exceptions are logged and suppressed, so the event can finish successfully with a base document that lacks enrichment. The manual single-inscription command is stricter about reporting the error, but Solr may still retain the already-posted base document.

Assembling all derived fields before the only Solr post would make each inscription update effectively all-or-nothing from the indexer's point of view. A local transformation error would prevent any new version from being posted, and a successful post would contain all expected fields.

### 4. Commit policy is duplicated and internally mixed

The base XML requests `commitWithin="500"`, while each enrichment requests an explicit soft commit. This creates as many as three visibility signals for one logical document. The first explicit soft commit occurs even though no subsequent calculation needs searchable bibliography, and the second occurs immediately afterward.

Frequent soft commits can be expensive because they repeatedly open searchers. The deployed Solr configuration and required user-facing freshness should determine one policy: rely on a configured autocommit/`commitWithin`, request one final soft commit, or attach the required commit option to the completed update. The Solr schema and server configuration are not stored in either reviewed repository, so that deployment detail must be checked before changing the policy.

### 5. Full reindexing magnifies per-inscription overhead

The current flattened checkout contains 3,419 inscriptions. At six requests each, a full rebuild makes 20,514 per-inscription Solr requests, plus the initial all-ID query and any orphan deletions. The full rebuild processes them sequentially.

The code also reparses `titles.xml`, reloads both XSL files, reparses inscription XML for separate transformations, and uses fresh top-level HTTP calls for every operation. Even if the six-request behavior were retained, reusing parsed resources and a persistent HTTP client would reduce work. The larger gain comes from sending complete documents in batches and committing per batch or at the end according to an agreed failure/recovery policy.

### 6. Filesystem preparation is broad but is not the six-call problem

Both incremental processing and `refresh_inscription` rebuild the complete flattened inscription tree, mirror all resources, and scan every copied XML file for XInclude replacements. This is more work than the selected inscription alone requires, but it guarantees overlay precedence, handles source moves/deletions, and keeps the browser-served corpus synchronized.

It can be optimized later by resolving the winning source file for one basename and copying only affected files/resources, but that introduces more edge cases than the Solr-call reduction. It should be treated as a separate improvement with tests for moves among `bib_only`, `metadata_only`, and `transcribed`, duplicate basenames, deletions, and resource-only changes.

## Forthright simplification options

### Option A: Repair bibliography relationships in place

Replace the obsolete ancestor XPath with a lookup built from current `title/@ref` values, while retaining the existing Solr select, atomic update, and soft commit. This is the smallest application change that repairs parent-publication results, but it remains a six-request path and retains the read-after-write race.

Use a complete-field replacement rather than atomic `add` if this phase can be changed without disrupting the current sequence. That allows a refreshed inscription to lose a parent relationship that has been removed or corrected in `titles.xml`.

### Option B: A low-risk consolidation to three calls

Extract direct bibliography IDs locally, calculate the correct related IDs locally, and combine bibliography plus transcription into one atomic update. Send the base document, send one combined atomic update, then make one final soft commit.

This reduces six calls to three and removes the read-after-write race without requiring an immediate rewrite of the main XSL output. It remains an intermediate architecture because one logical document is still split across two updates.

### Option C: One complete document update

Calculate all fields before contacting Solr:

1. Parse the inscription XML.
2. Run the main transform.
3. Obtain direct bibliography IDs from the source or transformed document.
4. Derive the intended related bibliography IDs from a parsed `titles.xml` lookup.
5. Run the transcription transform.
6. Add the derived fields to the main document.
7. Post the complete document once.

This is the recommended target. It eliminates requests 2–5, removes the visibility race, avoids partial enrichment, and makes error handling straightforward. The single post can retain `commitWithin` or use the deployment's selected commit option. If an independent explicit commit is operationally required, use one final commit and accept a two-request design.

The implementation does not need to force every concern into one XSL stylesheet. Bibliography and transcription may remain separate, testable Python/XSL components; they merely need to finish before the Solr boundary. Separation of calculation does not require separation of HTTP requests.

### Option D: Batch full rebuilds

Once complete documents can be built locally, collect them into bounded batches and send each batch in one update request, with commits at an intentional interval. Reuse parsed XSL transformers, a parsed bibliography lookup, and one `httpx.Client` for the processor run.

This changes failure granularity and should preserve useful logging that identifies the inscription responsible for a local transformation failure or rejected batch. It offers the largest performance improvement for full reindexes while incremental single-record updates remain simple.

## Recommended implementation sequence

1. **Implement the confirmed bibliography result.** A child citation must remain findable by its own ID and must also be found through each valid parent referenced by current `titles.xml`. Normalize fragment and bare local IDs, validate their targets, and traverse with cycle protection.
2. **Add focused bibliography tests using current-shaped data.** Include direct-only, fragment parent, bare-ID parent, nested-title reference, future multi-level, missing-target, and cyclic-reference cases. A test fixture should be flat and use the same reference attributes as current `titles.xml`; the existing nested test only verifies the obsolete shape.
3. **Remove the Solr read.** Pass locally extracted direct IDs into bibliography calculation. This immediately eliminates the visibility race and makes the calculation a pure local operation.
4. **Build the complete document before posting.** Insert corrected bibliography IDs and transcription into the base document and use one full-document update. Fail before posting if any required calculation fails.
5. **Choose one commit policy with deployed Solr evidence.** Record the Solr version, update-handler/autocommit configuration, schema requirements for atomic updates, and acceptable visibility delay. Retain only the chosen mechanism.
6. **Rebuild existing Solr documents and verify the webapp end to end.** Check full-text search, collection results, direct and parent publication pages, and browser-rendered inscription details. In particular, parent pages such as AJA and AJP should aggregate the inscriptions currently visible only under child entries.
7. **Make bibliography-resource changes actionable.** Ensure a changed `titles.xml` causes the affected documents—or, initially, the full index—to be rebuilt even when inscription XML is unchanged.
8. **Optimize full rebuilds separately.** Reuse parsed resources and connections first, then add bounded batch posting. Consider targeted filesystem preparation only after the indexing behavior is stable.

## Verification criteria

A simplified implementation should demonstrate all of the following before replacing the current flow:

- A representative inscription produces the same intended base Solr fields as the current `USEp_to_Solr.xsl` output.
- Direct bibliography IDs remain present after repeated reindexing.
- Parent bibliography IDs are correctly present according to the documented flat-file reference rule.
- Publication pages return inscriptions for both the direct and intended parent IDs.
- The normalized transcription is searchable through the webapp's full-text query.
- An inscription without an edition block receives the intended empty/missing transcription behavior.
- A failed local bibliography or transcription calculation leaves the previous Solr document unchanged.
- A successful update becomes visible within the agreed interval without multiple per-record commits.
- Repeating the same update does not accumulate duplicate multivalued fields.
- The copied XML, XSL, `titles.xml`, and XInclude references still support the browser-rendered inscription page.
- Full-reindex orphan deletion and incremental removal behavior remain unchanged.

Because the Solr schema/configuration is absent from the reviewed repositories, the final commit and field behavior should be tested against a development core configured like production rather than inferred solely from client code.

## Code and data reviewed

Indexer documentation and orchestration:

- `README.md`
- `AGENTS.md`
- `usep_indexer_app/lib/processor.py`
- `usep_indexer_app/lib/reindex.py`
- `usep_indexer_app/lib/indexer.py`
- `usep_indexer_app/management/commands/refresh_inscription.py`

HTTP and enrichment implementation:

- `usep_indexer_app/lib/solr_client.py`
- `usep_indexer_app/lib/bibliography.py`
- `usep_indexer_app/lib/transcription.py`
- `usep_indexer_app/tests/test_helpers.py`
- `usep_indexer_app/tests/test_refresh_inscription.py`

Source data and transformations:

- `../usep-data/resources/xsl/USEp_to_Solr.xsl`
- `../usep-data/resources/xsl/transcription_index_val.xsl`
- `../usep-data/resources/xsl/pubs.xsl`
- `../usep-data/resources/titles.xml`
- `../usep-data/resources/titles_old.xml`
- Representative inscription XML under `../usep-data/xml_inscriptions/`
- The `usep-data` history affecting `resources/titles.xml`

Public webapp:

- The sibling `usepweb_project/README.md`
- The sibling `usepweb_project/AGENTS.md`
- `usep_app/views.py`
- `usep_app/models.py`
- `usep_app/search.py`
- `usep_app/settings_app.py`
- `usep_app/usep_templates/display_inscription.html`
- Search, collection, result, and publication templates
- The live Collections, Publications, and representative parent/child publication-result pages linked in this report

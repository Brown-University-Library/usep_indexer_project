# USEP indexing refactor plan

Status: application implementation completed; external rollout checks remain

Prepared: 2026-07-21

## Table of contents

- [Outcome](#outcome)
- [Evidence and assumptions](#evidence-and-assumptions)
- [Front-end consumer contract](#front-end-consumer-contract)
- [Current gaps](#current-gaps)
- [Target design](#target-design)
- [The titles.xml repair](#the-titlesxml-repair)
- [Incremental and full-rebuild behavior](#incremental-and-full-rebuild-behavior)
- [Implementation sequence](#implementation-sequence)
- [Testing and acceptance](#testing-and-acceptance)
- [Deployment and rollback](#deployment-and-rollback)
- [Decisions required before production](#decisions-required-before-production)
- [Files expected to change](#files-expected-to-change)
- [Implementation notes](#implementation-notes)
- [Sources reviewed](#sources-reviewed)
- [Original prompt](#original-prompt)


## Outcome

Refactor the indexer around one rule: build the complete Solr representation of an inscription locally, validate it against the fields the public webapp actually consumes, and only then cross the Solr boundary.

For an ordinary inscription refresh, the target is exactly one complete-document update instead of the current six-request sequence. The implementation must not read the document back from Solr, send bibliography and transcription as later atomic updates, or send a separate visibility/commit request.

For a full rebuild, the same complete-document builder should feed bounded batches through one persistent `httpx.Client`. The target request count is proportional to the number of batches, not six times the number of inscriptions.

This refactor must also repair publication inheritance from `titles.xml`, make index-affecting resource changes trigger rebuilding, and prove that the resulting documents satisfy search, collection, publication, image, date, language, status, and transcription behavior in the public webapp. It must preserve the existing researcher workflow in which the public `usep-data` repository's XSL stylesheets—not hard-coded indexer mappings—remain the primary way XML experts add schema-compatible indexed fields and change the browser-rendered inscription display.

## Evidence and assumptions

### Terminology

The prompt mentions `titlex.xml`; no file by that name exists in the indexer, data, or webapp checkout. This plan treats it as a reference to `resources/titles.xml`, the file used by the indexer and the Publications page.

### Two public outputs must remain separate

The publishing workflow has two consumers:

```text
usep-data inscription XML and resources
                 |
                 +--> webserved_data --> browser XML/XSL transform --> inscription detail page
                 |
                 +--> local indexing transforms --> Solr --> search, collection, and publication pages
```

The detail-page route does not fetch an inscription record from Solr. Django builds the source-XML URL from the inscription ID in the request, and the browser loads the copied XML, `xipr.xsl`, `USEp_display.xsl`, SaxonCE, and resources referenced by XInclude. Search, collection, and publication listings obtain an `id` from Solr; the webapp then constructs the detail-page URL from that ID.

Therefore the refactor must preserve both outputs, but it should not mix their responsibilities. The indexer needs a correct `id`; it does not need to index a detail-page URL. The data preparation step must continue publishing the inscription XML and resources even though those files are not part of the Solr document.

### Live-site confirmation

The live site was checked on 2026-07-21:

- The search form exposes full text, status, date, genre, object type, material, language, writing, condition, special-character, and fake controls.
- A status-only search returned 2,463 transcribed records and result cards containing an ID link, status, description, and optional thumbnail.
- The `CA.Berk.UC.HMA` collection page returned 27 records grouped under Greek, Latin, and Undecided.
- A transcribed inscription page rendered summary, transcription, attributes, credit, bibliography, and images from its copied XML/XSL resources.
- A bibliography-only inscription page rendered its bibliography from its copied XML/XSL resources without a Solr detail record.
- The Publications overview links both `AJA` and child entries such as `AJA_Dennison`. The `AJA` page returned 0 inscriptions while `AJA_Dennison` returned 43. This is the visible `titles.xml` inheritance defect.

### Solr configuration authority

The webapp repository contains `misc/schema.xml`, whose `misc/readme.md` says it tracks the schema used by the USEP Solr instance. It is old and the repository contains no corresponding production `solrconfig.xml`, so it is valuable evidence but not sufficient proof of the deployed configuration. The deployed schema, update handler, autocommit settings, and Solr version must be recorded before production rollout.

### Researcher-owned transformation model

The public `usep-data` repository is not merely a data input. It is an intentionally researcher-controlled transformation layer containing inscription XML, the main TEI-to-Solr XSL, the transcription XSL, the browser-display XSL, and supporting resources. XML-knowledgeable researchers can update those files together so a new element can be indexed and displayed without requiring a corresponding processor-indexer release.

The refactor must preserve this division of responsibility:

- `usep-data` XSL remains authoritative for the general TEI-to-Solr field mapping and browser display.
- The indexer owns orchestration, safe parsing, cross-document bibliography expansion, complete-document assembly, Solr transport, retries, and rebuild decisions.
- The indexer may validate the minimum front-end contract and narrowly replace fields it must derive across resources, such as completed `bib_ids`, but it must not reconstruct the whole Solr document from a Python field allowlist.
- Every additional field emitted by the configured main XSL must pass through complete-document assembly unchanged when it is accepted by the deployed Solr schema.
- The processor must continue mirroring the complete resource tree so newly added XSL files, imports, includes, and display resources become available without an indexer code change.
- The indexer must load stylesheets from the freshly copied/configured `usep-data` resources for each processor run. It must not vendor a private copy of researcher-owned XSL into the application.

Researcher independence is bounded by the Solr and webapp contracts. A new field that already matches an existing or dynamic Solr field can be introduced through XSL alone. A field requiring a schema change, a new server-side search control, or new Django behavior still needs a coordinated programming/operations change. The indexer itself must not create an additional approval boundary.

## Front-end consumer contract

### Consumer-to-data mapping

| Front-end behavior | Solr query dependency | Stored fields read by the webapp | Non-Solr dependency |
| --- | --- | --- | --- |
| Search form options | Facets on `condition`, `language`, `material`, `object_type`, `text_genre`, `writing`, `status`, `char`, `name`, and `fake`; missing-value facet queries for selected fields | Facet values and counts | `include_taxonomies.xml` supplies display labels |
| Full-text search | `text:<user value>` | None from `text`, which is not stored | The configured Solr analyzers and copy-field or equivalent catch-all rules |
| Status filtering | Exact queries on `status`; metadata mode includes both `metadata` and `transcription` | `status` | None |
| Date filtering | Numeric range queries on `notBefore` and `notAfter` | `notBefore`, `notAfter` when dates are shown | None |
| Search result cards | Search query plus facets | `id`, `status`, `text_genre_desc`, `graphic_name`, the `msid_*` components, and optional date fields | Django constructs the inscription URL; `INSCRIPTIONS_URL_SEGMENT` prefixes relative image names |
| Search result grouping | No separate query | `msid_region`, `msid_settlement`, `msid_institution`, `msid_repository` | Django joins the present components with periods |
| Collection page | Prefix query such as `id:CA.Berk.UC.HMA*` | `id`, `msid_idno`, `language`, `status`, `text_genre_desc`, `graphic_name` | Collection title and description come from the webapp database |
| Publication page | Exact query `bib_ids:<publication-id>` | `id`, `status`, `text_genre_desc`, `graphic_name` | Publication title and links come from `titles.xml` and `pubs.xsl` |
| Inscription link | The listing must return `id` | `id` | Django reverses the inscription route from the ID |
| Inscription detail | No Solr query | None | Copied inscription XML, display XSL, SaxonCE, XInclude resources, and images |

The webapp currently requests `fl=*` or relies on the default field list, but the active templates and helper functions use the fields above. Returning every stored field is not itself a requirement.

### Required indexed-document fields

| Field or field family | Requirement | Source and invariant |
| --- | --- | --- |
| `id` | Required, indexed, stored, single-valued | `/TEI/teiHeader/fileDesc/publicationStmt/idno/@xml:id`; must equal the flattened XML filename stem so updates, removals, links, and detail XML resolve to the same object |
| `status` | Required, indexed, stored, single-valued; one of `bib_only`, `metadata`, or `transcription` | Must use the same definition of usable edition content as transcription generation; a `transcription` status must not accompany an absent/empty searchable transcription |
| `text` | Required as an indexed, non-stored catch-all | Must contain the metadata intended for full-text discovery and the normalized transcription; the active webapp queries only this field for full-text input |
| `condition`, `language`, `material`, `object_type`, `text_genre`, `writing`, `char` | Indexed for the advanced-search controls and result facets; stored values support returned documents and diagnostics | Derived from the existing TEI locations used by `USEp_to_Solr.xsl`; preserve multivalue behavior where the schema requires it |
| `fake` | Indexed for inclusion/exclusion and faceting | Omit the field for genuine/unspecified records because the webapp distinguishes `fake:*` from a missing field; do not send an empty placeholder |
| `name` and `name_*` | `name` must be facetable; typed `name_*` values must continue feeding it | Derived from edition `tei:name/@key`; either preserve the schema copy-field rule or construct the aggregate deterministically |
| `notBefore`, `notAfter` | Indexed and stored numeric years | Derived from origin date attributes; omit missing bounds, reject non-integer output, and validate an internally consistent range when both exist |
| `msid_region`, `msid_settlement`, `msid_institution`, `msid_repository` | Stored so search results can construct a collection code; indexed metadata can remain part of full text | Derived from `msIdentifier`; preserve absence rather than inserting empty components |
| `msid_idno` | Stored for collection-page sorting and indexed for metadata search | Derived from `msIdentifier/idno` |
| `graphic_name` | Stored for result thumbnails | First usable graphic URL/name according to the existing compatibility rule; omit when absent |
| `text_genre_desc` | Stored for the description displayed on search, collection, and publication result cards | Derived from the genre description currently passed to the XSL `fieldval` template |
| `bib_ids` | Indexed, stored, multivalued | Complete replacement containing every normalized direct local bibliography ID plus all valid parent IDs derived from `titles.xml`; unresolved direct local IDs remain searchable but produce diagnostics; no duplicates or empty values |
| `transcription` | Indexed, normally not stored | Normalized edition text produced by `transcription_index_val.xsl`; omit it for records without edition content so a full-document replacement removes a formerly present value |
| `title`, `decoration` | Preserve as indexed metadata/catch-all sources unless a measured compatibility review removes them | Derived by the existing base XSL and included in the tracked schema's `text` copy-field sources |
| `c_*` | Preserve while `char` remains the user-facing facet | Derived from `tei:g/@type` and `tei:hi/@rend`; not directly queried by the active webapp but part of the current stored representation |

This table is a minimum consumer contract, not a closed field allowlist. Additional schema-compatible fields emitted by the configured researcher-owned XSL must be preserved unchanged. Additional `*_desc` fields may be retained for compatibility even though only `text_genre_desc` is read by the active result templates; decisions to retire historical XSL output belong in a separately reviewed data/schema change, not implicit indexer filtering.

### Fields that are not active requirements

The unused `Publications` class in `usep_app/models.py` expects `bib_ids_types`, `bib_titles`, `bib_titles_all`, and `bib_authors`, but the active publications view uses the `Publication` class, `titles.xml`, and an exact `bib_ids` query. These legacy bibliography fields are not minimum current front-end requirements. `bib_ids_filtered` is also not used by an active view. The refactor must nevertheless pass them through if the researcher-owned XSL emits them and the schema accepts them; this analysis is not authorization to strip stylesheet output.

The tracked schema declares `place`, but the active webapp does not query or display it and the current indexing XSL does not emit it. The indexer need not require or synthesize it, but it must pass the field through if researchers add it to the indexing XSL.

### File-serving contract for detail pages

The refactor must not regress the preparation stage that provides:

- `webserved_data/inscriptions/<id>.xml` for every flattened winning inscription.
- `webserved_data/resources/titles.xml` for included bibliography data and the Publications overview.
- `include_publicationStmt.xml`, `include_taxonomies.xml`, display/indexing XSL files, and their imported resources.
- The entire researcher-maintained resource tree, including new stylesheet modules and assets not known when the indexer was released.
- Researcher-authored display XSL copied without rewriting its transformation logic.
- Relative XInclude replacements for the three known legacy absolute URLs.
- Existing overlay precedence: `transcribed` over `metadata_only` over `bib_only` for duplicate basenames.

## Current gaps

### Six requests and partial documents

`indexer.update_index_entry()` currently posts the base XML document, queries Solr for direct bibliography IDs, adds inherited bibliography IDs atomically, soft-commits, sets transcription atomically, and soft-commits again. Bibliography and transcription failures are suppressed during normal queued processing, so a successful job can leave a partially enriched document.

The immediate bibliography read also races the visibility of the base update. `commitWithin="500"` does not guarantee that the normal `/select` handler can see the just-written document before the first explicit soft commit.

### The current titles.xml model is flat

`bibliography.add_bibliography()` looks for enclosing `tei:bibl` ancestors. Current `titles.xml` has no nested `bibl` hierarchy; child-to-parent relationships are expressed through descendant `title/@ref` values. The current enrichment therefore adds no parent IDs.

Current data also contains both `#ID` and bare `ID` forms, Unicode IDs, an unresolved `#HCD` reference, and inscription bibliography pointers that can be empty, bare, or external. Stripping every `#` character and treating every target as a local ID is not a safe normalization rule.

### Resource changes do not invalidate indexed documents

An incremental change to `resources/titles.xml`, `USEp_to_Solr.xsl`, or `transcription_index_val.xsl` is copied to the web-served resource tree but ignored by `indexer.update_index()`. Existing Solr documents therefore retain old derived values until a manually requested full rebuild.

A hard-coded list containing only today's stylesheet filenames would create a new version of the same problem. Researchers can add an imported/included stylesheet or reorganize XSL modules without an indexer deployment. Rebuild detection must therefore follow the configured indexing stylesheets' dependency graph after resources are copied, with a safe fallback that treats any uncertain `resources/xsl/` change as index-affecting. Display-only XSL changes still need immediate copying and publication even when they do not require Solr work.

### Full-text transcription is not an established contract

The active search form sends user input as a `text` query. The tracked `misc/schema.xml` defines `transcription` separately but does not copy it to `text`, and the base XSL does not add transcription to `text`. A live search for an exact token visible in a transcribed detail page returned no result. The deployed schema may differ, but the refactor cannot assume that the current atomic transcription update makes transcription discoverable through the active search form.

### Repeated parsing and connections

The current full rebuild reparses `titles.xml`, reparses and recompiles XSL, reparses inscription XML for separate phases, and opens a new top-level HTTP connection for each request. These costs are secondary to the request count but should be removed as part of the same run-scoped design.

### Base/status and transcription predicates can diverge

The base XSL treats any nonempty `ab` below the text body as evidence of transcription, while `transcription.build_transcription()` selects only `div[@type='edition']/ab`. The researcher-owned indexing/transcription transformations should be aligned so status and searchable transcription use the same definition. The indexer should validate that contract and report a mismatch, not silently take ownership of the general status mapping.

## Target design

### Local complete-document pipeline

For one inscription, perform these steps before any Solr update:

1. Parse the flattened inscription XML once with entities disabled and network access disabled.
2. Run the freshly loaded, researcher-owned base XSL transform to obtain one Solr `doc` element, preserving every field it emits.
3. Validate that there is exactly one nonempty `id` and that it matches the filename stem.
4. Extract and normalize direct local bibliography IDs from the parsed inscription or the transformed document.
5. Resolve all valid `titles.xml` parent relationships from the run-scoped bibliography graph.
6. Replace the document's complete `bib_ids` values with stable, deduplicated direct-plus-parent IDs.
7. Build normalized transcription from the already parsed inscription using the compiled transcription XSL.
8. Add or omit `transcription` from the configured transcription transform, preserve the base XSL's `status`, and validate that the two outputs are consistent. Correct a mismatch in the researcher-owned transformations rather than silently replacing their general mapping in Python.
9. Ensure the active full-text field can search transcription according to the chosen schema strategy.
10. Validate the minimum required values, multiplicity, empty-value behavior, numeric dates, and front-end contract without rejecting additional schema-compatible stylesheet fields.
11. Serialize and post the complete document once.

Bibliography and transcription may remain separate pure modules and use separate XSL stylesheets. They do not need separate Solr requests.

### Stylesheet authority and pass-through

The base XSL output is the starting document, not a disposable intermediate used to populate a fixed Python model. Complete-document assembly may inspect fields and replace the narrow derived fields documented in this plan, but it must preserve all other `<field>` elements, including unknown-to-the-indexer names, values, order, and multiplicity.

The minimum-field validation layer should answer questions such as “does this document still satisfy the current webapp?” It must not answer “is this the complete list of fields researchers are allowed to create?” Field-name/type acceptance belongs to the deployed Solr schema. Where feasible, integration checks should read the schema contract rather than duplicate it as application constants.

The transcription path should likewise leave transformation choices in XSL. Prefer passing the complete parsed TEI tree to the configured transcription stylesheet and moving edition selection/normalization rules that researchers may need to change into that stylesheet. If a short compatibility stage is required during migration, keep it narrow, documented, and covered by a test showing that a later stylesheet-only change can replace it.

The browser-display path remains even more direct: copy the researcher-authored display stylesheets and dependencies unchanged, and do not make their publication depend on a Solr rebuild succeeding. File preparation should complete and be logged distinctly before indexing, as it is today.

### Run-scoped resources

After the Git pull and resource copy, introduce a small run-scoped resource object, with naming finalized during implementation, that owns:

- The parsed and compiled base XSL transformer.
- The parsed and compiled transcription transformer.
- The parsed `titles.xml` ID set and child-to-parent graph.
- One configured `httpx.Client`.
- The selected update-request options and batch size.

Single-inscription refreshes create one such object for one document. Incremental batches and full rebuilds create it once and reuse it across every document. The pipeline remains synchronous; concurrency is unnecessary to obtain the main performance gain and would complicate ordering, diagnostics, and Solr load.

### Document representation

Use one canonical in-memory representation between transformation and posting. Keeping an `lxml` Solr `doc` element is the least disruptive option because the main XSL already emits XML and naturally represents multivalued and researcher-added fields. Pure helpers should provide operations such as getting, narrowly replacing, and validating named fields without contacting Solr. They must not round-trip the document through a closed Python dictionary/schema that drops unrecognized fields.

Do not use atomic `add` for `bib_ids`. A full replacement must be able to remove a stale parent, a corrected direct reference, an old transcription, a former fake marker, or any other field that disappeared from source XML.

### Failure boundary

All parsing, relationship resolution, transcription building, and contract validation failures must occur before the document post. Normal queued processing and `refresh_inscription` should use the same strict behavior; the `strict_enrichment` branch and best-effort enrichment wrappers should disappear.

A failed local build leaves the previous Solr document unchanged. A rejected Solr update fails the queued batch so the existing retry mechanism can repeat it safely.

### Solr client boundary

Replace top-level `httpx.get()` and `httpx.post()` use with a focused client object that receives or owns one `httpx.Client`. It should provide complete-document batch update, ID selection for orphan reconciliation, and batch deletion. `select_bibliography_ids()`, bibliography atomic updates, transcription atomic updates, and all explicit commit operations should be removed.

All requests retain an explicit timeout and `raise_for_status()`. Logs should identify inscription or batch IDs, document count, elapsed build/post time, and update-request options without including Solr responses, private URLs, or source data.

### Full-text strategy

The product requirement should be stated as: text entered in the public Full Text Search control finds normalized inscription transcription as well as the intended descriptive metadata.

The recommended implementation is to add and deploy a schema rule copying `transcription` into `text`, update the tracked `misc/schema.xml`, and rebuild the index. This keeps catch-all composition in the schema and leaves the active webapp query unchanged. If production cannot accept that schema change, the alternative is to add the normalized transcription explicitly as a `text` value in the complete document. Changing the webapp to query both `text` and `transcription` is a valid cross-application alternative, but it expands the release beyond the indexer.

Whichever strategy is selected must be covered by an integration test against a development core configured like production. The field definitions and analyzers must not be inferred solely from the old tracked schema.

## The titles.xml repair

### Relationship algorithm

Parse `titles.xml` once per processor run and build a graph using these rules:

1. Index every `tei:bibl/@xml:id` as a valid local bibliography ID, preserving Unicode.
2. For each bibliography entry, inspect descendant `tei:title[@ref]` attributes, not only direct-child titles.
3. Trim surrounding whitespace and remove at most one leading `#` from a local reference.
4. Accept the normalized reference as a parent only when it matches an existing bibliography ID.
5. Record unresolved, empty, nonlocal, and malformed references as validation diagnostics; do not add them to `bib_ids`.
6. Starting from each normalized direct local inscription bibliography ID, retain the direct ID and traverse any known parents recursively with a visited set.
7. Deduplicate while retaining stable output order so transformed documents and tests are repeatable.

Current data is one level deep, but recursive traversal and cycle protection prevent another refactor if multi-level relationships return.

### Direct inscription references

Extract local bibliography IDs only from the inscription bibliography pointers that define USEP publication references. Normalize fragment and bare local forms, reject empty `#`, and distinguish HTTP(S) references from local IDs. Retain a syntactically valid direct local ID even when it is missing from `titles.xml`, while recording a diagnostic and adding no parents; this preserves direct-reference behavior without inventing a relationship. Do not run the old `translate(..., '#', '')` behavior, which removes every hash without validating the result.

The active `bib_ids` field is a publication identifier field, so external URLs should not be mixed into it. If external bibliography targets need search support, define a separate field and product requirement rather than overloading `bib_ids`.

### Data issues and front-end resilience

The indexer should handle both fragment and bare local references, but source data should still be made canonical. A companion `usep-data` change should:

- Canonicalize local parent references to `#ID` so `pubs.xsl` does not create empty/incorrect links from bare IDs.
- Resolve the `ObjectBiographies_Powers -> HCD` reference after a human confirms the intended target; current context suggests `ObjectBiographies`, but the indexer must not silently invent that correction.
- Remove or correct empty `target="#"` pointers and classify external bibliography pointers explicitly.
- Add data validation for duplicate bibliography IDs, unresolved local references, and cycles.

These data cleanups improve the Publications overview, but the indexer repair must not wait for all cleanup to be complete.

### Rebuild trigger

A change to `resources/titles.xml` must rebuild every Solr document whose `bib_ids` could change. The first implementation should promote the already prepared incremental batch to a full index rebuild because it is simple and correct. A later optimization may compute affected descendants and inscriptions only after there are reliable reverse indexes and tests.

## Incremental and full-rebuild behavior

### Incremental updates

For ordinary inscription changes:

- Pull and prepare public files as today.
- Coalesce paths as today so the winning flattened file is indexed once.
- Build and post one complete document per updated ID.
- Delete removed IDs without per-ID hard commits; batch deletes within the job where practical.
- Rely on the document/delete update request and the configured Solr update-handler/autocommit behavior; do not send a separate visibility request.
- Preserve safe repetition when the queue retries a failed batch.

### Index-affecting resource changes

Classify at least these resources as index-affecting globally:

- `resources/titles.xml`
- The configured main Solr XSL source and its transitive `xsl:import`/`xsl:include` dependencies, discovered from the freshly copied stylesheets
- The configured transcription XSL source and its transitive `xsl:import`/`xsl:include` dependencies, discovered the same way
- Any changed path under `resources/xsl/` when dependency discovery cannot prove that it is display-only

When any such resource changes, compile the current stylesheets, validate representative/all documents as appropriate, and rebuild all prepared inscription documents after the single pull/copy operation. This makes a valid researcher stylesheet change effective without an indexer release. Do not perform a second Git pull or copy by calling the current top-level full-reindex workflow recursively.

Changes proven to be used only by browser display should still be copied and published immediately but should not rebuild Solr unless they also affect indexed fields. Keep dependency discovery, the conservative fallback, and display-only behavior explicit and tested. A newly introduced import/include must work without adding its filename to indexer code.

### Full rebuilds

The recommended full-rebuild stages are:

1. Pull source data and validate all source XML before copying or contacting Solr, preserving current behavior.
2. Prepare the flattened/public files and XInclude references.
3. Load run-scoped XSL, bibliography, and HTTP resources once from the freshly copied `usep-data` resources, and record the public data-repository revision in non-sensitive logs.
4. Build and validate complete documents locally. Prefer finishing local construction before mutation so an XML/XSL/data error cannot stop halfway through posting.
5. Query current IDs once and compute orphan deletions.
6. Send complete documents in a configurable bounded batch size and send orphan deletions in a bounded update rather than committing each delete.
7. Send no separate visibility or commit request; visibility and durability follow the options on each batch update and the confirmed Solr update-handler/autocommit configuration.
8. Log document, batch, deletion, failure, and request counts.

If holding every serialized document in memory is measured to be unsuitable, build one bounded batch at a time after a separate validation pass. Do not assume this optimization is necessary for roughly several thousand documents.

### Visibility without a separate request

Before implementation removes the current two explicit soft commits, record:

- Production Solr version.
- Update-handler defaults.
- Soft- and hard-autocommit configuration.
- Whether the existing `commitWithin` update option should be retained, changed, or removed.
- Acceptable search-visibility delay for incremental updates.
- Durability expectations if a full rebuild process or server fails.

Then select the options carried by the update request itself. An incremental refresh still sends exactly one request; a full rebuild sends only its bounded document/delete batch requests. If `commitWithin` is retained, it is an option on those update requests. Otherwise visibility follows the server's configured autocommit behavior. The client must never call a separate soft-commit or hard-commit endpoint.

### Later filesystem optimization

Targeted copying for one inscription is not part of the initial indexing refactor. The existing full flatten/copy behavior resolves overlay precedence, moves, deletions, duplicate basenames, resource changes, and detail-page publication. Optimize it only after complete-document indexing is stable and only with tests for those cases.

## Implementation sequence

### Phase 0: Freeze and test the consumer contract

1. Add representative flat fixtures for `bib_only`, `metadata`, and `transcription` records.
2. Document the deployed Solr schema/configuration and compare it with the webapp's tracked `misc/schema.xml`.
3. Add a contract test that asserts the fields required by the active webapp exist with compatible indexed/stored/multivalued definitions while allowing additional schema-compatible fields.
4. Add a researcher-extension fixture whose base XSL emits an extra field and whose display XSL changes visible output, with no corresponding indexer code change.
5. Record baseline document counts, representative queries, current request counts, and full-rebuild duration in development.

### Phase 1: Pure builders and titles.xml repair

1. Refactor bibliography logic into pure functions that build the `titles.xml` graph and resolve complete IDs from supplied direct IDs.
2. Change transcription building to pass the already parsed inscription tree to the configured compiled transformer, minimizing hard-coded Python selection rules.
3. Add Solr-document field inspection, narrow replacement, pass-through, and minimum-contract validation helpers.
4. Preserve `status` from the base XSL and add a consistency check against transcription output; make any mapping correction in the researcher-owned stylesheet unless a narrow application-derived rule is explicitly justified.
5. Keep this phase free of Solr calls so all edge cases and researcher-extension behavior are fast unit tests.

### Phase 2: One complete update

1. Make `indexer.update_index_entry()` obtain or receive run-scoped resources loaded from the freshly copied/configured `usep-data` tree.
2. Build bibliography and transcription before posting.
3. Post the complete document once while preserving every non-derived field emitted by the base XSL.
4. Remove the Solr bibliography read, both atomic enrichment posts, both enrichment commits, best-effort wrappers, and `strict_enrichment` branching.
5. Select and implement the transcription-to-full-text strategy in the development schema/core.

At the end of this phase, an incremental refresh must make exactly one update request.

### Phase 3: Resource invalidation and shared workflows

1. Add dependency-aware classification for configured indexing stylesheets and their transitive imports/includes, with a conservative `resources/xsl/` fallback.
2. Refactor preparation so an incremental resource change can reuse the already pulled/copied data and run a full index rebuild without duplicating preparation.
3. Treat `titles.xml` and indexing-XSL dependency changes as full rebuild triggers, while publishing display-only stylesheet changes without unnecessary Solr work.
4. Update README and operator logging so a promoted rebuild and the `usep-data` revision are visible.

### Phase 4: Full-rebuild batching and reuse

1. Reuse parsed XSL, parsed bibliography graph, and one `httpx.Client` for the whole run.
2. Add configurable complete-document batch posting.
3. Batch orphan deletions and remove per-ID hard commits.
4. Add request-count and duration instrumentation.
5. Tune batch size against a development core configured like production.

### Phase 5: Data cleanup and production rebuild

1. Land the separately reviewed `usep-data` corrections for canonical parent references and confirmed invalid targets.
2. Deploy any required Solr schema change before sending documents that depend on it.
3. Deploy the indexer.
4. Run one full rebuild so every existing document receives corrected `bib_ids`, transcription behavior, and any schema-derived fields.
5. Complete end-to-end acceptance checks before declaring the refactor finished.

## Testing and acceptance

### Unit and component tests

Add focused tests for:

- Base transform output for each of the three status classes.
- A schema-compatible field added only to a fixture XSL surviving document assembly, enrichment, serialization, and batching unchanged.
- Multiple values and fields unknown to the indexer retaining their values and multiplicity.
- `id` required, unique, nonempty, and equal to filename stem.
- Missing optional fields being omitted rather than sent as empty values.
- Status and transcription using the same edition selection.
- Transcription choice/correction/original, surplus, expansion, names, numbers, line breaks, multiple edition blocks, and no-edition behavior.
- Direct bibliography only.
- `#parent` and bare `parent` references.
- Unicode bibliography IDs.
- Parent references nested inside descendant title elements.
- Multiple parents and future multi-level traversal.
- Missing target, empty `#`, external target, duplicate ID, and cyclic references.
- Stable deduplication of direct and parent IDs.
- Full replacement removing an old parent or transcription on reindex.
- One complete Solr post and no bibliography select, atomic enrichment, or explicit commit request.
- Local bibliography/transcription failure causing zero update posts.
- Solr failure propagating to the queue retry boundary.
- `titles.xml` and indexing-XSL changes promoting a batch to full rebuild.
- A newly added imported/included indexing stylesheet being discovered and its later standalone changes promoting a rebuild without an indexer filename change.
- Browser-only stylesheet/resource changes being copied and published without unnecessarily rebuilding Solr.
- Malformed researcher XSL failing before Solr mutation with an actionable stylesheet/data-repository revision in diagnostics.
- Batch boundaries, deletion batching, client reuse, and request counts.

### Schema/core integration tests

Against a disposable development core configured like production, prove:

- Every required field is accepted with the intended multiplicity and type.
- An additional field emitted by the main XSL is accepted without an indexer code change when it matches the deployed schema or a dynamic-field rule.
- A complete replacement removes formerly present multivalued and optional fields.
- Repeating the same document does not accumulate duplicate `bib_ids`, `char`, `name_*`, or other multivalued data.
- Normalized Latin and Greek transcription tokens are found through the public `text` query path.
- Facets distinguish missing fields from present values, especially `fake` and the null-faceted fields.
- The update-request/autocommit policy makes updates visible within the agreed interval without a separate commit request.
- Batch rejection and retry behavior is understood and logged.

### Front-end acceptance checks

Verify through the webapp, not only direct Solr queries:

- Full-text metadata search returns known records.
- Full-text transcription search returns a record using a token that occurs only in its edition text.
- Status options return the intended inclusive sets.
- Date inclusive/exclusive queries and displayed bounds are correct.
- Genre, object type, material, language, writing, condition, special-character, name, fake, and missing-value facets work.
- Search cards show ID, status, description, image fallback/thumbnail, and date when requested.
- Search results group under the correct collection code.
- Collection pages sort by `msid_idno` and group by language.
- A direct publication page returns its cited inscriptions.
- A parent publication page such as `AJA` aggregates inscriptions from children such as `AJA_Dennison` and no longer returns zero incorrectly.
- A bibliography-only detail page and a transcribed detail page still render from copied XML/XSL resources.
- A researcher-only display-XSL fixture change alters the rendered detail output after resource publication without an indexer application change.
- XInclude bibliography and taxonomy content still loads from the relative copied resources.

### Request-count acceptance

For an update of one inscription:

- Zero Solr reads.
- One complete-document update.
- Zero atomic enrichment updates.
- Zero explicit commit or visibility requests.

For a full rebuild of `N` documents with batch size `B`, document-update requests should be approximately `ceil(N / B)`, plus one ID query and bounded deletion request(s). There is no final commit request. The request count must not scale as `6N`.

### Completion criteria

The refactor is complete when all of the following are true:

- The front-end contract is encoded in tests and documentation.
- The configured researcher-owned XSL remains the authoritative general field mapping, and extra schema-compatible fields pass through without an indexer release.
- Every document is complete before its only update post.
- `titles.xml` parent relationships populate `bib_ids` correctly.
- Transcription is discoverable through the public Full Text Search control.
- Index-affecting resource changes rebuild dependent documents.
- New stylesheet imports/includes are discovered dynamically, while display-only resource changes are published independently of Solr indexing.
- Full rebuilds reuse resources and post bounded batches.
- Existing removal, orphan reconciliation, queue retry, and public-file preparation behavior remains correct.
- Development and production acceptance checks pass after a complete rebuild.

## Deployment and rollback

1. Exercise the complete workflow against a development core with production-like schema/configuration.
2. Capture pre-deploy counts and representative results for status, collections, direct publications, parent publications, dates, facets, images, and transcription-only terms.
3. Back up or snapshot the production core using the established Solr operational procedure before the required rebuild.
4. Deploy schema changes first when applicable, then the indexer code.
5. Run the full rebuild under normal processor locking so webhook and manual work cannot overlap it.
6. Compare post-rebuild counts and representative webapp pages with the acceptance baseline.
7. If acceptance fails, stop queued processing, restore the core snapshot or rebuild with the previous known-good indexer/schema combination, then re-enable processing. Reverting only application code is insufficient after a schema or complete-index migration.

## Decisions required before production

These do not block implementation of the pure builders and one-document tests, but they block production rollout:

1. What are the deployed Solr version, schema, update-handler, and autocommit settings?
2. What is the maximum acceptable delay before an incremental update becomes visible?
3. Should `transcription` reach the active `text` query through a schema copy-field, an explicit document `text` value, or a coordinated webapp query change? This plan recommends the schema copy-field.
4. Is omission of `transcription` for non-transcribed records acceptable? This plan recommends omission because full replacement removes stale values cleanly and `status` already describes availability.
5. Who will confirm the intended target for the unresolved `HCD` parent reference and approve cleanup of other malformed bibliography pointers?
6. What production core backup/restore procedure is approved for the mandatory rebuild?
7. Which existing/dynamic Solr field definitions are intentionally available for researcher-added XSL output, and what is the lightweight coordination path when a genuinely new schema field is needed?

## Files expected to change

Exact names may shift as responsibilities are clarified, but implementation is expected to touch:

- `usep_indexer_app/lib/indexer.py`: complete-document orchestration, minimum-contract validation, narrow derived-field replacement, and lossless pass-through of researcher XSL output.
- `usep_indexer_app/lib/bibliography.py`: pure flat-reference graph and traversal; no Solr access.
- `usep_indexer_app/lib/transcription.py`: parsed-tree/compiled-transformer input; no Solr access.
- `usep_indexer_app/lib/solr_client.py`: persistent client, complete-document batches, deletion batches, and update-request options without a commit endpoint call.
- `usep_indexer_app/lib/processor.py` and `usep_indexer_app/lib/reindex.py`: shared prepared-data workflows, dependency-aware resource invalidation, complete resource publication, and run-scoped resources loaded after copying.
- `usep_indexer_app/tests/`: front-end contract fixtures, researcher-extension/pass-through tests, builder tests, request-count tests, dependency-trigger tests, and batch tests.
- `README.md` and `AGENTS.md`: the new request pattern, researcher/indexer ownership boundary, resource triggers, failure boundary, and safe verification guidance.
- `../usep-data/resources/xsl/USEp_to_Solr.xsl` and `../usep-data/resources/xsl/transcription_index_val.xsl`: remain the researcher-owned authoritative transformations; change them only where researchers approve a mapping correction needed by this migration, and do not vendor or replace their general mapping with Python.
- `../usep-data/resources/titles.xml` and possibly `pubs.xsl`: separately reviewed canonical-reference and invalid-target corrections.
- The authoritative Solr schema and the webapp's tracked `misc/schema.xml`: transcription-to-full-text and any field-definition alignment.

Because `usep-data` and the webapp are separate repositories, their changes should be separate, coordinated pull requests with an explicit deployment order.

## Implementation notes

Implemented in the indexer repository on 2026-07-22.

- `indexer.IndexingResources` is the run-scoped resource object. It loads the configured base and transcription XSL once, parses the `titles.xml` graph once, owns one persistent `solr_client.SolrClient`, and carries the configured batch size and public data-repository revision.
- The canonical representation is a detached `lxml` `doc` element from the base XSL. Complete-document assembly narrowly replaces `bib_ids` and `transcription`, appends normalized transcription to `text`, omits empty values for documented optional consumer fields, and preserves every other base-XSL field, order, value, and multiplicity. The optional-value cleanup was needed because current researcher XSL emits empty description fields for representative bibliography-only and metadata records. Tests include a researcher-only extension field, including an unknown empty value, to enforce the remaining pass-through boundary.
- The full-text implementation uses the plan's explicit-document alternative rather than the recommended Solr schema `copyField`: normalized transcription is emitted as both `transcription` and an additional `text` value. This keeps the change deployable within this repository and leaves the public webapp's `text` query unchanged. A production-like Solr integration check is still required to confirm the deployed `text` field accepts the value and uses the intended analyzers.
- The current researcher-owned transcription stylesheet expects preselected edition content. The implementation therefore retains a narrow compatibility wrapper that passes copied `div[@type='edition']/ab` elements from the already parsed inscription to the run-scoped compiled transformer, then normalizes layout whitespace. The indexer does not reproduce the stylesheet's choice/correction/original rules in Python. A future coordinated `usep-data` stylesheet change can move the edition selection into XSL and remove this wrapper.
- Bibliography enrichment is now entirely local. Direct inscription pointers accept one optional leading `#`, preserve Unicode, reject empty/malformed/nonlocal values, retain unresolved but syntactically valid direct IDs, and recursively add stable, deduplicated parents from descendant `title/@ref` relationships. Duplicate `xml:id` values fail parsing/graph construction; unresolved references and cycles produce diagnostics without inventing corrections.
- One ordinary inscription refresh now sends exactly one complete-document update and performs no Solr read, atomic enrichment update, or separate commit request. Full rebuilds finish local construction before their first Solr request, query IDs once, reuse one HTTP client, and send bounded document and deletion batches. Administrative orphan deletion still reports individual failed IDs, but it reuses one client and no longer embeds hard commits.
- `SOLR_INDEX_BATCH_SIZE`, `SOLR_COMMIT_WITHIN_MS`, and `SOLR_TIMEOUT_SECONDS` were added with defaults of 100, 500, and 30. The source XSL's update wrapper is not reused; the client applies the configured `commitWithin` value to each document/delete update. The production version, update-handler, autocommit, acceptable visibility delay, and durability policy remain rollout decisions because those values are not available in this repository.
- Incremental resource invalidation discovers transitive local `xsl:import` and `xsl:include` dependencies after the resource copy. `resources/titles.xml` and indexing dependency changes promote the already prepared batch to a full rebuild without another Git pull or copy. A stylesheet proven unrelated to either configured indexing root is treated as display-only; uncertainty about a changed stylesheet conservatively promotes the rebuild.
- Incremental inscription changes are coalesced by flattened basename. If an upper-precedence source file is removed but a lower-precedence flattened winner remains, that winner is reindexed instead of incorrectly deleting the Solr ID.
- The full rebuild currently holds all built `doc` elements in memory before mutation, as recommended for the present corpus size. No measured need for the plan's separate-validation/bounded-build fallback was found during repository-only testing.
- The application test suite now has flat bibliography-only, metadata, and transcription XML fixtures; base/transcription XSL fixtures; bibliography graph edge cases; unknown-field pass-through checks; zero-request local-failure checks; exact one-request checks; resource dependency checks; batching/client-reuse checks; and queue-boundary failure checks. `uv run ./run_tests.py -v` and `uv run ruff check .` pass without `.env`, Solr, or a `usep-data` checkout. A separate read-only local check also built one current `usep-data` record from each source class with the real configured XSL/titles resources and an HTTP transport that rejected any network request.
- No files in the sibling researcher-owned `usep-data` repository were changed. Canonical parent-reference cleanup, confirmation of the unresolved `HCD` target, and any future transcription-XSL edition-selection change remain separate reviewed work. No webapp schema repository or live Solr core was available in this workspace, so the schema/core integration tests, live front-end acceptance checks, baseline timing, production backup, deployment, and mandatory production rebuild remain operational rollout work rather than application-code changes.

## Sources reviewed

Indexer repository:

- `README.md`
- `AGENTS.md`
- `REPORT__USEP_indexing.md`
- `usep_indexer_app/lib/indexer.py`
- `usep_indexer_app/lib/bibliography.py`
- `usep_indexer_app/lib/transcription.py`
- `usep_indexer_app/lib/solr_client.py`
- `usep_indexer_app/lib/processor.py`
- `usep_indexer_app/lib/reindex.py`
- `usep_indexer_app/lib/orphans.py`
- Relevant indexer tests and settings

Data repository:

- `resources/xsl/USEp_to_Solr.xsl`
- `resources/xsl/transcription_index_val.xsl`
- `resources/xsl/pubs.xsl`
- `resources/titles.xml`
- Representative `bib_only`, `metadata_only`, and `transcribed` XML

Webapp repository:

- `README.md`
- `AGENTS.md`
- `config/urls.py`
- `usep_app/views.py`
- `usep_app/search.py`
- `usep_app/models.py`
- Search, result, collection, publication, and inscription templates
- `misc/schema.xml` and `misc/readme.md`
- The public USEP site and representative search, collection, publication, and inscription pages


## Original prompt

Goal: Create a refactor-indexer report.

Context:

- The `usep_indexer_project/REPORT__USEP_indexing.md` report makes it clear that indexing can be significantly improved in at least two significant ways: 
  - multiple solr http calls can be consolodated into a single http call.
  - titles indexing needs to be updated.

- Concept: The index-refactor should be driven by what is actually needed by the front-end webapp:
  - code: `/path/to/usep_webapp_stuff/usepweb_project`.
  - url: <https://library.brown.edu/projects/usep/>

- The front-end webapp needs include the detail inscription pages for a couple of different kinds of inscriptions such as those with and without transcriptions. Most of this display, IIRC, is handled via browser-based xsl-transforms, but IIRC the urls come from solr.

- The "search" pages of the front-end webapp are solr-driven.

Tasks:

- Examine the front-end webapp in whatever ways are useful to you: programmatically, via computer-use, etc -- to determine a list of requirements for data-elements that the indexer's processing and indexing components will need to account for.

- Review `usep_indexer_project/README.md` to understand the purpose of this project.

- Review `usep_indexer_project/AGENTS.md` to understand the code and coding-directives.

- Review `usep_indexer_project/REPORT__USEP_indexing.md` to understand an analysis of the current indexing situation -- as well as some guidance for possible improvements.

- Create a plan to refactor indexing, and address the `titlex.xml` issue.

- Save the plan to `usep_indexer_project/PLAN__indexing_refactor.md`

- Before creating the plan, feel free to ask me up to three clarification questions -- if needed -- that may help you implement this goal. Thx!

### Followup prompt

One other important piece of context...

- There is a separate, public `usep_data` github repository that researchers update with inscription-xml.

- That repository also includes resources such as xsl stylesheets.

- That repository is used by this processor-indexer webapp.

- These stylesheets are updated by researchers. This allows the xml-knowledgeable researchers to implement changes to the webapp independently -- they do not need to contact our programming-team for every desired web-interface change.

- Example: if there were some new data element they wanted indexed, they'd be able to update the stylesheet producing the solr doc. And perhaps update the stylesheet producing the front-end inscription web-display, and then begin incorporating that data-element into the inscription-xml files.

Tasks:

- Be sure that your improved indexing plan does not remove the ability of the researchers to perform this independent work.

- Review the existing plan, and incorporate this contextual-requirement into the plan in an appropriate place(s) -- and if need be, update the plan accordingly.

---

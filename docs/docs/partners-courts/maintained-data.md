---
id: maintained-data
title: Maintain search terms and court reference data
sidebar_label: Maintained reference data
---

# Maintain search terms and court reference data

Court code synchronization and partner terminology review are different jobs.
Synchronization copies the upstream catalogs; it cannot establish that a glossary
definition, search synonym or checklist is correct. This inventory covers the
October 13, 2026 audit (`b4be391`). Use the [maintenance register](./maintenance.md)
for named owners, sources, scope, approvals and review dates. Roles below are
proposed responsibilities; no person has been assigned by this documentation.

## Data inventory

Paths are relative to `efile_app/`. Local overrides use each file's schema;
there is no universal `court_specific_requirements` override for every dataset.

| Asset | Owner/reviewer to assign | What changes / scope | Refresh after deploy |
| --- | --- | --- | --- |
| EFSP court, category, case-type and filing catalogs | Proxy/court code steward; LITEfile operator checks copy health | Authoritative provider names and IDs, separated by environment/jurisdiction/court/new or existing stage | Automatic sync or operator resync; [operations checklist](../admin/filing-code-operations.md) |
| `efile/data/filing_code_search.yaml` | Partner terminology editor + court/legal reviewer; search maintainer | Synonyms, stems, stop words, matching/explanation rules; changes discovery, not a submitted Tyler code | Restart web/workers to clear cached rules; rebuild search index |
| `efile/data/filing_concept_definitions.yaml` | Partner editor + local court/legal reviewer | Concept descriptions/sources with jurisdiction, explicit county mapping and exact-court precedence | Web restart; response-only definitions, no catalog rebuild |
| `efile/data/filing_code_facets.yaml` | Partner editor + search maintainer | Labels/classification rules for result facets and local overrides | Web restart; response-only facets, no catalog rebuild |
| `efile/data/case_type_guidance.yaml` | Local legal-help editor + court/legal reviewer | Case-family descriptions, source links, role/context/amount filters and jurisdiction/county/court overrides | Web restart; no catalog rebuild |
| `efile/data/legal_glossary.yaml` | Partner terminology editor + legal reviewer | Short meanings for matched court terms; general `terms` with `jurisdictions` overrides | Web restart to clear glossary cache; no catalog rebuild |
| `efile/data/zip_counties.json.gz`, `efile/utils/zip_to_county_il.py` | Geography-data maintainer + jurisdiction implementer | Census ZIP/county relationships; Illinois PO-box-aware supplement | Rebuild artifact if source changes; restart cached readers and run ZIP examples |
| MACourts/VTCourts packages and State YAML `court_selector` | Package maintainer + jurisdiction/court reviewer | Town/address/ZIP mapping into the live provider court list | Review dependency update and selector fixtures; restart application |
| Massachusetts docket inference | Jurisdiction implementer + Massachusetts court reviewer | Conservative hints in `efile/services/docket_courts.py`; confirm ambiguous cases | Regression fixtures and staging lookup; deploy code |
| Appellate lower-court mappings | Jurisdiction implementer + appellate court reviewer | State YAML `court_selector` choices and lower-court behavior, tracked in [#276](https://github.com/SuffolkLITLab/LITEFile/issues/276) | Test source court/county and appellate alternatives; update fixtures when routes/names change |

For exact discovery schemas and local override precedence, retain the
[jurisdiction reference](./jurisdiction-config.md#filing-code-search-terms-and-explanations).
Definitions/facets decorate results outside cached search responses. Indexed
terms/explanations need an index refresh. None of these aids creates a provider
code or determines legal eligibility. Geographic and inferred-court suggestions
always need filer confirmation; overlapping or unrecognized locations must not
be documented as definitive jurisdiction decisions.

## Correction and catalog-drift recipe

1. Receive the report with jurisdiction, court, environment, case stage, exact
   provider label/code, query, observed result and expected result. Use synthetic
   examples; omit names, docket numbers and document contents from shared issues.
2. Inspect current provider metadata and local sync freshness. If the upstream
   label is wrong or missing, escalate to the code steward. If discovery wording
   is wrong, identify the curated file above. Do not fix terminology by inventing
   a Tyler code. Vermont “Complaint” versus “Initial Filing” remains partner
   terminology work in [#296](https://github.com/SuffolkLITLab/LITEFile/issues/296).
3. Record the authoritative source, supported state/court/case stage, proposed
   wording and reviewer. Check broad synonyms do not imply remedies are legally
   interchangeable. Give local definitions their own source rather than inheriting
   a conflicting general citation.
4. Edit the smallest applicable override. Run representative searches and
   regressions below: positive query, ambiguous query, no-match query, another
   jurisdiction, new/existing stages, ZIP filtering and cleared filters.
   Inspect result explanations and accessible labels, not only the first result.
5. Review changed language/screenshots and translations using the
   [copy workflow](./customization.md). Data-file language is not automatically
   translated merely by running `extract_config_text`; inspect the actual
   translation support for that renderer.
6. Deploy/restart/reindex as the table requires. On staging, apply a selected
   search path and verify it against the current live lists. Post-sync, recheck
   exact-code `filing_guidance` rules and name-bound availability/checklist rules.
7. Record content and technical approval, validation links, deployed scope and
   next review. Roll back the curated file and rebuild again when its indexed
   rules changed. Keep the last good catalog; do not purge it to repair wording.

```bash
cd efile_app
uv run pytest -q efile/tests/test_filing_code_search.py efile/tests/test_case_type_guidance.py
# Only when indexed search/explanation rules changed, in the target environment:
uv run python manage.py refresh_filing_code_index --rebuild --jurisdiction vermont
```

Record representative searches in the asset's register entry, for example:

| Scope | Search | Review expectation |
| --- | --- | --- |
| Illinois, new, name change | `change my name` | Current court choices; distinguish adult/child options without asserting eligibility |
| Massachusetts, existing, housing | `summary process answer` | Existing-case context and appropriate local explanation; keep ambiguous results visible |
| Vermont, new, civil | `complaint` | Inspect offered provider labels and unresolved #296 wording; do not invent an “authoritative” filing code |
| Any supported state | Nonsense query, unknown ZIP, then clear ZIP | Recoverable no-match/location state; statewide results return when filter cleared |

The [ZIP rebuild script](https://github.com/SuffolkLITLab/LITEFile/blob/main/efile_app/scripts/build_zip_counties.py)
takes two positional file paths: the Census 2020 national ZCTA/county relationship
file and the extracted national county-subdivision Gazetteer. From `efile_app`,
after obtaining those source files, run:

```bash
uv run python scripts/build_zip_counties.py tab20_zcta520_county20_natl.txt 2020_Gaz_cousubs_national.txt
```

It writes `efile/data/zip_counties.json.gz`. Record source vintage, retrieval date
and artifact diff, then update ZIP/selector fixtures. MACourts/VTCourts source changes,
provider route renames and appellate mapping changes similarly require fixture
review rather than accepting a dependency update solely because it installs.

## Review record for each dataset

Use one entry per local override or meaningful review scope in the register:
content owner, technical owner, court/legal reviewer, source URL/version,
last **substantive** review, next date/event trigger, jurisdiction/court/stage,
representative searches and results, approval decision and rollback evidence.
A code sync timestamp or successful HTTP request is not a substantive review.
Monthly terminology review and post-sync drift checks are proposed starting
points awaiting owner approval.

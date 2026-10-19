---
id: customization
title: Change wording and local guidance
sidebar_label: Change wording and guidance
---

# Change wording and local guidance

Use this page to locate a change before opening a pull request. It describes the
code at the October 13, 2026 audit, commit `b4be391`, for
[issue #324](https://github.com/SuffolkLITLab/LITEFile/issues/324). A merged feature
does not establish partner approval or deployed enablement. Record those separately
in the [maintenance register](./maintenance.md).

## I want to change…

Paths below are relative to `efile_app/efile/`. **State YAML** means
`static/config/states/<jurisdiction>.yaml`; shared defaults are in
`static/config/base-case-types.yaml`. Merge into existing sections; duplicate YAML
keys can replace earlier settings.

| Visible element | Exact location/key | Default or fallback | Scope and markup | Code change? |
| --- | --- | --- | --- | --- |
| Start page branding and help | State YAML `jurisdiction.brand_name`, `logo`, `logo_alt`, `help_name`, `help_url`, `help_number`, `contact_email` | Base `jurisdiction` settings | Jurisdiction; plain labels, explicit URL fields | No |
| Start menu, upload heading, AI opt-out label | `templates/efile/options.html`, `upload_documents.html` | Translated template copy | Shared; template markup | Yes; no corresponding `text` hook |
| Upload preparation help | State YAML `text.upload_documents.preparation_help` / `preparation_help_unflattened` | UI catalog defaults below; branch depends on `document_preparation.flatten_pdf_forms` | Jurisdiction; inline Markdown | No |
| Pre-upload court notices | State YAML `upload_disclaimers[].name`, `.text` | Configured snapshot; absent means no notice | Jurisdiction; sanitized HTML paragraphs, emphasis, lists and links with preserved line breaks; separate from `text` Markdown | No; requires source review |
| Confirm information court/category labels and help | State YAML `text.extraction_review.court_label`, `court_help`, `case_category_label`, `case_category_help` | UI catalog | Jurisdiction; labels plain, help inline Markdown | No |
| Court questions and choices | State YAML `court_selector.steps[].label`, `.help`, `.options`; see [configuration reference](./jurisdiction-config.md#4-the-court-question) | Flat court list when selector absent | Jurisdiction; selector-specific rendering, not `UIString` | No for existing selector mechanisms |
| Filing-code search terms, result explanations and filters | `data/filing_code_search.yaml`, `filing_concept_definitions.yaml`, `filing_code_facets.yaml`, `case_type_guidance.yaml`, `legal_glossary.yaml` | General entry, then configured local overrides | Per file's schema; [maintained data](./maintained-data.md) | No for existing schema; new controls need code |
| Existing-case confirmation heading and court-owned fields | `templates/efile/case_confirmation.html`; imported court data | “Is this your court case?”; court response | Shared template; court data cannot be reworded as an override | Yes for heading; do not change imported facts |
| Checklist labels, descriptions, side and lead variants | State YAML `case_types.<id>.documents`, `.filer_roles`, `case_categories`; `court_specific_requirements.<court>.case_types` | Inherited base checklist; absent list stays empty | State/case/exact court; schema fields | No; [checklist guide](./document-checklists.md) |
| Finder intent and form/help links | State YAML `document_finder.intents.<id>` (`title`, `summary`, `keywords`, `filing_path`, `checklist.case_type` or `.case_category`, `filer_role`, `lead_filing_type_name`, `resources`, `document_resources`) | No-guide state when absent | Jurisdiction; plain copy, HTTPS resource links | No; [finder guide](./document-finder.md) |
| Organize main document, component and confidentiality help | State YAML `text.organize_documents.*` and `text.terms.*` | UI catalog below | Jurisdiction; prose Markdown only where listed | No for listed hooks |
| People role question/help and contact introduction | State YAML `text.parties.role_question`, `.role_help`, `text.your_information.lede` | UI catalog | Jurisdiction; question plain, prose Markdown | No |
| Individual People/contact field labels | `templates/efile/parties.html`, `party_details.html`, `your_information.html` | Template/form labels | Shared; ordinary translation workflow | Usually yes; confirm actual renderer before changing legacy YAML labels |
| Fees eligibility and waiver-document instructions | State YAML `fee_waiver.benefits`, `.income_bands[].description`, `.document_guidance`, `.source_url` | Benefits/bands omitted if absent; default instruction is to include a waiver form or court order | Jurisdiction; plain escaped copy, separate source URL | No; substantive source/approval review required |
| Case-exemption explanation on Fees | State YAML `fee_estimates.surcharges.platform.exemptions[].note` | No custom note unless the selected configured exemption supplies one | Jurisdiction and configured exemption matching; plain escaped copy | No; does not establish unimplemented exemption coverage |
| Fees contacts and ordinary screen copy | State YAML `court_specific_requirements.<court>.contact`, `jurisdiction.help_*`; `templates/efile/payment.html` for ordinary copy | Jurisdiction help fallback and translated template copy | Jurisdiction/exact court for contacts; explicit URLs; ordinary copy requires template edit | No for contacts; yes for other wording |
| Informational message on Fees, Review or another step | State YAML `filing_guidance[]` | No local guidance enabled by default | Jurisdiction, exact saved codes; plain text, HTTPS resources | No |
| Review heading, summaries and acknowledgment | `templates/efile/review.html`; `services/disclaimers.py` fetches court requirements | “Review your filing”; live requirements | Shared copy; court requirements are separate from curated notices | Yes for ordinary copy; court owns requirements |
| Filing status labels/tracker | `services/filings.py` (`STATUS_PRESENTATION`, `TRACKER_STAGES`), `templates/efile/filing_detail.html` | Neutral presentation for missing/unknown updates | Shared translated copy | Yes |
| Review timing/hours | State YAML `filing_timing`, exact court `.filing_timing` | UTC if timezone missing; no estimate without source/label | Jurisdiction/exact court; explicit source URLs | No; [email and tracker review](./email-review.md) |
| Submission and court-update emails | State YAML `submission_notifications.enabled`, `.format`, `.templates` | Inherited eight-field set; disabled | Jurisdiction; two substitution stages, HTML wrapper | No for copy; transport/sender changes require operator work |

Field coverage remains incomplete in [#74](https://github.com/SuffolkLITLab/LITEFile/issues/74).
The partner configuration editor is separate work in
[#275](https://github.com/SuffolkLITLab/LITEFile/issues/275). The current workflow
is YAML → pull request → technical and content review → staging → deployment.
Partners can request an edit without writing YAML: send proposed wording,
jurisdiction/court scope, approved source and an annotated synthetic screenshot
to their agreed project contact or open a repository issue.

## Supported UI catalog

Every key below is nested under `text` in State YAML. The English default lives
in [ui_text.py](https://github.com/SuffolkLITLab/LITEFile/blob/main/efile_app/efile/utils/ui_text.py).
An omitted or empty override uses that default. These hooks are jurisdiction-wide;
putting `text` under a court does not create a court-specific override.

| Key under `text` | English fallback | Markdown links? |
| --- | --- | --- |
| `upload_documents.preparation_help` | Upload your files as they are. We make PDF copies for court. | Yes |
| `upload_documents.preparation_help_unflattened` | We turn Word files into PDFs. You can check them next. | Yes |
| `terms.starting_document_example` | petition | No |
| `terms.main_document` | main document | No |
| `organize_documents.main_document_question` | Which PDF is the `{main_document}`? | No |
| `organize_documents.main_document_help` | Choose the document that starts this filing, like a `{starting_document_example}`. | Yes |
| `organize_documents.filing_component_label` | Court filing component | No |
| `organize_documents.filing_component_help` | The court may call more than one PDF a “Lead Document”. This is separate from your `{main_document}`. | Yes |
| `organize_documents.filing_component_fixed_note` | Required by the court. | No |
| `organize_documents.filing_component_required_note` | Required by the court for this filing type. | No |
| `organize_documents.confidentiality_help` | Choose confidential only when a court rule or court order requires it. | Yes |
| `organize_documents.optional_services_summary` | Certified copy and courtesy email (optional) | No |
| `organize_documents.optional_services_help` | A certified copy is a copy the court marks as official. | Yes |
| `organize_documents.choose_filing_type_first` | Select a filing type first | No |
| `organize_documents.loading_choices` | Loading choices… | No |
| `organize_documents.no_document_types` | No confidentiality choices are available | No |
| `organize_documents.no_filing_components` | No filing components are available | No |
| `extraction_review.court_label` | Court or county | No |
| `extraction_review.court_help` | Choose the court that should receive this filing. | Yes |
| `extraction_review.case_category_label` | Case category | No |
| `extraction_review.case_category_help` | Choose the broad category that best describes the case. | Yes |
| `your_information.lede` | Check your name and contact information. | Yes |
| `parties.role_question` | What is your role in this case? | No |
| `parties.role_help` | Look for your role next to your name on the court documents. | Yes |
| `about.project_partner_description` | LITEfile is built with legal aid partners who help fund, build, and deploy it for self-represented litigants in their states: | Yes |

## Make a safe wording change

This staging example tests a supported hook. It replaces any existing state
override for that key, including Illinois’s local wording, and adds a suggestion
to ask the clerk. Obtain the court/legal review described below before using it
as public guidance; preserve the prior override for rollback:

```yaml
# Merge into the existing text section of the target State YAML.
text:
  organize_documents:
    confidentiality_help: >-
      Choose confidential only when a court rule or court order requires it.
      Ask the court clerk if you need help finding the applicable rule.
```

1. Record the current wording, proposed wording, target jurisdiction and reviewer
   in the [register](./maintenance.md). Court-policy wording needs a court/legal
   reviewer; a technical reviewer checks configuration and rendering.
2. Edit the existing state section. Run the checks below from `efile_app`.
   Commit the regenerated `efile/config_text_strings.py` alongside the YAML
   change so translation extraction stays current.
3. In staging, open Organize with a synthetic document and an actual confidentiality
   choice. Verify the changed passage and another jurisdiction's fallback.
   Check mobile wrapping, accessible labels, keyboard use and translated screens
   for each enabled language. English is the only shipped language at this snapshot.
4. Attach before/after evidence to the PR, obtain the recorded reviews, and deploy
   the same file to all instances. Existing state files reload on changed file
   metadata; a new jurisdiction or a change to shared `base-case-types.yaml`
   defaults requires an application restart. Revert the
   YAML/catalog changes together if the wording or rendering is wrong.

```bash
uv run python manage.py check --fail-level WARNING
uv run python manage.py extract_config_text
uv run python manage.py extract_config_text --check
uv run pytest -q efile/tests/test_ui_text.py efile/tests/test_filing_guidance.py
```

When changing translations, follow the
[translation workflow](https://github.com/SuffolkLITLab/LITEFile/blob/main/efile_app/efile/locale/README.md):
run `makemessages -l es` after extraction, translate/review the catalog, then
`compilemessages`. Gettext tools and an enabled application language are required;
running extraction alone does not ship a translation.

## Markup, escaping and placeholders

`UIString(..., links=True)` permits inline Markdown links, emphasis, strong text
and code when rendered as prose by the template tag. HTML is sanitized to inline
tags and allowed `http`, `https`, `mailto`, `tel` protocols; headings, images and
lists are not supported. Web links announce a new tab. Plain labels/questions
and strings sent to JavaScript reduce Markdown to text. Do not put a link inside
a control label.

`{brand_name}`, `{state_name}`, `{state_code}`, `{court_name}` and
`{jurisdiction_name}` come from configuration. Each `terms.*` value supplies its
last segment as a placeholder, such as `{main_document}`. Caller parameters can
override values; interpolated prose values are escaped. Unknown placeholders
remain visible and log a warning, so inspect the rendered page for braces.
Use doubled braces for literal braces. UI braces are unrelated to email variables.

`efile.W002` flags unknown `text` keys; `efile.W003` flags links in unsupported
keys. A key's presence in the catalog does not override every similar sentence
in a template. After the copy simplification in #319, the workflow stage “Confirm
filing” and page heading “Confirm information” differ; neither is a catalog hook.

## Choose the right guidance mechanism

| Mechanism | Matching and effect | Links and translation |
| --- | --- | --- |
| `text` | Supported catalog key; changes wording, not validation or provider codes | Contextual gettext; sanitized inline Markdown only for `links=True` |
| `filing_guidance` | All configured conditions must match; any value within a list; exact saved provider codes, including any document's filing code. Shows after saved selections, never blocks | Plain titles/text, escaped HTML; separate absolute HTTPS resources open in same tab; extracted for translation |
| `court_specific_requirements.<court>.filing_availability` | Exact court or supported county prefix; exact authoritative names or explicit full-match regexes; matching rule blocks. Rechecked for supporting documents, saved drafts and submission | Plain message as configured, no Markdown/placeholders or separate `text` translation |

See [contextual guidance](./jurisdiction-config.md#contextual-filing-guidance) and
[availability](./filing-availability.md). Code-bound guidance must be checked after
catalog syncs. `efile.W004` reports invalid guidance. One invalid rule suppresses **all**
configured guidance for that jurisdiction; it does not block filings. An availability matcher needs actual selection tests beyond
`manage.py check`.

## Request an unsupported field

Copy this template into the agreed issue or review channel. Store screenshots in
a gist, capture temporary images under `/tmp`, and use synthetic data.

```text
Jurisdiction / court / new or existing case:
Page heading and route:
Current label (exact words):
Proposed label and help text:
Reason and authoritative source:
Content reviewer / translator / technical reviewer (unassigned if unknown):
Screenshot gist and numbered annotation pointing to the control:
Commit, capture date, viewport, language, scenario, synthetic/live-test scope:
Expected accessible name and any related validation message:
Related implementation issue (#74 for missing hooks):
```

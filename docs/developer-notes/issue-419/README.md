# Choosing the document to read before case extraction

Design exploration for [issue #419](https://github.com/SuffolkLITLab/LITEfile/issues/419).

The source for case suggestions should be an explicit choice before extraction starts. Upload order is a weak signal: the first file may be an exhibit containing another case's caption, parties, or docket number. Even a correctly chosen main PDF can contain those same documents as embedded exhibits. Selecting the file and selecting the relevant pages are related decisions.

This PR contains two portable, clickable prototypes and this design note. It makes **no production changes**, calls **no models**, accepts **no real uploads**, and uses fictional documents and simulated extraction results. Suggestions in the assisted concept are scenario fixtures, not measured model predictions.

## Explore the prototypes

| Concept | Open | Decision being tested |
| --- | --- | --- |
| A: suggest, then confirm | [Open assisted selection](https://raw.githack.com/SuffolkLITLab/LITEfile/prototype-419-main-document/docs/developer-notes/issue-419/index.html?concept=assisted) | Can a bounded source suggestion reduce effort while keeping the filer in control? |
| B: choose and organize | [Open manual selection](https://raw.githack.com/SuffolkLITLab/LITEfile/prototype-419-main-document/docs/developer-notes/issue-419/index.html?concept=manual) | Is an explicit source choice clear enough to prevent the wrong first scan? |

The browser links serve the public PR branch through raw.githack.com. That service may first show a destination-confirmation page; continue to the prototype. The exact same files can run locally using the command below.

The scenario switcher covers an exhibit uploaded first, a combined main document and exhibits, ambiguous candidates, and an unreadable scan. Both concepts support changing the source, restricting the pages used for suggestions, reviewing source/page provenance, and entering information manually.

To run the exact source locally, from the repository root:

```bash
python3 -m http.server 8000 --directory docs/developer-notes/issue-419
```

Then open [concept A](http://localhost:8000/?concept=assisted) or [concept B](http://localhost:8000/?concept=manual). The prototype is plain HTML, CSS, and JavaScript; no application server, dependencies, or credentials are needed.

## What the application does today

These observations are pinned to main commit `53aa27ee82f64154a6a2ae85376e3342436b3f1a`.

- [`upload_files()`](https://github.com/SuffolkLITLab/LITEfile/blob/53aa27ee82f64154a6a2ae85376e3342436b3f1a/efile_app/efile/services/document_uploads.py) assigns the first prepared file the internal `LEAD` role and queues extraction on transaction commit. Later files become `SUPPORTING`.
- The [upload screen](https://github.com/SuffolkLITLab/LITEfile/blob/53aa27ee82f64154a6a2ae85376e3342436b3f1a/efile_app/efile/templates/efile/upload_documents.html) labels the source “Document we read for case suggestions.” It does not offer an initial source choice.
- The [organize screen](https://github.com/SuffolkLITLab/LITEfile/blob/53aa27ee82f64154a6a2ae85376e3342436b3f1a/efile_app/efile/templates/efile/organize_documents.html) already has main-document radios and supporting-document up/down controls. Its [view](https://github.com/SuffolkLITLab/LITEfile/blob/53aa27ee82f64154a6a2ae85376e3342436b3f1a/efile_app/efile/views/organize_documents.py) requires court information and runs after the initial extraction; changing the source clears old guesses and queues a new analysis. Moving this whole screen earlier would bring unnecessary court and filing-code dependencies with it.
- [Extraction](https://github.com/SuffolkLITLab/LITEfile/blob/53aa27ee82f64154a6a2ae85376e3342436b3f1a/efile_app/efile/services/document_extractions.py) only queues lead documents and checks current source and AI preference before outbound analysis and before applying results. [Default settings](https://github.com/SuffolkLITLab/LITEfile/blob/53aa27ee82f64154a6a2ae85376e3342436b3f1a/efile_app/efile/settings_base.py) cap PDF extraction at 20 leading pages, text-classification input at 3 pages, and Word text at 100,000 characters. These bounds can still include embedded exhibits.
- [Existing terminology](https://github.com/SuffolkLITLab/LITEfile/blob/53aa27ee82f64154a6a2ae85376e3342436b3f1a/efile_app/efile/utils/ui_text.py) deliberately separates the main document for suggestions from the court's own “lead document” filing component. The [model](https://github.com/SuffolkLITLab/LITEfile/blob/53aa27ee82f64154a6a2ae85376e3342436b3f1a/efile_app/efile/models.py) also makes the internal lead's filing type authoritative for the draft. Production work must account for that coupling.

## Concept A: suggest, then confirm

A proposed small selector would compare bounded evidence for each candidate separately: filename, page count, first-page title/caption, and an explicitly limited text or image excerpt. Its task is to nominate the document describing the filing being made now, or abstain. It must not merge candidate text into one case-extraction prompt or infer the current case from every uploaded file.

The prototype shows a suggestion with a short reason and lets the filer accept it or choose another file. Full extraction waits for confirmation. Ambiguous or unreadable evidence returns the filer to an explicit choice or manual entry. No numerical confidence is shown because no model has been evaluated or calibrated here.

This approach still reads limited material from multiple candidates when AI is enabled. It reduces the proposed scope of that preliminary read; it does not mean that other files are never examined. A foreign case's pleading can look like a main document on its first page, so a plausible suggestion remains a suggestion.

## Concept B: choose and organize

Multiple files begin with no radio selected. The filer chooses the document describing the filing they are making, checks its preview and page span, then starts the scan. Supporting documents remain available to file. Up/down controls change display order independently of the source choice.

This is the recommended first implementation. It removes the dependency on upload order, uses an interaction already present later in the application, and provides a fallback that concept A will need anyway. For a single-file upload, identify the sole source clearly but still allow the filer to limit pages or enter information manually.

Neither concept silently changes the court's filing order, filing components, or saved case answers when the source changes. Whether the implementation introduces a separate extraction-source field or deliberately reuses the existing internal role is an engineering decision that must preserve the current filing-type behavior.

## Handling the nested-exhibit problem

File selection alone is insufficient for a combined PDF. The page-span control lets the filer identify the portion containing the current main document; the combined fixture makes that distinction visible. The proposed extraction input is only that confirmed span, and each displayed suggestion identifies its source file and page scope.

The span limits the evidence used for case suggestions; it does not remove those pages from the court filing. A future page-boundary suggestion would require its own evaluation. Do not claim that the selector reliably detects embedded exhibits, and do not automatically expand a span when the model is uncertain. If the main document is not contiguous, allow manual entry initially and consider an explicit multi-span editor separately. Word documents need a stable rendered preview or another clearly defined text boundary before promising page-based selection.

## Proposed production state and invalidation

Add a small source-selection stage after file preparation and before full extraction. Keep preparation/conversion distinct from case analysis. Store a server-validated selection bound to the draft:

- Document ID and content revision or immutable content fingerprint.
- Confirmed page span and the corresponding preview revision.
- Selection origin (`manual` or `assisted_confirmed`) and confirmation time.
- Effective AI preference, plus an analysis generation/version used by queued jobs.

Validate ownership, document readiness, readable page bounds, and the current revision on the server. A proposed classifier response must contain an allowed document ID or an abstention; validate the response and treat all document text as data, never as instructions.

Changing the selected ID, replacing its bytes, changing the page span, or changing AI preference invalidates pending analysis and unconfirmed suggestions. Removing the selected document requires another choice; do not silently promote and scan the next upload. Reordering files does not change the selected source. Adding another file should not silently replace an already confirmed source, although the UI can invite reconsideration. Preserve answers the filer already confirmed and distinguish them from new suggestions.

Queue extraction only after source confirmation commits. Each job captures the full selection fingerprint and generation. Before any outbound call, and again in a transaction before saving results, compare those values with the current draft. Superseded jobs must not publish results or overwrite a newer selection. Cover rapid source changes, simultaneous tabs, replacement during analysis, and preference changes in these checks. Scope any result cache by content revision, page span, preference, and extraction configuration.

Honor the existing AI opt-out for both source selection and extraction. With AI off, do not call the proposed model selector; keep manual selection and the existing local-rule path available. Model failure, timeout, abstention, or unreadable evidence must leave a usable manual path without widening the scan to all uploads.

## Review and validation checklist

Use the same scenarios in both concepts and compare the following outcomes:

- An exhibit uploaded first is not automatically scanned as the current filing.
- No full case extraction starts before an explicit source confirmation.
- Changing the radio and changing display order are understandable, separate actions.
- A combined file uses only its confirmed page span; results show that scope.
- Changing a source or span removes stale suggestions before another scan.
- Ambiguity and unreadable text have a clear manual-entry escape.
- AI opt-out produces no model-based selector or extraction calls in production.
- Keyboard users can reach radios, previews, page controls, reorder buttons, and primary actions; every control has a visible label or accessible name.
- Selection, validation errors, scan progress, and result changes are announced appropriately; status is not communicated by color alone.
- Focus remains useful after reordering, source changes, and result transitions; small screens do not require horizontal scrolling to complete the task.

Production tests should verify these state transitions and race protections at the service boundary, not just button visibility. Prototype review cannot establish extraction accuracy, classifier reliability, or legal correctness.

## Evaluation before adding automation

Ship and evaluate the manual source choice first. Then compare a bounded selector with the first-upload baseline and human-confirmed choices on a permissioned, labeled dataset containing exhibits from other cases, combined PDFs, generic filenames, multiple plausible main documents, scanned pages, and relevant jurisdictions. Measure incorrect suggestions, abstention, filer overrides, latency, cost, and whether users understand which pages supplied a result. Evaluate page-boundary detection separately from file selection.

Choose any model, input budget, and release threshold only after that evaluation. This design exploration reports no measured model performance.

# Miro wireframe alignment review

Reviewed 2026-10-17 against the 18 device-screen containers on [E-filing Wireframe](https://miro.com/app/board/uXjVEabHGug=/), using the signed-in browser's accessible outline and a visual check of Confirm case / Organize documents. The connector still denied access. Baseline: clean local `main`; branch: `codex/miro-vision-alignment`.

## Scope and design intent

Use short instructions, “documents” and “e-file” consistently, pop-ups for short optional definitions, and disclosures for longer optional help. Preserve the existing eight stages. The board uses Easy File branding; confirm before replacing LITEfile or jurisdiction branding.

This is not a complete flow specification. It contains alternative home/start layouts and questions about removing the new/existing distinction, clickable progress, and fee-waiver timing. Sample errors and duplicate party names are examples, not behavior to reproduce. Illinois-specific material must remain jurisdiction-specific.

## Screen-by-screen comparison

Implementation paths below are relative to `efile_app/efile/`. “Proposed” items are not implemented.

| Board frame | Implementation | Required changes / current alignment |
| --- | --- | --- |
| General design/information sharing | Shared header, footer, workflow base | Proposed: consistent information hierarchy and optional help; confirm Easy File branding. Get more help/Call already exist where configured, including Illinois Court Help. The simplified footer proposal differs from the fuller footers in other frames: resolve before removing GitHub, Call or credit. |
| Landing / Sign in | `templates/efile/login.html` | Implemented “Sign in” heading and existing-account instruction with state parameter. Email/password, Forgot password, Sign in and Create an account already match. Proposed: Learn more about e-filing link/destination. Keep password visibility. |
| Create an account | `templates/efile/register.html`, `forms.py` | Implemented “Create an account”, “Name”, “Address”. Proposed: “Create an account to e-file in [state]”, contact-reuse note, explicit optional middle-name label. Preserve address line 2 and validation despite omissions in drawing. |
| Activation | `views/register.py`, login messages | Already redirects to sign-in with an activation message. No new route needed. Verify provider email subject/link before adopting exact quoted wording from the board across states. |
| Home options: new case; existing case/draft | `templates/efile/options.html`, `views/draft_views.py` | Implemented My filings / View filings and status description. Proposed: one Start filing card, visible View drafts/count instead of collapsed resume controls, concise state-specific welcome. Keep plans accessible through Menu and preserve fresh-draft isolation. |
| Start: What are you trying to do? | `templates/efile/filing_path.html` | New/existing/unsure options already exist. Small remaining rename: File in → File into. Board asks whether this question can go; recommend deferring to Confirm information, not silently inferring the answer. Awaiting decision. |
| Start: What do you want to do? New account; existing account/filing | Options and workflow progress | Treat as alternative home/start designs, not two new sequential screens. Choose one canonical home with conditional draft card and Find documents/help. Do not repeat a new-sign-in banner on every return. |
| Upload: no documents; rules/AI/documents uploaded | `templates/efile/upload_documents.html` | Implemented Upload your documents, Choose PDF or Word files or drag them here, Learn more about how we use AI. Proposed: concise branch instruction/Change, less routine size/readability/preparation copy, remove redundant document-role sublabels. Keep validation/errors and conversion/extraction states. AI explainer content already matches. Court-notice title must describe actual state notices. |
| Upload navigation | Upload → Confirm information | Board conflicts: empty state says Preview your PDFs, populated state Confirm information. Recommend current Confirm information plus inline PDF preview. A separate Preview screen would change workflow; the legacy route currently maps to Confirm. |
| Confirm case | `templates/efile/extraction_review.html`, corresponding view/script | Proposed: simplified summary; move people editing to People; retain PDF/Change files; section heading Confirm the details used for this filing; code search after selectors; case-number help where lookup occurs. Drawing combines case number and new-case classifications, unlike the app's separate existing-case lookup. Do not copy that mixed state literally. Preserve partial-read/failure/AI-off states. |
| Organize documents | `templates/efile/organize_documents.html` | Mostly matches: main document, name, filing type, confidentiality, component, optional services/courtesy email, PDF. Trim routine guidance; preserve conditional warnings. Single-document example does not imply removing main-document selection or reorder controls for multiple documents. |
| People: your information | `templates/efile/your_information.html`, UI text catalog | Proposed concise Check your name and contact information plus Why do you need this? explainer. Fields and Continue to parties largely match. Keep jurisdiction-specific required fields/state restrictions. |
| People: who is involved | `templates/efile/parties.html`, `party_details.html`, people services | Role, Save role, Add another person, list/Edit/Continue exist. Proposed destination for extracted-name/side/identity editing; removal inside Edit. Preserve deduplication, organizations, representation, notice recipient, required parties and court-owned records. Do not use You already told us when no people were supplied. |
| Fees: fee waiver/add; choose credit card | `templates/efile/payment.html`, payment/waiver scripts | Most controls match. Keep detailed state-specific eligibility behind short trigger; do not reduce benefits/hardship criteria to income only. Preserve exemptions/zero-fee states, waiver PDF review, account removal, bank retry and fee confirmation gates. |
| Review | `templates/efile/review.html` | Implemented short review/Edit instruction. Existing sections, markers, PDFs, court requirements and consent mostly match. Retain missing Documents Edit, conditional questions/correction/plan warnings and Submit and pay when payable. Sample provider errors are not normal copy. |

## Controls absent from the wireframes and required destinations

Absence is not proof of intentional removal. The following must remain reachable or be explicitly redesigned.

| Current control | Recommended destination / dependency |
| --- | --- |
| Home/Menu separate new/existing Begin buttons | Generic Start filing must still create a fresh draft. Ask the branch on Confirm before code selection. Unspecified path currently redirects to the separate question: changing the label alone is insufficient. |
| Upload Add or replace a document select | Per-document Replace or secondary Upload disclosure. Preserve replacement ID, saved filing choices, extraction reset and new PDF review. Remove+upload is not equivalent. |
| Remember this AI choice and saved account preference | Secondary preference disclosure or account settings. No other screen currently manages the account-wide preference. Preserve scope, reversibility and visibility; never enable AI implicitly. |
| Upload Find forms and instructions | Home/Menu or compact Upload help link; preserve draft/return context. Alternative Start frames include Find documents. |
| Confirm names/sides/add/remove/This is me | Move review and persistence to People, including identity matching. Change Confirm POST handling together with UI; omitted rows must not erase saved people. |
| Confirm I checked these details checkbox | Keep beside PDF, or explicitly redesign Continue and server acknowledgement. `needs_acknowledgement` rejects missing `reviewed_extraction=yes`; deleting HTML alone blocks progression. Prepared-PDF fingerprint approval is a separate safeguard that must also remain. |
| Confirm new/existing Change/radios | Keep concise correction and undecided-state choice. Branch changes invalidate incompatible codes, case identity and fees. |
| Confirm Which side of this case are you on? | Before Check documents or in that screen's existing fallback. Side-specific document plans need the answer before People. |
| Existing-case suggested filing type | Keep editable proposal on Confirm or move to scoped Organize filing-type search; it is not yet a court-approved code. |
| Registration street address 2 | Keep optional apartment/unit field or equivalent address component. |
| People This is me / Replace with me / Add me as a party | Preserve conditional identity/name-choice controls before matching. Prevent duplicate account/extracted parties. |
| People Remove party | Inside Edit or overflow control, preserving court-owned-party restrictions. |
| Who are you filing for? / Email for court notices | Conditional People panel for representation and existing cases. The board only draws the not-a-party radio, not its necessary follow-up. |
| Multiple-document main selection / move up/down | Retain for multiple documents; add missing state to design. Single-document omission needs no relocation. |
| Payment Remove account | Compact Manage payment methods or per-account action. No alternate management screen exists. Preserve confirmation and selection clearing. |
| Waiver This copy looks right / Remove and upload a different file | Post-upload preview/check state next to waiver upload. Preserve prepared-file review fingerprints. |
| Review Documents Edit | Keep Organize link with `return_to=review` and access to change files. Otherwise name/type/confidentiality cannot be corrected from this summary. |
| Review Submit and pay | Keep when confirmed fees are payable; board's universal Submit filing obscures payment consequence. Preserve fresh fee and consent gates. |
| Profile sign out/change state | Retain account menu actions; only icon/Menu are drawn. |

## Missing designs versus missing app screens

- Check documents is stage 4 but has no frame. Existing implementation includes plans, side-specific lists, attach/detach, progress and PDF checks. Do not remove the stage.
- Existing-case lookup, no-match/failure, confirmation and imported parties need designs; routes already exist.
- Add/edit person, organizations, required parties, duplicate-name claim and representation need designs; components/routes already exist.
- Case questions/amount in controversy, plans/document finding, drafts, filing status/detail, corrections and successful submission/tracking are not drawn. Their omission does not authorize removal.
- State designs needed: registration/reset errors, extraction failure/partial/AI off, rejection/conversion, multiple documents, payment account creation/removal, unavailable banks, waiver preview and blocked submission.
- New UI work likely needed: contact-purpose explainer, case-number help, consolidated home and approved secondary controls. Activation does not require a new route.
- Progress is currently noninteractive (`components/workflow_progress.html` renders spans). Board asks for clickable stages: recommend completed-stage links with save/dirty-form handling and existing branch prerequisites, not arbitrary jumps past validation.

## Copy to reduce versus conditional guidance to retain

Reduce routine preparation/requirements text, document-role sublabels, repeated introductions and always-visible contact-purpose explanation. Use optional disclosures and short contextual errors. Do not delete partial extraction, branch/court change, confidentiality, court-owned record, eligibility, unavailable-filing or error messages because the happy-path frame omits them. Court acknowledgements and final consent are functional controls. The board retains notices at Upload and Review.

## Decisions requested

1. One Start filing with branch selection on Confirm (recommended), separate question before Upload, or existing split buttons.
2. Move people controls to People and retain explicit review acknowledgement (recommended), replace acknowledgement with Continue confirmation, or retain current layout.
3. Preserve omitted actions in compact secondary controls (recommended), or retain current placement.

Also unresolved: Easy File branding, canonical home variant, scope of clickable progress, and whether a standalone PDF preview is intended. These initial questions were answered; see the approved implementation below.

## Initial copy-only changes and verification

Five template-only files changed: login, register, upload_documents, options, review. No routes, input names, persistence, validation, payment behavior or migrations changed.

- WSL server running at http://127.0.0.1:8000. Updated login/registration observed in browser; fields and navigation intact.
- 64 tests passed: interface consistency, reorganized start, review submission flow, AI opt-out.
- Used `uv sync --group dev`, then `uv run --group dev python -m pytest`; existing pytest executable could not spawn.
- Server started without extraction/index workers or LocalStack startup, and without migrations. Existing `EFSP_TEST_DOCUMENT_URL` substitutes a sample PDF on submission. No live filing/payment; authenticated end-to-end upload/fee flow not browser-verified.
- Copy remains in translation tags; catalogs need normal localization follow-up. English verified.


## Approved implementation after review

The user approved one Start filing action with the branch decision at Confirm information, and compact secondary controls. They clarified that detected roles must be confirmed immediately next to the detection; only undetected roles may be deferred.

- Home and Menu now offer one Start filing action. Generic starts create a fresh, blank-branch draft and go directly to Upload. Back returns to Home. Explicit-path legacy starts and plan starts remain compatible.
- Confirm information asks new/existing before exposing new-case classifications. Detected names, sides and This is me remain beside the extraction, honoring the clarification rather than moving all identity matching to People.
- A side-specific role suggestion must still be confirmed immediately. Without a suggestion or an already confirmed role, that question is deferred; Check documents asks if its checklist needs the side. Normal party details continue on People.
- Document-review acknowledgement remains and now sits beside the PDF previews.
- Upload replacement is in Replace a document. Saved AI preference is in AI preferences for future filings; opening it does not save a preference. Changing the checkbox saves through the existing endpoint.
- Party removal is in the party Edit screen under More options for this person or organization, posting to the same protected removal endpoint with return context.
- Review's document Edit link is under Document options and retains return-to-review behavior.
- Branding, broader layout changes, payment-method management, clickable progress and a standalone preview screen remain outside this approved implementation.

## Verification of the approved implementation

- Broad regression run: 267 passed, 2 skipped, and 2 stale UI assertions failed. Updated those assertions for the renamed path label and native AI-preference disclosure; both passed on rerun.
- Final focused run: 75 passed, including both corrected assertions, navigation, role confirmation/deferral, reorganized start, document previews and interface consistency.
- Ruff, JavaScript syntax checks for both changed scripts, and git diff whitespace checks passed.
- In-app browser inspection of Django-rendered synthetic previews confirmed one Start filing button and working native disclosures for replacement and saved AI preference. These were layout checks with scripts removed, not authenticated end-to-end tests. Temporary synthetic account/session data was removed.
- A separate headless browser check was rejected by automatic approval review with only “blocked by policy”; it was not retried. Authenticated upload, extraction, payment and submission still require an end-to-end browser pass. No live filing or payment was performed.
- Implementation branch: codex/miro-vision-alignment. The WSL development app was checked at http://127.0.0.1:8000 with the limitations described above.

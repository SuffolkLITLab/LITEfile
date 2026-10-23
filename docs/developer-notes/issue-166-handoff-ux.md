# Streamlining the RFA interview-to-filing flow

Reviewed on October 22, 2026 for [issue 166](https://github.com/SuffolkLITLab/LITEfile/issues/166). The RFA changes build on the still-open [sender PR 426](https://github.com/LSVermont/docassemble-RFApackage/pull/426), branch `feature/litefile-handoff`. The shared sender now lives in `SuffolkLITLab/docassemble-LITEfile`, pinned by RFA to v0.1.0.

## Changes in this pass

- Put the next action above the imported information. Group missing fields into one task per destination screen.
- Keep coordinated case and filing choices editable in an optional details section; reserve correction confirmation for actual correction requirements.
- Approve the transferred PDF copies on the handoff page using the existing document fingerprint check. Previously final review redirected the user back through the case-information form solely because those PDFs were not marked reviewed.
- Match ordinary document readiness: honor an explicitly confirmed empty confidentiality choice, and leave filing-component validation to the existing final payload checks. Do not supply new confidentiality defaults. Clerk-requested corrections still block continuation.
- Skip repeated account claiming for an already-owned draft. Use “Your interview” instead of an internal source identifier.
- Add a direct link to the submission section while keeping court terms, the attestation, and the submit action together.
- In RFA, put electronic filing first when enabled; replace download-only introductory and completion text only in that mode. Keep the manual filing instructions when disabled. Prefill service-question city and state from the previously supplied defendant address; leave both editable.

## Independent reviews

Requested read-only reviews from `agy --model gemini-4-argon-high --mode plan` and Claude CLI in plan mode. Both completed. Argon traced the original flow; Claude also inspected the in-progress edits. These were source reviews, not independent live staging sessions.

Both identified the duplicate case-confirmation loop, buried actions, outdated shared Vermont text, and optional document-setting inconsistencies. Claude also caught that returning to the interview updates PDFs without synchronizing the saved filing metadata. The action now says “Fix a PDF in my interview” and explains that changed names, addresses, or court details must also be edited in LITEfile. Model findings were checked against the implementation; proposed blanket PDF approval and unverified confidentiality defaults were not adopted.

## Proposed next improvements

1. **Synchronize interview corrections:** design a revision protocol for changed names, addresses, county, and added/removed forms. The current replacement endpoint deliberately updates only known PDFs. Do this before promising an unrestricted “Edit answers” round trip.
2. **Verify confidential contact handling:** RFA asks `share_contact`, while its sender exports contact fields to filer/party records. Check which fields the target EFM exposes and represent the interview's privacy choice explicitly. Do not treat PDF confidentiality as proof that party metadata is confidential.
3. **Shorten the shared sender transition:** consider continuing directly after a successful transfer, while retaining the retry/recovery screen on failure. This belongs in `docassemble-LITEfile`, with a package release and RFA dependency update.
4. **Reuse account email carefully:** a missing interview email currently opens a form that defaults to the account email. Consider using that known value earlier while preserving an obvious way to choose a safe contact address.
5. **Unify filing instructions:** review the RFA NEXT STEPS document alongside the new online path, so users understand when they have already submitted and what remains to do. Do not change legal or emergency-filing instructions without partner review.
6. **Resolve only verified choices:** add maintained semantic aliases or single required-component handling where live metadata supports them. Leave ambiguous matches and confidentiality decisions visible; do not guess that ordinary RFA forms should be public.

## Validation and limits

- Handoff, final-review, and PDF-preview tests: 89 passed before the optional-settings refinement; final handoff suite: 43 passed. Ruff and Ty pass.
- Parsed the RFA YAML and rendered its introduction/download Mako templates with synthetic helpers in both enabled and disabled modes. Confirmed the enabled mode omits the contradictory manual-only instructions and puts the filing action before downloads.
- Chromium at 390 × 844: ready/missing handoff primary actions are above the fold; the saved-details disclosure opens and closes with Enter. [Synthetic screenshots](https://gist.github.com/nonprofittechy/841e3dd9efb519a3cc91e481aa1f1a40).
- These checks do not establish a successful Vermont test filing. Full Docassemble execution, test-EFM submission, court receipt/status, and Vermont partner acceptance remain outstanding under issue 166.

## Independent code review and follow-up

Claude and agy/Argon reviewed the full implementation through `0a10c55` independently. Their raw reports are retained in the unlisted evidence gist; some report line numbers and symbol names were inaccurate, so findings were checked against the source and regression tests.

Confirmed issues addressed in the follow-up:

- Restrict the focused replacement lookup to new cases. Existing-case drafts keep the court-record lookup and case-specific permitted filing types; drafts with an undecided case stage no longer show unusable replacement links.
- Resume matching of dependent interview hints after a successful replacement. Keep metadata matching out of PDF approval actions so metadata-only timestamp changes cannot invalidate the displayed fingerprint.
- Approve the displayed PDF copies when continuing to missing details, avoiding duplicate review at payment; stale fingerprints still block approval.
- Reset blank confidentiality confirmation when its filing-type scope changes, including clerk corrections. Carry the live amount-in-controversy requirement into document metadata.
- Preserve all saved codes, refresh conflict results from current records, and recover when a document disappears during live validation. Write one provenance event per selected replacement.
- Seed lookup from one relevant hint rather than combining alternative names into an AND query; preserve the lead document's top-level hint fallback. Surface unmatched filing and party-role hints outside the accordion.
- Display unavailable results with their explanation and no selection control, with a link to later result pages.

Validation uses synthetic court responses. It does not establish a live Vermont EFM acceptance test. The focused replacement route does not replace the existing party-role or confidentiality editors.

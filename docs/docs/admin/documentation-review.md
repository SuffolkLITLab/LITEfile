---
id: documentation-review
title: Review documentation and screenshot evidence
sidebar_label: Documentation review
---

# Review documentation and screenshot evidence

The current documentation audit uses October 13, 2026 `main` at `b4be391`, as
specified in [#324](https://github.com/SuffolkLITLab/LITEFile/issues/324). Capture
temporary images under `/tmp`, store validation screenshots in a gist and link
to them from notes/PRs. Do not commit screenshots or put them in developer notes.
Internal notes stay outside `docs/docs/`.

## Screenshot register

| Evidence | PR/commit and capture date | Jurisdiction/scenario | Scope and current use |
| --- | --- | --- | --- |
| User guide screenshots in `docs/static/img/user-guide/` | `1138b29`; 2026-11-03 | Illinois new small claims case (DuPage) and existing case lookup (Sangamon test case) against the Tyler staging proxy; 1280px and 390px | Committed documentation images used by every user guide page. One staging submission (envelope 325803, test card, `EFSP_TEST_DOCUMENT_URL` sample PDF). AI reading was not configured locally, so Confirm information shows the manual path. The test account name, staging case captions and parties were replaced with the fictional Jordan Rivera and Sam Parker before capture; no labels, statuses or amounts were changed |
| `docs/static/img/user-guide/confirm-existing-case.png` | Issue #396; 2026-11-03 | Illinois existing case with a suggested filing code; 1280px | Local Chromium capture with a synthetic test-database draft and fictional document. No staging or court calls. Replaces the earlier manual-path capture of this file. Used on the Step 6 user guide page |
| [Document confirmation and filing options gallery](https://gist.github.com/nonprofittechy/f80a6ef9e2010d318ffc60bb36cb6068) | Issues #406 and #409; 2026-11-05 | Illinois new/existing cases, later uploads, service quantities, contact details, roles, review and fee waiver; 1280px and 390px | Local Django test database and synthetic court/account responses with fictional Jordan Rivera and Sam Parker. No live court calls, filing or payment. Refreshed nine public guide images: `confirm-new-case`, `confirm-existing-case`, `organize-existing-case`, `find-filing-type`, `check-documents`, `choose-role`, `add-person`, `review`, and `fee-waiver-upload`. Sample fees are illustrative. Mobile and extra validation captures are in the unlisted gist only. Copy reviewed with agy / Argon for clarity and approximate sixth-grade readability (qualitative assessment) |
| [Current finder gallery and manifest](https://gist.github.com/nonprofittechy/e88c3fce4148bc53c4a425d0fa3f67cc) | #324 audit; `b4be391`; 2026-10-13 | Illinois ambiguous name-change search and adult guide; Massachusetts no-guide state, 1280px/390px | Fresh anonymous local Chromium captures, configured content/test database. Six HTTP-200 screens, expected headings, no horizontal overflow. No account data, live court calls, filing/payment/email. IL substantive approval remains pending |
| [Existing-case screens](https://gist.github.com/nonprofittechy/b855c1c8c3a69e8cd694f8ee3909b5a3#file-01-case-flow-html) | [PR #319](https://github.com/SuffolkLITLab/LITEFile/pull/319); `719cfed`; 2026-10-13 UTC | Massachusetts Confirm information, lookup/confirmation, changing inferred court; Vermont Organize | Synthetic browser/court/storage fixtures. Current labels/path checked against `b4be391` templates; not live-provider compatibility |
| [Fees and Review screens](https://gist.github.com/nonprofittechy/b855c1c8c3a69e8cd694f8ee3909b5a3#file-02-fees-and-review-html) | PR #319; `719cfed`; 2026-10-13 UTC | Illinois waiver PDF check, replacement and return-to-Review, desktop/mobile | Synthetic; current in-place/review behavior, no actual waiver decision/payment |
| [People and account screens](https://gist.github.com/nonprofittechy/b855c1c8c3a69e8cd694f8ee3909b5a3#file-03-people-and-account-html) | PR #319; `719cfed`; 2026-10-13 UTC | Contact/parties/person/questions/registration; configured jurisdiction fixtures described in source | Synthetic; state-list fallback notice reflects the fixture, not a promise of live missing court data |
| [Filing tracker screens](https://gist.github.com/nonprofittechy/b855c1c8c3a69e8cd694f8ee3909b5a3#file-04-filing-status-html) | PR #319; `719cfed`; 2026-10-13 UTC | Tracker sign-in, accepted and returned synthetic filing | Current status presentation; no live acceptance/rejection callback or email signoff |
| [Earlier preparation corpus](https://gist.github.com/nonprofittechy/54857d2ed0b841a486dbf7a30b1ad915) | #113 validation; 2026-09-30; source version in its [validation note](https://github.com/SuffolkLITLab/LITEFile/blob/main/docs/developer-notes/issue-113/validation.md) | Vermont form answers/signature/multiline; synthetic Word/PDF conversion | Older screen UI, including retired Preview step. Reuse only for PDF-content comparison, not current navigation |
| Earlier `docs/static/img/screenshots/01…14` walkthrough images | Capture version/date/scope not recorded | Older fixed-step walkthrough | Removed from published static assets after a reference audit; preserved in Git history, not current evidence |

The user guide embeds committed screenshots from `docs/static/img/user-guide/`
with alt text and captions. Validation-only evidence stays in gists, as below.
Retake the user guide screenshots when a screen's labels or layout change.

The full gist HTML galleries embed PNGs with descriptions/alt text. GitHub shows
HTML source: select Raw, save as `.html`, and open locally to view/download the
screenshots. The PR #319 and current finder manifests include hashes and labels.
Keep these source links even when embedded images fail to load. Describe the
screen and task in alt text; a filename alone is insufficient. Any demo-only
restriction needs a visible caption saying it is a demo, plus the actual deployed
scope in the register.

## Publication checklist

1. Reconcile wording/paths with current templates, `efile/workflow.py`, settings
   and configuration; check both new/existing cases and conditional detours.
2. Run `manage.py check --fail-level WARNING`, relevant existing configuration
   and matching regressions, and `extract_config_text --check` from `efile_app`.
   For changed application copy, regenerate extraction and review enabled-language
   catalogs/screenshots/accessibility using the [translation workflow](../partners-courts/customization.md#make-a-safe-wording-change).
3. Check content links, navigation and anchors, then run `npm ci` and
   `npm run build` from `docs`. Preview the built site and check tables, code
   blocks, sidebar links and mobile navigation. Do not add internal notes to
   the sidebar or Docusaurus source tree.
4. Record commands/results, source versions, screenshot provenance and remaining
   gaps in the internal review record linked from the
   [maintenance register](../partners-courts/maintenance.md). Documentation tests
   do not replace court approval or the release/accessibility matrix in #200.
5. Have a partner/editor complete one supported configuration change from the
   [wording recipe](../partners-courts/customization.md#make-a-safe-wording-change)
   without reading Python. Record reviewer, task, proposed/source-approved copy,
   checks, accessible/translated evidence, confusing steps and rollback. This
   human handover exercise is pending until an external reviewer performs it.

The audit's delivered pages and automated results are recorded in the internal
[handover note](https://github.com/SuffolkLITLab/LITEFile/blob/main/docs/developer-notes/issue-324-documentation-handover.md).
It records outstanding human approval separately from completed documentation.

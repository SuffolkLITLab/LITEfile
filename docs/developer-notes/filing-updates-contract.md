# Filing updates contract

This note records the verified contract for LITEfile filing emails and the upstream EFSPProxy. It is an implementation and release checklist, not evidence of partner approval or production delivery.

## Earlier configuration hook audit (October 13, 2026)

The implementation update below supersedes this earlier gap audit for conditional waiver text, contact fallback, and court calendars.

This is a **source audit**, not verification that the requested partner customization has been deployed or signed off. See the [partner copy-packet format](../docs/partners-courts/email-review.md) and issues [#335](https://github.com/SuffolkLITLab/LITEFile/issues/335), [#328](https://github.com/SuffolkLITLab/LITEFile/issues/328), [#329](https://github.com/SuffolkLITLab/LITEFile/issues/329), and [#331](https://github.com/SuffolkLITLab/LITEFile/issues/331).

- **Current rendering model.** `efile/services/submission_notifications.py` reads `submission_notifications.templates` from the merged jurisdiction configuration and uses Python `string.Template.safe_substitute`; it does **not** run arbitrary Jinja2/Jinjava templates in LITEfile. All eight subject/body fields must be nonblank in the *merged* config, otherwise the app omits the entire custom set. Browser-injected email fields are stripped. The proxy later runs Jinjava on **bodies only**; subjects go to the provider without that second rendering. Raw upstream expressions authored by a partner are not a defined/tested LITEfile customization interface.
- **Existing approved input vocabulary is narrow.** LITEfile substitutes `$tracker_url`, `$help_line`, and `$waiver_line`. The first is a local tracker URL, the second formats `court_contact()` data, and the third is an **optional request note** when `selected_payment_account_type == WV` or `has_waiver_document()` is true. `$waiver_line` is a single hardcoded message in `_waiver_line`, not arbitrary partner-supplied text. It does not know if a later fee waiver was approved, nor distinguish a statutory no-fee exemption as a separate event. If the same fixed message suffices it can be inserted into any of the four *body* fields without a new template engine.
- **#328 gap.** No tested config mechanism currently lets partners define `if / else` paragraphs keyed by explicit facts (e.g., fee waiver requested vs not, statutory exemption vs hardship application, document type, court, or court's later ruling). Add a small, server-owned conditional text contract or typed `when` rules rather than silently treating raw proxy Jinjava as supported. Define allowed boolean/enumerated facts, availability at submission versus callback, precedence, omission/unknown behavior, escaping and both transport formats; test all four events, waiver/nonwaiver/exemption, and subjects separately. Do **not** infer waiver acceptance from WV selection or attached paperwork.
- **#329 existing hook and available ILAO data.** `efile/services/filings.py::court_contact` supports exact `court_specific_requirements.<court_code>.contact` with `name`, `phone`, `email`, and `url`, falling back to the state `jurisdiction.help_*` fields. `$help_line` formats that contact for any body containing the token; `filing_detail` uses the same source. **Do not solicit a new Illinois-wide clerk directory by default:** the [ILAO Appearance with e-filing interview](https://github.com/IllinoisLegalAidOnline/docassemble-AppearanceEfile/blob/main/docassemble/AppearanceEfile/data/questions/appearance.yml) already loads [the ILAO shared court list](https://github.com/IllinoisLegalAidOnline/docassemble-ILAO/blob/main/docassemble/ILAO/data/sources/il_courts.xlsx) through `ALCourtLoader` and uses `trial_court.phone` in its submission/status email templates. Engineering should assess the list's update/usage terms and freshness, compare the public [Illinois Courts directory](https://www.illinoiscourts.gov/courts-directory/), and build an explicit, reviewed ILAO-record-to-Tyler-code mapping, including Cook division exceptions. The inspected [proxy `CourtLocationInfo` model](https://github.com/SuffolkLITLab/EfileProxyServer/blob/main/proxyserver/src/main/java/edu/suffolk/litlab/efsp/tyler/ecfcodes/CourtLocationInfo.java) contains codes/names/settings but no clerk-phone field; this does not exclude another Tyler service. ILAO also [documents a separate clerk/general-phone lookup](https://github.com/IllinoisLegalAidOnline/website-docs/blob/main/docs/source/dhi_helpful_functions.rst); evaluate independently, not as an assumed production dependency. Partners should verify ambiguous, missing or stale results and local-vs-state contact policy, not re-enter a list already stored by ILAO. This is primarily mapping, data maintenance and regression testing—not demonstrated blocked partner research.
- **Partial-contact fallback bug/risk.** The current `court_contact()` copies a nonempty local `contact`, then uses `setdefault` to fill missing `url` and `phone` from statewide fields. If a court supplies only a local clerk **name**, `$help_line` and the filing detail can display that name with a **statewide** number/URL without identifying the distinction. Before closing #329/#331 either require complete local contact entries and test them or implement an explicit, labeled two-tier fallback. Also confirm courts with no local data receive the proper statewide name.
- **No current per-court template selection.** Text templates are selected at jurisdiction level; *court contact data* varies by court, but arbitrary **court-specific subject/body copy** is not a documented config hook. A sender-name/domain change is upstream operations/configuration, not an email body override. If partners request per-court or per-filing-type copy or sender identity, scope those as separate additions and validate the proxy delivery contract.

### Closure evidence for #328/#329

Partner packet should include, per event, exact approved subject/body, court scope, each conditional sentence with explicit true/false triggers, a reused contact source plus mapping/exception reviewer (not a newly created statewide spreadsheet), and rendered previews. Engineering should exercise both `format: html` (SendGrid) and `format: plain_text` (SMTP), missing/partial/local/statewide contact, with/without detected waiver, statutory exemption distinct from waiver request, neutral/literal `returned` status, and inaccessible/unrecognized callback metadata. Sender/domain approval and callback delivery remain #157/EfileProxyServer #375 release gates. Do not enable notifications merely because a YAML string renders.

## Email ownership and contract

LITEfile does not send the filing notifications directly. It selects server-owned, jurisdiction-configured subject and body text, then includes those fields in the filing request. EFSPProxy sends the submission confirmation and later callback messages. Tyler may also send its own messages, including account activation. The Illinois partner issue asks that LITEfile-configured notices continue alongside Tyler messages, use an Illinois-branded sender domain, and have jurisdiction-specific subject/body text. It also calls out Tyler Odyssey activation mail as a separate, earlier message that needs an explanation during onboarding. See [LITEfile #157](https://github.com/SuffolkLITLab/LITEFile/issues/157).

The upstream JSON contract in `docassemble-EFSPIntegration/efm_client.py` forwards these per-filing template fields:

| Event | Payload fields |
| --- | --- |
| Submission confirmation | `email_confirmation_subject`, `email_confirmation_contents` |
| Accepted status | `acceptance_subject`, `acceptance_contents` |
| Rejected status | `rejected_subject`, `rejected_contents` |
| Other status | `neutral_subject`, `neutral_contents` |

The proxy sends one confirmation for a submitted envelope. Its `transaction_id` template value joins the envelope's transaction IDs, and the confirmation also receives `envelope_id`. A later callback is handled for an individual filing transaction and its template variables include that transaction ID. The callback context has no `filing_id` field, so callback email copy must not assume it can link directly to one filing by ID. LITEfile's configured tracker URL points to the local submission's random `public_id` (a UUID, so links can't be walked to find other submissions); when that submission contains multiple filing IDs, the tracker asks the filer to choose which filing to view. The current copy's wording “A filing in your submission” reflects that individual filings in one submission can reach different outcomes.

EFSPProxy renders the callback status templates with `name`, `court_name`, `case_type`, `case_title`, `status`, `message_text`, `transaction_id`, and `message_url` when the callback supplies a nonblank URL. Its confirmation template also has `envelope_id`; it uses the same name, court, case type, title, and transaction ID values. EFSPProxy renders bodies with Jinjava. Its SendGrid transport sends `text/html` when the rendered body starts with `<!DOCTYPE html>`, otherwise `text/plain`. Its SMTP transport always sends plain text; it does not currently create multipart alternatives. Its REST guide describes the template fields and variables; implementation is in `OrgMessageSender.java`.

For upstream confirmation, the proxy chooses `users[0].email` when `users[0]` is marked as the form filler and has an email. Otherwise it chooses `lead_contact.email`, which is required when the form-filler is not among the users. The Docassemble client serializes both fields when present. For a later court callback, the recipient is the email stored on the proxy transaction at filing time. The sender is the proxy server's configured `fromEmail`; when unset, the current proxy source falls back to `massaccess@suffolk.edu`. Transport uses `EMAIL_METHOD` and either SendGrid (`SENDGRID_API_KEY`) or SMTP settings. Do not treat the legacy default as the approved Illinois sender.

Upstream callback statuses are `ACCEPTED`, `REJECTED`, or neutral/other. `UpdateMessageStatus.fromStr` maps the raw status strings `accepted` and `rejected` to the first two categories; `returned` and unknown strings map to neutral. The template variable named `status` is separate: the callback fills it with the court's configured status display name when available, otherwise the raw status code or status text. When the court config has `showreturnonreject`, the callback's `message_text` says “returned” while its category remains `REJECTED`. As a result, the rejection template is used for that configured rejection, but a literal `returned` status code selects the neutral template. Do not assume a template conditional can infer the category from `status` in every court configuration.

## Delivery and duplicate behavior

The upstream sender considers HTTP-style results 200, 202, and 204 successful; it returns false for other results, an invalid/missing recipient, or a caught `IOException`. The callback logs a failed send and still returns an OK callback receipt. Filing submission similarly invokes `sendConfirmation` without acting on its boolean result. The inspected callback and submission paths do not provide an idempotency guard around notification sends. A repeated status callback can therefore send another email, while a failed email does not make the callback retry guarantee visible to the caller. These are observations of the proxy code. The live delivery checks below confirm provider acceptance and receipt separately.

## Related work and release gates

- [LITEfile #297](https://github.com/SuffolkLITLab/LITEFile/issues/297) requests customizable submission, acceptance, and rejection messages, including tracker link, next steps, and local court/help contacts. The existing upstream contract carries templates and basic filing/court status values; it does not itself prove those jurisdiction-specific contents are configured or approved.
- [LITEfile #229](https://github.com/SuffolkLITLab/LITEFile/issues/229) defines the filing detail tracker and its status presentation. Keep email status language aligned with the tracker and concise: state what happened, what the filer needs to do, and only context needed to act. Do not repeat status banners or include inapplicable guidance.
- [EfileProxyServer #375](https://github.com/SuffolkLITLab/EfileProxyServer/issues/375) tracks per-server SendGrid API credentials. The issue remains open in the inspected GitHub state. Discussion rejects storing API keys directly in the database and considers secret storage. It does not establish that per-jurisdiction sender domains or credentials are implemented.

The filing detail page presents `STATUS_PRESENTATION` labels and a tracker derived from the existing filing status. Its normal path is Sent → Received → Clerk review, followed by the actual outcome. `submitting` maps to Sent; `submitted` and `receipted` map to Received; `under-review` and `reviewed` map to Clerk review; `accepted` ends at Accepted; `rejected` and `returned` end at Returned for changes. `failed` and `cancelled` stop progress, `served` is presented separately, and unknown statuses use a neutral message. A blank status says the filing was sent, not that the court sent an update. See `efile/services/filings.py` (`STATUS_PRESENTATION`, `TRACKER_STAGES`, and `filing_tracker`). Email links should land on the submission tracker and let a filer select among multiple filings when needed.

Before release, verify the following with configuration and an end-to-end delivery check:

1. Decide which channel handles each event so LITEfile-configured proxy messages and Tyler messages do not duplicate or contradict one another. Keep Tyler account activation mail identified separately.
2. Confirm jurisdiction-specific sender name/address/domain, provider credentials, and email authentication with the partner/operations owner. No partner approval or production sender configuration is evidenced by the code/issues reviewed here.
3. Confirm the configured short copy for submission, accepted, rejected, and neutral statuses. Account for the upstream mapping where a literal `returned` status selects the neutral template, and where `showreturnonreject` only changes callback message text. Each email should plainly state the event, the filer action (if any), and the minimum useful context. Do not claim an action is required when none is.
4. Exercise successful delivery, invalid/missing recipients, provider errors, duplicate callbacks, and retries. Confirm what the filer and support staff see when sending fails.
5. Verify the tracker URL uses the correct submission reference and that the destination gives the filer the detail and correction path described by the status email.
6. Confirm final filing-detail access remains authorized. LITEfile now checks ownership when a detail ID is present in another local draft, while remote-only filing history remains protected by the user's Tyler token. The proxy passes that user's credentials to Tyler for the requested filing ID and has no separate filing-owner lookup. Verify Tyler enforces record access for that credential, and preserve access to legitimate history entries that have no local `FilingDraft`.

## Indexed filing ownership

Migration `0044_filing_identifiers` creates searchable filing identifiers and backfills saved submission responses in bounded batches. Apply this migration before serving the updated filing-detail view. Both local-submission lookup and cross-account authorization use the filing-ID index; remote-only lookups do not load other accounts’ draft responses or snapshots.

`FilingDraft.save()` updates these identifiers atomically when saving `submission_response`. Code that changes this response must use `save()` rather than `QuerySet.update()` or bulk writes. This table stores identifiers only; filing status still comes from the proxy.

Correction links use a normalized returned/rejected status, including mixed-case and whitespace-padded values. Failed filings remain ineligible. The review regression tests cover response-index updates, historical backfill, normalized correction links, and remote-only lookups without materializing other accounts’ drafts; all 49 targeted checks passed.

Recipient addresses and provider message IDs are redacted from this public
repository note. Delivery results, dates, sender identity and remaining release
gates are preserved; redaction does not supply production approval.

## Evidence reviewed

- Local `docassemble-EFSPIntegration/docassemble/EFSPIntegration/efm_client.py` whitelist and lead-contact serialization.
- `EfileProxyServer/docs/rest_guide.md`, `OrgMessageSender.java`, `SendMessage.java`, `FilingReviewService.java`, and the Tyler callback implementation from the local EfileProxyServer checkout.
- Local `FilingInformationDocassembleJacksonDeserializer.java`: when `users[0]` is marked form filler and has email, that person's email takes precedence. Otherwise the proxy uses `lead_contact`, whose email it requires. The Docassemble client serializes both `users` and `lead_contact` when present.
- Local `FilingReviewService.getFilingDetails`: requires proxy authorization, obtains the authenticated `TylerUser`, and passes that user's credentials to the EFM detail request. No separate filing-owner check is present in the inspected proxy method.
- The repository proxy rendering harness is in `efile_app/tests/notification-proxy-contract/`. It uses the proxy's real Jinjava and callback status enum from the supplied proxy JAR. It checks that bodies render while subject fields remain unchanged, since upstream sends the subject directly to the mail provider. A synthetic `{{ status }}` subject probe is preserved literally while an equivalent body probe is rendered. It does not call any provider.

To run the harness, use the `efspserver-with-deps.jar` from an EfileProxyServer build and pass its location through `EFSP_PROXY_JAR`. Run these commands from `efile_app/`:

```sh
set -eu
export EFSP_PROXY_JAR=/path/to/EfileProxyServer/proxyserver/target/efspserver-with-deps.jar
proxy_contract_dir="$(mktemp -d)"
UV_CACHE_DIR="${TMPDIR:-/tmp}/litefile-uv-cache" uv run python tests/notification-proxy-contract/generate_payload.py --output "$proxy_contract_dir/input.json"
java -cp "$EFSP_PROXY_JAR" tests/notification-proxy-contract/RenderContract.java --input "$proxy_contract_dir/input.json" --output "$proxy_contract_dir/report.json"
```

The original pass recorded here was stale: the generator failed with `AttributeError` because its synthetic draft lacked `public_id`, before Java ran. The review fix adds a deterministic UUID and checks that every rendered body retains the corresponding tracker link. The commands above now stop on failure and use a fresh directory so a failed generator cannot feed an older payload to Java.

The repaired harness was rerun on October 11, 2026 (America/New_York), with OpenJDK 21.0.12.1 and the existing local `EfileProxyServer/proxyserver/target/efspserver-with-deps.jar`. The JAR was not rebuilt; its SHA-256 was `94c8f36925c202d10ae1626929d2391bcb8df9fd1479ce4eaaade8272e26579d`. Generation and Java rendering both exited successfully. Temporary payload and report: `/tmp/litefile-contract-review/input.json` and `/tmp/litefile-contract-review/report.json`.

Validation before the HTML tracker follow-up:

| Check | Result |
| --- | --- |
| Full backend regression suite (`uv run pytest -q`) | 1,849 passed; 4 skipped in 313.93 seconds |
| Notification, submission tracker, filing history, review/submit, and browser tests | 78 passed |
| Integrated browser check | Passed with synthetic court data: email tracker URL → signed-out login → filing selection → accepted, returned, failed, and all five pending states; pending summaries explicitly say no action is needed |
| Proxy Jinjava contract harness | Passed after regenerating the payload; UUID links, body rendering, subject pass-through, returned/unknown categories, and optional URLs checked; no provider called by this harness |
| Live Tyler test submission and tracker | Passed: real Illinois login, fee quote, one submission, confirmation page, signed-out UUID tracker link, login redirect, and live filing detail at mobile width |
| End-to-end plain-text email delivery | Recipient confirmed the proxy email arrived in spam; Tyler also sent its own email |

The live check used `efile-test.suffolklitlab.org` with the configured Illinois test account and an isolated database at `/tmp/litefile-contract-review/live.sqlite3`. Notification templates were enabled only in a temporary settings wrapper, with `[LITEfile test PR 316]` prepended to subjects and notices addressed to the designated test recipient. Repository notification flags remain disabled. The local development stand-in PDF was active. The first fee calculation timed out; a read-only retry succeeded. Exactly one submission was attempted and succeeded. The generated tracker link opened the returned filing ID after real login, showing `submitting` and “No action is needed right now.” This verifies the live submission/tracker path. The recipient subsequently confirmed that the proxy email arrived in spam. Live accepted/rejected callbacks remain unverified. Those callback categories are covered by the synthetic browser and Jinjava checks above. The proxy log records SendGrid HTTP 202 at 22:34:53 Eastern with a redacted provider message ID; its database confirms the intended recipient and persisted callback templates.

Repository-wide Ruff, `ty check`, ESLint for the changed browser script, and `git diff --check` passed for the review fix. The unchanged tracker stylesheet passed Stylelint in the earlier validation.
- GitHub issues #297, #229, #157, and EfileProxyServer #375, read on 2026-10-11.


## HTML email tracker follow-up

The recipient requested the progress graphic inside the email. The default notification configuration now selects `format: html`, while the existing jurisdiction enable flags remain off. LITEfile wraps the jurisdiction-owned copy in an HTML email with inline styles and a four-stage table: Sent → Received → Clerk review → Court decision. Completed, current, and upcoming labels convey progress without relying on color or image loading. The stages reuse `filing_tracker()`; the confirmation highlights Received, and accepted/returned messages highlight the corresponding outcome. Neutral callbacks show a stage only for explicit known status codes or display labels; unknown statuses retain the message and link without inventing progress.

The graphic is a snapshot at send time. Its link opens the latest live tracker. Subjects remain plain strings. Configuration/contact text is HTML-escaped, URL attributes are separately escaped, and interpolated template delimiters remain encoded before Jinjava runs. `format: plain_text` preserves the previous output and must be used with the proxy's current SMTP transport. This is a format choice, not a multipart fallback.

Validation: 14 focused notification/browser tests passed; the actual proxy Jinjava harness passed with HTML bodies, returned and pending stage branches, and the unknown-status fallback. Chromium previews of all four message types fit at 320px and 600px, each showing one current stage. Template lint, type checking, and the focused security scan passed. The HTML email was sent through the existing proxy JAR's real `SendMessage`/SendGrid implementation to the designated test recipient, from `massaccess@suffolk.edu`, with subject `[LITEfile HTML tracker test] Your filing was submitted`. SendGrid returned HTTP 202 at 23:02:04 Eastern, a redacted provider message ID. It links to the earlier live test submission; no additional filing was submitted. The recipient confirmed that the HTML tracker renders correctly. Required pre-push backend and JavaScript tests also passed. At the time of that validation, the PR was unmerged. PRs #316/#317 are merged at the October 13 audit; customized notifications remain disabled pending partner sender/copy approval and the other release gates above.

## Stage timing and court hours

The tracker shows source-reported event times in the court's configured time zone, including the zone abbreviation and daylight-saving adjustment. `filingSubmissionDate` supplies Sent, `filingAcceptDate` supplies Accepted, and the optional NIEM `filingStatus.statusDate` supplies the current status's time. Receipt times distinguish system receipt from court receipt; `reviewed` is labeled Review completed rather than Review started. Earlier stages do not inherit the current status's timestamp. Missing times and explicit date-only values never become invented midnight timestamps. The API does not provide a complete transition history, so completed stages may have no time.

Configure `filing_timing.timezone` at jurisdiction level, with court overrides under `court_specific_requirements.<court>.filing_timing`. Both `review` and `office_hours` accept a `label`, `source_url`, and maintenance metadata `checked_on`. A source URL is required before guidance is displayed. For example, a partner-verified review entry may use `label: "1–2 business days"`; no universal range is assumed. Court settings override jurisdiction guidance, and missing time-zone configuration falls back to explicitly labeled UTC.

Illinois displays “Typically 1–2 days” as plain-language timing guidance based on the [Illinois Courts' published 24–48 business-hour review guidance](https://www.illinoiscourts.gov/News/656/12-Ways-to-Avoid-A-Rejected-EFiling-and-Other-Illinois-EFiling-Tips/news-detail/). Adams County uses the [circuit clerk's published Monday–Friday, 8:15 a.m.–4:30 p.m. hours](https://www.adamscountyil.gov/government/departments/circuit-clerk/services-provided). Both sources were checked October 12, 2026. The estimate is labeled “Typically,” with holiday/closure context and links to the sources; they do not calculate filing deadlines or promise a decision date. Review estimates are omitted after a decision. Unconfigured courts do not inherit another court's hours.

The compact tracker shows the estimate beneath Clerk review. Court hours are collapsed on the web and linked from email. The previous missing-times note and separate estimate disclaimer have been removed. Known event times remain beneath their stages. HTML emails include the same sourced range. The proxy's current email context has no event timestamps, and the templates are generated before filing submission, so the emails do not manufacture send or callback times. Their linked tracker reads the court's available timestamps.

Validation: 59 focused timing/history/notification/browser tests passed, covering time-zone conversion, missing dates, date-only values, current-status mapping, sourced court overrides, mobile display, and accessible tracker navigation. The proxy Jinjava contract harness passed. A real signed-out browser followed the existing Adams County test submission link through Tyler login and displayed the reported Sent timestamp as Oct 11, 2026, 9:34 p.m. CDT, the review estimate, and office hours without mobile overflow. The API supplied no separate receipt time. An updated email, `[LITEfile timing tracker test] Your filing was submitted`, was sent through the real proxy JAR/SendGrid transport to the same recipient; SendGrid returned HTTP 202. No new filing was submitted. The recipient has been asked to review this timing presentation.


### Simpler timing and business days

After reviewing the first timing email, Quinten requested less timing text and a short “1–2 days” estimate with relevant closure context. The `holidays` dependency now checks the configured country and subdivision calendar, including observed holidays, using the court's local date. `filing_timing.business_calendar` sets `country`, `subdivision`, `review_days`, `closed_weekdays`, and optional display-name overrides in `holiday_names`. The scan covers today through the next configured number of business days, including year boundaries. Illinois uses US/IL, two days, and Saturday/Sunday closures. The holiday note says a weekday holiday “may add 1 day”; it does not promise an exact court reopening or decision date. Government holiday calendars are advisory, and court-specific calendars can override these settings.

Only the immediate confirmation includes the date-sensitive holiday note. Later callback templates, saved at submission, omit it to avoid repeating a stale warning. The live web tracker recalculates the note when viewed. Known hours remain available without expanding the main tracker text.

Validation: 67 focused tests passed, including observed holidays, local-date boundaries, year rollover, and omission of stale holiday guidance from callbacks. The actual proxy Jinjava harness passed. All four email variants fit at 320px and 600px. The real signed-out Tyler test-filing link passed again, with the reported Sent time, “1–2 days,” current holiday context, and collapsed court hours. The original filing was reused. The earlier timing revision also passed the complete backend suite (1,862 passed, 4 skipped) and JavaScript suite; its push was stopped when the requested presentation changes were made during the hook.

The simplified email was sent through the real proxy JAR/SendGrid transport to the designated test recipient, with subject `[LITEfile simpler timing test] Your filing was submitted`; SendGrid returned HTTP 202. Manual review was requested.


## Contact and conditional-copy implementation (October 13, 2026)

The shared resolver now returns either the configured local contact or a complete
statewide fallback. It never adds a statewide phone/URL to a local clerk name.
Both the filing-detail page and all four default email bodies use this resolver.
`submission_notifications.conditional_text` supports named literal `then` and
optional `otherwise` text for `when: fee_waiver_requested`. See the
[configuration example](../docs/partners-courts/email-review.md). These facts are
submission-time facts, not court decisions. Custom notifications remain disabled
pending the existing delivery and copy review.

### Illinois contact provenance and refresh

The [review CSV](./court-contacts/illinois-crosswalk.csv) records every ILAO row,
its explicit Tyler code, both phone values, and the disposition, plus unmatched
Tyler codes. Generated entries appear under `court_specific_requirements` in
`efile_app/efile/static/config/states/illinois.yaml`.

Inputs checked on October 13, 2026:

- ILAO workbook: `https://raw.githubusercontent.com/IllinoisLegalAidOnline/docassemble-ILAO/5fe7d8f4528bfd88b23ed5ecbcefaa680fa781ac/docassemble/ILAO/data/sources/il_courts.xlsx`. Latest workbook commit was September 28, 2026. The 110 rows include nine Cook courthouse records without Tyler codes.
- Tyler codes: `https://efile-test.suffolklitlab.org/jurisdictions/illinois/codes/courts/?with_names=true`. This is the test environment snapshot; new or different production codes intentionally fall back until checked.
- Official verification: [AOIC circuit clerks](https://www.illinoiscourts.gov/courts/circuit-court/circuit-court-clerks/).
- Cook source: [clerk customer service](https://www.cookcountyclerkofcourt.org/contact-us), 312-603-5030, independently matching AOIC. This is explicitly county-wide service, not an inferred division phone.
- ILAO's repository LICENSE file was empty; GitHub reported `NOASSERTION`. No license grant is assumed and the workbook is not vendored. Published contact facts are independently verified against official public sources; the audit preserves attribution. Confirm reuse terms with ILAO before redistributing their workbook.

Refresh by downloading those three inputs, then run from the repository root:

```bash
uv run --with openpyxl --with beautifulsoup4 --with pyyaml python scripts/build_illinois_court_contacts.py \
  --workbook /tmp/litefile-il-courts.xlsx \
  --courts /tmp/litefile-tyler-courts.json \
  --official-html /tmp/litefile-official-clerks.html \
  --overrides /tmp/litefile-court-overrides.yaml \
  --audit docs/developer-notes/court-contacts/illinois-crosswalk.csv
```

Review the CSV and merge the generated overrides into the state YAML, preserving
existing case-type and timing settings. Do not deploy unmatched codes or conflicting
phone numbers automatically. The script does not fetch data or change runtime
configuration itself. Contact maintenance has no runtime external dependency.

Crosswalk counts: 63 verified, 37 phone differs from official clerk directory; review required, 9 unmapped; Cook uses county-wide clerk contact separately, 1 code absent from Tyler snapshot, 213 no verified contact; statewide fallback, 84 verified county-wide contact; no division inference.

- Input SHA-256 `litefile-il-courts.xlsx`: `18bb6673fa7427196921896962e246084abb30fdc7c2a897e2ac411b5d351bd1`.
- Input SHA-256 `litefile-tyler-courts.json`: `c8ca177f4604dc378b49042a7074f93d8abcfc86e73aab14bc346a0985d75fc7`.
- Input SHA-256 `litefile-official-clerks.html`: `4188491e45b84aa833f9d2ccf956124e44dc503f0ddad28ebdac3303c343db7e`.

### Official clerk reconciliation (October 14, 2026)

The contact generator now uses AOIC's published **circuit clerk** phone when
ILAO's explicit Tyler code is unique, present in the provider snapshot, and the
provider county name agrees with the workbook county. This resolves the 37
previously withheld counties. It does not assume their ILAO numbers are invalid:
they may reach a courthouse or another office. Both values remain in the CSV.
[ILAO issue #25](https://github.com/IllinoisLegalAidOnline/docassemble-ILAO/issues/25)
reports every difference with the proposed clerk number highlighted and asks the
maintainer to confirm the intended field semantics.

The AOIC parser now joins adjacent bold headings, fixing its previous failure
to read Cumberland and Rock Island. Duplicate official headings, ambiguous
workbook codes, missing official numbers, and inconsistent county identities
are withheld. Existing Cook contacts remain explicitly county-wide.

The refreshed output contains **184 contacts**: 100 county records (63 matching
ILAO phones and 37 official clerk alternatives) and 84 Cook provider codes.
There are 176 provider codes without a verified contact, nine unmapped workbook
Cook courthouse records, and one workbook code absent from the snapshot. These
still use the existing statewide fallback. The same resolver supplies the status
page and all four notification bodies; notifications retain their existing
enablement and approval settings.

The workbook commit and Tyler snapshot are unchanged from the October 13
inputs above. The AOIC HTML fetched October 14 has SHA-256
`f92aba7174f959a24822c1a8600a1acb2d1e1ad4b6ebf6f913681024ba18ce2a`.
The generation command above is unchanged. Review its audit before merging
output; preserve other court configuration. The earlier instruction to withhold
all differing numbers is superseded by this explicit official-clerk policy.

Run the generator's regression tests from `efile_app`:

```bash
uv run --with openpyxl --with beautifulsoup4 pytest -q ../scripts/tests/test_build_illinois_court_contacts.py
```

### Cook holiday scope

The [official Cook legal holiday schedule](https://www.cookcountycourtil.gov/about/legal-court-holidays)
confirms March 2, 2026 as Casimir Pulaski Day and identifies First Appearance and
Juvenile Court exceptions. The installed holiday library already supplies the
first-Monday-in-March recurrence, but incorrectly broadens this guidance to all
Illinois courts. State configuration now excludes it, with explicit Cook court
calendar overrides restoring it. Daily courtroom sessions do not establish
clerk e-filing review availability; all configured Cook codes retain the
advisory closure note. Calendar overrides deep merge with state settings. Dates remain advisory
review guidance, not legal deadline adjustments. Only the immediate HTML email
uses today's closure note; saved later-event templates must not repeat it.


## Illinois proposed copy imported for issue 435

The four Illinois templates now follow the [partner proposal](https://docs.google.com/document/d/1CTSgHFW5qyGlT7DIQBRMaDw_4JdunZPVXa5FBrEO_Uw/edit), with LITEfile branding, Odyssey eFileIL explanations, event-specific next steps, local clerk phone/site details, and Illinois Court Help in every message. The existing disabled setting remains in place; importing copy does not establish sender configuration, partner signoff, or live delivery.

The confirmation uses the proxy envelope number. Callback emails identify the individual filing transaction and link to the authenticated tracker for document names and envelope details, because those fields are absent from callback template context. They explicitly allow different outcomes for other filings in the same envelope. Case titles appear only when available; no case number is fabricated. The existing progress graphic reflects known statuses rather than asserting a court decision.

The delivery placeholders are `$envelope_number` (confirmation only) and `$filing_transaction`, which refer to proxy-generated identifiers. `$case_description` and `$court_description` are escaped submission-time snapshots, not callback-supplied text. The real proxy Jinjava test demonstrated recursive interpretation of template syntax inside delivery values, even with the HTML `escape` filter. Consequently the imported copy uses a generic greeting and directs the filer to the tracker for clerk comments; it does not interpolate the proxy’s recipient name, case title, court name, or free-text message. This is a deliberate adaptation of the proposal until upstream literal-value rendering is available.

`$local_help_line` includes only configured local clerk name, phone, and website, omitting email and statewide fallback. Illinois Court Help is separately identified. The waiver paragraph appears only when a waiver was requested, without implying approval. Literal returned statuses retain correction guidance in the neutral template.

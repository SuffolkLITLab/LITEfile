---
id: email-review
title: Review filing emails and tracker guidance
sidebar_label: Emails and tracker review
---

# Review filing emails and tracker guidance

Customized emails are implemented and **disabled pending sender/copy and release
signoff** in the October 13, 2026 snapshot. This page prepares a partner review;
it does not authorize enabling delivery. Release gates remain in
[#157](https://github.com/SuffolkLITLab/LITEFile/issues/157) and sender work in
[EfileProxyServer #375](https://github.com/SuffolkLITLab/EfileProxyServer/issues/375).
The [filing updates contract](https://github.com/SuffolkLITLab/LITEFile/blob/main/docs/developer-notes/filing-updates-contract.md)
is the technical authority, including inspected proxy behavior and limited live
test evidence. It remains an internal repository note.


## Partner copy packet for issue #335: what to send

**You do not need to edit YAML, write HTML or write Jinja2.** Please send a
Google Doc, Markdown comment or spreadsheet with the following four
**subject-and-body pairs for each jurisdiction**. Drafts can be incomplete;
record who can approve each version.

| Event | Exact subject key | Exact body key | Meaning |
| --- | --- | --- | --- |
| Submission received by e-filing system | `email_confirmation_subject` | `email_confirmation_contents` | Not court acceptance |
| An individual filing was accepted | `acceptance_subject` | `acceptance_contents` | Do not say all documents/envelope filings were accepted |
| An individual filing was rejected | `rejected_subject` | `rejected_contents` | Explain where to read clerk comments and fix/resubmit |
| Other/unknown update | `neutral_subject` | `neutral_contents` | Do not guess an outcome; literal `returned` callbacks currently use this category |

For **every row**, supply exact one-line subject, body text (ideally 1–3
short paragraphs), where a tracker link and local help should go, jurisdiction
and any court restriction, any wording that should only appear for a waiver or
other condition, court/rule source when relevant, reviewer's role/date, and
`draft | needs court review | approved` status. Specify which sender display
name/domain should be approved, who owns that decision, what separate emails
Tyler sends, and the desired explanation of Tyler's account activation email.

Copy-and-fill format:

```text
Jurisdiction / courts covered:
Email event: Submitted | Accepted | Rejected | Other
Subject (exact words):
Body (exact words):
  ...
  [Filing-status link here]
  [Court-specific help contact here]
Conditional sentence, if any:
  Show this sentence IF ...; otherwise omit / instead say ...
Policy or official wording source:
Reviewer and date:
Decision: draft | needs court review | approved
```

In the copy packet, **describe conditions in English**, such as "include this
paragraph only if the filer requested a waiver." Do **not** add arbitrary
`{% if ... %}` or `{{ ... }}` expressions as a partner-submitted contract.
Programmers can translate approved conditions into supported configuration or
record a missing hook in [#328](https://github.com/SuffolkLITLab/LITEFile/issues/328).

### How an implementer translates the packet to YAML

The default templates live in
`efile_app/efile/static/config/base-case-types.yaml`; state YAML inherits
them. Each overridden subject/body is a **plain string**, not a file containing
Jinja2. The server expands these built-in Python `string.Template` values:
`$tracker_url` (a sign-in-safe tracker link), `$help_line` (court-specific
contact with fallback), and `$waiver_line` (a fixed optional **request**
notice, not waiver approval). `$$` represents a literal dollar sign.
The HTML tracker presentation is supplied by code. For example:

```yaml
submission_notifications:
  enabled: false  # stay disabled until delivery, copy and sender signoff
  format: html     # choose plain_text for the current SMTP transport
  templates:
    from_email: "noreply@example.com
    email_confirmation_subject: "Your filing was submitted"
    email_confirmation_contents: |-
      The e-filing system received a filing in your submission.
      This does not mean the court has accepted it.
      Check the filing's status: $tracker_url$waiver_line$help_line
    acceptance_subject: "Filing accepted"
    acceptance_contents: |-
      A filing in your submission was accepted by the court.
      See its status and next steps: $tracker_url$help_line
    rejected_subject: "Filing returned for changes"
    rejected_contents: |-
      A filing in your submission was returned for changes.
      Read the clerk's comments and next steps: $tracker_url$help_line
    neutral_subject: "Filing update"
    neutral_contents: |-
      A filing in your submission has an update.
      Check its status before taking action: $tracker_url$help_line
```

This is **illustrative copy only**, not partner-approved language. The
combined/merged configuration must have all eight nonblank fields; inheriting
the base fields is allowed. Do not assume that adding a body expression makes
it work in the subject: the upstream proxy performs its second, Jinjava
rendering **only on bodies**, not subjects. The upstream `status` is also a
display value, not always the message category. The exact status and multiple
filings in an envelope must be tested against the approved wording.

### Local court contacts: reuse the ILAO directory first (#329 and #331)

**Do not ask Illinois partners to construct a new statewide contact spreadsheet
before checking the data we already have.** The
[ILAO Appearance with e-filing interview](https://github.com/IllinoisLegalAidOnline/docassemble-AppearanceEfile/blob/main/docassemble/AppearanceEfile/data/questions/appearance.yml)
loads
[`docassemble.ILAO:data/sources/il_courts.xlsx`](https://github.com/IllinoisLegalAidOnline/docassemble-ILAO/blob/main/docassemble/ILAO/data/sources/il_courts.xlsx)
through `ALCourtLoader`, resolves `trial_court`, and places
`trial_court.phone` in its help text and status emails. The court list is
shared across other ILAO interviews.

The checked crosswalk uses the spreadsheet's explicit `tyler_court_code`,
requires a unique match in the Tyler court list, and compares phone numbers
with the [official circuit clerk directory](https://www.illinoiscourts.gov/courts/circuit-court/circuit-court-clerks/).
Verified entries populate `court_specific_requirements.<exact court code>.contact`.
Cook divisions use the clearly named county-wide clerk customer-service line;
no courthouse-to-division phone mapping is inferred. Unmatched codes and
conflicting numbers retain Illinois Court Help pending targeted review.

`$help_line` includes this contact in all four default email categories. The
filing-detail screen uses the same resolver. Local contacts are kept intact:
a local phone, website, or email never inherits statewide details. An entry
with only a name falls back to the complete statewide help contact.

ILAO has also [documented a separate court-info lookup](https://github.com/IllinoisLegalAidOnline/website-docs/blob/main/docs/source/dhi_helpful_functions.rst)
that distinguishes `clerk_phone_number` from `court_phone_number`, falling
back to Illinois Court Help. Investigate whether that directory is maintained,
accessible, and suitable for reuse; **do not assume it is the same dataset as
the Appearance interview or a guaranteed production service.**

**Targeted partner review only where needed:** ask ILAO/AOIC who maintains the
source, which clerk/general number should appear, and to confirm ambiguous or
outdated court mappings. If partners propose a replacement source, collect
`court | exact Tyler code if known | public clerk/help phone and name |
official URL | source | last verified | reviewer | exception`, but do not
require them to re-create an existing statewide list. The primary work is
data-source assessment, court-code mapping, and UI/email tests—not a blanket
partner-data blocker.

### Customization gap summary (issues #328 and #329)

| Partner need | Currently supported? | Remaining work |
| --- | --- | --- |
| Four jurisdiction-specific subject/body pairs | **Yes** via `submission_notifications.templates` | Partner-reviewed content, sender/release checks |
| Tracker link in body | **Yes** via `$tracker_url` | Rendering and authorization verification |
| Court-specific help contact | **Yes**, provided exact court-code contact is configured and `$help_line` is included | Review remaining conflicting/unmatched records; periodically refresh the checked crosswalk |
| Fixed fee-waiver **request** note | **Yes** via `$waiver_line`; it is omitted absent the selected WV account/detected waiver document | Partner approval of the wording and scope |
| Custom waiver-request paragraphs | **Yes**, via `conditional_text` below, in subjects and bodies | Partner approval of wording |
| Other conditions, including statutory exemptions or court waiver decisions | **No** | Require independently available facts and explicit server support |
| Dynamic conditional **subjects**, including upstream `{{ status }}` | **No** proxy second-pass rendering | Do not promise; would need an explicit upstream/app contract change |
| Per-court different email prose or sender domain | **Not shown by the current state-level template/sender config** | Scope separately if partners request it; contact interpolation alone is not per-court prose or sender branding |

Define named literal paragraphs under `submission_notifications.conditional_text`
and insert `$fee_note` (or another configured name) in any subject or body:

```yaml
submission_notifications:
  conditional_text:
    fee_note:
      when: fee_waiver_requested
      then: "Check the court’s fee-waiver decision for any next steps."
      otherwise: ""  # optional; omitted by default
  templates:
    acceptance_contents: >-
      Check your filing: $tracker_url$fee_note$help_line
```

`fee_waiver_requested` is true when a WV payment account was selected or a
waiver document was detected. Missing payment data alone and statutory
exemptions do not establish a waiver request. This fact is captured at
submission and does not change when a court later decides the waiver.
Unknown conditions, malformed blocks, and names that replace built-in values
are rejected. Selected text is literal: braces are escaped before the proxy
pass and HTML bodies escape markup. Different event wording can use different
named paragraphs. These settings are jurisdiction-wide.

For any new condition, the partner should provide:
`event | when true | exact true sentence | false/omit sentence | state/courts/forms | legal source | reviewer`.
Engineering must not conflate a **requested** waiver with a court's
**approved** waiver, or an income-based waiver with a statutory fee exemption.
The existing app knows submission-time waiver intent; a later court decision
would require independently verified upstream callback data.

These findings describe the inspected configuration and server path, **not**
an end-to-end acceptance test of every proposed partner variation. See
[#335](https://github.com/SuffolkLITLab/LITEFile/issues/335),
[#328](https://github.com/SuffolkLITLab/LITEFile/issues/328),
[#329](https://github.com/SuffolkLITLab/LITEFile/issues/329), and
[#331](https://github.com/SuffolkLITLab/LITEFile/issues/331).

## Locate and propose the change

Defaults are inherited from `efile_app/efile/static/config/base-case-types.yaml`.
Merge overrides into `efile_app/efile/static/config/states/<jurisdiction>.yaml`:

```yaml
submission_notifications:
  enabled: false                # keep off while copy and delivery are reviewed
  format: html                  # use plain_text for the current SMTP transport
  templates:
    acceptance_subject: "Filing accepted"
    acceptance_contents: >-
      A filing in your submission was accepted by the court.
      Check its status and next steps here: $tracker_url$help_line
```

All eight template members must be nonblank strings in the **merged** configuration.
If any member is missing or blank, the server omits the entire customized template
set; it does not send a partially customized set. Do not mistake proxy-default or
Tyler mail for proof that these overrides worked. The server also discards
browser-supplied email fields.

Implementation: [submission_notifications.py](https://github.com/SuffolkLITLab/LITEFile/blob/main/efile_app/efile/services/submission_notifications.py).
HTML presentation: [filing_notification.html](https://github.com/SuffolkLITLab/LITEFile/blob/main/efile_app/efile/templates/efile/emails/filing_notification.html)
and [filing_progress.html](https://github.com/SuffolkLITLab/LITEFile/blob/main/efile_app/efile/templates/efile/emails/filing_progress.html).

## Review packet for all four categories

These are the inherited subject/body previews before substitution and the HTML
wrapper. For each category, attach its actual rendered preview at 320px and
600px, plus a real inbox/spam capture from the approved test delivery.

| Category and exact fields | Default subject | Body preview / review concern | Partner approval |
| --- | --- | --- | --- |
| Submission: `email_confirmation_subject`, `email_confirmation_contents` | Your filing was submitted | “The e-filing system received a filing in your submission. This is not court acceptance.” Then `$tracker_url$waiver_line$help_line` | Unassigned; pending |
| Accepted: `acceptance_subject`, `acceptance_contents` | Filing accepted | “A filing in your submission was accepted by the court.” Then `$tracker_url$help_line`; avoid saying every filing was accepted | Unassigned; pending |
| Rejected: `rejected_subject`, `rejected_contents` | Filing returned for changes | “A filing in your submission was returned for changes.” Then “Review the clerk’s comments and next steps here: `$tracker_url$help_line`” | Unassigned; pending |
| Neutral/other: `neutral_subject`, `neutral_contents` | Filing update | Known returned display text gets returned guidance; otherwise “A filing in your submission changed status. Check its status and next steps here: `$tracker_url$help_line`”. Unknown status must not invent progress | Unassigned; pending |

For each row record: jurisdiction/court, complete subject and body after both
rendering stages, sender/domain, recipient-selection case, mobile layout and
accessible stage labels, tracker destination/sign-in, court/help contacts,
waiver-request wording (with and without a request), reviewer/date/decision, and
evidence URL. Use the [maintenance register](./maintenance.md); do not include
real filing data or secrets in a shared packet.

## Two substitution stages and delivery ownership

LITEfile fills `$tracker_url`, `$help_line`, and `$waiver_line` with Python
`string.Template` substitution. Use `$$` for a literal dollar sign. The tracker
URL names the local submission UUID; it does not grant access. Help comes from
court contact settings with jurisdiction fallback. Waiver wording appears only
for a selected waiver account or a document whose filing-type name identifies
a waiver, indigency or in-forma-pauperis request. A configured generic fallback
filing type additionally needs explicit waiver provenance (the waiver checklist
item or an explicit-purpose upload). Include a waiver-named document with no
waiver account in the review packet, as well as a generic fallback with and
without that provenance. The line asks the filer to check the court’s decision;
it does not say that a waiver is approved.

The proxy subsequently renders **bodies** with Jinjava. Its variables include
`name`, `court_name`, `case_type`, `case_title`, `status`, `message_text`,
`transaction_id`, optional `message_url`, and confirmation-only `envelope_id`.
There is no callback `filing_id` variable. Proxy subjects pass through unchanged;
do not put `{{ status }}` in a subject expecting rendering. Inserted configuration
values are escaped to prevent them becoming second-stage template expressions.

LITEfile supplies copy; EFSPProxy sends it. The inspected SendGrid path recognizes
HTML bodies by their doctype. The current SMTP path sends plain text, so choose
`format: plain_text` there. There is no multipart fallback. Tyler can independently
send filing notices and account-activation mail. Sender identity is owned by proxy
operations, not a state `text` setting. The legacy proxy sender fallback is not
evidence of an approved partner sender.

Confirmation recipient selection prefers `users[0].email` when that user is the
form filler; otherwise it uses `lead_contact.email`. Callbacks use the recipient
saved on the proxy transaction. Review both self-filing and filing-for-someone-else
cases with the operations owner.

A raw `rejected` callback selects `REJECTED`; `showreturnonreject` can change its
display wording to returned without changing that category. A literal `returned`
callback selects neutral/other. The display variable `status` is not always the
category. One submission may contain several filings with different outcomes;
the tracker lets the authorized filer choose the filing.

## Timing, office hours and stale guidance

Configure `filing_timing.timezone` in State YAML, with exact court overrides under
`court_specific_requirements.<court>.filing_timing`. Missing/invalid zones fall
back to labeled UTC. Event times come from the court response; unavailable or
date-only times do not become invented timestamps or a complete history.

```yaml
# Illustrative structure; replace with a verified local source before display.
filing_timing:
  timezone: America/New_York
  review:
    label: "Partner-reviewed estimate"
    source_url: "https://court.example.org/review-times"
    checked_on: "2026-10-13"
  office_hours:
    label: "Partner-reviewed office hours"
    source_url: "https://court.example.org/contact"
    checked_on: "2026-10-13"
  business_calendar:
    country: US
    subdivision: MA
    review_days: 2
    closed_weekdays: [5, 6]      # Monday is 0
```

Both a label and valid source URL are needed to display review/hours guidance.
`checked_on` is review metadata, not automatic expiry. Exact court settings
merge individual fields of `review`, `office_hours`, and `business_calendar`
with the jurisdiction settings. A court can override `excluded_holidays`
without losing the state’s timezone, subdivision, or display names. Set
`business_calendar.enabled: false` only when clerk review availability has
been independently verified. Illinois excludes Casimir Pulaski Day by default; configured
Cook courts retain the library’s first-Monday-in-March holiday. Daily courtroom sessions do not establish clerk e-filing review availability,
so all configured Cook codes retain the advisory clerk closure note. This affects
advisory review estimates only, not filing deadlines or online submission.
Review source content, timezone/daylight-saving

behavior, `business_calendar` subdivision, observed holidays, closure weekdays,
and any `holiday_names` overrides before release and after court-calendar changes.
Government holidays are advisory; they do not establish court deadlines.

To withdraw a stale estimate, remove its `review` entry at every applicable
scope or override it with `review: {}` at the court; removing only a court override
can expose the statewide estimate again. Apply the same approach to stale hours.
Record why it was withdrawn and confirm no estimate appears in web/email previews.
The email graphic is a send-time snapshot. The linked tracker is live. The
date-sensitive closure note appears in confirmation and is recalculated on the
web; saved callback templates omit it. Neither estimated intervals nor waiver
requests promise court action or approval.

## Enablement and maintenance checklist

- [ ] Partner approves all four categories, local contacts, waiver language and
  sourced timing; record approver, scope/date and next review.
- [ ] Operations approves sender/domain, credentials and authentication; identify
  which events Tyler and proxy mail each handle.
- [ ] Run configuration and notification/timing regressions; render all categories
  at mobile/desktop widths, including literal returned and unknown status.
- [ ] Verify actual acceptance and rejection callbacks, provider errors,
  missing/invalid recipients, duplicate callbacks, failed-send handling and inbox/spam
  presentation. Existing tests/live confirmation do not establish these gates.
- [ ] Verify signed-out tracker → sign-in → correct filing; test another account
  cannot read it, legitimate remote history and mixed-outcome submissions.
- [ ] Record evidence under #157 and the register, then enable only the approved
  jurisdiction/transport. Revert `enabled` to `false` if necessary; templates
  already stored at submission may still be used by later proxy callbacks.

The inspected proxy can acknowledge callbacks despite failed mail and can send
again for repeated callbacks. Do not promise automatic delivery retries or
deduplication. Review delivery failures weekly and copy/contacts before each
release and source change, subject to assigned owner approval.

```bash
# From efile_app; tests use synthetic data, not live delivery.
uv run python manage.py check --fail-level WARNING
uv run pytest -q efile/tests/test_submission_notifications.py efile/tests/test_filing_timing.py
```

For the real proxy body-rendering harness and its prerequisites, use the existing
contract. Enabling emails or sending a live filing is separate release work.

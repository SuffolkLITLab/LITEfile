---
id: filing-availability
title: Control filing availability
sidebar_label: Filing availability
sidebar_position: 3
---

# Control filing availability

Use court configuration to stop filings that LITEfile cannot support yet—for
example, when a court requires hearing scheduling that the e-filing service
does not provide. Filing is enabled by default. A restriction can cover a whole
court or selected case categories, case types, and filing codes.

Filers see a warning as soon as their selections match a restriction, before
continuing to the remaining questions. The restriction controls filing through
LITEfile; it does not change the court's rules or availability through other
filing providers.

## Where to configure restrictions

Edit the jurisdiction's existing YAML file:

```text
efile_app/efile/static/config/states/<jurisdiction>.yaml
```

Add `filing_availability` inside the relevant court entry under
`court_specific_requirements`. It is a sibling of that court's `case_types`,
`contact`, and other settings. Preserve those existing settings, and merge
examples into the existing entry rather than repeating a YAML key.

There is no environment variable or database migration for this feature.
`filing_availability` is not a top-level jurisdiction setting.

### Court keys and type names

These identify two different things:

| Value | What to use |
| --- | --- |
| Court entry, such as `"cook:cvd1"` | The court route key used by the e-filing API and the existing court configuration. |
| `case_categories`, `case_types`, `filing_types` | The human-readable names from the court's choice lists, such as `"Small Claims"`, `"Contract"`, or `"Motion"`. |

Tyler's numeric type IDs can change. Do not use those IDs in availability
selectors. Do not use document-checklist keys such as `name_change` unless
that is also the court's actual displayed name. Use the name shown for the
selected court, without LITEfile's recommendation marker (`*`). Verify names in
the environment where you will deploy: test and production choices can differ.

The same name may appear in several categories. Combine category and type
selectors when you need to distinguish those cases. A change to a numeric ID
does not require changing a name rule; a change to the human-readable name may.

## Configuration reference

All fields are optional. Omit `filing_availability` when no restriction is needed.

| Field | Type and default | Behavior |
| --- | --- | --- |
| `enabled` | Boolean; enabled unless explicitly `false` | `false` blocks every filing for this court. Use the YAML boolean `false`, not the string `"false"`. `true` does not cancel matching rules. |
| `message` | Plain-text string; built-in message if absent or empty | Default explanation for this court's restrictions. A message alone does not disable filing. |
| `rules` | List; empty by default | Each matching rule blocks filing. Rules are evaluated in list order. |
| `rules[].case_categories` | List of name matchers | Match the selected case category. |
| `rules[].case_types` | List of name matchers | Match the selected case type. |
| `rules[].filing_types` | List of name matchers | Match a filing code on any document in the envelope, including supporting documents. |
| `rules[].message` | Plain-text string; court message if absent or empty | Explanation for this particular restriction. |

A **name matcher** is either a literal string or a mapping with a `regex` string.
Selectors must be lists, even when there is only one matcher:

```yaml
case_types: ["Contract"]
filing_types:
  - "Motion"
  - regex: '(?i)motion to .+'
```

These rules apply to both new and existing cases. There is no rule field for
filing phase, date range, or whether an individual filing needs a hearing. Only
configure a combination you intend to block in full; the rule does not determine
hearing requirements automatically.

### Exact names and regular expressions

A plain string matches the entire name, case-sensitively. `"Motion"` does not
match `"motion"`, `"Motion to dismiss"`, or `"Notice of Motion"`. Parentheses,
periods, and other punctuation in a plain string are literal.

LITEfile removes surrounding whitespace from the selected name. It does not
collapse internal spaces, change capitalization, or trim the configured literal.

A `regex` matcher uses Python `re.fullmatch`, also case-sensitive by default.
Use `(?i)` for case-insensitive matching and `.*` when you intentionally want
to match additional text. Single-quote YAML regexes so backslashes remain literal.

| Matcher | Matches | Does not match |
| --- | --- | --- |
| `"Motion (Other)"` | `Motion (Other)` | `Motion Other` |
| `{regex: '(?i)motion'}` | `Motion`, `MOTION` | `Motion to dismiss` |
| `{regex: '(?i)motion to .+'}` | `Motion to dismiss` | `Notice of Motion` |
| `{regex: '(?i).*hearing.*'}` | `Notice of hearing` | `Complaint` |

An invalid regex raises a configuration error when evaluated. It does not
silently become a literal or an allow rule. Check patterns before deployment.

### Combining selectors

- Match **any entry** within one selector list.
- Match **every selector present** in one rule.
- Match **any rule** to block filing.
- A missing or not-yet-selected name does not match. A rule with no recognized
  selectors, or an empty list for any selector it includes, never matches.

For example, this rule blocks Motion or Notice of hearing only for Contract
cases in Small Claims:

```yaml
case_categories: ["Small Claims"]
case_types: ["Contract"]
filing_types: ["Motion", "Notice of hearing"]
```

A Complaint in the same case would not match. If its envelope also includes a
supporting document with filing code Motion, the whole envelope is blocked.
To block all Contract cases **or** all Motions, use two separate rules instead.

## Examples

All type names below are illustrative. Confirm the affected names and court
requirements before enabling a restriction.

### Disable a whole court

The smallest restriction uses the built-in message:

```yaml
court_specific_requirements:
  "example:civil":
    filing_availability:
      enabled: false
```

To provide a court-specific explanation, add `message`:

```yaml
court_specific_requirements:
  "example:civil":
    filing_availability:
      enabled: false
      message: >-
        LITEfile cannot submit filings to this court yet. Contact the court
        clerk to ask how to file.
```

### Disable a case type in Cook County

This example blocks the human-readable case type Contract in Cook County's
Municipal Civil Division and explains the hearing-scheduling limitation:

```yaml
court_specific_requirements:
  "cook:cvd1":
    filing_availability:
      rules:
        - case_types: ["Contract"]
          message: >-
            LITEfile cannot file this case type in Cook County yet because it
            requires scheduling a hearing. Our e-filing service does not support
            hearing scheduling yet. Contact the court clerk to ask how to file.
```

The example in `states/illinois.yaml` is commented out. It is not an active
restriction or a determination that all Contract filings require scheduling.
If only particular filing codes require scheduling, narrow the rule by adding
`filing_types` to the same rule:

```yaml
rules:
  - case_types: ["Contract"]
    filing_types:
      - "Motion"
      - regex: '(?i)motion to .+'
    message: "LITEfile does not support hearing scheduling for this filing yet."
```

### Disable categories or filing codes independently

Each of these rules is independent. This example blocks every filing in Small
Claims and, in other categories, any envelope containing Notice of hearing:

```yaml
court_specific_requirements:
  "example:civil":
    filing_availability:
      message: "LITEfile cannot submit this filing to this court yet."
      rules:
        - case_categories: ["Small Claims"]
        - filing_types: ["Notice of hearing"]
          message: >-
            This filing needs hearing scheduling, which LITEfile does not
            support yet. Contact the court clerk to ask how to file.
```

### Apply a restriction across a county

Use `"cook:*"` to cover every court route key starting with `cook:`:

```yaml
court_specific_requirements:
  "cook:*":
    filing_availability:
      rules:
        - filing_types: ["Notice of hearing"]
          message: "LITEfile does not support hearing scheduling for this filing yet."
```

The supported prefix is the text before the first colon, followed by `:*`.
`cook:*` matches `cook:cvd1` and `cook:law1`, but not `cook`, `cooksville:law1`,
or an unrelated court. This is not arbitrary wildcard or regex matching.

Other jurisdictions can use this form if their court route keys share a county
prefix. Otherwise, repeat the rule for each affected court. Court entries are
scoped to their jurisdiction. Prefix matching applies only to availability;
other court-specific features such as checklist overrides still use exact keys.

## Messages and precedence

The built-in English message is:

> LITEfile cannot submit this filing to this court right now. Contact the court clerk to ask how to file.

Messages are plain text. HTML, Markdown links, and template placeholders are not
rendered or expanded. Custom YAML messages are displayed as written; they do not
use the separate `text` section's translation or Markdown system. Explain the
limitation and give the filer a practical next step without suggesting that they
choose an incorrect court or case type.

Evaluation follows this order:

1. Check the exact court entry. Within it, evaluate rules in list order. The
   first match supplies its message, falling back to that entry's `message`,
   then the built-in message. More detailed selectors do not automatically win;
   place more specific explanations first.
2. If no exact-court rule matches and that court has `enabled: false`, block
   with its court message or the built-in message.
3. If the exact entry did not block filing, repeat those checks for the county
   prefix entry, when one applies.
4. If neither entry blocks filing, allow it.

A matching rule can therefore supply a specific explanation even when its court
also has `enabled: false`. An exact-court block takes precedence over a prefix
block. An exact court's message does not become the fallback for a prefix rule.

Restrictions are additive. `enabled: true` and `rules: []` on an exact court do
not exempt it from a county-prefix restriction. There is no allow-rule override.

## What filers experience

On the confirmation screen, changing a court, category, case type, or filing code
starts a live availability check. The warning appears beside the changed
selection. Continue is disabled while the check is pending or the selection is
blocked. Changing to an allowed selection clears the warning; late responses
for an older selection cannot replace the current result.

Court selection during an existing-case lookup uses the same check. Once a case
lookup identifies its category and type, any restriction appears on the case
confirmation screen before the filer confirms it. Changing a document's filing
type on Organize documents also checks availability for the whole envelope.

If the live check fails, Continue remains disabled and the filer is asked to
reload the page. A failed check is not treated as permission to proceed.

Server checks also stop blocked choices on form submission, on the checklist
and review pages, and during final submission. Existing drafts are not exempt
from newly added restrictions. Drafts and documents are retained for correction;
removing the restriction lets an otherwise valid draft continue.

At final submission, LITEfile resolves outgoing numeric IDs to current names
from the court API for configured type selectors and applies the same rules.
It does not trust browser-provided labels as proof that the filing is allowed.
If required names cannot be resolved, it stops submission with a retry message
before sending the filing. A whole-court restriction does not need a name lookup.

## Deploy, verify, and re-enable

1. Confirm the affected court route keys and the exact names offered in the
   target environment. Decide whether to block the whole court, a category,
   a case type, a filing code, or a combination.
2. Add the smallest appropriate restriction to the existing YAML entry. Keep
   other court settings intact and write a plain-text explanation when the
   built-in message is insufficient.
3. Test a matching selection, an allowed selection, a supporting-document match,
   and any relevant county-prefix overlap in development or staging. Confirm
   that the warning appears immediately and that correcting the choice clears
   it. Test both new- and existing-case paths when they are affected.
4. Deploy the updated YAML through the normal application deployment process
   so every application instance receives the same file.
5. Reopen an affected saved draft and verify the restriction. An already-open
   browser page is not notified automatically; reload it or change a selection
   to refresh the warning. Submission always rechecks availability.

For an existing jurisdiction file, the configuration loader checks its modified
time and size and reloads changed settings on the next request. Editing its
availability rules does not itself require a worker restart. Adding an entirely
new jurisdiction file requires restarting the application so the loader discovers
it. Follow the normal restart behavior of your deployment system when shipping
files in a new image.

To re-enable filing, remove the matching rule or the entire availability section.
For a whole-court block, remove `enabled: false` or set it to `true`, and also
remove any matching rules. Check both the exact court and county-prefix entries;
a remaining restriction in either still blocks filing. Verify an allowed result
after deploying the change. Numeric ID changes alone do not require rule edits.

### Check a configured selection locally

From `efile_app`, use the same matcher that the application uses. This example
reads the installed Illinois YAML without making a filing or calling the EFSP:

```bash
uv run python manage.py shell -c 'from efile.services.filing_availability import filing_unavailable_message; print(filing_unavailable_message("illinois", "cook:cvd1", case_category="Small Claims", case_type="Contract", filing_types=["Motion"]))'
```

A nonempty result is the blocking message; an empty result means no configured
restriction matched. With the shipped Cook County example still commented out,
this command returns an empty result. This verifies configuration matching, not
the availability of all court services or the validity of a filing.

Run the feature's regression tests with:

```bash
uv run pytest -q efile/tests/test_filing_availability.py
```

`manage.py check` is not a complete availability-schema or regex validator.
Misspelled selector keys are ignored; a rule containing only unrecognized keys
never matches. Exercise the actual names and regexes before deployment.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| A rule never matches | Confirm the jurisdiction and court route key, YAML nesting, and selector spelling. Use a list of human-readable names, not numeric IDs or checklist keys. Compare capitalization, punctuation, and internal spaces. |
| A regex matches less than expected | Matching covers the whole name. Use `.*` for additional text and `(?i)` if capitalization should not matter. Use single-quoted YAML for patterns containing backslashes. |
| Too many filings are blocked | Multiple rules are alternatives. Put selectors in the same rule to require a combination. Look for `enabled: false`, broad regexes, and county-prefix rules. |
| The wrong explanation appears | First matching rule wins within an entry. Check exact-court precedence and message fallbacks; rules are not automatically ranked by specificity. |
| A court stays blocked after enabling it | `enabled: true` does not cancel its rules or a county-prefix restriction. Remove every applicable restriction and refresh the page. |
| The live check cannot finish | Check the availability endpoint and application logs, then reload. Invalid YAML or regexes need a configuration fix. Do not assume a failed check means the selection is allowed. |
| Submission cannot confirm availability | Verify the EFSP is reachable and the outgoing IDs still appear in its current choice lists. Re-select stale choices if necessary. A failed required name lookup stops submission. |

## Live-check API and implementation

The UI uses `GET /api/filing-availability/`. This is a read-only check of supplied
selections, not a reservation or authorization to submit a filing.

| Query parameter | Meaning |
| --- | --- |
| `jurisdiction` | Installed jurisdiction key. Falls back to the session jurisdiction when omitted. |
| `court` | Court route key. |
| `case_category_name` | Selected human-readable category name, if known. |
| `case_type_name` | Selected human-readable case-type name, if known. |
| `filing_type_name` | Selected human-readable filing-type name. Repeat the parameter for multiple documents. |

For example, using a local development server:

```bash
curl --get 'http://127.0.0.1:8000/api/filing-availability/' \
  --data-urlencode 'jurisdiction=illinois' \
  --data-urlencode 'court=cook:cvd1' \
  --data-urlencode 'case_type_name=Contract' \
  --data-urlencode 'filing_type_name=Motion' \
  --data-urlencode 'filing_type_name=Exhibit'
```

A successful check returns HTTP 200 even when filing is blocked:

```json
{"success": true, "available": false, "message": "The configured blocking message."}
```

An allowed result has `available: true` and `message: ""`. Responses use
`Cache-Control: no-store`. An unknown jurisdiction returns HTTP 400. Missing
names do not match type selectors, so an allowed result for partial choices
does not guarantee that later choices will be allowed. Browser-provided names
are advisory; the final submission check resolves names independently.

The shared matcher and final name resolution live in
`efile_app/efile/services/filing_availability.py`. The live endpoint is in
`efile_app/efile/api/filing_availability.py`, and the shared browser controller
is `efile_app/efile/static/js/filing-availability.js`. Regression coverage lives
in `efile_app/efile/tests/test_filing_availability.py` and the live-availability
cases in `efile_app/tests/confirm-case-editing.spec.js`.


## Release-scope register

Extend the [shared maintenance register](./maintenance.md) with one row per
jurisdiction/court/type scope before enabling a service or removing a restriction:

| Jurisdiction/environment | Court route and type names/patterns | Enabled/disabled and reason | Content/technical owner and approver | Verification evidence | Removal/recheck conditions |
| --- | --- | --- | --- | --- | --- |
| Illinois / target environment to record | Cook `cook:cvd1` Contract example | Example is commented out; no active block from it | Unassigned; no scope approval recorded | Matcher example yields no block; live scheduling not established | Confirm actual scheduling requirement and supported service before activating a scoped rule |
| Massachusetts / target environment to record | Court/type list to supply | Unverified release scope; absence of a block is not approval | Unassigned | New/existing, supporting-document and saved-draft evidence required | Court/provider/service change and each release |
| Vermont / target environment to record | Court/type list to supply | Unverified release scope; absence of a block is not approval | Unassigned | New/existing, supporting-document and saved-draft evidence required | Court/provider/service change and each release |

Record reason, source/date, accepted owner, approver/decision, deployed version,
allowed and blocked examples, saved-draft behavior and conditions to remove the
restriction. The restriction engine does not implement hearing scheduling;
[#199](https://github.com/SuffolkLITLab/LITEFile/issues/199) remains separate.

Informational `filing_guidance` uses exact **saved provider codes**, while
availability uses authoritative **names or explicit patterns**. After a code
sync, review code-bound guidance, renamed availability selectors and checklist
matches. Test an affected supporting document and a reopened saved draft, as
well as both case paths. Keep scheduling limitations, fee exemptions, payment
restrictions and confidentiality decisions in their own scoped approval records.

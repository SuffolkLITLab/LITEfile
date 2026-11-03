---
id: document-finder
title: Configuring the document finder
sidebar_label: Document finder
sidebar_position: 4
---

# Document finder <span className="wip-badge">WIP</span>

The document finder helps a filer who does not know yet which documents they
need. They describe what they want to do in their own words — *change my name*,
*respond to a small claims case* — and LITEfile shows the documents filings like
that often include, plus links to guided interviews, official blank forms, and
help pages.

Filers reach it from the **Upload documents** step ("Not sure which documents you
need?"), from the start page, or directly at
`/jurisdiction/<state>/find-documents/`. It works without signing in, and a
filer who opens it from Upload can go back to the same draft.

The finder is a small, static index. Each entry, called an *intent*, points at a
checklist you have already written in `case_types` or `case_categories` (see
[Document checklists](./document-checklists.md)). It never has its own list of
documents, so the finder and the filer's plan always agree.

---

## 1. Adding an intent

Intents live in the `document_finder` section of the state file:

```yaml
# efile_app/efile/static/config/states/illinois.yaml
document_finder:
  links_checked_on: "2026-10-13"
  intents:
    respond_to_an_eviction:          # used in the page address
      title: "Respond to an eviction"
      summary: "Your landlord has started a case to make you leave, and you want to take part."
      keywords: [evicted, kicked out, landlord wants me to leave]
      filing_path: existing          # new | existing
      checklist:
        case_type: eviction          # or case_category: small_claims
      filer_role: tenant             # required when the case type has filer_roles
```

| Key | What it does |
| --- | --- |
| `title` | What the filer sees in the results. Write it as something a person would say. |
| `summary` | One sentence that helps a filer tell similar intents apart. |
| `keywords` | Extra words and phrases to search by. See [Search keywords](#2-search-keywords). |
| `filing_path` | `new` if this filing starts a case, `existing` if it goes into one. Shown as a label, used as a search filter, and carried into a new filing. |
| `checklist` | The `case_types` or `case_categories` key whose checklist to show. |
| `filer_role` | Which side of the case, for case types that declare `filer_roles`. Without it, a two-sided case shows no list. |
| `lead_filing_type_name` | Optional. Shows the items whose `when.lead_filing_type_names` match, such as the extra items for a child's name change. |

Statewide items are shown, and items a court adds in
`court_specific_requirements` are listed separately under "Some courts also ask
for", because the filer has not chosen a court yet.

## 2. Search keywords

A search lists an intent when one of the filer's words appears in its title, its
keywords, or the court's own case type names (`matches.names`) — or when every
word appears somewhere on it, including its document labels. Several matches are
all shown, best first; the filer chooses.

Searches ignore case, word endings (*evicted* finds *eviction*), and common words
such as *my*, *case*, and *court*. The synonym groups in
`efile/data/filing_code_search.yaml` (`concepts`) also apply, so *summary
process* and *eviction* are treated alike.

Good keywords are the words filers actually use: *kicked out*, *being sued*,
*security deposit*. Avoid words that fit many intents, such as *motion*.

## 3. Interview, form, and help links

Links go in `resources` for the intent as a whole, or in `document_resources`
keyed by the checklist item ID for one document:

```yaml
      resources:
        - kind: interview
          label: "Respond to an eviction (Easy Form)"
          source: "Illinois Legal Aid Online"
          url: "https://www.illinoislegalaid.org/legal-information/respond-eviction"
      document_resources:
        appearance:
          - kind: form
            label: "Appearance"
            source: "Illinois Courts"
            url: "https://ilcourtsaudio.blob.core.windows.net/.../AP%20Appearance.pdf"
```

`kind` is one of:

- `interview` — a guided interview that helps fill in the forms. Shown first and
  labelled **Guided interview**.
- `form` — an official blank or fillable form, labelled **Blank form**.
- `instructions` — official instructions for completing the forms.
- `help` — a court or legal-help page about this kind of case.

Rules:

- New or replacement links need scoped partner review. The shipped Illinois
  starter set has HTTP checks but still needs substantive approval (see below).
  Never guess an address or treat HTTP success as approval of a form.
- Links must start with `https://`. Others are dropped and logged. Write spaces
  in an address as `%20`.
- Every link opens in a new tab. The page tells filers that these sites do not
  file anything for them.
- Use YAML anchors (`&fee_waiver_resources` / `*fee_waiver_resources`) to reuse
  the same links across intents.

## 4. Keeping links current

Court PDF addresses change when a form is revised. Check every link with:

```bash
cd efile_app
uv run python manage.py check_document_finder_links
```

It lists each link that does not open and exits with an error if any fail.
Assign an operator to run it, with a backup and escalation contact in the
[maintenance register](./maintenance.md). Weekly link-failure triage is proposed,
not an already scheduled job. Save dated command results in the agreed review
record/issue; the forms editor replaces or removes broken/obsolete destinations
after content review. Update `document_finder.links_checked_on` in State YAML
after a completed reachability check and record failures still awaiting repair.
Do not set the date merely because the command was started.

Also open destination pages and PDFs: check jurisdiction, filer side, form version,
instructions, accessible presentation and whether an interview still produces
the intended forms. A redirect or HTTP 200 can lead to obsolete content. Keep
substantive approval scope/date separate from `links_checked_on`.

## 5. What the finder does not do

- It does not choose a court, case type, or filing code. Signing in from a guide
  returns to that selected guide (the #323 behavior), then "Start this filing"
  or "File into my case" starts an ordinary filing carrying only new/existing.
  Filer side, checklist selections and provider codes are not copied into the
  draft by this action. Codes are chosen against the court's live lists.
- It does not make a checklist item required. The list stays a guide.
- A state with no `document_finder` section shows a short "no guides yet" page
  with the state's court-help contact, and the Upload step does not link to it.

## Approval and recurring review

The Illinois set shipped in [#322](https://github.com/SuffolkLITLab/LITEFile/pull/322)
contains **seven guides and 52 links**, all HTTP-checked on October 13, 2026.
**Partner review before release is still pending.** The source PR's HTTP result
is not substantive approval. The finder and filing-plan checklist use the same
resolver and remain non-blocking.

| Intent key | Substantive approval status | Review scope |
| --- | --- | --- |
| `adult_name_change` | Pending; reviewer unassigned | Adult intent, petition/order/notice variants and current resources |
| `child_name_change` | Pending; reviewer unassigned | Child lead variant, consent/notice items and current resources |
| `divorce` | Pending; reviewer unassigned | Filer side, child-related conditions and current resources |
| `evict_a_tenant` | Pending; reviewer unassigned | Landlord scope, lead/notice/service items and local supplements |
| `respond_to_an_eviction` | Pending; reviewer unassigned | Tenant scope, appearance/answer and service resources |
| `start_small_claims` | Pending; reviewer unassigned | Claimant scope, complaint/service and amount guidance |
| `respond_to_small_claims` | Pending; reviewer unassigned | Responding side, appearance/answer and local instructions |

For each guide add content owner, technical owner, court/legal approver, source
and form versions, last substantive review, next date/event trigger, evidence,
enabled/deployed scope and escalation issue to the shared register. Review intent
wording, `filer_role`, `lead_filing_type_name`, every linked interview/form/help
resource and `court_specific_requirements` supplements. Monthly content review
and review after form/rule/link changes are proposed, subject to owner acceptance.

Massachusetts and Vermont have no populated finder at this snapshot. To add
coverage, partners supply an approved checklist/case-family match, clear intent
wording and search examples, new/existing path, side/lead variants, authoritative
resources, local supplements and an accepted review owner. Do not imply that
creating a route supplies those resources. Test no-guide and no-match states,
ambiguous search, shared checklist output, guide → sign-in → same guide, start
path and Upload → guide → same owned draft.

```bash
uv run pytest -q efile/tests/test_document_finder.py
```

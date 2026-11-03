---
id: jurisdiction-config
title: Configuring jurisdiction-specific features
sidebar_label: Jurisdiction config
sidebar_position: 2
---

# Jurisdiction & court configuration <span className="wip-badge">WIP</span>

All jurisdiction-specific branding, court rules, and document mappings live in single, human-readable YAML files located at `efile_app/efile/static/config/states/{jurisdiction}.yaml`.

---

## 1. High-level jurisdiction metadata

The `jurisdiction` section configures the application title, brand identity, navigation logos, official court statements, and support contact details.

```yaml
# efile_app/efile/static/config/states/illinois.yaml

jurisdiction:
  name: "Illinois eFile"
  code: "illinois"
  display_name: "Illinois"
  brand_name: "LITEfile"
  logo: "img/litefile-logo.svg"
  logo_alt: "LITEfile logo"
  icon: "fas fa-balance-scale"
  svg: "img/IL.svg"
  api_endpoint: "/api/illinois"
  official_court_name: "the Illinois Courts"
  official_tool_statement: "LITEfile is an official, approved electronic filing tool of the Illinois Courts."
  court_logos:
    - name: "Illinois Courts"
      src: "img/court-logo-illinois.svg"
      alt: "Seal of the Supreme Court of Illinois"
  partner_logos:
    - name: "Illinois Legal Aid Online (ILAO)"
      src: "img/logo-ilao.png"
      alt: "Illinois Legal Aid Online logo"
      url: "https://www.illinoislegalaid.org/"
      role: "Project & Funding Partner"
  help_url: "https://www.ilcourthelp.gov/"
  help_number: "833-411-1121"
  contact_email: "litlab@suffolk.edu"
  contact_address: "Suffolk University Law School LIT Lab, 120 Tremont Street, Boston, MA"
```

### Configurable elements:
- `official_tool_statement`: Displayed on the footer and login splash screen to provide court legitimacy.
- `court_logos` & `partner_logos`: Array of logos displayed across the landing page, header, and footer.
- `help_url` & `help_number`: Shown to filers when an error occurs or when a filing is rejected.
- `contact_email` & `contact_address`: Displayed on the Terms of Service and Privacy Policy pages.

---

## 2. Court-specific requirements & clerk contact details

Different courts within the same state often have distinct divisional rules, cover sheet requirements, or clerk contact information. Configure these using `court_specific_requirements`:

```yaml
court_specific_requirements:
  "cook:cd1": # Cook County Circuit Court - County Division
    contact:
      name: "Clerk of the Circuit Court of Cook County - County Division"
      phone: "312-603-5030"
      email: "countydivision@cookcountycourt.com"
      url: "https://www.cookcountyclerkofcourt.org/"
    case_types:
      name_change:
        documents:
          county_division_cover_sheet:
            label: "County Division Cover Sheet"
            requirement: always
            role: supporting
```

:::tip Clerk contact information
When a filing is rejected by a court clerk, LITEfile automatically surfaces the clerk's phone number and email address directly on the filer's status screen so they know who to call for assistance.
:::

### Other-party address rules

An other party's address is optional by default. Add a `party_address` rule only when a court or filing workflow is known to require it. The rule may be placed under `defaults`, under a case type, or directly under a court in `court_specific_requirements`. More specific layers override the default.

```yaml
defaults:
  party_address:
    required: false
    required_for_party_types: []
    required_for_filing_types: []
    required_for_services: []

court_specific_requirements:
  "example:civil":
    party_address:
      required_for_filing_types: ["SUMMONS"]
      required_for_services: ["PERSONAL_SERVICE"]
      reason: "The court needs an address to issue or serve these documents."
```

Set `required: true` when every other party in that layer needs an address. The three `required_for_*` lists may contain a Tyler code or name and are matched without regard to capitalization. A matching party type, filing code, or selected optional service makes the address required. LITEfile also honors an address-required flag from live Tyler party metadata if the code list provides one.

---

## 3. Wording that differs by state

Start with the [change-location map and supported UI catalog](./customization.md)
for a specific screen edit, reviewer workflow and incomplete field-hook coverage.

Courts do not use the same words for the same thing. The document that opens a case is a *petition* in Illinois and a *complaint* in Vermont, and telling a Vermont filer to look for a petition is wrong information, not a stylistic difference.

The `text` section holds the strings this state says differently. Every key has an English default in `efile_app/efile/utils/ui_text.py`; anything you do not list keeps that default.

```yaml
# efile_app/efile/static/config/states/vermont.yaml

text:
  terms:
    starting_document_example: "complaint"
  about:
    project_partner_description: >-
      LITEfile's Vermont e-filing integration was developed in close partnership
      with Legal Services Vermont (LSV), who collaborated on funding, building,
      and deploying LITEfile to expand accessible electronic court filing for
      self-represented litigants in Vermont.
```

### How keys work

- Keys are nested in YAML and referred to elsewhere with dots: `terms.starting_document_example`.
- Keys under `terms` are short nouns. Each one is also available to every longer string as a placeholder named for its last segment, so changing `starting_document_example` changes every sentence that names it.
- Longer strings can also use `{brand_name}`, `{state_name}`, `{state_code}`, and `{court_name}`, which come from the `jurisdiction` and `state` sections of the same file.

### Links in your text

Help text and other longer passages can link to your own rules, forms, or help pages. Write the link in Markdown:

```yaml
text:
  organize_documents:
    confidentiality_help: >-
      You may only file a document confidentially when
      [Supreme Court Rule 138](https://www.illinoiscourts.gov/rules/supreme-court-rules?a=ii)
      or a court order requires it.
```

- Web links (`https://…`) open in a new tab, and screen readers announce "opens in a new tab". Email (`mailto:`) and phone (`tel:`) links are allowed too.
- `*emphasis*`, `**bold**`, and `` `code` `` also work. Headings, lists, images, and raw HTML are removed, because these passages sit inside a sentence or paragraph the page already provides.
- Links only work in longer passages: the keys marked `links=True` in `efile_app/efile/utils/ui_text.py`, such as `*_help`, `your_information.lede`, and `about.project_partner_description`. A label, question, or term shows only the words of a link, and `manage.py check` warns about it (`efile.W003`).
- Translators see the Markdown, so they keep the link and translate only its words.

### What is *not* here

Ordinary copy that no state wants to change stays in the templates. Only strings a state might genuinely reword belong in `text`, so the file stays readable and reviewable by the people who own the wording.

:::tip Check your keys
A misspelled key is ignored, which means the page quietly keeps the default wording — the exact wording you were trying to change. `manage.py check` reports any `text` key that does not exist (`efile.W002`), so run it after editing.
:::

### Translation

Strings configured here are translatable along with the rest of the application. `xgettext` cannot read YAML, so `manage.py extract_config_text` restates them in a generated Python file that `makemessages` reads. Each string carries its key as the gettext message context, so one key's Illinois wording and Vermont wording stay separate messages for a translator. See `efile_app/efile/locale/README.md` for the full workflow.

---

## 4. The court question

Every filing needs a court, and the e-filing service answers that question with one flat list: 207 courts in Illinois, 170 in Massachusetts. A single dropdown over that list asks a filer to recognize their court among the ones that happen to sort next to it.

`court_selector` replaces the list with the questions your state's court structure is actually made of. Illinois routes by county, Massachusetts by court department plus a place the filer knows, Vermont by Superior Court unit — and each of them narrows the same live court list. A jurisdiction with no `court_selector` section keeps the flat list.

```yaml
court_selector:
  title: "Choose the Vermont court for this filing"
  lede: >-
    Start with the court on your paperwork. Superior Court filings then need a
    division, and most divisions need the court unit the case belongs to.
  steps:
    - id: level
      type: choice
      label: "What Vermont court is this filing for?"
      options:
        - value: superior
          label: "Superior Court"
          help: "Vermont's trial court. You will choose a division next."
          courts:
            name_pattern: "Unit$|^Environmental Division$"
        - value: supreme
          label: "Supreme Court"
          courts:
            name_pattern: "^Supreme Court$"

    - id: unit
      type: select
      when:
        level: [superior]
      label: "Which court unit?"
      placeholder: "Choose a unit…"
      options_from_courts:
        match:
          name_pattern: " Unit$"
        strip: " Unit$"
        label: "{name} ({stem} County)"
```

### How the questions run

An answered question folds to a single line with a **Change** link, and once there is a court, every question folds and the screen states the answer. The court field shares a row with the case category and type, so the cascade has to collapse rather than push them down the page.

Steps are asked in order. A step is shown when its `when:` conditions hold and it has something to ask; the filer's answer narrows the pool of courts, and the deepest answer wins, so "Cook County" is replaced by the division under it. When the pool is down to one court, that is the court. When it is down to a handful, the filer chooses among those rather than among two hundred.

| Key | Meaning |
| --- | --- |
| `type` | `choice` (radio cards), `select` (a dropdown), or `location` (a place lookup). |
| `when` | `{level: [trial]}` shows the step only for those answers; `{department: {not: [land]}}` hides it for those. |
| `options` | Written-out answers. Each may carry a `courts:` query and any data a matcher needs. |
| `options_from_courts` | Answers read off the live court list instead — counties, Cook County's divisions, Vermont's units. A step generated this way disappears when it has fewer than two options, which is how only the counties that divide their Circuit Court get asked about divisions. |
| `option_groups` | One option per `members:` entry, all sharing the group's `courts:` query. Illinois asks which county an appeal came from and answers with its appellate district. |
| `alternative_to` | Two questions that name the same court. Both stay on screen; answering either one answers for both. |
| `short_label` | What the question is called once it is answered and folded to one line, e.g. `Division or courthouse`. Defaults to the full `label`, which is usually too long to read well there. |
| `default_by` / `default_hint` | Start a question at an answer, chosen from an earlier one: `{answer: county, values: {Cook: "cook:cvd"}}` starts a Cook County filing at Municipal Civil. A suggested answer never folds away and carries `default_hint` saying it is a suggestion, so it cannot be mistaken for something the filer said. |

### Naming the courts

E-filing services name courts so that they sort. Massachusetts lists a court as `Juvenile Court -- Suffolk County -- Boston`, department first, because that is how the list is organized — but nobody calls it that, and a filer holding paperwork is looking for the Boston Juvenile Court. `court_names` puts the name back:

```yaml
court_names:
  - match: '^Juvenile Court -+ (?P<county>.+?) -+ (?P<place>.+)$'
    name: "{place} Juvenile Court - {county}"
  - match: '^District Court -+ (?P<place>.+)$'
    name: "{place} District Court"
```

Each rule is a regular expression with named captures, and `name` is the rewrite. The first rule that matches wins; a name no rule matches is already right, so Vermont's `Addison Unit` and Massachusetts' `Middlesex Probate and Family Court` need no rules at all.

The rewrite happens once, when the court list is read, so everything downstream — the questions, the court finally chosen, and what is saved on the filing — uses the readable name. That includes the `courts:` queries below, so write them against the names as they read, not as the service lists them.

### Court queries

A `courts:` query says which courts an answer leads to. Every rule is optional and they combine with "and":

| Rule | Matches |
| --- | --- |
| `codes` | Exact court codes, in the order given. |
| `code_prefix`, `code_pattern` | The court's code starts with, or matches, this. |
| `name_pattern` | The court's name matches this (case-insensitive). |
| `exclude_code_pattern`, `exclude_name_pattern` | Drop the courts these match. |
| `group` | The court belongs to this group — see `group_by` below. |

Courts that are only a heading over the courts beneath them never reach the questions at all. "Cook County" is such a row: every Cook filing goes to one of the eighty locations whose code hangs off it, and choosing the county itself returns an empty case-category list with nothing to explain why. Cook County's divisions are headings too: a filing goes to a district location such as Cook County - Municipal Civil - District 1, and the e-filing service refuses one sent to Municipal Civil itself. A court is dropped only when the e-filing service leaves it out of its fileable list **and** a court that list does include hangs off its code. The fileable list alone is not enough, because it also leaves out courts that take filings, such as most Massachusetts District Courts. The same rule applies to the court lists, to filing code search, and to the check made when a search result is chosen. A caption that names only such a county still routes: the questions it does settle are filled in, and `default_by` can start the rest somewhere sensible.

Prefer `name_pattern`. Court **names** are stable and readable; Tyler's codes differ from court to court and change without notice. `{value}`, and any earlier step's id, can be used as a placeholder inside a query.

`option_group_pattern` groups a dropdown under headings. It is matched against each court's name (after `strip`), and its named captures say which part is which: `group` is the heading, `label` is what to show under it, and `extra` is anything worth adding only when it is not the heading's own place. Cook County lists more than eighty locations, and reading them as one alphabetical run is what made the old dropdown unusable.

```yaml
        option_group_pattern: '^(?P<label>.*?)\s+-\s+(?P<group>District \d+)\b(?:\s+-\s+(?P<extra>.*))?$'
        option_group_names:
          - "District 1 - Chicago"
          - "District 2 - Skokie"
        option_group_other: "No courthouse given"
```

`option_group_names` gives each heading its full name and, by the order they are written in, the order they are shown in. That is how the Chicago courts come first in Cook County, where most of its filings go, rather than wherever the alphabet puts them; `option_group_other` heads the courts the pattern found no heading for, and they come last. Without `option_group_names`, headings are taken from the pattern itself and sorted, and a heading over a single court is dropped so that court keeps its full name.

`group_by` on the selector itself is a regular expression whose first capture is the group a court belongs to. Illinois needs one because it names its counties' courts every which way — `Adams County`, `Kankakee - Civil`, `Peoria CR`, `St. Clair County-Backlog` — and they all have to land in one county.

### Asking for a place instead of a county

A `location` step hands what the filer types to a matcher:

```yaml
    - id: place
      type: location
      when:
        level: [trial]
        department:
          not: [land]
      label: "Where is the case connected to?"
      placeholder: "Town, city, or street address"
      button_label: "Find courts"
      examples: ["Cambridge", "Somerville", "24 Beacon St, Boston"]
      matcher: macourts
      court_types_from: department
      manual_label: "Or choose your court from the list"
```

`matcher:` names the library that answers the question. `macourts` uses [MACourts](https://github.com/SuffolkLITLab/MACourts), which owns the Massachusetts court records and jurisdiction rules; `vtcourts` uses [VTCourts](https://github.com/SuffolkLITLab/VTCourts), which resolves a Vermont town, county, or ZIP to its Superior Court unit. `court_types_from` names the step whose chosen option carries the `court_types:` the matcher should search, where a matcher needs one. Matches are mapped back onto the e-filing service's own courts — by code where the source carries one, by name otherwise — and a court this environment does not carry is dropped rather than offered.

Massachusetts asks this way because its Trial Court departments do not divide the state the same way: the District Court that serves Somerville is not in the county its Probate and Family Court is, and which Boston Municipal Court division serves an address is a question about which side of a ward line the building is on. A filer knows their town. Asking them for a county produces a confident wrong answer.

The lookup is always an alternative, never a requirement, and there are two ways to say so. Massachusetts sets `manual_label`, which puts the department's full court list beside the lookup for a filer who already knows their court and for a place the rules cannot resolve. Vermont instead marks its lookup `alternative_to: unit`: the unit dropdown stays where it is, and the lookup is the way in for a filer who knows their town but not that the town is in the Washington Unit. Answering either one answers for both.

`no_match_hint` is what the screen says when the lookup finds nothing — worth wording per jurisdiction, since it should point at whichever alternative that state actually offers.

A cross-county ZIP, or two courts with overlapping jurisdiction, come back as a short list to confirm rather than as a guess between them.

:::tip Check your patterns against the live list
The court names a pattern has to match are the ones the e-filing service returns, and they need no authentication to read: `curl "https://efile-test.suffolklitlab.org/jurisdictions/vermont/codes/courts?with_names=true"`.
:::

### Fee waiver guidance

Each state can define a `fee_waiver` section with `benefits`,
`document_guidance`, `source_url`, and `income_bands`. Each income band has a
user-facing `description` and a `max_fpl_percent`; ranges may also specify
`min_fpl_percent_exclusive`. An optional `waiver_percent` describes the
percentage of fees waived. Keep thresholds as percentages of the federal
poverty level (FPL), without converting them to dollar amounts. Descriptions
should identify the income basis and whether relief is full or partial.

These settings explain eligibility; they do not determine it or create waiver
accounts. The filer must choose to request a waiver. Keep the source link and
document guidance current, and leave unconfirmed requirements explicit.

### Court code fee estimates

The estimator reads the selected court's case-type and filing-code records in
Illinois, Massachusetts, and Vermont. For a new case, it includes the published
case opening fee once, then adds any separate document fees. Existing cases
only include document fees. It does not identify free filings by court or case
names.

A matched filing-code record with a blank or zero fee indicates no separate
fixed document fee only when all three monetary-input flags
(`amountincontroversy`, `civilclaimamount`, and `probateestateamount`) are
explicitly `Not Available`. Missing records, incomplete flags, and unreadable
fees remain unknown. A required or optional monetary input prevents a zero
placeholder from being treated as free, even if the user already entered an
amount.

State `fee_estimates.rules` remain available for supported amount-dependent
fees. Initial-only rules estimate a case opening fee when it is missing or is
a zero placeholder tied to a monetary input. The estimator does not use
amount-in-controversy bands for estate-value or civil-claim inputs. Unsupported
calculations remain unknown until the live quote.

State `fee_estimates.surcharges` controls platform and processing fees:

```yaml
surcharges:
  reviewed: "2026-09-29"
  waiver_account_types: [WV]
  payment_methods:
    BankAccount:
      fixed: "0.25"
      only_when_amount_due: true
    CC:
      percent: "2.89"
      basis: court_and_platform
      only_when_amount_due: true
  platform:
    amount: "22.00"
    timing: new_case
```

Each payment method key is Tyler's account type code. A rule may combine
`fixed` and `percent`; percentages use either `court` or `court_and_platform`
as their basis and round to cents. `only_when_amount_due` defaults to true;
set it to false for a charge that applies even when the subtotal is zero.
The estimator adds these charges once per filing, not once per document.
With no recognized payment method, it estimates the range across configured
ordinary methods. Waiver accounts are excluded from that range.

`platform.timing` accepts `new_case` or `first_filing_per_filer`. Massachusetts
uses the former ($22), Vermont the latter ($14). For an existing case, callers
may supply `estimate_fees(..., first_use=True/False)` when prior payment by this
filer/firm is known. Otherwise Vermont's platform fee remains a range. A free
response in a paid case does not prove that the per-filer fee is exempt.

Platform `exemptions` have stable `id` values and may have a state-wide
`case_pattern` regular expression, `requires_waiver_account`, a plain-language
`note`, and an optional official `source_url`. `initial_only: true` limits
automatic matching to drafts marked as a new case. A matched pattern shows
exemption guidance; it never creates or selects an account. Rules without a
pattern, such as Vermont's `government_or_appointed` and `no_court_fee_case`,
match only when a caller supplies `exemption="rule_id"` for an established fact
that case names cannot show. Paid Vermont accounts keep the platform fee even
when court fees are zero, because a catalog zero does not prove statutory
eligibility. Unknown court fees remain unknown.

A statutory exemption that requires a waiver account changes the payment choice
label and help text, so users need not claim low income. Account creation still
requires their explicit choice. Merely selecting a waiver account does not
establish a confirmed fee quote. The waiver flow displays $0 due now subject
to court review; ordinary paid accounts require a current quote.

Keep `reviewed` and `fee_estimates.sources` current when updating rates. Illinois
has $0.25 bank processing and 2.89% card processing; Massachusetts uses those
rates plus its platform fee; Vermont uses $1 bank processing and 2.89% card
processing plus its platform fee. The old unconditional `convenience_fee` setting
has been replaced. Other services may still apply. The live quote replaces the
estimate; configured charges are never added to a live quote. Public code samples from all
three states are saved in
`efile_app/efile/tests/fixtures/fee_code_samples.json` for regression tests.
State configuration changes refresh the cached state settings on the next
request.

## Temporarily disable filing

Add `filing_availability` under a court's `court_specific_requirements` entry to
block the whole court or selected categories, case types, and filing codes.
Filing is enabled by default. Type selectors match human-readable names exactly
or with explicit regexes, independent of Tyler's numeric type IDs. Filers see
restrictions immediately after choosing an affected court or type.

See [Control filing availability](./filing-availability.md) for the complete
field reference, Cook County hearing-scheduling example, county-prefix rules,
message precedence, deployment and re-enabling instructions, troubleshooting,
and live-check API.

## Filing code search terms and explanations

The “Find the right filing code” modal on Confirm case searches complete paths
across court, case category, case type, and filing code. A selected court does not
limit results. New and existing cases have separate filing-type lists. Search uses
English Snowball stemming, phrase synonyms, and conservative spelling suggestions;
it does not call an LLM or decide which court has jurisdiction over a person's case.

The modal walks through three steps, each answering one question:

1. **What are you filing?** A plain-language search, the case ZIP, and short
   questions shown as chips. An empty search offers common filing-type starting
   points. Results group repeated filing labels and recognized synonyms across
   courts, so the same filing does not occupy a separate result for every county.
   Codes a court splits only by jury, government-filer, or fee qualifiers
   (Cook County's “Complaint / Petition - Fraud (Jury - 12) (Govn't) - Fee”) also
   appear once; the exact one is chosen in step 2. Filings whose own names match
   the search are listed first. When some do, filings that match only through
   their case type are held behind a “Show N more” button.
2. **Choose your court and case type.** Courts that accept the chosen filing,
   under the current answers and ZIP, appear as cards (or a dropdown when there
   are more than six). A court already selected on the form is offered first and
   preselected. Choosing a court lists its exact case-type paths; when a court
   names the filing differently, the case type says so. Some courts code one
   filing as many case types that differ only by claim amount, a lawyer or
   self-represented filer, jury size, or a government filer (Cook County has 28
   for one personal-injury complaint). The court step reads those from the case
   type and filing names and asks, as chips, only the ones that court's paths
   differ on. It assumes a filer isn't filing for a government body, and shows
   that answer so it can be changed. A path that doesn't say stays listed.
3. **Check your choices.** The full court, case category, case type, and filing
   type, each with a link back to the step that set it. Applying revalidates the
   path with the court before it fills the form.

When a search matches eight or more filing codes, step 1 also asks **What do you
want to do?** (start a case, respond, ask the court, settle or end, supporting
papers, proposed order, notice), with a count on each choice. The answer is a
guess from the filing name alone, from the ordered `actions` rules in
`efile_app/efile/data/filing_code_facets.yaml`; names no rule matches are
shown only under “Not sure”.

When the matching case types name different claim amounts (“Small Claims -
$0 to $10,000”, “Amount Claimed Greater Than $10,000”), step 1 also asks **How
much are you asking for?**, in any case area. A range is labeled “(small
claims)” when its case types say so and the others don't. Case types that name
no amount stay listed.

Step 1 also offers **Case category** (Cook's “Civil”, “Personal Injury/Wrongful
Death”, “Probate”, …) when results span more than one, so a filer can set aside
a division that doesn't fit. “Kind of case” and “Case category” list their most
used choices first and collapse the rest behind an “N more” chip. A case
question in `case_type_guidance.yaml` can set `other:` (for example
`other: Something else`) to add a choice that keeps only the case types none
of its options name, such as the general personal-injury lawsuit.

### Court-term glossary

`efile_app/efile/data/legal_glossary.yaml` gives plain-language meanings for
court terms that appear in filing and case-type names, such as subrogation,
dram shop, and tort. Each term lists the words that name it (`match`, whole
words, case-insensitive), a one- or two-sentence `text`, and an optional
`source` link. Under `jurisdictions:`, a state can reword a general term or add
its own (Massachusetts' “summary process”); its entries replace general ones
key by key. Wherever a result names a term, code search offers a “What does
‘subrogation’ mean?” disclosure: on each filing code, above a court's case
types, and on the final check. Meanings describe a term; they don't advise
whether to file.

Grouping does not substitute one court's code for another's, and distinct
documents such as complaints and answers remain separate. Contexts reflect the
paths matching the current search, not unrelated uses of the same filing name.

Case stage (new or existing) appears as one line under the search, with a
**Change** link. Document kind (main document or attachment/exhibit) sits behind
**More filters**. Starting a claim is not a determination of the filer's party
role: for example, an existing case can include a counterclaim.
`efile_app/efile/data/filing_code_facets.yaml` contains ordered filing-name rules;
the first match supplies the classification. Rules may limit `jurisdictions`.
Unmatched or ambiguous names remain unclassified and are shown when no document
filter is chosen. Filters apply before pagination and carry
through court and exact-path selection. These labels do not change which documents
the court permits as attachments. Choosing a different case stage changes the form
only after the filer applies a validated path. The API still accepts a `purpose`
filter, but the modal no longer offers it.

Shared rules live in `efile_app/efile/data/filing_code_search.yaml`. The
`jurisdictions` list currently includes Illinois, Massachusetts, and Vermont.
Concepts group search terms, such as eviction, unlawful detainer, and summary
process. A concept can include its own `jurisdictions` list to limit its scope.
These are search associations, not statements that remedies or procedures are
legally interchangeable. Multiword phrases match together: “summary judgment”
does not expand to “summary process.”

Use `path_aliases` when terminology depends on the case and filing stage.
Each rule requires `jurisdictions`, `initial` (`true` for new cases), and exact
`filing_type` names. Optional `case_category` and `case_type` lists also match
whole normalized names; `case_concepts` requires every named concept in the
category/type context. `terms` adds search vocabulary only to matching paths.
For example, the Vermont complaint rules add “complaint” to **Initial Filing**
only in the reviewed Family, small-claims, and eviction case types. Administrative
answer-only cases, foreign-order registrations, and other divisions receive no
complaint alias.

A concept with `path_only: true` expands phrases only in filing-code queries.
Matching aliases must explicitly grant it through `concepts`; a matching word
in a court or case name cannot grant that document-specific concept. Illinois
“seal eviction” uses this approach for the exact record-removal motion in an
existing eviction case. If the court does not offer that motion, the alias
creates no substitute. These restricted concepts do not alter document-finder
queries or rename provider labels.

Rules need a `source` documenting the association, plus regression tests for the
opposite stage, unrelated case types, and other courts. Restrict a local alias
with `court_codes: ["cook:dr1"]` or `court_code_prefixes: ["cook:"]`, using actual
provider court codes. Grouped labels keep different indexed vocabularies separate
so Cook aliases cannot leak to another county with the same label names. Use a
separately reviewed explanation rule for the contextual description.

The optional `facet` is `filing_type` by default. Set it to `case_category` or
`case_type` to add vocabulary to that level, with an exact label constraint for
that facet. For example, Cook Domestic Relations category aliases add “family”
to case search terms without claiming that a particular filing document is a
family-law form.

`guided_form_aliases_file` names the generated JSON supplement beside this YAML.
It contains printed-form concepts, scoped path aliases, and sourced definitions;
its contents also affect the index digest. Regenerate it with the reviewed form
reconciliation script, not by treating a repository title as a provider code.
Restart workers and rebuild the affected jurisdiction's index after editing
these rules, including when catalogs themselves have not changed.

When a result was found through a related term rather than its own name (a
search for “unlawful detainer” finding an eviction filing), it says “Related to
eviction”. Concept definitions are expandable under “About this filing code”.
They describe the topic, not the purpose of every document.

At the bottom of the results, a link such as “10 results in ‘existing case’
filings” appears when the same search and answers find filings in the other
case stage. This matters for filings like sealing an eviction, which go in the
existing case. Choosing it switches the case stage and searches again.

Display definitions live separately in
`efile_app/efile/data/filing_concept_definitions.yaml`, keyed by the same normalized
concept IDs as the search rules. The precedence is general default, state, county,
then exact court. Before a court is chosen, results show the general or statewide
meaning. Selected-court paths use the local meaning and identify its scope.

For example, the following illustrates where **court-reviewed local wording**
belongs; the placeholder text is not a statement of Cook County procedure:

```yaml
version: 1
county_courts:
  illinois:
    Cook:
      names: [Cook County]
      prefixes: ["Cook County - "]
concepts:
  eviction:
    label: Eviction
    default:
      text: A court process about recovering possession of property.
    jurisdictions:
      illinois:
        text: "<Statewide explanation>"
        source: "https://example.org/state-court-source"
        counties:
          Cook:
            text: "<Reviewed county-specific explanation>"
            source: "https://example.org/county-court-source"
        courts:
          Cook County - Municipal Civil - District 1 - Chicago:
            text: "<Reviewed court-specific explanation>"
            source: "https://example.org/court-specific-source"
```

County membership uses explicitly configured court names or prefixes, matched
without regard to capitalization. Use a separating boundary in prefixes, as above;
do not assume a court's county from a substring. Exact court overrides take priority
over county overrides. A replacement `text` uses its own `source`; it never inherits
a potentially unrelated source link. An empty `text` suppresses the inherited
definition. `label` can also be overridden. Unconfigured localities use the state
definition, then the general fallback.

```yaml
concepts:
  eviction:
    terms: [eviction, evict, unlawful detainer, summary process]

explanations:
  - jurisdictions: [massachusetts]
    filing_type: [Summary Process Complaint, Complaint for Summary Process]
    text: A complaint asking the court to decide an eviction case.
    source: https://www.mass.gov/how-to/file-an-eviction-case
```

Explanation rules match whole filing-type names after normalizing capitalization,
accents, and punctuation. They can also specify lists of exact `court`,
`case_category`, and `case_type` names, plus an `initial` boolean. All specified
conditions must match; the first matching rule supplies the explanation and its
source link. Put specific rules before general ones. Use court sources and short,
descriptive text rather than eligibility advice. Avoid matching numerical codes,
which can change between courts or environments.

Unknown or renamed codes remain searchable without an explanation. The modal says
when no explanation is available and shows the official name and full path.
Live selection validation regenerates the explanation using the current names.
Filing availability restrictions remain visible and prevent choosing blocked paths.

For changes to indexed terms or filing-code explanation rules in
`filing_code_search.yaml`, deploy with the application, restart workers, and refresh the index
using the [deployment instructions](../admin/deployment.md#filing-code-search-index).
Changes to concept definitions or facet classifications need only a web-process
restart; they are applied to responses outside the cached search results and do
not invalidate or rebuild the filing catalog.
Add search and explanation examples to
`efile_app/efile/tests/test_filing_code_search.py` when changing matching rules.

### ZIP filters in filing code search

Every jurisdiction offers an optional case ZIP. A state whose `court_selector`
configures a location `matcher` answers from that package: Massachusetts asks
MACourts and Vermont asks VTCourts which courts serve the ZIP. Courts the
package has no record of are placed by county instead. Other states (Illinois
and any new state) go through counties alone: the ZIP maps to counties through
`efile/data/zip_counties.json.gz`, built from Census 2020 ZCTA relationship files
by `efile_app/scripts/build_zip_counties.py`. Illinois's own
`efile/utils/zip_to_county_il.py` is checked first because it also knows
PO-box ZIPs. A court is placed in a county by its code (`cook:cvd1`), its name
(“Orange Unit”, “Middlesex Probate and Family Court”), or a town at the end of
its name (“District Court - Cambridge”). A court nothing can place, such as an
appeals court or a statewide division, stays visible for every ZIP.

Filtering happens before grouping, so case-type previews, courts, and exact
paths all use the same scope. Clearing the ZIP restores statewide results.
Unknown ZIPs produce a recoverable message rather than silently showing
statewide results. ZIP matching is a location aid, not a determination of which
court has jurisdiction. The field is never prefilled. Instead, one-click
shortcuts offer the filer's own address ZIP and the last three ZIP codes they
used with an applied filing code. These are kept on the account as
`recent_case_zips`, because filers who help others often file far from their
own address.

### Context-specific case guidance

`efile/data/case_type_guidance.yaml` defines 20 starter case families with
plain-language descriptions, source links, and contextual facets. This is a
coverage set, not a statistical ranking of filing volume. Definitions are
matched by names, independently of environment-specific numeric IDs.

| Case family | Contextual choices |
| --- | --- |
| Eviction | Tenant/landlord, home/business, possession/money |
| Foreclosure | Owner/lender, property, mortgage/other lien |
| Debt collection | Collecting/responding, debt or collection type |
| Small claims | Claimant/responding, dispute type, claim amount |
| Contracts | Claimant/responding, services/goods/insurance |
| Personal injury | Claimant/responding, vehicle/medical/property/death |
| Property damage | Claimant/responding, kind of property |
| Replevin and detinue | Requesting/responding, recovery procedure |
| Divorce and separation | Requesting/responding, children, kind of request |
| Parentage | Requesting/responding, establishing/challenging, order action |
| Parenting and custody | Requesting/responding, decisions/time/moving, order action |
| Child support | Requesting/responding, order action, interstate order |
| Protection orders | Requesting/responding, kind of protection, duration |
| Adult guardianship | Requesting/objecting, personal/financial authority, duration |
| Child guardianship | Requesting/objecting, personal/financial authority, duration |
| Estates and probate | Requesting/objecting, administration/will dispute/claim |
| Name changes | Requesting/objecting, adult/child |
| Adoption | Requesting/objecting, adult/child, relationship |
| Agency review | Challenging/responding, kind of agency decision |
| Employment | Worker/responding, wages/discrimination/dismissal |

The configuration supports Illinois, Massachusetts, and Vermont. General starter
meanings do not establish eligibility or replace local court requirements. State
entries under a topic's `jurisdictions` can replace descriptions and facet rules.
County entries can provide `court_names` or `court_prefixes` and override `text`
and `source`; exact court entries under `courts` take precedence. Local wording
can differ without changing the state-level discovery classification. Detailed
rules should use explicit terms; absent information stays `unknown`.

Each facet defines a key, label, and options. An option's regular expression
matches the case label, filing name, or both, according to its `field`.
Dimensions with no recognized values are hidden. Unknown classifications remain
included when filtering, with an explanation in the interface. Role choices
narrow explicit document names and keep shared/unclassified documents; they do
not decide a filer's legal party status. Changing the query or case area resets
its contextual choices. Filtering applies before pagination and remains in
effect when listing courts and exact paths.

Small claims takes precedence over substantive labels within that track, such
as a contract claim in a small-claims category. The amount dropdown appears when
remaining labels contain different recognizable claim brackets. It parses
amounts in integer cents, including dollar signs, commas, decimal amounts, and
`K` suffixes. It never reads internal code IDs or filing fees. Labels without
recognizable brackets stay visible. A selected amount remains visible and can
be cleared even if another filter removes its matching labeled paths.

Amount brackets describe catalog choices, not legal eligibility. State-specific
help links explain the usual limits and exceptions: Illinois generally uses
$10,000 excluding interest and costs; Massachusetts generally uses $7,000 with
an automobile property-damage exception; Vermont generally uses $10,000 but
limits consumer-credit and medical-debt claims to $5,000. Brackets crossing the
usual limit are marked for checking. Sources are stored alongside the wording
in the YAML, including the [Illinois court rules](https://www.illinoiscourts.gov/rules/supreme-court-rules/),
[Massachusetts small claims guidance](https://www.mass.gov/info-details/small-claims-court),
and [Vermont small claims guidance](https://www.vtcourts.gov/civil/suing-and-being-sued-small-claims).

The guidance configuration is cached per web process. Restart the web service
after editing it; no filing catalog rebuild or database migration is required.

## Contextual filing guidance

Use `filing_guidance` for informational warnings and help that depend on the
filer's saved choices. These messages never require an acknowledgment or block
navigation or submission. Court requirements accepted on Review remain separate.

Each message appears above the form on its configured workflow steps. Choose a
step before the relevant task: for example, `upload_documents` for statewide
upload help, `document_checklist` or `organize_documents` for court-specific
document instructions, and `payment` for payment help. Include `review` when the
same information is useful before submission. Court-specific help appears only
after a court is saved; use a later step if court selection happens after upload.

The following example uses **placeholder codes and copy**, not actual court
instructions. Replace them with current provider codes and partner-reviewed text
and links before deploying:

```yaml
filing_guidance:
  - id: local_document_help
    steps: [document_checklist, organize_documents, review]
    title: "Preparing documents for this court"
    text: "Read this court's document instructions if you need help preparing your filing."
    when:
      court_codes: ["example:division-a", "example:division-b"]
      case_type_codes: ["example-case-type"]
      existing_case: ["new"]
    resources:
      - label: "Court document instructions"
        url: "https://court.example.org/filing-help"
```

- `id`, `steps`, `title`, and `text` are required. IDs must be unique within the
  jurisdiction. Titles and text are plain text; HTML is escaped.
- Omit `when` for statewide help. All configured conditions must match; within
  each list, any one value matches. Missing selections do not match a condition.
- Supported conditions are `court_codes`, `case_category_codes`, `case_type_codes`,
  `case_subtype_codes`, `filing_type_codes`, and `existing_case` (`new` or
  `existing`). Code matching is exact. Quote numeric provider codes in YAML.
- A `filing_type_codes` condition matches any saved document, including supporting
  documents. It does not use an AI prediction or an obsolete draft-level default.
- To target a county, division, or district, list its current filing court codes
  in `court_codes`. Names, prefixes, and inferred geographic matches are not used.
  Maintain this list when provider codes change.
- `resources` is optional. Each resource needs a descriptive `label` and an
  absolute HTTPS `url`. Links open in the same tab.
- Supported steps are the active keys in `efile/workflow.py` (`FILING_WORKFLOW`).
  Messages appear in configuration order. They refresh on the next rendered page
  after choices are saved, including when a draft is resumed. They do not update
  while a dropdown has an unsaved selection.

No local court guidance is enabled by default. Add reviewed messages to the
appropriate state's YAML file. Run `uv run python manage.py check` after editing;
invalid configuration produces `efile.W004` and that jurisdiction's guidance is
omitted. Filing remains available. Use
`uv run python manage.py check --fail-level WARNING` to reject these errors in a
configuration review. Run `uv run python manage.py extract_config_text` and then
`uv run python manage.py makemessages -l es` to include the new copy and link
labels in the existing translation workflow.

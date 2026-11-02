---
id: document-checklists
title: Configuring document checklists & plans
sidebar_label: Document checklists
sidebar_position: 3
---

# Document checklists & filing plans <span className="wip-badge">WIP</span>

Document checklists give filers plain-language guidance about documents that
often go with a case type. They do not decide which documents an individual
must file and do not block a filer from proceeding.

---

## Temporarily hiding checklists

Set `document_checklists_enabled: false` at the top level of a state's YAML
configuration to hide suggested-document checklists and filing-plan prompts.
Illinois currently uses this setting. Other states keep the default, `true`.

The setting preserves saved plans and their answers. Document previews,
validation, case-side questions, and the form finder remain available, including
links to individual forms. Set the value back to `true` to restore checklists.

## 1. Defining case type matching & guidance

Checklists match against the **human-readable names** returned by the court e-filing system (never hardcoded numeric codes).

```yaml
# efile_app/efile/static/config/states/illinois.yaml

case_types:
  name_change:
    extends: "base_case_types.name_change"

    # Court names returned by the EFSP
    matches:
      names:
        - "Name Change" # Cook County County Division
        - "Change of Name" # Statewide Circuit Courts
      aliases:
        - "Petition - Change of Name"

    # Plain-language explanation for filers
    about:
      summary: >-
        A name change asks a judge to make your new name official. Most of the
        work is paperwork: a request that says who you are and what you want to
        be called, notice in a newspaper so the change is public, and an order
        for the judge to sign.
      learn_more_url: "https://www.illinoislegalaid.org/legal-information/changing-your-name"
      learn_more_label: "Changing your name in Illinois (Illinois Legal Aid Online)"

    documents:
      petition:
        label: "Request for name change"
        requirement: always
        role: lead
        description: "Your name now, the name you want, and how long you have lived in Illinois."
        filing_type_names:
          - "Petition for Name Change"
          - "Petition"
          - "Complaint"

      proposed_order:
        label: "Proposed order for the judge to sign"
        requirement: always
        filing_type_names:
          - "Proposed Order"
          - "Order"
          - "Other Document Not Listed"

      publication_notice:
        label: "Proof of newspaper publication"
        requirement: usually
        description: "Notice published in a local newspaper once a week for three consecutive weeks."
        filing_type_names:
          - "Certificate of Publication"
          - "Affidavit of Publication"
```

---

## 2. Requirement levels

Checklist items are grouped into three tiers. The YAML keys describe how often a document goes with this kind of filing. Filers see softer labels, because LITEfile cannot know which forms a particular filer's situation calls for:

| Requirement level | Displayed as | Meaning |
| :--- | :--- | :--- |
| `always` | **Commonly included** | Documents that almost every filing of this type includes, such as the form that opens the matter. |
| `usually` | **Often included** | Standard documents included in most filings of this type. |
| `sometimes` | **May apply** | Documents that depend on specific facts. |

The checklist never blocks a filer. Filers can continue with any item unticked, and they are never asked to confirm that the list is complete.

---

## 3. Two-sided cases & role-based checklists

In cases with two opposing sides (such as landlord/tenant evictions or domestic relations), the landlord and tenant need completely different documents and instructions.

Configure `filer_roles` to create role-tailored checklists:

```yaml
case_types:
  eviction:
    filer_roles:
      landlord:
        label: "The Landlord (or property owner/agent)"
        description: "You are asking the court to regain possession of the property."
        party_type_keywords: ["plaintiff", "landlord"]
      tenant:
        label: "The Tenant (or occupant)"
        description: "You are defending an eviction or responding to court notice."
        party_type_keywords: ["defendant", "tenant"]

    documents:
      complaint:
        label: "Eviction Complaint"
        requirement: always
        role: lead
        for_roles: ["landlord"] # Only shown to landlords

      appearance:
        label: "Appearance and Answer"
        requirement: always
        for_roles: ["tenant"] # Only shown to tenants

      proof_of_service:
        label: "Proof of Service"
        requirement: usually
        by_role:
          landlord:
            label: "Proof that the tenant was served with court papers"
            description: "Filed by the sheriff or special process server."
          tenant:
            label: "Proof that you sent a copy of your answer to the landlord"
            requirement: always
```


## Approval and maintenance

Examples above illustrate the schema; their local rule statements require
current partner/court review before reuse. The document finder and filing-plan
checklists share `efile/services/document_checklists.py`, including role and
lead-document conditions. Keep both non-blocking when reviewing wording.

Use the [shared maintenance register](./maintenance.md) and the
[per-guide approval table](./document-finder.md#approval-and-recurring-review)
instead of treating a YAML merge as legal approval. For each checklist record
case family/court/stage, intent wording, filer side, lead-document variants,
requirement labels, linked interview/form/help versions, local supplements,
authoritative sources, content/technical owners, approver/date/scope and next
review. Review both sides and child/adult or other lead variants in staging.
Recheck when forms, local rules or court case/filing names change. HTTP link dates
are separate from substantive approval. The Illinois starter guides are pending
partner review; Massachusetts/Vermont finder coverage is not populated.

---
id: index
title: Partner & court guide overview
sidebar_label: Overview (WIP)
sidebar_position: 1
---

# Partner & court configuration guide <span className="wip-badge">Work in progress</span>

:::warning Work in progress (WIP)
This section is actively being expanded as new court systems, case types, and partner integrations are brought online. Contact the [Suffolk LIT Lab](mailto:litlab@suffolk.edu) for technical collaboration or pilot deployment inquiries.
:::

LITEfile is engineered from the ground up to be **declarative, customizable, and multi-jurisdictional**. Court administrators, state administrative offices of the courts (AOCs), and legal aid partners (such as Illinois Legal Aid Online) can configure:

- **Court & partner branding**: Official court seals, partner badges, help line phone numbers, and customized terms of service.
- **Dynamic case types & checklists**: Rule-based filing plans that advise filers which documents are needed for their case.
- **Court-specific overrides**: Tailored document rules and clerk phone numbers down to the individual county or division level.
- **AI document extraction prompts**: Customized prompt instructions and deduction hints for state-specific case codes and court nomenclature.

---

## How LITEfile configures jurisdictions

```
efile_app/efile/static/config/
├── README.md                    # Technical documentation
├── base-case-types.yaml         # Base configuration (shared across states)
└── states/
    ├── illinois.yaml            # Illinois configuration
    ├── massachusetts.yaml       # Massachusetts Trial Court configuration
    └── vermont.yaml             # Vermont Judiciary configuration
```

- **Base configuration (`base-case-types.yaml`)**: Defines common party structures, input widgets, and default field validations.
- **State configuration (`states/{jurisdiction}.yaml`)**: Inherits from base and specifies state court naming conventions, document checklists, filing code mappings, and court contact directories.

---

## Guides in this section

1. [**Jurisdiction & court configuration**](./jurisdiction-config.md): Setting up state metadata, logos, court codes, and clerk contact numbers. See [Filing availability](./filing-availability.md) to disable filing for courts or selected category, case-type, and filing-type names, with immediate explanations for filers.
2. [**Document checklists & filing plans**](./document-checklists.md): Authoring plain-language document checklists with requirement levels and role-based conditions.
3. [**Document finder**](./document-finder.md): Letting filers search by what they want to do, and linking guided interviews, blank forms, and help pages.
4. [**Customizing AI extraction & prompts**](./ai-customization.md): Fine-tuning LLM extraction prompts and field dictionaries for court documents.
5. [**Docassemble & AssemblyLine integration**](./interview-integration.md): Connecting automated interview workflows to e-file directly.


## Partner handover tasks

- [Change wording and local guidance](./customization.md): one change-location
  map, supported text keys, markup, translation and the current YAML/PR workflow.
- [Review emails and tracker guidance](./email-review.md): four message categories,
  sender/delivery gates and sourced estimates; customized emails remain disabled
  pending signoff.
- [Maintain reference data](./maintained-data.md): distinguish automatic catalogs
  from curated terms, definitions, geography and court mappings.
- [Content approval and maintenance register](./maintenance.md): accepted owners,
  source reviews, approval scope, deployed enablement and next checks.
- [Interpret usage reports](./reporting.md): metrics, observation boundaries,
  suppression and account-versus-person limits.

The October 13, 2026 audit covers implemented features, not a declaration of RC1
readiness. Use the register for unassigned/pending reviews and related decision
issues. Operators and support staff can start with [role-specific onboarding](../admin/staff-onboarding.md)
and [service maintenance](../admin/service-maintenance.md).

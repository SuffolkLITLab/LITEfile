---
id: staff-onboarding
title: Onboard support staff and reporting partners
sidebar_label: Staff onboarding
---

# Onboard support staff and reporting partners

Choose the role needed for the task. Staff tools are at a private URL supplied by
the service operator; public documentation does not disclose that deployment URL.
The technical authority remains the internal
[staff privacy and analytics runbook](https://github.com/SuffolkLITLab/LITEFile/blob/main/docs/developer-notes/staff-privacy-and-analytics.md).
This page gives onboarding paths rather than duplicating console setup or
destructive procedures.

## Common access and recovery

The operator applies migrations, bootstraps the initial superuser and provisions
local staff identities. A superuser grants **Account management** and/or
**Aggregate reporting** separately for each jurisdiction. `is_staff` alone grants
neither role. Superusers have both across jurisdictions; use that access only for
operator tasks. Staff accounts cannot use ordinary filer workflows.

Sign in with the assigned local password and a fresh TOTP authenticator code.
Court login is not used. Import the provisioning secret directly into the approved
authenticator; do not capture its console output, setup URI or QR code in logs,
tickets or screenshots. Staff sessions require reauthentication after 15 minutes.
Role revocation/deactivation applies on the next request.

If the authenticator is lost, follow the approved staff identity-verification and
operator recovery procedure. Only the authorized operator resets the TOTP device
from the deployment console; resetting revokes sessions. There is no password-only
or static recovery-token bypass. The operator must approve a recovery procedure
and record its owner before onboarding staff.

## Account-support onboarding

1. Obtain an Account management grant for the supported jurisdiction and read its
   approved intake/identity-verification procedure. Agree escalation contacts for
   disputed authority, active submissions and external storage/provider work.
2. Practice **Account lookup** on synthetic data. Look up an explicit account ID,
   email **plus jurisdiction**, or draft ID. An email matching across states does
   not establish a single account or shared authority. The lookup shows minimal
   metadata/counts, not filings, passwords, payment information or session payloads.
3. For a privacy request, verify identity and authority through the approved
   contact channel. Never request a password or a court filing as identity evidence.
   Store correspondence in the approved support system, not backend free-text fields.
4. Review only selected owned drafts or the verified local account scope. Escalate
   another person's records, unclaimed handoffs and contested ownership; do not
   infer ownership from a name/document. Reconcile `submitting`/`error` drafts with
   the filing service before deletion; a timeout does not prove failure.
5. If deletion is approved and enabled, follow the runbook's inventory preview,
   verification and final confirmation. A partial S3/version failure remains
   **needs attention**, with frozen records and a retry manifest; escalate and
   retry through the established workflow. Never report a partial attempt complete
   or manually unfreeze it to clear a queue.
6. **Filings with an unknown outcome.** The staff home page counts filings that may
   or may not have reached the court: a submit cut off mid-call stays `submitting`,
   and an unclear court reply leaves `error`. LITEfile never resubmits these
   itself, because a retry could file and charge twice. Find each one in
   **Account lookup** with status Submitting or Error. Ask the filer to check
   **My filings**, or check the filing service for an envelope from that account,
   court and time. Tell the filer the outcome before anyone submits again.
   Operators can list them without filer details with
   `manage.py unconfirmed_submissions`.
7. Confirm backup/provider disposition separately before resolving the request.
   Deleting LITEfile's active copies does not withdraw court filings or delete Tyler
   accounts/records. Permanent anonymous aggregates remain. Explain the actual
   remaining exceptions, backup expiry/isolation and provider work to the requester.

Account-support access does not authorize report export. Use synthetic cases for
training and screenshots. Complete supervised lookup, denied out-of-scope access
and escalation exercises before handling real requests; record the trainer/date
in the [maintenance register](../partners-courts/maintenance.md).

## Aggregate-reporting onboarding

1. Obtain Aggregate reporting grants only for the agreed jurisdictions. Analysts
   cannot look up accounts, process privacy requests or grant roles.
2. Read the [metrics dictionary](../partners-courts/reporting.md), then practice
   **Aggregate usage** on a synthetic period: select jurisdiction, dates, daily
   core or full-calendar-month detail, and an approved breakdown.
3. Read coverage start, freshness and pending work before interpreting totals.
   Review suppressed/unknown cells, account contributors and observed court outcomes.
   Missing or suppressed results are not zero activity.
4. Export CSV only if enabled and the reporting/disclosure policy permits it.
   Dashboard and CSV use the same suppression rules. Do not reconstruct hidden
   cells, join reports to identifiable case records, or present account counts as
   unique people. There is no user-level analytics export.
5. Record the period, collection boundaries, thresholds, freshness and limitations
   with the report. Obtain the designated review before external sharing.

## Operator and policy gates

Collection (`LITEFILE_ANALYTICS_ENABLED`), export
(`LITEFILE_ANALYTICS_EXPORT_ENABLED`) and deletion
(`LITEFILE_PRIVACY_DELETION_ENABLED`) default to false. Record explicit service/policy
approval for each switch, jurisdiction scope, disclosure thresholds, retention
exceptions, verification, backup/provider disposition and approved report dimensions.
Collection approval does not authorize exports or deletion.

Finish the web/worker rollout before enabling deletion: old instances do not
enforce all freeze checks. Test storage IAM, versioned/shared objects and interrupted
cleanup with staging fixtures; keep manifests after failures. Anonymous aggregates
are retained indefinitely; backups/provider copies need their own policies, and
restores must reapply deletion dispositions before returning to service. The
application deleting a row does not prove every copy has been erased.

Use the runbook for bootstrap/reset commands, local synthetic seeding, workers,
retention and restore details. Record enablement evidence, assignments and the next
review here through the register; this documentation does not approve those policies.

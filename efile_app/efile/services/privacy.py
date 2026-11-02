"""Verified, scoped, resumable deletion of live LITEfile data."""

from typing import cast

from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from efile.models import (
    DocumentExtraction,
    DocumentPreparationJob,
    FilingDocument,
    FilingDraft,
    PendingActivation,
    PrivacyRequest,
    StaffAudit,
    StaffRoleGrant,
    StoredUpload,
    UserProfile,
)
from efile.services.analytics import drain_events
from efile.staff_security import require_scope
from efile.utils.s3_upload_handler import S3UploadHandler


def audit(operator, jurisdiction, action, outcome, *, reference=None, counts=None):
    StaffAudit.objects.create(
        operator=operator,
        jurisdiction=jurisdiction,
        action=action,
        outcome=outcome,
        reference=reference,
        counts=counts or {},
    )


def account_sessions(user, draft_ids=None):
    """Decode privately for matching; never send session payloads to staff/logs."""
    ids = {str(value) for value in draft_ids} if draft_ids is not None else None
    matches = []
    for session in Session.objects.all().iterator():
        data = SessionStore().decode(cast(Session, session).session_data)
        if str(data.get("_auth_user_id", "")) != str(user.pk):
            continue
        if ids is not None and not any(
            str(data.get(key, "")) in ids for key in ("filing_draft_id", "last_submitted_filing_draft_id")
        ):
            # Legacy sessions have complete case/upload copies with no draft ID;
            # revoke them too when removing one of this account's filings.
            if not ("case_data" in data or "upload_data" in data):
                continue
        matches.append(session)
    return matches


def selected_drafts(request):
    if request.target_id is None:
        return FilingDraft.objects.none()
    drafts = FilingDraft.objects.filter(user_id=request.target_id, jurisdiction=request.jurisdiction)
    return drafts if request.account_wide else drafts.filter(pk__in=request.draft_ids)


def clear_staff_lookup_cache(user, draft_ids, account_wide):
    """Remove temporary staff search locators while preserving their login sessions."""
    draft_ids = {str(value) for value in draft_ids}
    emails = {value.casefold() for value in (user.email, user.account_email) if value}
    store = SessionStore()
    for session in Session.objects.all().iterator():
        data = store.decode(cast(Session, session).session_data)
        lookup = data.get("staff_lookup")
        if not isinstance(lookup, dict) or lookup.get("jurisdiction") != user.tyler_jurisdiction:
            continue
        matches = str(lookup.get("draft_id")) in draft_ids
        if account_wide:
            matches = (
                matches
                or str(lookup.get("account_id")) == str(user.pk)
                or str(lookup.get("email", "")).casefold() in emails
            )
        if matches:
            data.pop("staff_lookup")
            Session.objects.filter(pk=cast(Session, session).pk).update(session_data=store.encode(data))


def preview(request):
    user = request.target
    if user is None:
        return {"counts": {}, "blockers": ["account_missing"], "keys": [], "draft_ids": []}
    drafts = selected_drafts(request)
    ids = list(drafts.values_list("pk", flat=True))
    documents = FilingDocument.objects.filter(draft_id__in=ids)
    keys = sorted({key for pair in documents.values_list("s3_key", "original_s3_key") for key in pair if key})
    keys = set(keys) | set(StoredUpload.objects.filter(draft_id__in=ids).values_list("key", flat=True))
    for draft in drafts:
        for document in (draft.submission_snapshot or {}).get("documents", []):
            if isinstance(document, dict):
                keys.update(
                    key
                    for key in (document.get("s3_key"), document.get("original_s3_key"))
                    if isinstance(key, str) and key
                )
    keys = sorted(keys)
    blockers = []
    if user.is_staff or user.is_superuser:
        blockers.append("staff_account")
    if user.tyler_jurisdiction != request.jurisdiction:
        blockers.append("jurisdiction_mismatch")
    if not request.account_wide and set(ids) != set(request.draft_ids):
        blockers.append("scope_changed")
    if request.account_wide and any(
        relation.exclude(jurisdiction=request.jurisdiction).exists()
        for relation in (user.filing_drafts, user.filing_plans, user.archived_cases)
    ):
        blockers.append("cross_jurisdiction_data")
    if drafts.filter(status__in=["submitting", "error"]).exists():
        blockers.append("submission_requires_reconciliation")
    if DocumentExtraction.objects.filter(document__draft_id__in=ids, status="processing").exists():
        blockers.append("extraction_worker_in_flight")
    if DocumentPreparationJob.objects.filter(draft_id__in=ids, lease_expires_at__gt=timezone.now()).exists():
        blockers.append("preparation_storage_in_flight")
    if FilingDraft.objects.filter(correction_of_id__in=ids).exclude(pk__in=ids).exists():
        blockers.append("corrections_outside_scope")
    shared = FilingDocument.objects.filter(Q(s3_key__in=keys) | Q(original_s3_key__in=keys)).exclude(draft_id__in=ids)
    shared_keys = {key for pair in shared.values_list("s3_key", "original_s3_key") for key in pair if key in keys}
    shared_keys.update(
        StoredUpload.objects.filter(key__in=keys).exclude(draft_id__in=ids).values_list("key", flat=True)
    )
    if shared_keys:
        blockers.append("shared_objects")
    counts = {
        "accounts": int(request.account_wide),
        "drafts": len(ids),
        "documents": documents.count(),
        "parties": sum(draft.parties.count() for draft in drafts),
        "extractions": DocumentExtraction.objects.filter(document__draft_id__in=ids).count(),
        "metadata_events": sum(draft.metadata_events.count() for draft in drafts),
        "handoffs": sum(int(hasattr(draft, "handoff")) for draft in drafts),
        "plans": user.filing_plans.count() if request.account_wide else 0,
        "archived_cases": user.archived_cases.count() if request.account_wide else 0,
        "objects": len(keys),
        "shared_objects": len(shared_keys),
        "sessions": len(account_sessions(user, None if request.account_wide else ids)),
    }
    return {"counts": counts, "blockers": blockers, "keys": keys, "draft_ids": ids}


@transaction.atomic
def create_request(operator, target, *, account_wide, draft_ids=()):
    require_scope(operator, target.tyler_jurisdiction, StaffRoleGrant.Role.ACCOUNTS)
    if target.is_staff or target.is_superuser:
        raise ValueError("Staff accounts cannot enter the litigant deletion workflow.")
    UserProfile.objects.select_for_update(no_key=True).get(pk=target.pk)
    ids = [] if account_wide else sorted(set(draft_ids))
    if not account_wide and (
        not ids or target.filing_drafts.filter(pk__in=ids, jurisdiction=target.tyler_jurisdiction).count() != len(ids)
    ):
        raise ValueError("Select only drafts owned by this account in this jurisdiction.")
    if PrivacyRequest.objects.filter(target=target).exclude(status="completed").exists():
        raise ValueError("This account already has an open request.")
    request = PrivacyRequest.objects.create(
        target=target,
        operator=operator,
        jurisdiction=target.tyler_jurisdiction,
        account_wide=account_wide,
        draft_ids=ids,
    )
    audit(operator, request.jurisdiction, "request_received", "received", reference=request.reference)
    return request


@transaction.atomic
def verify_request(request_id, operator):
    request = PrivacyRequest.objects.select_for_update().get(pk=request_id)
    require_scope(operator, request.jurisdiction, StaffRoleGrant.Role.ACCOUNTS)
    if request.status != "received":
        raise ValueError("Only a received request can be verified.")
    request.status = PrivacyRequest.Status.VERIFIED
    request.verified_at = timezone.now()
    request.operator = operator
    request.save()
    audit(operator, request.jurisdiction, "identity_verified", "verified", reference=request.reference)


def process_request(request_id, operator, *, handler=None, expected=None):
    """Hold database locks through cleanup; S3 progress commits separately.

    The durable manifest is created before touching S3. Each object completion is
    saved independently, so a database rollback never pretends to restore S3.
    A failed request keeps frozen drafts and its manifest until a successful retry.
    """
    if not settings.LITEFILE_PRIVACY_DELETION_ENABLED:
        raise ValueError("Deletion is disabled pending operator policy configuration.")
    with transaction.atomic():
        request = PrivacyRequest.objects.select_for_update().get(pk=request_id)
        require_scope(operator, request.jurisdiction, StaffRoleGrant.Role.ACCOUNTS)
        if request.status == "completed":
            return request
        if request.verified_at is None:
            raise ValueError("Verify identity and authority before processing.")
        # Lock the owner before drafts: new draft creation also takes this lock.
        # PostgreSQL NO KEY UPDATE permits analytics/submission FK key-share
        # checks while serializing owner workflows, avoiding lock inversion.
        UserProfile.objects.select_for_update(no_key=True).get(pk=request.target_id)
        list(selected_drafts(request).select_for_update())
        plan = preview(request)
        if expected is not None and any(expected[key] != plan[key] for key in ("counts", "draft_ids")):
            raise ValueError("The deletion scope changed. Review a fresh preview before confirming.")
        if plan["blockers"]:
            request.status = PrivacyRequest.Status.ATTENTION
            request.outcome = plan["blockers"][0]
            request.save()
            audit(
                operator,
                request.jurisdiction,
                "deletion_deferred",
                request.outcome,
                reference=request.reference,
                counts=plan["counts"],
            )
            return request
        if request.status != "processing" and not request.object_keys:
            request.object_keys = plan["keys"]
            request.draft_ids = plan["draft_ids"]
            request.counts = plan["counts"]
        request.status = PrivacyRequest.Status.PROCESSING
        request.outcome = ""
        request.operator = operator
        request.save()
        selected_drafts(request).update(deletion_pending=True)
        # Live sessions are revoked at freeze time, including failed attempts.
        Session.objects.filter(
            pk__in=[s.pk for s in account_sessions(request.target, None if request.account_wide else request.draft_ids)]
        ).delete()

    handler = handler or S3UploadHandler()
    # Serialize workers on the request. Object progress is durable outside the
    # final database deletion transaction; failed keys remain in the manifest.
    for key in request.object_keys:
        with transaction.atomic():
            request = PrivacyRequest.objects.select_for_update().get(pk=request_id)
            if request.status == "completed":
                return request
            if key in request.deleted_keys:
                continue
            if (
                FilingDocument.objects.filter(Q(s3_key=key) | Q(original_s3_key=key))
                .exclude(draft_id__in=request.draft_ids)
                .exists()
            ) or StoredUpload.objects.filter(key=key).exclude(draft_id__in=request.draft_ids).exists():
                result = {"success": False, "outcome": "shared_objects"}
            else:
                try:
                    result = handler.erase_file(key)
                except Exception:
                    result = {"success": False}
            if not result.get("success"):
                request.status = PrivacyRequest.Status.ATTENTION
                request.outcome = result.get("outcome", "storage_failed")
                request.save()
                audit(
                    operator,
                    request.jurisdiction,
                    "deletion_failed",
                    request.outcome,
                    reference=request.reference,
                    counts=request.counts,
                )
                return request
            request.deleted_keys = [*request.deleted_keys, key]
            request.save()

    with transaction.atomic():
        request = PrivacyRequest.objects.select_for_update().get(pk=request_id)
        if request.status == "completed":
            return request
        user = UserProfile.objects.select_for_update(no_key=True).get(pk=request.target_id)
        drafts = selected_drafts(request)
        list(drafts.select_for_update())
        # Drain eligible outbox events before their operational links disappear.
        drain_events(request.draft_ids, limit=1_000_000)
        clear_staff_lookup_cache(user, request.draft_ids, request.account_wide)
        Session.objects.filter(
            pk__in=[s.pk for s in account_sessions(user, None if request.account_wide else request.draft_ids)]
        ).delete()
        if request.account_wide:
            PendingActivation.objects.filter(jurisdiction=request.jurisdiction).filter(
                Q(email__iexact=user.email) | Q(email__iexact=user.account_email)
            ).delete()
            user.delete()
        else:
            drafts.delete()
        # No target identifiers, storage keys, contact data, or payloads survive.
        request.target = None
        request.draft_ids = []
        request.object_keys = []
        request.deleted_keys = []
        request.status = PrivacyRequest.Status.COMPLETED
        request.outcome = "live_system_deleted"
        request.completed_at = timezone.now()
        request.save()
        audit(
            operator,
            request.jurisdiction,
            "deletion_completed",
            request.outcome,
            reference=request.reference,
            counts=request.counts,
        )
    return request

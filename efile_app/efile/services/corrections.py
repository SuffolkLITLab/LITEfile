"""Copy policy, source eligibility and shared correction readiness.

The durable draft remains authoritative. Court comments are display-only text.
"""

from urllib.parse import urlencode

import requests
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.urls import reverse

from efile.models import FilingDraft
from efile.services.draft_urls import draft_url
from efile.services.filings import describe_filing_detail, fetch_filing_detail
from efile.workflow import RETURN_TO_REVIEW, with_return_to

COPY_DRAFT_FIELDS = (
    "user",
    "plan",
    "jurisdiction",
    "workflow_version",
    "existing_case",
    "court_code",
    "court_name",
    "case_category_code",
    "case_category_name",
    "case_type_code",
    "case_type_name",
    "case_subtype_code",
    "case_subtype_name",
    "filing_type_code",
    "filing_type_name",
    "document_type_code",
    "document_type_name",
    "existing_case_snapshot",
    "previous_case_id",
    "docket_number",
    "case_title",
    "notice_email",
    "name_change_reason",
    "optional_services",
    "extracted_guesses",
    "filing_type_proposal",
    "ai_assistance_opted_out",
    "document_checklist_acknowledged",
    "filer_role",
    "amount_in_controversy",
    "supplemental_fields",
)


COPY_DOCUMENT_FIELDS = (
    "role",
    "sort_order",
    "name",
    "original_filename",
    "size",
    "content_type",
    "s3_key",
    "original_s3_key",
    "upload_has_form_fields",
    "preparation",
    "preparation_reviewed_at",
    "document_type_confirmed",
    "filing_type_code",
    "filing_type_name",
    "document_type_code",
    "document_type_name",
    "filing_component_code",
    "filing_component_name",
    "filing_requires_amount_in_controversy",
    "checklist_item_id",
    "courtesy_copy_email",
    "requested_optional_services",
)
COPY_PARTY_FIELDS = (
    "role",
    "sort_order",
    "party_type",
    "party_type_name",
    "external_party_id",
    "source",
    "source_case_id",
    "representation",
    "is_self",
    "is_filing_party",
    "party_side",
    "party_role_hint",
    "first_name",
    "middle_name",
    "last_name",
    "suffix",
    "organization_name",
    "email",
    "phone",
    "address_line_1",
    "address_line_2",
    "city",
    "state",
    "zip_code",
    "country",
)


def readiness_fingerprint(draft):
    from efile.services.handoff import fingerprint, full_snapshot

    return fingerprint(full_snapshot(draft))


def submission_url(draft):
    from efile.services.handoff import submitted_filing_ids

    ids = sorted(submitted_filing_ids(draft.submission_response))
    if ids:
        return reverse(
            "filing_detail",
            kwargs={
                "jurisdiction": draft.jurisdiction,
                "court_code": draft.court_code,
                "filing_id": ids[0],
            },
        )
    return draft_url(reverse("filing_confirmation", kwargs={"jurisdiction": draft.jurisdiction}), draft.pk)


def correction_destination(draft):
    if draft.status == FilingDraft.Status.SUBMITTED:
        return submission_url(draft)
    if draft.status == FilingDraft.Status.ABANDONED:
        return reverse("filing_statuses", kwargs={"jurisdiction": draft.jurisdiction})
    return draft_url(reverse("case_review", kwargs={"jurisdiction": draft.jurisdiction}), draft.pk)


def source_eligibility(request, draft, *, fetch=None, known=None):
    """Fresh, envelope-wide result used by details, copy and final submit."""
    from efile.services.handoff import submitted_filing_ids

    def denied(reason):
        return {"eligible": False, "reason": reason, "detail": {}}

    if (
        not request.user.is_authenticated
        or draft.user_id != request.user.pk
        or draft.status != FilingDraft.Status.SUBMITTED
        or draft.deletion_pending
    ):
        return denied("Only your locally saved, submitted filings can be copied.")
    ids = sorted(submitted_filing_ids(draft.submission_response))
    if not ids:
        return denied("No confirmed filing identifiers are available. Check the original filing status.")
    details = []
    try:
        for filing_id in ids:
            detail = (known or {}).get(filing_id)
            if detail is None:
                detail = describe_filing_detail(
                    (fetch or fetch_filing_detail)(
                        request,
                        draft.jurisdiction,
                        draft.court_code,
                        filing_id,
                    )
                )
            if (
                not detail
                or str(detail.get("filing_id")) != filing_id
                or detail.get("status", "").strip().lower() not in {"returned", "rejected"}
            ):
                return denied("The court must confirm every filing as returned or rejected before it can be copied.")
            details.append(detail)
    except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError):
        return denied("The court status is unavailable. Check the original filing status and try again later.")
    return {
        "eligible": True,
        "reason": "",
        "detail": {
            **details[0],
            "filings": details,
            "comments": [
                {**comment, "filing_id": item["filing_id"]} for item in details for comment in item.get("comments", [])
            ],
        },
    }


def metadata_issues(draft, *, empty_choices: set[str] | None = None):
    """Check saved codes in their current scope, distinguishing empty lists from outages."""
    from efile.services.efsp_payload import _EfspLookups

    lookups = _EfspLookups(budget=10)
    confirmed_empty: set[str] = empty_choices if empty_choices is not None else set()
    issues = []
    base = f"{settings.EFSP_URL}/jurisdictions/{draft.jurisdiction}/codes/courts/"

    def check(path, value, label, view, *, optional=False, params=None, requirement_path=None):
        url = base + path
        if params:
            url += "?" + urlencode(params)
        rows = lookups.get(url)
        if not isinstance(rows, list) or any(not isinstance(row, dict) or "code" not in row for row in rows):
            message = f"The court's current {label} choices are unavailable. Try again before submitting."
        elif str(value) in {str(row["code"]) for row in rows} or (optional and not rows and not value):
            if optional and not rows and not value and requirement_path:
                confirmed_empty.add(requirement_path)
            return
        else:
            message = f"Choose a current {label}."
        issues.append({"message": message, "view": view})

    court = draft.court_code
    timing = "Subsequent" if draft.existing_case == "existing" else "Initial"
    check("", court, "court", "extraction_review", params={"with_names": "true"})
    if not court:
        return issues
    check(
        f"{court}/categories",
        draft.case_category_code,
        "case category",
        "extraction_review",
        params={"timing": timing, "fileable_only": "true"},
    )
    if not draft.case_category_code:
        return issues
    check(
        f"{court}/case_types/",
        draft.case_type_code,
        "case type",
        "extraction_review",
        params={"category_id": draft.case_category_code, "timing": timing},
    )
    if not draft.case_type_code:
        return issues
    for doc in draft.documents.all():
        check(
            f"{court}/filing_types/",
            doc.filing_type_code,
            f"filing type for {doc.name}",
            "organize_documents",
            params={
                "initial": "false" if draft.existing_case == "existing" else "true",
                "category_id": draft.case_category_code,
                "type_id": draft.case_type_code,
            },
        )
        if not doc.filing_type_code:
            continue
        for field, endpoint, label in (
            ("document_type_code", "document_types", "document type"),
            ("filing_component_code", "filing_components", "filing component"),
        ):
            check(
                f"{court}/filing_types/{doc.filing_type_code}/{endpoint}",
                getattr(doc, field),
                f"{label} for {doc.name}",
                "organize_documents",
                optional=True,
                requirement_path=f"documents.{doc.pk}.{field}",
            )
        for service in doc.requested_optional_services:
            check(
                f"{court}/filing_types/{doc.filing_type_code}/optional_services",
                service.get("code", "") if isinstance(service, dict) else service,
                f"optional service for {doc.name}",
                "organize_documents",
            )
    for party in draft.parties.all():
        if party.source != "court" and (party.role != "filer" or party.is_self or party.is_filing_party):
            check(
                f"{court}/case_types/{draft.case_type_code}/party_types",
                party.party_type,
                f"party type for {party}",
                "parties",
                optional=True,
                requirement_path=f"parties.{party.pk}.party_type",
            )
    return issues


def storage_issues(draft):
    from efile.utils.s3_upload_handler import S3UploadHandler

    handler = S3UploadHandler()
    issues = []
    for doc in draft.documents.all():
        try:
            if not doc.s3_key or not handler._ensure_initialized() or handler.s3_client is None:
                raise ValueError("Unavailable storage")
            handler.s3_client.head_object(Bucket=handler.bucket_name, Key=doc.s3_key)
        except (BotoCoreError, ClientError, ValueError, RuntimeError):
            issues.append(
                {
                    "message": f"We could not access {doc.name}. Retry or replace this PDF before submitting.",
                    "view": "upload_documents",
                }
            )
    return issues


def correction_readiness(draft):
    """Same blockers on Review and submit; never clears answers to validate them."""
    if not draft.correction_of_id:
        return []
    from efile.services.appeals import appeal_answers_complete
    from efile.services.document_previews import require_document_previews
    from efile.services.handoff import issues_for

    issues = issues_for(draft, current_metadata=True)
    if draft.status != FilingDraft.Status.DRAFT:
        issues.append(
            {
                "message": "This submission cannot be sent again automatically. Check its status first.",
                "view": "case_review",
            }
        )
    empty_choices = set()
    metadata = metadata_issues(draft, empty_choices=empty_choices)
    issues = [issue for issue in issues if issue.get("path") not in empty_choices]
    known_paths = {issue.get("path") for issue in issues} | empty_choices
    from efile.services.handoff import documents_replaced

    child_ids = {
        "documents": {str(pk) for pk in draft.documents.values_list("pk", flat=True)},
        "parties": {str(pk) for pk in draft.parties.values_list("pk", flat=True)},
    }
    for path in draft.correction_fields:
        parts = path.split(".")
        # A removed row no longer has values to correct. Ordinary readiness still
        # requires a main document and the necessary people on the remaining draft.
        if len(parts) >= 3 and parts[0] in child_ids and parts[1] not in child_ids[parts[0]]:
            continue
        if path not in known_paths and not (path == "documents" and documents_replaced(draft)):
            issues.append(
                {
                    "message": "Complete the saved correction requirement: " + path,
                    "view": "organize_documents" if path.startswith("documents") else "extraction_review",
                }
            )
    issues.extend(metadata)
    issues.extend(storage_issues(draft))
    if not appeal_answers_complete(draft):
        issues.append({"message": "Complete the lower court information.", "view": "case_questions"})
    try:
        require_document_previews(draft)
    except ValueError as error:
        issues.append({"message": str(error), "view": "extraction_review"})
    for issue in issues:
        issue["section"] = {
            "extraction_review": "case",
            "case_lookup": "case",
            "case_confirmation": "case",
            "organize_documents": "documents",
            "upload_documents": "documents",
            "your_information": "filer",
            "parties": "parties",
            "case_questions": "questions",
            "payment": "payment",
        }.get(issue["view"], "case")
        if issue["message"] == "Review your PDFs before you submit.":
            issue["section"] = "documents"
        issue["url"] = draft_url(
            with_return_to(
                reverse(
                    issue["view"],
                    kwargs={
                        "jurisdiction": draft.jurisdiction,
                    },
                ),
                RETURN_TO_REVIEW,
            ),
            draft.pk,
        )
    return issues


def refresh_correction_documents(payload, draft):
    """Use the reviewed durable bytes and metadata, never a stale browser URL."""
    from efile.services.efsp_payload import bind_filing_documents

    payload.update(
        efile_case_category=draft.case_category_code,
        efile_case_type=draft.case_type_code,
        efile_case_subtype=draft.case_subtype_code,
        previous_case_id=draft.previous_case_id,
        user_started_case=not bool(draft.previous_case_id),
    )
    if draft.amount_in_controversy:
        payload["amount_in_controversy"] = draft.amount_in_controversy
    else:
        payload.pop("amount_in_controversy", None)
    if draft.previous_case_id:
        payload["docket_number"] = draft.docket_number
    else:
        payload.pop("docket_number", None)
    bind_filing_documents(payload, draft)

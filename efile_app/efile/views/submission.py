import json
import logging
import uuid

from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from efile.api.suffolk_api_views import get_tyler_token
from efile.models import FilingDraft
from efile.services.court_payments import payment_error, validate_filing_payment
from efile.services.current_drafts import clear_current_draft, get_current_draft
from efile.services.disclaimers import validate_acceptance
from efile.services.document_previews import require_document_previews
from efile.services.fee_quotes import fee_fingerprint, fee_quote_is_usable
from efile.services.filing_availability import draft_unavailable_message
from efile.services.filing_plans import mark_attached_items_filed
from efile.services.submission_errors import PRE_SUBMIT_ERROR_CODES, SubmissionErrorCode
from efile.utils.efsp_security import require_efsp_login

from .confirmation import LAST_SUBMITTED_DRAFT_SESSION_KEY
from .session_api import forward_final_filing

logger = logging.getLogger(__name__)

# Only a clean DRAFT may be submitted. A draft is deliberately never auto-recovered
# out of SUBMITTING or ERROR: without an idempotency key on the external filing API,
# a retry after an ambiguous outcome could file a second time. Those states require
# manual review instead.
_CLAIMABLE_STATUSES = (FilingDraft.Status.DRAFT,)


@transaction.atomic
def _claim_for_submission(
    draft: FilingDraft, acceptance: dict, *, checked_fingerprint=None, checked_correction=None
) -> bool:
    """Atomically move a DRAFT into SUBMITTING, recording the accepted court requirements.

    Only one request can win this transition, so concurrent double-clicks cannot
    each forward to the external API. A draft already SUBMITTING or ERROR is not
    reclaimed here -- those are not safe to retry automatically.
    """
    locked = FilingDraft.objects.select_for_update().get(pk=draft.pk)
    if locked.status not in _CLAIMABLE_STATUSES:
        return False
    if checked_fingerprint is not None and fee_fingerprint(locked) != checked_fingerprint:
        raise ValueError("This filing changed while we checked your payment. Reload Review before submitting.")
    if checked_correction is not None:
        from efile.services.corrections import readiness_fingerprint

        if readiness_fingerprint(locked) != checked_correction:
            raise ValueError("This correction changed while we checked it. Reload Review before submitting.")
    if message := draft_unavailable_message(locked):
        raise ValueError(message)
    require_document_previews(locked)
    claimed = FilingDraft.objects.filter(pk=draft.pk, status__in=_CLAIMABLE_STATUSES).update(
        status=FilingDraft.Status.SUBMITTING,
        disclaimer_acceptance=acceptance,
        updated_at=timezone.now(),
        submission_operation=uuid.uuid4(),
    )
    if claimed:
        from efile.services.analytics import record_event

        draft.refresh_from_db(fields=["submission_operation"])
        record_event(draft, "submission_attempt", operation=draft.submission_operation)
        draft.status = FilingDraft.Status.SUBMITTING
        draft.disclaimer_acceptance = acceptance
    return bool(claimed)


def _release_claim(draft: FilingDraft) -> None:
    """Return a claimed draft to DRAFT when nothing was filed (pre-call or rejection)."""
    FilingDraft.objects.filter(pk=draft.pk, status=FilingDraft.Status.SUBMITTING).update(
        status=FilingDraft.Status.DRAFT,
        updated_at=timezone.now(),
    )
    draft.status = FilingDraft.Status.DRAFT


def _json_payload(response: JsonResponse) -> dict:
    try:
        return json.loads(response.content.decode(response.charset or "utf-8"))
    except (AttributeError, UnicodeDecodeError, ValueError):
        return {}


def _failed_before_external_call(payload: dict) -> bool:
    """True only when the submit view rejected the request before calling the API."""
    if "api_status_code" in payload:
        return False
    return payload.get("error_code") in PRE_SUBMIT_ERROR_CODES


def _confirmed_api_rejection(payload: dict) -> bool:
    """True when the filing API answered with a 4xx: the filing was definitely not accepted."""
    code = payload.get("api_status_code")
    # A timeout is ambiguous, and a conflict can mean the filing already exists.
    return isinstance(code, int) and 400 <= code < 500 and code not in (408, 409)


@require_http_methods(["POST"])
@require_efsp_login(session_jurisdiction=True)
def submit_final_filing(request):
    """Submit through the session path, guarding against duplicate external filings."""

    jurisdiction = request.session.get("jurisdiction")
    draft = get_current_draft(request, jurisdiction=jurisdiction, resume_latest=False)

    if draft is None:
        return JsonResponse(
            {"success": False, "error": "Open your filing and review it before submitting."}, status=400
        )

    try:
        submission_data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        submission_data = None
    if not isinstance(submission_data, dict):
        return JsonResponse(
            {"success": False, "error": "We could not read your submission. Reload the review page and try again."},
            status=400,
        )

    if message := draft_unavailable_message(draft):
        return JsonResponse(
            {"success": False, "error_code": SubmissionErrorCode.FILING_UNAVAILABLE, "error": message}, status=403
        )

    try:
        require_document_previews(draft)
    except ValueError as error:
        return JsonResponse({"success": False, "error": str(error)}, status=412)

    checked_correction = None
    if draft.correction_of_id:
        from efile.services.corrections import (
            correction_readiness,
            readiness_fingerprint,
            source_eligibility,
            submission_url,
        )

        checked_correction = readiness_fingerprint(draft)

        issues = correction_readiness(draft)
        if issues:
            return JsonResponse(
                {
                    "success": False,
                    "error": "Complete the correction requirements before submitting.",
                    "issues": issues,
                    "error_code": "correction_not_ready",
                    "error_actions": [{**issue, "label": "Review this section"} for issue in issues],
                },
                status=412,
            )
        eligibility = source_eligibility(request, draft.correction_of)
        if not eligibility["eligible"]:
            return JsonResponse(
                {
                    "success": False,
                    "error": eligibility["reason"],
                    "error_actions": [
                        {"label": "Check the original filing status", "url": submission_url(draft.correction_of)}
                    ],
                },
                status=409,
            )

    # The filer agreed to a total on Review. If anything that prices the filing
    # changed since, that total is not the one they would be charged, so the
    # page has to show the new one before anything reaches the court.
    if not fee_quote_is_usable(draft):
        return JsonResponse(
            {
                "success": False,
                "error_code": SubmissionErrorCode.FEE_QUOTE_STALE,
                "error": "The court's fees for this filing changed or could not be confirmed. "
                "Check the new total on this page before you submit.",
            },
            status=412,
        )

    try:
        acceptance = validate_acceptance(draft, submission_data)
    except ValueError as error:
        return JsonResponse({"success": False, "error": str(error)}, status=400)

    checked_fingerprint = fee_fingerprint(draft)
    try:
        validate_filing_payment(
            draft.jurisdiction,
            draft.court_code,
            get_tyler_token(request, draft.jurisdiction),
            draft.selected_payment_account_id,
        )
    except ValueError as error:
        return JsonResponse(payment_error(draft, error), status=412)
    if isinstance(submission_data.get("efile_data"), dict):
        submission_data["efile_data"]["tyler_payment_id"] = draft.selected_payment_account_id

    # Claim the draft before forwarding so a concurrent request can't file twice.
    try:
        claimed = _claim_for_submission(
            draft, acceptance, checked_fingerprint=checked_fingerprint, checked_correction=checked_correction
        )
    except ValueError as error:
        return JsonResponse({"success": False, "error": str(error)}, status=412)
    if not claimed:
        return JsonResponse(
            {"success": False, "error": "This filing can't be submitted again automatically."},
            status=409,
        )

    response = forward_final_filing(request, submission_data)
    payload = _json_payload(response)

    if response.status_code < 400 and payload.get("success") is True:
        with transaction.atomic():
            draft.mark_submitted(payload.get("api_response") or {})
            mark_attached_items_filed(draft)
        request.session[LAST_SUBMITTED_DRAFT_SESSION_KEY] = draft.pk
        request.session.modified = True
        clear_current_draft(request)
    elif _failed_before_external_call(payload) or _confirmed_api_rejection(payload):
        from efile.services.analytics import record_event

        record_event(draft, "submission_error", operation=draft.submission_operation)
        # Nothing was filed (rejected before the call, or the API refused it),
        # so it is safe to return the draft to DRAFT for a corrected retry.
        _release_claim(draft)
    else:
        # Ambiguous: a network error or an error after requests.post may mean the
        # filing went through. Leave it ERROR for manual review -- ERROR is not
        # claimable, so a click cannot silently re-file.
        draft.mark_error({"status_code": response.status_code, "response": payload})

    return response

"""Check a newly prepared copy where it was added, instead of on another screen.

A file added after the preview step (a fee waiver on Fees, a missing document
on the checklist) is shown open on that same page. The filer confirms the
copy the court will get, or removes it, without losing their place.
"""

from urllib.parse import urlencode

from django.db import transaction
from django.http import JsonResponse
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from efile.api.suffolk_api_views import get_tyler_token
from efile.models import FilingDocument, FilingDraft
from efile.services.current_drafts import explicit_draft_id, get_current_draft
from efile.services.document_choices import filing_choices, save_document_type
from efile.services.document_preparation import cleanup_unreferenced_uploads
from efile.services.document_previews import document_storage_keys, preview_fingerprint
from efile.services.drafts import ACTIVE_DRAFT_STATUSES
from efile.services.fee_quotes import fee_inputs_token, invalidate_fee_quote
from efile.utils.config_loader import config_loader
from efile.workflow import ExistingCase


def is_removable(document):
    """Supporting files can go from here. The main document is changed on the
    upload step, where a replacement is chosen and read again."""
    return document.role == FilingDocument.Role.SUPPORTING


def document_check(document):
    draft = document.draft
    if draft.existing_case == ExistingCase.EXISTING:
        url = reverse("case_filing_types", kwargs={"jurisdiction": draft.jurisdiction}) + f"?draft={draft.pk}"
    else:
        url = "/api/dropdowns/filing-types/?" + urlencode(
            {
                "jurisdiction": draft.jurisdiction,
                "court": draft.court_code,
                "case_category": draft.case_category_code,
                "case_type": draft.case_type_code,
                "existing_case": "no",
            }
        )
    return {
        "document_choices_url": url if draft.case_type_code else "",
        "upload_disclaimers": config_loader.get_upload_disclaimers(draft.jurisdiction),
        "document": document,
        "fingerprint": preview_fingerprint([document]),
        "removable": is_removable(document),
    }


def unchecked_documents(documents):
    """Template context for each document whose copy has not been confirmed."""
    return [document_check(document) for document in documents if document.preparation_reviewed_at is None]


def document_check_html(request, document):
    return render_to_string(
        "efile/components/document_check.html",
        {**document_check(document), "jurisdiction": document.draft.jurisdiction},
        request=request,
    )


@require_http_methods(["POST"])
def document_checks(request, jurisdiction):
    if not request.user.is_authenticated or not get_tyler_token(request, jurisdiction):
        return JsonResponse({"error": "Sign in again to continue."}, status=401)
    if explicit_draft_id(request) is None:
        return JsonResponse({"error": "Reload this page before checking a document."}, status=409)
    draft = get_current_draft(request, jurisdiction=jurisdiction)
    action = request.POST.get("action", "")
    if draft is None or action not in ("confirm", "remove"):
        return JsonResponse({"error": "This filing is not available to edit."}, status=409)
    with transaction.atomic():
        draft = FilingDraft.objects.select_for_update().get(pk=draft.pk)
        if draft.status not in ACTIVE_DRAFT_STATUSES:
            return JsonResponse({"error": "This filing is not available to edit."}, status=409)
        inputs_token = request.POST.get("fee_inputs_token")
        if inputs_token is not None and inputs_token != fee_inputs_token(draft):
            # Payment will adopt the token returned below while keeping its
            # page's case data. Only advance it from inputs that still match.
            return JsonResponse(
                {"error": "This filing changed. Reload this page before checking documents or calculating fees."},
                status=409,
            )
        document = draft.documents.filter(pk=request.POST.get("document_id") or None).first()
        if document is None:
            return JsonResponse(
                {"error": "This document is no longer part of your filing. Reload this page."}, status=409
            )
        if action == "confirm":
            if not document.preparation:
                return JsonResponse({"error": "This file is not ready. Remove it and upload it again."}, status=409)
            if request.POST.get("preview_fingerprint") != preview_fingerprint([document]):
                return JsonResponse({"error": "This file changed. Reload this page and check it again."}, status=409)
            code = request.POST.get("filing_type")
            if code is not None:
                try:
                    options = {item["value"]: item for item in filing_choices(draft)}
                    if code not in options:
                        raise ValueError("Choose a type of document offered for this case.")
                    option = options[code]
                    save_document_type(
                        document,
                        code,
                        option["text"],
                        requires_amount=str(option.get("amountincontroversy", "")).lower() == "required",
                    )
                    invalidate_fee_quote(draft)
                except ValueError as exc:
                    return JsonResponse({"error": str(exc)}, status=400)
            document.preparation_reviewed_at = timezone.now()
            document.save(update_fields=["preparation_reviewed_at", "updated_at"])
            return JsonResponse({"success": True, "fee_inputs_token": fee_inputs_token(draft)})
        if not is_removable(document):
            return JsonResponse({"error": "Change your main document from the upload step."}, status=400)
        keys = document_storage_keys(document)
        document.delete()
        # Fewer documents can mean different fees. The page gets the new
        # inputs token so a quote it asks for next describes this filing.
        invalidate_fee_quote(draft)
        transaction.on_commit(lambda: cleanup_unreferenced_uploads(keys))
        return JsonResponse({"success": True, "fee_inputs_token": fee_inputs_token(draft)})

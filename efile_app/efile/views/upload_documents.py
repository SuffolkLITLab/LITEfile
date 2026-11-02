import logging
import math

from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from efile.api.suffolk_api_views import get_tyler_token
from efile.models import DocumentExtraction, FilingDocument, FilingDraft
from efile.services.current_drafts import ensure_current_draft, get_current_draft
from efile.services.document_extractions import (
    EXTRACTION_WAIT_LIMIT,
    extraction_for_document,
    extraction_is_waiting,
    queue_document_extraction,
)
from efile.services.document_finder import has_document_finder
from efile.services.document_preparation import requires_flattening
from efile.services.document_previews import document_storage_keys
from efile.services.document_uploads import upload_files
from efile.services.drafts import draft_snapshot, read_upload_data
from efile.utils.config_loader import config_loader
from efile.utils.s3_upload_handler import S3UploadHandler
from efile.workflow import (
    ExistingCase,
    WorkflowStepKey,
    get_step_url,
    get_workflow_context,
    return_target,
    with_return_to,
)

logger = logging.getLogger(__name__)

#: What the opt-out checkbox sends when it is checked. An unchecked box sends
#: nothing at all, which is why the absence of the field means "AI allowed".
_CHECKED_VALUES = frozenset({"yes", "true", "on", "1"})


#: What the "remember this" checkbox sends when it is cleared. It is sent
#: explicitly, rather than simply left out, so that clearing it is told apart
#: from a request that never offered the choice at all.
_CLEARED_VALUES = frozenset({"no", "false", "off", "0"})


def _opted_out(request):
    return request.POST.get("ai_opt_out", "").strip().casefold() in _CHECKED_VALUES


def _remember_choice(request):
    """Whether to save this answer to the account: True, False, or None to leave it."""
    value = request.POST.get("remember_ai_choice", "").strip().casefold()
    if value in _CHECKED_VALUES:
        return True
    if value in _CLEARED_VALUES:
        return False
    return None


def _apply_remembered_choice(request, opted_out):
    """Carry this filing's answer onto the account, or stop carrying it.

    Clearing the box does not merely stop saving: it takes the standing
    preference back off the account, so a filer who changes their mind in the
    same breath is not left with a default they just rejected.
    """

    remember = _remember_choice(request)
    if remember is None:
        return False
    account_default = opted_out if remember else False
    if request.user.ai_assistance_opted_out != account_default:
        request.user.ai_assistance_opted_out = account_default
        request.user.save(update_fields=["ai_assistance_opted_out", "updated_at"])
    return remember


@require_http_methods(["GET", "POST"])
def upload_documents(request, jurisdiction):
    if not request.user.is_authenticated or not get_tyler_token(request, jurisdiction):
        return redirect("efile_login", jurisdiction=jurisdiction)

    draft = ensure_current_draft(
        request,
        jurisdiction,
        current_step=WorkflowStepKey.UPLOAD_DOCUMENTS,
        workflow_version=2,
    )

    if request.method == "POST":
        action = request.POST.get("action", "upload")
        if action == "ai_preference":
            # The filer changed their mind after uploading. Drop what AI read
            # from the document before re-reading it the way they now want it
            # read, so an answer from the old mode is never left on the screen.
            opted_out = _opted_out(request)
            remembered = _apply_remembered_choice(request, opted_out)
            with transaction.atomic():
                draft = FilingDraft.objects.select_for_update().get(pk=draft.pk)
                changed = draft.ai_assistance_opted_out != opted_out
                lead = FilingDocument.objects.filter(draft=draft, role=FilingDocument.Role.LEAD).first()
                if changed:
                    draft.ai_assistance_opted_out = opted_out
                    draft.extracted_guesses = {}
                    draft.save(update_fields=["ai_assistance_opted_out", "extracted_guesses", "updated_at"])
                    if lead is not None:
                        queue_document_extraction(lead)
            return JsonResponse(
                {
                    "success": True,
                    "ai_opted_out": opted_out,
                    "remembered": remembered,
                    "reanalyzing": changed and lead is not None,
                }
            )
        if action == "remove":
            document_id = request.POST.get("document_id")
            document = FilingDocument.objects.filter(pk=document_id, draft=draft).first()
            if document is None:
                return JsonResponse({"success": False, "error": "Document not found."}, status=404)
            removed_lead = document.role == FilingDocument.Role.LEAD
            storage_keys = document_storage_keys(document)
            promote_document = None
            other_documents = FilingDocument.objects.filter(draft=draft).exclude(pk=document.pk)
            if document.role == FilingDocument.Role.LEAD:
                replacement = other_documents.order_by("sort_order", "created_at").first()
                if replacement is not None:
                    promote_document = replacement.pk
            document.delete()
            if promote_document is not None:
                replacement = FilingDocument.objects.get(pk=promote_document)
                replacement.role = FilingDocument.Role.LEAD
                replacement.sort_order = 0
                replacement.save(update_fields=["role", "sort_order", "updated_at"])
                queue_document_extraction(replacement)
            if removed_lead and draft.extracted_guesses:
                draft.extracted_guesses = {}
                draft.save(update_fields=["extracted_guesses", "updated_at"])
            if storage_keys:
                handler = S3UploadHandler()
                if handler._ensure_initialized():
                    for key in storage_keys:
                        if FilingDocument.objects.filter(Q(s3_key=key) | Q(original_s3_key=key)).exists():
                            continue
                        deletion = handler.delete_file(key)
                        if not deletion.get("success"):
                            logger.warning("Could not delete removed draft document from storage")
            return JsonResponse({"success": True})

        uploaded_files = request.FILES.getlist("documents")
        if not uploaded_files:
            return JsonResponse(
                {"success": False, "error": "Choose at least one PDF or Word document to upload."}, status=400
            )
        # Saved before the upload, because uploading the lead queues the
        # analysis that this choice decides the shape of.
        opted_out = _opted_out(request)
        _apply_remembered_choice(request, opted_out)
        if draft.ai_assistance_opted_out != opted_out:
            draft.ai_assistance_opted_out = opted_out
            draft.save(update_fields=["ai_assistance_opted_out", "updated_at"])
        try:
            upload_data = upload_files(
                draft, uploaded_files, jurisdiction, replace_document_id=request.POST.get("replace_document_id")
            )
        except ValueError as error:
            logger.warning("Document upload failed")
            return JsonResponse({"success": False, "error": str(error)}, status=400)
        return JsonResponse(
            {
                "success": True,
                "redirect_url": with_return_to(
                    get_step_url(WorkflowStepKey.EXTRACTION_REVIEW, jurisdiction), return_target(request)
                ),
                "document_count": FilingDocument.objects.filter(draft=draft).count(),
                "extraction_pending": FilingDocument.objects.filter(
                    draft=draft,
                    role=FilingDocument.Role.LEAD,
                    extraction__status__in=[
                        DocumentExtraction.Status.PENDING,
                        DocumentExtraction.Status.PROCESSING,
                    ],
                ).exists(),
            }
        )

    upload_data = read_upload_data(draft)
    documents = list(FilingDocument.objects.filter(draft=draft).order_by("role", "sort_order", "created_at"))
    lead = next((document for document in documents if document.role == FilingDocument.Role.LEAD), None)
    extraction = extraction_for_document(lead) if lead else None
    context = {
        "saved_path": draft.existing_case if draft.existing_case in {ExistingCase.NEW, ExistingCase.EXISTING} else "",
        "is_logged_in": True,
        "filing_draft": draft_snapshot(draft),
        "documents": documents,
        "has_lead_document": lead is not None,
        "extraction": extraction,
        "extraction_pending": extraction is not None
        and extraction.status in {DocumentExtraction.Status.PENDING, DocumentExtraction.Status.PROCESSING},
        "extraction_wait_seconds": _wait_seconds(extraction),
        "flatten_pdf_forms": requires_flattening(jurisdiction),
        "upload_data": upload_data,
        "ai_opted_out": draft.ai_assistance_opted_out,
        "account_ai_opted_out": request.user.ai_assistance_opted_out,
        # Set when the filer came to change files from a later screen; the
        # way on goes back there instead of through every step again.
        "return_to": return_target(request),
    }
    context["upload_disclaimers"] = config_loader.get_upload_disclaimers(jurisdiction)
    context["has_document_finder"] = has_document_finder(jurisdiction)
    context.update(get_workflow_context(WorkflowStepKey.UPLOAD_DOCUMENTS, jurisdiction, draft))
    return render(request, "efile/upload_documents.html", context)


def _wait_seconds(extraction):
    """Seconds left before the filer may go on without the analysis."""
    if not extraction_is_waiting(extraction):
        return 0
    remaining = extraction.created_at + EXTRACTION_WAIT_LIMIT - timezone.now()
    return max(1, math.ceil(remaining.total_seconds()))


@require_http_methods(["GET"])
def document_extraction_status(request, jurisdiction):
    """Poll the current lead's durable background analysis state."""
    if not request.user.is_authenticated or not get_tyler_token(request, jurisdiction):
        return JsonResponse({"success": False, "error": "Sign in again to continue."}, status=401)

    draft = get_current_draft(request, jurisdiction=jurisdiction)
    if draft is None:
        return JsonResponse({"success": True, "status": "not_queued", "ready": False})
    lead = FilingDocument.objects.filter(draft=draft, role=FilingDocument.Role.LEAD).first()
    extraction = extraction_for_document(lead) if lead else None
    if extraction is None:
        return JsonResponse(
            {
                "success": True,
                "status": "not_queued",
                "ready": lead is not None,
                "ai_opted_out": draft.ai_assistance_opted_out,
            }
        )

    return JsonResponse(
        {
            "success": True,
            "status": extraction.status,
            "ai_opted_out": draft.ai_assistance_opted_out,
            "ready": extraction.status in {DocumentExtraction.Status.COMPLETE, DocumentExtraction.Status.FAILED},
            "wait_seconds": _wait_seconds(extraction),
            "pages_analyzed": extraction.pages_analyzed,
            "total_pages": extraction.total_pages,
            "review_url": with_return_to(
                get_step_url(WorkflowStepKey.EXTRACTION_REVIEW, jurisdiction), return_target(request)
            ),
        }
    )

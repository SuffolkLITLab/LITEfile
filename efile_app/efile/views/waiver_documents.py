from django.db import transaction
from django.db.models import Max
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from efile.api.suffolk_api_views import get_tyler_token
from efile.models import FilingDocument, FilingDraft
from efile.services.current_drafts import explicit_draft_id, get_current_draft
from efile.services.document_preparation import cleanup_uploads, store_prepared_document
from efile.services.drafts import ACTIVE_DRAFT_STATUSES
from efile.services.fee_quotes import fee_inputs_token, invalidate_fee_quote
from efile.services.waiver_documents import record_waiver_upload, waiver_document_choices, waiver_filing_types
from efile.utils.s3_upload_handler import S3UploadHandler
from efile.views.document_checks import document_check_html


@require_http_methods(["GET", "POST"])
def waiver_documents(request, jurisdiction):
    if not request.user.is_authenticated or not get_tyler_token(request, jurisdiction):
        return JsonResponse({"error": "Sign in again to continue."}, status=401)
    if explicit_draft_id(request) is None:
        return JsonResponse({"error": "Reload the payment page before adding a document."}, status=409)
    draft = get_current_draft(request, jurisdiction=jurisdiction)
    if draft is None or draft.status not in ACTIVE_DRAFT_STATUSES or not draft.court_code:
        return JsonResponse({"error": "This filing is not available to edit."}, status=409)
    keys = []
    handler = S3UploadHandler()
    try:
        options = waiver_filing_types(draft)
        data = request.POST if request.method == "POST" else request.GET
        code = data.get("filing_type", "") or (str(options[0]["code"]) if options else "")
        selected = next((item for item in options if str(item["code"]) == code), None)
        if selected is None:
            raise ValueError(
                "We could not find a fee waiver filing code for this court. Try again or contact the court."
            )
        types, component = waiver_document_choices(draft, code)
        if request.method == "GET":
            return JsonResponse({"filing_types": options, "selected": code, "document_types": types})
        document_type = next((item for item in types if str(item["code"]) == data.get("document_type")), None)
        if document_type is None:
            raise ValueError("Choose a confidentiality setting for this document.")
        files = request.FILES.getlist("document")
        if len(files) != 1:
            raise ValueError("Choose one PDF or Word document to upload.")
        file = files[0]
        validation = handler.validate_file(file, max_size_mb=10, allowed_types=[".pdf", ".doc", ".docx"])
        if not validation["valid"]:
            raise ValueError(validation["error"])
        if not handler._ensure_initialized():
            raise ValueError("Document storage is not available. Try again.")
        with transaction.atomic():
            draft = FilingDraft.objects.select_for_update().get(pk=draft.pk)
            if (
                draft.deletion_pending
                or draft.status not in ACTIVE_DRAFT_STATUSES
                or data.get("fee_inputs_token") != fee_inputs_token(draft)
            ):
                return JsonResponse(
                    {"error": "This filing changed. Reload this page before adding a document."}, status=409
                )
            if not draft.documents.filter(role=FilingDocument.Role.LEAD).exists():
                raise ValueError("Add your main document before adding a fee waiver.")
            prepared = store_prepared_document(handler, file, jurisdiction, FilingDocument.Role.SUPPORTING, keys=keys)
            highest = draft.documents.filter(role=FilingDocument.Role.SUPPORTING).aggregate(order=Max("sort_order"))[
                "order"
            ]
            document = FilingDocument.objects.create(
                draft=draft,
                role=FilingDocument.Role.SUPPORTING,
                sort_order=0 if highest is None else highest + 1,
                **prepared,
                filing_type_code=code,
                filing_type_name=selected["name"],
                filing_requires_amount_in_controversy=str(selected.get("amountincontroversy", "")).casefold()
                == "required",
                document_type_code=str(document_type["code"]),
                document_type_name=document_type["name"],
                filing_component_code=str(component["code"]),
                filing_component_name=component["name"],
            )
            record_waiver_upload(document)
            invalidate_fee_quote(draft)
            return JsonResponse(
                {
                    "success": True,
                    "fee_inputs_token": fee_inputs_token(draft),
                    "check_html": document_check_html(request, document),
                }
            )
    except ValueError as error:
        cleanup_uploads(handler, keys)
        return JsonResponse({"error": str(error)}, status=400)
    except Exception:
        cleanup_uploads(handler, keys)
        raise

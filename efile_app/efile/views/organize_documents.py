import json

import requests
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from efile.api.suffolk_api_views import get_tyler_token
from efile.models import FilingDocument, FilingDraft
from efile.services.case_filing_types import case_fingerprint, permitted_filing_types
from efile.services.current_drafts import ensure_current_draft
from efile.services.document_choices import validate_document_types, validate_services
from efile.services.document_extractions import queue_document_extraction
from efile.services.drafts import draft_snapshot
from efile.services.existing_cases import import_ready
from efile.services.filing_availability import draft_unavailable_message
from efile.utils.config_loader import config_loader
from efile.utils.ui_text import get_texts
from efile.workflow import (
    ExistingCase,
    WorkflowStepKey,
    clean_return_to,
    continue_step,
    continue_url,
    get_step_url,
    get_workflow_context,
    return_target,
    with_return_to,
)

# Strings the organize-documents script renders itself, handed to it through the
# page. Keyed in the JSON by their last path segment: `loading_choices`, and so on.
JS_TEXT_KEYS = [
    "organize_documents.choose_filing_type_first",
    "organize_documents.loading_choices",
    "organize_documents.choices_load_error",
    "organize_documents.retry_choices",
    "organize_documents.choices_still_loading",
    "organize_documents.choices_load_failed",
    "organize_documents.no_document_types",
    "organize_documents.no_filing_components",
    "organize_documents.filing_component_fixed_note",
    "organize_documents.filing_component_required_note",
]


def _document_id(value):
    """Read a document primary key off the wire, or None if there isn't one.

    ``None`` means "the client did not name one" -- including every shape that
    stands in for an unset numeric field, such as ``NaN``, ``null`` or ``""``.
    Callers fall back rather than treating an unreadable value as an error.
    """
    try:
        document_id = int(value)
    except (TypeError, ValueError):
        return None
    return document_id if document_id > 0 else None


def _court_document_types(draft, filing_type):
    """Return the document types the court offers for one filing type."""
    url = (
        f"{settings.EFSP_URL}/jurisdictions/{draft.jurisdiction}/codes/courts/"
        f"{draft.court_code}/filing_types/{filing_type}/document_types"
    )
    try:
        response = requests.get(url, headers={}, timeout=30)
        if response.status_code != 200:
            raise ValueError
        choices = response.json()
        if not isinstance(choices, list):
            raise ValueError
    except (requests.RequestException, ValueError) as error:
        raise ValueError("The court's confidentiality choices could not be checked. Try again.") from error
    return choices


def _check_case_unchanged(draft, confirmed_case_fingerprint):
    if confirmed_case_fingerprint != case_fingerprint(draft):
        raise ValueError("Your court case changed. Reload this page before continuing.")


def _save_document_details(draft, document_details, main_document_id, *, confirmed_case_fingerprint=None):
    # The court is asked for this case's filing types before the draft is
    # locked, so a slow response never holds the lock. The same check runs
    # again under the lock, so the list still belongs to the case being saved.
    permitted = None
    if confirmed_case_fingerprint is not None:
        _check_case_unchanged(draft, confirmed_case_fingerprint)
    if draft.existing_case == ExistingCase.EXISTING:
        _check_case_unchanged(draft, confirmed_case_fingerprint)
        permitted = {option["value"]: option for option in permitted_filing_types(draft)}
    if draft.existing_case != ExistingCase.EXISTING:
        document_details = validate_document_types(draft, document_details, list(draft.documents.all()))
        confirmed_case_fingerprint = case_fingerprint(draft)
    _save_locked_document_details(draft, document_details, main_document_id, permitted, confirmed_case_fingerprint)


@transaction.atomic
def _save_locked_document_details(draft, document_details, main_document_id, permitted, confirmed_case_fingerprint):
    draft = FilingDraft.objects.select_for_update().get(pk=draft.pk)
    if draft.existing_case == ExistingCase.EXISTING:
        if permitted is None:
            raise ValueError("Your court case changed. Reload this page before continuing.")
        _check_case_unchanged(draft, confirmed_case_fingerprint)
    else:
        _check_case_unchanged(draft, confirmed_case_fingerprint)
        permitted = None
    documents = {document.pk: document for document in FilingDocument.objects.select_for_update().filter(draft=draft)}
    if len(document_details) != len(documents) or {item.get("id") for item in document_details} != set(documents):
        raise ValueError("The document list changed. Refresh the page and try again.")
    if main_document_id not in documents:
        raise ValueError("Choose the document we should read for case suggestions.")

    # Move every document out of the final ranges before changing the main
    # document or order, so role/order swaps cannot trip the uniqueness rule.
    for document in documents.values():
        FilingDocument.objects.filter(pk=document.pk).update(sort_order=1_000_000 + document.pk)

    for document in documents.values():
        document.role = FilingDocument.Role.LEAD if document.pk == main_document_id else FilingDocument.Role.SUPPORTING

    previous_source = draft.documents.filter(role=FilingDocument.Role.LEAD).values_list("pk", flat=True).first()
    supporting_order = 0
    document_types_by_filing_type = {}
    for item in document_details:
        document = documents[item["id"]]
        filing_type = str(item.get("filing_type") or "").strip()
        document_type = str(item.get("document_type") or "").strip()
        if not filing_type:
            raise ValueError(f"Choose a type of document (filing code) for {document.name}.")
        if permitted is not None:
            if filing_type not in permitted:
                raise ValueError(
                    "This type of document is not available for your confirmed case. Choose another type of document."
                )
            item = {**item, "filing_type_name": permitted[filing_type]["text"]}
        if not document_type:
            if filing_type not in document_types_by_filing_type:
                document_types_by_filing_type[filing_type] = _court_document_types(draft, filing_type)
            if document_types_by_filing_type[filing_type]:
                raise ValueError(f"Choose a confidentiality setting for {document.name}.")

        document.name = str(item.get("name") or "").strip()[:255] or document.name
        document.filing_type_code = filing_type
        document.filing_type_name = str(item.get("filing_type_name") or "")[:255]
        document.document_type_code = document_type
        document.document_type_name = str(item.get("document_type_name") or "")[:255]
        document.document_type_confirmed = True
        document.filing_component_code = str(item.get("filing_component") or "")[:100]
        document.filing_component_name = str(item.get("filing_component_name") or "")[:255]
        courtesy_copy_email = str(item.get("courtesy_copy_email") or "").strip()[:254]
        if courtesy_copy_email:
            try:
                EmailValidator()(courtesy_copy_email)
            except ValidationError as error:
                raise ValueError(f"Enter a valid courtesy copy email address for {document.name}.") from error
        document.courtesy_copy_email = courtesy_copy_email
        optional_services = item.get("requested_optional_services")
        document.requested_optional_services = validate_services(draft, filing_type, optional_services or [])
        document.filing_requires_amount_in_controversy = bool(item.get("requires_amount_in_controversy"))
        if document.pk == main_document_id:
            document.sort_order = 0
        else:
            document.sort_order = supporting_order
            supporting_order += 1
        document.save()
    if previous_source != main_document_id:
        draft.extracted_guesses = {}
        draft.supplemental_fields = {**draft.supplemental_fields, "_suggestion_source_changed": True}
        draft.save(update_fields=["extracted_guesses", "supplemental_fields", "updated_at"])
        transaction.on_commit(lambda: queue_document_extraction(documents[main_document_id]))


@require_http_methods(["GET", "POST"])
def organize_documents(request, jurisdiction):
    if not request.user.is_authenticated or not get_tyler_token(request, jurisdiction):
        return redirect("efile_login", jurisdiction=jurisdiction)

    draft = ensure_current_draft(
        request,
        jurisdiction,
        current_step=WorkflowStepKey.ORGANIZE_DOCUMENTS,
        workflow_version=2,
    )
    documents = FilingDocument.objects.filter(draft=draft).order_by("role", "sort_order", "created_at")
    if not documents.exists():
        return redirect(
            with_return_to(get_step_url(WorkflowStepKey.UPLOAD_DOCUMENTS, jurisdiction), return_target(request))
        )
    if draft.existing_case == ExistingCase.EXISTING and not import_ready(draft):
        step = WorkflowStepKey.CASE_CONFIRMATION if draft.previous_case_id else WorkflowStepKey.CASE_LOOKUP
        return redirect(with_return_to(get_step_url(step, jurisdiction), return_target(request)))
    if not draft.court_code:
        # Filing types can't be looked up without a court. Send the filer back
        # to whichever step is responsible for setting one, instead of
        # stranding them here with no way to recover.
        fix_step = (
            WorkflowStepKey.CASE_LOOKUP
            if draft.existing_case == ExistingCase.EXISTING
            else WorkflowStepKey.EXTRACTION_REVIEW
        )
        messages.error(request, "Confirm the court for this filing before organizing your documents.")
        return redirect(with_return_to(get_step_url(fix_step, jurisdiction), return_target(request)))

    if request.method == "POST":
        previous_source_id = documents.filter(role=FilingDocument.Role.LEAD).values_list("pk", flat=True).first()
        try:
            data = json.loads(request.body)
            details = data.get("documents")
            if not isinstance(details, list):
                raise ValueError("Document details are missing.")
            main_document_id = _document_id(data.get("main_document_id"))
            if main_document_id is None:
                # The client could not name a main document: no radio rendered,
                # a stale script, or one of the sentinels a browser produces for
                # an empty numeric field. Anything unreadable is treated the
                # same, because the draft already knows which document leads.
                lead_doc = documents.filter(role=FilingDocument.Role.LEAD).first() or documents.first()
                if lead_doc is None:
                    raise ValueError("Choose the document we should read for case suggestions.")
                main_document_id = lead_doc.id
            _save_document_details(
                draft, details, main_document_id, confirmed_case_fingerprint=data.get("case_fingerprint")
            )
        except (json.JSONDecodeError, ValueError) as error:
            return JsonResponse({"success": False, "error": str(error)}, status=400)

        if message := draft_unavailable_message(draft):
            return JsonResponse({"success": False, "error": message}, status=403)

        return_to = clean_return_to(data.get("return_to"))
        if previous_source_id != main_document_id:
            messages.info(
                request,
                "We are reading the document you chose. Check the new suggestions below. We kept the answers you already saved.",
            )
            return JsonResponse(
                {
                    "success": True,
                    "redirect_url": with_return_to(
                        get_step_url(WorkflowStepKey.EXTRACTION_REVIEW, jurisdiction), return_to
                    ),
                }
            )
        draft.current_step = continue_step(draft, return_to, WorkflowStepKey.YOUR_INFORMATION)
        draft.save(update_fields=["current_step", "updated_at"])
        return JsonResponse(
            {
                "success": True,
                "redirect_url": continue_url(draft, jurisdiction, return_to, WorkflowStepKey.YOUR_INFORMATION),
            }
        )

    for document in documents:
        document.optional_services_json = json.dumps(document.requested_optional_services or [])
    jurisdiction_config = config_loader.load_jurisdiction_config(jurisdiction) or {}
    confidentiality_config = jurisdiction_config.get("document_confidentiality") or {}
    context = {
        "is_logged_in": True,
        "availability_message": draft_unavailable_message(draft),
        "filing_draft": draft_snapshot(draft),
        "documents": documents,
        "return_to": return_target(request),
        "organize_context": {
            "jurisdiction": jurisdiction,
            "case_fingerprint": case_fingerprint(draft),
            "filing_types_url": reverse("case_filing_types", kwargs={"jurisdiction": jurisdiction})
            + f"?draft={draft.pk}",
            "court": draft.court_code,
            "case_category": draft.case_category_code,
            "case_type": draft.case_type_code,
            "case_category_name": draft.case_category_name,
            "case_type_name": draft.case_type_name,
            "existing_case": "yes" if draft.existing_case == ExistingCase.EXISTING else "no",
            "guessed_filing_type": (draft.extracted_guesses or {}).get("filing type", ""),
            "default_confidentiality": confidentiality_config.get("default", ""),
            "return_to": return_target(request),
            # The script rewrites these choice lists once the court answers, so
            # the strings it renders have to travel with the page -- otherwise
            # they are the only copy on this screen that no state can reword and
            # no translator can reach.
            "text": get_texts(JS_TEXT_KEYS, jurisdiction=jurisdiction),
        },
    }
    context.update(get_workflow_context(WorkflowStepKey.ORGANIZE_DOCUMENTS, jurisdiction, draft))
    return render(request, "efile/organize_documents.html", context)

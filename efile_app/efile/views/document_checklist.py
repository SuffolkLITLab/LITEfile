from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from efile.api.suffolk_api_views import get_tyler_token
from efile.models import FilingDocument
from efile.services.current_drafts import ensure_current_draft
from efile.services.document_previews import unreviewed_documents
from efile.services.document_uploads import upload_files
from efile.services.draft_urls import draft_url
from efile.services.drafts import draft_snapshot
from efile.services.filing_availability import draft_unavailable_message, unavailable_response
from efile.services.filing_plans import (
    attach_document_to_item,
    attach_lead_document,
    attached_documents,
    checklist_answers_from_post,
    detach_item,
    documents_missing_from_envelope,
    ensure_plan_for_draft,
    filer_role_label,
    filer_roles_for_draft,
    filing_type_for_item,
    grouped_checklist,
    mark_item_have,
    needs_filer_role,
    set_checklist_answers,
    set_filer_role,
    status_choices,
)
from efile.views.document_checks import unchecked_documents
from efile.workflow import (
    WorkflowStepKey,
    continue_step,
    continue_url,
    get_step_url,
    get_workflow_context,
    return_target,
    with_return_to,
)


def _this_page(request, jurisdiction):
    """Reload this step, keeping any "on my way back to Review" marker."""

    return with_return_to(
        get_step_url(WorkflowStepKey.DOCUMENT_CHECKLIST, jurisdiction),
        return_target(request),
    )


def _newest_document(draft):
    """The file just uploaded: an upload appends to the end of the list."""

    return (
        FilingDocument.objects.filter(draft=draft, role=FilingDocument.Role.SUPPORTING)
        .order_by("-sort_order", "-pk")
        .first()
        or FilingDocument.objects.filter(draft=draft, role=FilingDocument.Role.LEAD).first()
    )


def _attach_to_item(request, draft, plan, jurisdiction):
    """Answer one checklist item with a document, uploading it if need be.

    This is the step that turns the plan from a list into a filing: the filer
    says "this file is my fee waiver", and from then on the checklist, the
    review step, and the envelope all agree about it.
    """

    item_id = request.POST.get("item_id", "")
    if plan is None or item_id not in (plan.checklist or {}):
        messages.error(request, "That document is not on your plan.")
        return redirect(_this_page(request, jurisdiction))

    uploaded_files = request.FILES.getlist("document")
    document_id = request.POST.get("document_id", "")
    label = plan.checklist[item_id].get("label") or item_id

    if uploaded_files:
        try:
            upload_files(draft, uploaded_files, jurisdiction, current_step=WorkflowStepKey.DOCUMENT_CHECKLIST)
        except ValueError as error:
            messages.error(request, str(error))
            return redirect(_this_page(request, jurisdiction))
        document = _newest_document(draft)
        uploaded = True
    else:
        uploaded = False
        document = FilingDocument.objects.filter(draft=draft, pk=document_id).first() if document_id else None

    if document is None:
        messages.error(request, f"Choose a PDF to add for {label}, or pick a file you already added.")
        return redirect(_this_page(request, jurisdiction))

    attach_document_to_item(draft, item_id, document)
    mark_item_have(plan, item_id)

    # The plan knows what this document is, so it can say what the court calls
    # it. Only ever fills a blank: a filing type the filer chose is theirs.
    if not document.filing_type_code:
        code, name = filing_type_for_item(draft, item_id)
        if code:
            document.filing_type_code = code
            document.filing_type_name = name
            document.document_type_confirmed = False
            document.save(
                update_fields=["filing_type_code", "filing_type_name", "document_type_confirmed", "updated_at"]
            )

    messages.success(request, f"{label} is in this filing.")
    # A new file is checked right here, before the filer moves on.
    return redirect(_this_page(request, jurisdiction) + ("#document-checks" if uploaded else ""))


@require_http_methods(["GET", "POST"])
def document_checklist(request, jurisdiction):
    if not request.user.is_authenticated or not get_tyler_token(request, jurisdiction):
        return redirect("efile_login", jurisdiction=jurisdiction)

    draft = ensure_current_draft(
        request,
        jurisdiction,
        current_step=WorkflowStepKey.DOCUMENT_CHECKLIST,
        workflow_version=2,
    )
    if message := draft_unavailable_message(draft):
        return unavailable_response(request, draft, message)
    documents = FilingDocument.objects.filter(draft=draft).order_by("role", "sort_order", "created_at")
    if not documents.exists():
        messages.error(request, "Upload at least one document before checking your filing.")
        return redirect("upload_documents", jurisdiction=jurisdiction)

    if documents.filter(preparation="").exists():
        return redirect(
            with_return_to(get_step_url(WorkflowStepKey.EXTRACTION_REVIEW, jurisdiction), return_target(request))
        )

    if request.method == "POST" and request.POST.get("action") == "upload":
        uploaded_files = request.FILES.getlist("documents")
        if not uploaded_files:
            return JsonResponse({"success": False, "error": "Choose at least one PDF to add."}, status=400)
        try:
            upload_files(
                draft,
                uploaded_files,
                jurisdiction,
                current_step=WorkflowStepKey.DOCUMENT_CHECKLIST,
            )
        except ValueError as error:
            return JsonResponse({"success": False, "error": str(error)}, status=400)
        return JsonResponse({"success": True, "document_count": FilingDocument.objects.filter(draft=draft).count()})

    # In a two-sided case the same case type means two different jobs, and the
    # documents follow the side rather than the case. Nothing can be listed
    # until we know which side this filer is on, so that is asked first.
    filer_roles = filer_roles_for_draft(draft)
    if request.method == "POST" and request.POST.get("action") == "set_filer_role":
        if not set_filer_role(draft, request.POST.get("filer_role", "")):
            messages.error(request, "Choose which side of this case you are on.")
        return redirect(_this_page(request, jurisdiction))

    # The plan holds the filer's own list for this matter. It outlives this
    # filing, so it is created here and only read from the draft.
    plan = ensure_plan_for_draft(draft)
    attach_lead_document(draft, plan)

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "attach_item":
            return _attach_to_item(request, draft, plan, jurisdiction)
        if action == "detach_item":
            detach_item(draft, request.POST.get("item_id", ""))
            return redirect(_this_page(request, jurisdiction))

        if plan is not None:
            set_checklist_answers(
                plan,
                checklist_answers_from_post(request.POST, plan),
                keep_have=attached_documents(draft).keys(),
            )
        if action == "save_progress":
            messages.success(request, "We saved your document list.")
            return redirect(_this_page(request, jurisdiction))
        # Files added here are checked here. The page keeps Continue off until
        # they are; this covers a stale tab, so no later screen sends the filer
        # to a preview and back.
        if unreviewed_documents(draft).exists():
            messages.error(request, "Check each new file before you continue.")
            return redirect(_this_page(request, jurisdiction) + "#document-checks")
        # The checklist is a guide, not a gate: the filer can continue with any
        # item unticked, and is never asked to say the list is complete. It
        # cannot know which forms a filer's situation actually needs.
        #
        # Coming back here from Review to add a document is common now that
        # the review step names what is missing. Go straight back to Review,
        # unless a document still needs its filing details and confidentiality
        # checked on Organize.
        return_to = return_target(request)
        draft.current_step = continue_step(draft, return_to, WorkflowStepKey.ORGANIZE_DOCUMENTS)
        draft.save(update_fields=["current_step", "updated_at"])
        return redirect(continue_url(draft, jurisdiction, return_to, WorkflowStepKey.ORGANIZE_DOCUMENTS))

    missing = documents_missing_from_envelope(plan, draft)
    context = {
        "is_logged_in": True,
        "filing_draft": draft_snapshot(draft),
        "documents": documents,
        "checked_documents": [document for document in documents if document.preparation_reviewed_at is not None],
        "document_checks": unchecked_documents(documents),
        "document_checks_url": draft_url(reverse("document_checks", kwargs={"jurisdiction": jurisdiction}), draft.pk),
        "plan": plan,
        "filer_roles": filer_roles,
        "filer_role": draft.filer_role,
        "filer_role_label": filer_role_label(draft),
        # Normally answered on the confirm-filing step. It can still be
        # unanswered here on the existing-case path, where the case type is not
        # known until after the case lookup, so the question has a home here too.
        "choosing_filer_role": needs_filer_role(draft),
        "checklist_groups": grouped_checklist(plan, draft),
        "status_choices": status_choices(),
        "guidance": plan.guidance if plan else {},
        # Only the documents the filer says they have: an unticked "always
        # needed" item is already an empty box on this page, and repeating it
        # here would nag rather than help.
        "ready_to_add": [item for item in missing if item["reason"] == "have"],
        "return_to": return_target(request),
    }
    context.update(get_workflow_context(WorkflowStepKey.DOCUMENT_CHECKLIST, jurisdiction, draft))
    return render(request, "efile/document_checklist.html", context)

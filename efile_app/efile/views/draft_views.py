import logging

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_http_methods

from efile.api.suffolk_api_views import get_tyler_token
from efile.models import FilingPlan
from efile.services.current_drafts import attach_current_draft, create_current_draft, get_current_draft
from efile.services.drafts import draft_snapshot, write_case_data
from efile.services.filing_path import FilingPathSource, record_filing_path_source
from efile.services.filing_plans import create_draft_from_plan
from efile.utils.django_helpers import flush_cache_stay_logged_in
from efile.workflow import ExistingCase, WorkflowStepKey, get_step_url, normalize_existing_case

logger = logging.getLogger(__name__)


@require_http_methods(["POST"])
def start_filing(request, jurisdiction):
    """Start a fresh draft; ordinary starts choose new/existing after upload.

    Explicit paths from older links remain supported. Neither kind of start
    carries answers over from the draft the user was previously working on.
    """

    if not request.user.is_authenticated or not get_tyler_token(request, jurisdiction):
        return redirect("efile_login", jurisdiction=jurisdiction)

    path = normalize_existing_case(request.POST.get("existing_case"))
    flush_cache_stay_logged_in(request.session)
    draft = create_current_draft(
        request,
        jurisdiction,
        current_step=WorkflowStepKey.UPLOAD_DOCUMENTS,
        workflow_version=2,
    )
    logger.info("Started draft id=%s from the navigation menu (path=%s)", draft.pk, path or "unset")

    # Without an answer, leave it unset until the filer confirms it beside
    # their document.
    if path in {ExistingCase.NEW, ExistingCase.EXISTING}:
        write_case_data(draft, {"existing_case": path})
    record_filing_path_source(draft, FilingPathSource.START_MENU)
    return redirect(get_step_url(WorkflowStepKey.UPLOAD_DOCUMENTS, jurisdiction))


@require_http_methods(["POST"])
def start_filing_from_plan(request, jurisdiction, plan_id):
    """Start another filing in a matter the filer already has a plan for.

    The plan's court, case category, and case type are carried over by name and
    resolved against the court's current code lists, so an old plan never files
    against a code the court has since reused for something else.
    """

    if not request.user.is_authenticated or not get_tyler_token(request, jurisdiction):
        return redirect("efile_login", jurisdiction=jurisdiction)

    plan = get_object_or_404(FilingPlan, pk=plan_id, user=request.user, jurisdiction=jurisdiction)

    flush_cache_stay_logged_in(request.session)
    draft = create_draft_from_plan(request.user, plan)
    record_filing_path_source(draft, FilingPathSource.PLAN)
    attach_current_draft(request, draft)
    logger.info("Started draft id=%s from plan id=%s", draft.pk, plan.pk)

    if plan.is_linked_to_a_case:
        messages.success(request, f"We started another filing for {plan.title}, in case {plan.docket_number}.")
    else:
        messages.success(request, f"We started another filing for {plan.title}.")
    return redirect(get_step_url(WorkflowStepKey.UPLOAD_DOCUMENTS, jurisdiction))


@require_http_methods(["GET"])
def get_current_draft_view(request):
    """Return the durable draft attached to this session/user."""

    if not request.user.is_authenticated:
        return JsonResponse({"success": False, "error": "Authentication required"}, status=401)

    draft = get_current_draft(request)
    return JsonResponse({"success": True, "data": {"filing_draft": draft_snapshot(draft)}})

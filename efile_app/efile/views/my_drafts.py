"""My draft e-filings: every filing this filer has started and not sent.

The durable-draft layer has always allowed several drafts at once; until now
there was nowhere to see them. Without this screen a second draft is invisible
-- the filer is offered "continue where you left off" and has no way to know
which filing that is, or to throw away the one they started by mistake.
"""

import logging
import uuid

from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from efile.api.suffolk_api_views import get_tyler_token
from efile.models import FilingDocument, FilingDraft
from efile.services.current_drafts import (
    CURRENT_DRAFT_SESSION_KEY,
    adopt_draft,
    clear_current_draft,
    pointed_at_draft,
)
from efile.services.drafts import active_drafts_for
from efile.services.handoff import receipt_for
from efile.workflow import ExistingCase, get_resume_step_url, get_step

logger = logging.getLogger(__name__)

PATH_LABELS = {
    ExistingCase.NEW: "Starting a new case",
    ExistingCase.EXISTING: "Filing into an existing case",
    ExistingCase.UNSURE: "Still deciding which kind of filing this is",
}


def _describe(draft: FilingDraft, current_draft_id: int | None) -> dict:
    try:
        step_label = get_step(draft.current_step).label
    except KeyError:
        step_label = ""
    # Only the court's record, once the lookup has found it: before that the
    # title may be stale and the number is just what the filer searched for.
    found = draft.is_filing_into_found_case
    return {
        "draft": draft,
        "title": (draft.case_title if found else "") or (draft.plan.title if draft.plan else ""),
        "docket_number": draft.docket_number if found else "",
        "path_label": PATH_LABELS.get(draft.existing_case, ""),
        "step_label": step_label,
        "resume_url": get_resume_step_url(draft.current_step, draft.jurisdiction, draft_id=draft.pk),
        "document_count": FilingDocument.objects.filter(draft=draft).count(),
        "is_current": draft.pk == current_draft_id,
    }


@require_http_methods(["GET", "POST"])
def my_drafts(request, jurisdiction):
    if not request.user.is_authenticated or not get_tyler_token(request, jurisdiction):
        return redirect("efile_login", jurisdiction=jurisdiction)

    if request.method == "POST":
        action = request.POST.get("action")
        draft_id = request.POST.get("draft_id")
        # Ownership is enforced by the queryset, not by trusting the form.
        draft = active_drafts_for(request.user, jurisdiction=jurisdiction).filter(pk=draft_id).first()
        if draft is None:
            messages.error(request, "That draft is no longer here.")
        elif action == "resume":
            from efile.services.analytics import record_event

            record_event(draft, "draft_resumed", operation=uuid.uuid4())
            adopt_draft(request, draft.pk, jurisdiction=jurisdiction)
            if receipt_for(draft) or draft.correction_of_id:
                return redirect("handoff_review", draft_id=draft.pk)
            resume_url = get_resume_step_url(draft.current_step, jurisdiction, draft_id=draft.pk)
            return redirect(resume_url or reverse("efile_options", kwargs={"jurisdiction": jurisdiction}))
        elif action == "delete" and draft.status == FilingDraft.Status.ERROR:
            # The filing may be with the court (#445). Keep it for staff to check;
            # throwing it away would also hide it from that check.
            messages.error(
                request,
                "We are not sure whether this filing reached the court, so it can't be thrown away. "
                "Check My filings, or contact us for help.",
            )
        elif action == "delete":
            from efile.services.analytics import record_event

            record_event(draft, "draft_discarded")
            # Abandoned, not deleted: it leaves every list the filer sees, and
            # a filer who deleted the wrong thing can still be helped.
            draft.status = FilingDraft.Status.ABANDONED
            draft.save(update_fields=["status", "updated_at"])
            if str(request.session.get(CURRENT_DRAFT_SESSION_KEY)) == str(draft.pk):
                clear_current_draft(request)
            logger.info("Filer discarded draft id=%s", draft.pk)
            messages.success(request, "We threw that draft away.")
        else:
            messages.error(request, "We did not recognize that action.")
        return redirect("my_drafts", jurisdiction=jurisdiction)

    current = pointed_at_draft(request, jurisdiction=jurisdiction)
    drafts = active_drafts_for(request.user, jurisdiction=jurisdiction).select_related("plan")
    return render(
        request,
        "efile/my_drafts.html",
        {
            "is_logged_in": True,
            "drafts": [_describe(draft, current.pk if current else None) for draft in drafts],
        },
    )

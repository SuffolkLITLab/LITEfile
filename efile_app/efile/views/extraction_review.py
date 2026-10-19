import re

from botocore.exceptions import BotoCoreError, ClientError
from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from efile.api.suffolk_api_views import get_tyler_token
from efile.models import DocumentExtraction, FilingDocument
from efile.party_sides import PARTY_SIDE_HELP, PARTY_SIDE_LABELS, PartySide
from efile.services.account_profile import cached_account_profile
from efile.services.current_drafts import ensure_current_draft
from efile.services.document_checklists import resolve_filer_roles
from efile.services.document_extractions import extraction_for_document, extraction_is_waiting
from efile.services.document_preparation import PreparationError, PreparationUnavailable
from efile.services.document_previews import (
    DocumentReviewError,
    approve_document_review,
    prepare_document_review,
    preview_fingerprint,
    unreviewed_documents,
)
from efile.services.drafts import draft_snapshot, write_case_data
from efile.services.extracted_parties import extracted_party_suggestions, review_rows, save_reviewed_parties
from efile.services.extraction_confirmation import extraction_is_confirmed, extraction_review_fingerprint
from efile.services.extraction_fields import display_extracted_fields, document_summary_details
from efile.services.filing_availability import filing_unavailable_message
from efile.services.filing_path import (
    change_filing_path,
    clear_changed_classification,
    describe_path_change,
    filing_path_conflict,
)
from efile.services.filing_plans import needs_filer_role
from efile.services.filing_type_proposals import current_proposal, save_proposal
from efile.utils.s3_upload_handler import S3UploadHandler
from efile.workflow import (
    ExistingCase,
    WorkflowStepKey,
    continue_step,
    continue_url,
    get_next_step,
    get_step_url,
    get_workflow_context,
    return_target,
    with_return_to,
)


def _offered_filer_roles(request, jurisdiction):
    """The sides on offer for the case the filer is choosing right now.

    Resolved from what was submitted rather than from the draft: the case type
    on this screen is not saved until the form is, and a role only means
    something against the case it was offered for.
    """

    return resolve_filer_roles(
        jurisdiction=jurisdiction,
        court_code=request.POST.get("court_code", ""),
        case_category_name=request.POST.get("case_category_name", ""),
        case_type_name=request.POST.get("case_type_name", ""),
        lead_filing_type_name=request.POST.get("filing_type_name", ""),
    )


def _submitted_party_rows(request):
    """Read the party editor back off the form, keeping its rows aligned.

    Every row posts all of its inputs, including the empty id of a row the
    filer just added, so the lists stay index-aligned even when rows were
    added or removed in the browser.
    """

    ids = request.POST.getlist("party_id")
    names = request.POST.getlist("party_name")
    sides = request.POST.getlist("party_side")
    hints = request.POST.getlist("party_role_hint")
    # A hidden value rather than a checkbox, so an unticked row still posts
    # something and the lists stay index-aligned with the names beside them.
    selves = request.POST.getlist("party_is_self")
    rows = []
    for index, name in enumerate(names):
        raw_id = ids[index] if index < len(ids) else ""
        rows.append(
            {
                "id": int(raw_id) if str(raw_id).isdigit() else None,
                "name": name,
                "side": sides[index] if index < len(sides) else "",
                "role_hint": hints[index] if index < len(hints) else "",
                "is_self": selves[index] if index < len(selves) else "",
            }
        )
    return rows


def _set_lead_filing_type(draft, filing_type_code, filing_type_name):
    if not filing_type_code:
        return
    lead = FilingDocument.objects.filter(draft=draft, role=FilingDocument.Role.LEAD).first()
    if lead is None:
        return
    if lead.filing_type_code != filing_type_code:
        lead.document_type_confirmed = False
        lead.document_type_code = ""
        lead.document_type_name = ""
        lead.filing_component_code = ""
        lead.filing_component_name = ""
        lead.requested_optional_services = []
        lead.filing_requires_amount_in_controversy = False
    lead.filing_type_code = filing_type_code
    lead.filing_type_name = filing_type_name
    lead.save(
        update_fields=[
            "filing_type_code",
            "filing_type_name",
            "document_type_confirmed",
            "document_type_code",
            "document_type_name",
            "filing_component_code",
            "filing_component_name",
            "requested_optional_services",
            "filing_requires_amount_in_controversy",
            "updated_at",
        ]
    )


def _zip_shortcuts(request, draft, jurisdiction):
    """ZIP codes code search offers one click away: the filer's own, then recent ones.

    The account profile is the same cached fetch "Your information" makes, so
    asking for it here moves the call earlier rather than adding one.
    """
    filer = draft.parties.filter(role="filer").first()
    own = filer.zip_code if filer else ""
    if not own and request.method == "GET":
        own = (cached_account_profile(request, jurisdiction) or {}).get("zip") or ""
    shortcuts = []
    if re.fullmatch(r"[0-9]{5}(?:-[0-9]{4})?", own):
        shortcuts.append({"zip": own[:5], "kind": "address"})
    for postal_code in request.user.recent_case_zips or []:
        if all(item["zip"] != postal_code for item in shortcuts):
            shortcuts.append({"zip": postal_code, "kind": "recent"})
    return shortcuts


def _case_identity(existing_case, docket_number):
    """The case number and title this screen may save.

    A new case has neither until the court opens it, so both are cleared (and
    a number would make Tyler treat the filing as subsequent). An existing
    case's number is only a search term for the case lookup; its title is left
    alone, because the court sets it when the case is found.
    """
    if existing_case == ExistingCase.NEW:
        return {"docket_number": "", "case_title": ""}
    return {"docket_number": docket_number}


def _document_approval_error(draft, request):
    """Approve the displayed copies, or return the message and status saying why not."""
    fingerprint = request.POST.get("preview_fingerprint")
    if not fingerprint and not unreviewed_documents(draft).exists():
        return None
    try:
        approve_document_review(draft, fingerprint)
    except DocumentReviewError as exc:
        return str(exc), exc.status
    return None


@require_http_methods(["GET", "POST"])
def extraction_review(request, jurisdiction):
    if not request.user.is_authenticated or not get_tyler_token(request, jurisdiction):
        return redirect("efile_login", jurisdiction=jurisdiction)

    draft = ensure_current_draft(
        request,
        jurisdiction,
        current_step=WorkflowStepKey.EXTRACTION_REVIEW,
        workflow_version=2,
    )
    if not FilingDocument.objects.filter(draft=draft).exists():
        messages.error(request, "Upload at least one document before reviewing the filing.")
        return redirect("upload_documents", jurisdiction=jurisdiction)

    preview_error = ""
    status = 200
    if request.method == "GET":
        try:
            prepare_document_review(draft, S3UploadHandler())
        except PreparationUnavailable as exc:
            preview_error, status = str(exc), 503
        except PreparationError as exc:
            preview_error, status = str(exc), 422
        except (BotoCoreError, ClientError):
            preview_error, status = "We could not load your files. Try again later.", 503

    lead = FilingDocument.objects.filter(draft=draft, role=FilingDocument.Role.LEAD).first()
    extraction = extraction_for_document(lead) if lead else None
    if extraction_is_waiting(extraction):
        messages.info(request, "Please wait while we read your first PDF. You can continue when it is ready.")
        return redirect(
            with_return_to(get_step_url(WorkflowStepKey.UPLOAD_DOCUMENTS, jurisdiction), return_target(request))
        )

    # What the party editor should show: what was just submitted, so a filer
    # sent back to fix a validation error keeps the names they typed, and
    # otherwise what is saved (falling back to what the document named).
    party_rows = _submitted_party_rows(request) if request.method == "POST" else review_rows(draft)

    # The acknowledgement is only asked for when the page shows something to
    # acknowledge, so the check below and the template both key off this.
    guesses = display_extracted_fields(draft.extracted_guesses or {})
    needs_acknowledgement = bool(guesses) and not extraction_is_confirmed(draft)
    show_party_editor = bool(extracted_party_suggestions(draft.extracted_guesses)) or (
        request.method == "POST" and bool(party_rows)
    )
    acknowledgement_error = False
    availability_message = ""

    if request.method == "POST":
        existing_case = request.POST.get("existing_case", draft.existing_case)
        court_code = request.POST.get("court_code", "")
        case_category_code = request.POST.get("case_category_code", "")
        case_type_code = request.POST.get("case_type_code", "")

        availability_message = (
            ""
            if existing_case == ExistingCase.EXISTING
            else filing_unavailable_message(
                jurisdiction,
                court_code,
                case_category=request.POST.get("case_category_name", ""),
                case_type=request.POST.get("case_type_name", ""),
                filing_types=[request.POST.get("filing_type_name", "")],
            )
        )

        roles = _offered_filer_roles(request, jurisdiction) if existing_case == ExistingCase.NEW else []
        offered_roles = {role["id"] for role in roles}
        # Confirm detected roles beside the suggestion. Without a suggestion,
        # Check documents can ask later if its side-specific plan needs one.
        role_confirmation_required = any(role["suggested"] for role in roles) or draft.filer_role in offered_roles
        filer_role = request.POST.get("filer_role", "")

        if availability_message:
            pass  # Show the persistent notice alongside the editable choices.
        elif needs_acknowledgement and request.POST.get("reviewed_extraction") != "yes":
            # Shown beside the checkbox rather than as a toast, so it stays put
            # and is tied to the control that needs attention.
            acknowledgement_error = True
        elif existing_case not in {ExistingCase.NEW, ExistingCase.EXISTING}:
            messages.error(request, "Choose whether this is a new or existing court case to continue.")
        elif existing_case == ExistingCase.NEW and not (court_code and case_category_code and case_type_code):
            # Tyler's e-filing API only accepts exact court/category/type codes, so
            # a new case can't proceed on free-text guesses -- unlike an existing
            # case, which resolves these from the case lookup step instead.
            messages.error(request, "Choose a court, case category, and case type from the lists to continue.")
        elif (role_confirmation_required or filer_role) and filer_role not in offered_roles:
            # A detected or previously confirmed role must be checked here.
            # With no suggestion, the checklist can ask for the side later.
            messages.error(request, "Choose which side of this case you are on to continue.")
        elif approval_error := _document_approval_error(draft, request):
            # Last, so the files are only marked checked when the rest of the
            # form is accepted along with them.
            preview_error, status = approval_error
        else:
            # Validated above, so this is an offered side or, when nothing
            # needed confirming, a blank that drops a side the new case type
            # no longer has.
            if existing_case == ExistingCase.NEW and draft.filer_role != filer_role:
                draft.filer_role = filer_role
                draft.save(update_fields=["filer_role", "updated_at"])
            # Before the rest is saved: switching kinds of filing clears the
            # filing types chosen for the old kind, and the lead's new one is
            # set from this form just below.
            path_change = change_filing_path(draft, existing_case)
            if path_change.changed and path_change.cleared:
                messages.info(request, describe_path_change(path_change))
            if existing_case == ExistingCase.NEW and clear_changed_classification(
                draft, court_code, case_category_code, case_type_code
            ):
                messages.info(request, "Filing path changed. Check each document's filing options and fees again.")
            if existing_case == ExistingCase.NEW:
                write_case_data(
                    draft,
                    {
                        "existing_case": existing_case,
                        "court": court_code,
                        "court_name": request.POST.get("court_name", ""),
                        "case_category": case_category_code,
                        "case_category_name": request.POST.get("case_category_name", ""),
                        "case_type": case_type_code,
                        "case_type_name": request.POST.get("case_type_name", ""),
                        **_case_identity(existing_case, ""),
                    },
                    current_step=WorkflowStepKey.EXTRACTION_REVIEW,
                )
                _set_lead_filing_type(
                    draft, request.POST.get("filing_type_code", ""), request.POST.get("filing_type_name", "")
                )
            else:
                # Court-owned identity comes only from lookup/confirmation.
                # Keep a number from an older Confirm form as a search term,
                # but never allow that form to edit a confirmed court case.
                values = {"existing_case": existing_case}
                if not draft.previous_case_id:
                    values["docket_number"] = request.POST.get("docket_number", draft.docket_number)
                write_case_data(draft, values, current_step=WorkflowStepKey.EXTRACTION_REVIEW)
            # The people the document named, as the filer has now corrected
            # them. They are stored as sides here; the party screen turns each
            # side into this court's own party type once the case type it
            # depends on has been saved just above.
            if show_party_editor and existing_case == ExistingCase.NEW:
                save_reviewed_parties(draft, party_rows)
            if not draft.ai_assistance_opted_out and "proposed_filing_type" in request.POST:
                save_proposal(draft, request.POST.get("proposed_filing_type"))
            draft.extraction_review_fingerprint = extraction_review_fingerprint(draft)
            draft.save(update_fields=["extraction_review_fingerprint", "updated_at"])
            # A different kind of filing is not an edit Review can take back
            # as-is: an existing case has to be found in the court's records,
            # and a switched path clears the documents' filing types. The
            # filer goes through exactly those steps, still on the detour.
            return_to = return_target(request)
            if return_to:
                if existing_case == ExistingCase.EXISTING and not draft.previous_case_id:
                    write_case_data(draft, {}, current_step=WorkflowStepKey.CASE_LOOKUP)
                    return redirect(with_return_to(get_step_url(WorkflowStepKey.CASE_LOOKUP, jurisdiction), return_to))
                if needs_filer_role(draft):
                    # The side was left for the checklist to ask, so the
                    # detour goes there before returning.
                    write_case_data(draft, {}, current_step=WorkflowStepKey.DOCUMENT_CHECKLIST)
                    return redirect(
                        with_return_to(get_step_url(WorkflowStepKey.DOCUMENT_CHECKLIST, jurisdiction), return_to)
                    )
                linear = get_next_step(WorkflowStepKey.EXTRACTION_REVIEW, draft)
                default_step = linear.key if linear else WorkflowStepKey.REVIEW
                write_case_data(draft, {}, current_step=continue_step(draft, return_to, default_step))
                return redirect(continue_url(draft, jurisdiction, return_to, default_step))
            next_step = get_next_step(WorkflowStepKey.EXTRACTION_REVIEW, draft)
            if next_step:
                write_case_data(draft, {}, current_step=next_step.key)
                return redirect(get_step_url(next_step.key, jurisdiction))

    classification = extraction.classification if extraction is not None else {}

    def classified(level, key):
        selection = classification.get(level, {}) if isinstance(classification, dict) else {}
        return selection.get(key, "") if selection.get("status") == "selected" else ""

    # Only what identifies the document is shown as read-only text. Anything
    # else the extraction found is either asked for by the form below, where
    # the filer can correct it, or not used by the filing at all, so it is not
    # shown.
    summary_details = document_summary_details(guesses)
    party_side_options = [
        {"value": str(side), "label": PARTY_SIDE_LABELS[side], "help": PARTY_SIDE_HELP[side]} for side in PartySide
    ]
    extraction_context = {
        "jurisdiction": jurisdiction,
        "guesses": guesses,
        "existing_case": draft.existing_case,
        "court_code": draft.court_code or classified("court", "route_key"),
        "court_name": draft.court_name or classified("court", "name"),
        "case_category_code": draft.case_category_code or classified("case category", "route_key"),
        "case_category_name": draft.case_category_name or classified("case category", "name"),
        "case_type_code": draft.case_type_code or classified("case type", "route_key"),
        "case_type_name": draft.case_type_name or classified("case type", "name"),
        "filing_type_code": (lead.filing_type_code if lead else "") or classified("filing type", "route_key"),
        "filing_type_name": (lead.filing_type_name if lead else "") or classified("filing type", "name"),
        # Keep a confirmed side visible; otherwise ask here only when the
        # filing type suggests one. The checklist handles undecided sides.
        "filer_role": draft.filer_role,
        "zip_shortcuts": _zip_shortcuts(request, draft, jurisdiction),
    }
    suggested_existing_case = (
        "new"
        if not draft.existing_case and (extraction.evidence if extraction else {}).get("filing phase") == "initial"
        else "existing"
        if not draft.existing_case and (extraction.evidence if extraction else {}).get("filing phase") == "subsequent"
        else ""
    )
    chosen_existing_case = draft.existing_case or suggested_existing_case
    # The answer already given, shown as an answer with Change rather than
    # asked again. "Not sure" is not an answer to show, so it is still asked.
    saved_path = draft.existing_case if draft.existing_case in {ExistingCase.NEW, ExistingCase.EXISTING} else ""
    docket_number = draft.docket_number or guesses.get("docket number")
    if request.method == "POST":
        # Sent back to fix something: show what the filer submitted, not what
        # was saved before, so no answer has to be entered twice.
        for key in (
            "court_code",
            "court_name",
            "case_category_code",
            "case_category_name",
            "case_type_code",
            "case_type_name",
            "filing_type_code",
            "filing_type_name",
            "filer_role",
        ):
            extraction_context[key] = request.POST.get(key, "")
        chosen_existing_case = request.POST.get("existing_case", "")
        docket_number = request.POST.get("docket_number", "")
        extraction_context["existing_case"] = chosen_existing_case
    # Against the answer on screen, which after a refused POST is the one
    # submitted rather than the one saved.
    path_conflict = filing_path_conflict(
        draft,
        extraction.evidence if extraction else {},
        document_title=guesses.get("document title", ""),
        chosen=chosen_existing_case,
    )
    documents = list(FilingDocument.objects.filter(draft=draft).order_by("role", "sort_order", "pk"))
    context = {
        "filing_type_proposal": current_proposal(draft),
        "documents": documents,
        "preview_fingerprint": preview_fingerprint(documents),
        "preview_error": preview_error,
        "preparation_pending": any(not doc.preparation for doc in documents),
        "is_logged_in": True,
        "availability_message": availability_message,
        "lead_document": lead,
        "filing_draft": draft_snapshot(draft),
        "has_guesses": bool(guesses),
        "show_party_editor": show_party_editor,
        "document_summary_details": summary_details,
        "party_rows": party_rows,
        "party_side_options": party_side_options,
        "extraction_failed": extraction is not None and extraction.status == DocumentExtraction.Status.FAILED,
        # The filer turned AI off for this filing, so the details below came
        # from a keyword scan of the document's own text. The screen says so
        # rather than crediting a reading that never happened.
        "ai_opted_out": draft.ai_assistance_opted_out,
        "extraction_pages_analyzed": extraction.pages_analyzed if extraction else None,
        "extraction_total_pages": extraction.total_pages if extraction else None,
        "extraction_text_truncated": bool(extraction and extraction.analysis_metadata.get("source_text_truncated")),
        "classification": classification,
        "chosen_existing_case": chosen_existing_case,
        "saved_path": saved_path,
        "path_conflict": path_conflict,
        # Open when there is no saved answer to show, or when the filer was
        # sent back with a different answer than the saved one.
        "path_question_open": not saved_path or chosen_existing_case != saved_path,
        "docket_number": docket_number,
        "needs_acknowledgement": needs_acknowledgement,
        "acknowledgement_error": acknowledgement_error,
        "reviewed_extraction": request.method == "POST" and request.POST.get("reviewed_extraction") == "yes",
        "extraction_context": extraction_context,
        "return_to": return_target(request),
    }
    if request.method == "POST" and "proposed_filing_type" in request.POST and not draft.ai_assistance_opted_out:
        context["filing_type_proposal"] = {"name": request.POST.get("proposed_filing_type", "")}
    context.update(get_workflow_context(WorkflowStepKey.EXTRACTION_REVIEW, jurisdiction, draft))
    return render(request, "efile/extraction_review.html", context, status=status)

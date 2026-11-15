from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from efile.api.suffolk_api_views import get_tyler_token
from efile.models import FilingDocument
from efile.services.appeals import LOWER_COURT_FIELDS
from efile.services.current_drafts import ensure_current_draft
from efile.services.document_extractions import extraction_for_document
from efile.services.drafts import draft_snapshot
from efile.services.people import get_case_questions, needs_amount_in_controversy, parse_question_answer
from efile.services.taxonomy_classification import primary_amount_in_controversy
from efile.workflow import WorkflowStepKey, continue_step, continue_url, get_workflow_context, return_target


def _parse_amount(raw: str) -> Decimal | None:
    cleaned = raw.replace(",", "").replace("$", "").strip()
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return None
    return amount if amount > 0 else None


@require_http_methods(["GET", "POST"])
def case_questions(request, jurisdiction):
    if not request.user.is_authenticated or not get_tyler_token(request, jurisdiction):
        return redirect("efile_login", jurisdiction=jurisdiction)

    draft = ensure_current_draft(
        request,
        jurisdiction,
        current_step=WorkflowStepKey.CASE_QUESTIONS,
        workflow_version=2,
    )
    questions = get_case_questions(draft)
    show_amount_field = needs_amount_in_controversy(draft)
    if not questions and not show_amount_field:
        return_to = return_target(request)
        draft.current_step = continue_step(draft, return_to, WorkflowStepKey.SERVICE)
        draft.save(update_fields=["current_step", "updated_at"])
        return redirect(continue_url(draft, jurisdiction, return_to, WorkflowStepKey.SERVICE))

    for question in questions:
        raw_value = (
            request.POST.get(question["name"], "")
            if request.method == "POST"
            else (draft.supplemental_fields or {}).get(question["name"], "")
        )
        if question["type"] == "radio":
            raw_value = "" if raw_value in (None, "") else str(raw_value).lower()
        elif raw_value is None:
            raw_value = ""
        question["value"] = raw_value

    if request.method == "POST":
        answers = {}
        errors = []
        for question in questions:
            value = request.POST.get(question["name"], "").strip()
            if question["name"] == "child_count" and request.POST.get("has_children") == "false":
                value = ""
            if question["required"] and value == "" and question["name"] != "child_count":
                errors.append(question["label"])
                continue
            try:
                answers[question["name"]] = parse_question_answer(question, value)
            except ValueError:
                errors.append(question["label"])
        if request.POST.get("has_children") == "true" and not request.POST.get("child_count"):
            errors.append("Number of minor children")

        amount_in_controversy = None
        if show_amount_field:
            amount_in_controversy = _parse_amount(request.POST.get("amount_in_controversy", ""))
            if amount_in_controversy is None:
                errors.append("Amount in controversy")

        if errors:
            messages.error(request, f"Answer these questions: {', '.join(dict.fromkeys(errors))}.")
        else:
            lower_question = next((q for q in questions if q["name"] == "lower_court_code"), None)
            if lower_question:
                selected = next(o for o in lower_question["options"] if o["value"] == answers["lower_court_code"])
                answers["lower_court_name"] = selected["label"]
                answers["lower_court_prod_code"] = selected["prod_code"]
            else:
                draft.supplemental_fields = {
                    k: v for k, v in (draft.supplemental_fields or {}).items() if k not in LOWER_COURT_FIELDS
                }
            draft.supplemental_fields = {
                **(draft.supplemental_fields or {}),
                **answers,
                "_case_questions_required": True,
            }
            update_fields = ["supplemental_fields", "current_step", "updated_at"]
            if show_amount_field:
                draft.amount_in_controversy = str(amount_in_controversy)
                update_fields.append("amount_in_controversy")
            return_to = return_target(request)
            draft.current_step = continue_step(draft, return_to, WorkflowStepKey.SERVICE)
            draft.save(update_fields=update_fields)
            return redirect(continue_url(draft, jurisdiction, return_to, WorkflowStepKey.SERVICE))

    amount_value = draft.amount_in_controversy
    if show_amount_field:
        if request.method == "POST":
            amount_value = request.POST.get("amount_in_controversy", "")
        elif not amount_value:
            lead = FilingDocument.objects.filter(draft=draft, role=FilingDocument.Role.LEAD).first()
            extraction = extraction_for_document(lead) if lead else None
            amount_value = primary_amount_in_controversy(extraction.evidence) if extraction else ""

    context = {
        "is_logged_in": True,
        "filing_draft": draft_snapshot(draft),
        "questions": questions,
        "answers": draft.supplemental_fields or {},
        "return_to": return_target(request),
        "show_amount_field": show_amount_field,
        "amount_in_controversy": amount_value,
    }
    context.update(get_workflow_context(WorkflowStepKey.CASE_QUESTIONS, jurisdiction, draft))
    return render(request, "efile/case_questions.html", context)

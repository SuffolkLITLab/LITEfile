"""Create and maintain a filer's FilingPlan -- the matter behind their filings.

A plan is the filer's own list of documents for a matter ("my name change"),
kept across however many envelopes that matter takes. Two ideas matter here:

* The plan stores names, not court codes. When the filer starts another filing
  from the plan, the stored names are resolved against the court's *current*
  code lists, so a plan made six months ago still works after the court renumbers
  everything.
* The checklist is snapshotted into the plan when it is created. Later edits to
  partner YAML change what new plans get, and leave existing plans alone.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import requests
from django.conf import settings
from django.db import transaction
from django.utils.dateparse import parse_date

from efile.models import FilingDocument, FilingDraft, FilingPlan
from efile.services.document_checklists import (
    REQUIREMENT_LABELS,
    REQUIREMENT_ORDER,
    normalize_name,
    resolve_document_checklist,
    resolve_filer_roles,
    resolve_plan_guidance,
)
from efile.services.drafts import create_draft
from efile.workflow import ExistingCase, WorkflowStepKey

logger = logging.getLogger(__name__)

DEFAULT_PLAN_TITLE = "My filing"

# Where the filer is with one document. "I have it" is not the only way to be
# done with something: plenty of documents are already at the court from an
# earlier filing, and plenty are deliberately being left until later.
STATUS_NONE = ""
STATUS_HAVE = "have"
STATUS_FILED = "filed"
STATUS_LATER = "later"

STATUS_LABELS: dict[str, str] = {
    STATUS_NONE: "Not yet",
    STATUS_HAVE: "I have it now",
    STATUS_FILED: "I already filed this",
    STATUS_LATER: "I will file it later",
}

# The same answers, short enough to sit side by side against every item. Each
# one is a phrase from the sentence above it, so what a filer sees and what a
# screen reader announces are the same answer.
STATUS_SHORT_LABELS: dict[str, str] = {
    STATUS_NONE: "Not yet",
    STATUS_HAVE: "I have it",
    STATUS_FILED: "Already filed",
    STATUS_LATER: "File it later",
}
STATUS_ORDER: tuple[str, ...] = (STATUS_NONE, STATUS_HAVE, STATUS_FILED, STATUS_LATER)

# Statuses that mean the document is accounted for: it is with the court, or in
# the filer's hands ready to go.
SETTLED_STATUSES = frozenset({STATUS_HAVE, STATUS_FILED})

# Remembers that we already guessed which checklist item the main document is,
# so the guess is offered once rather than every time the page is opened.
LEAD_MATCHED_FIELD = "_lead_document_matched"


def item_status(item: dict[str, Any]) -> str:
    """Where the filer is with one item, reading plans written before statuses.

    Plans made when "I have it" was the only answer stored a ``complete`` flag.
    They are read as saying exactly what they said.
    """

    status = _clean_status(item.get("status"))
    if status:
        return status
    return STATUS_HAVE if item.get("complete") else STATUS_NONE


def _clean_status(value: Any) -> str:
    status = str(value or "")
    return status if status in STATUS_LABELS else STATUS_NONE


def _clean_due_date(value: Any) -> str:
    """Keep a date only if it is one; a date we cannot read is not a promise."""

    if isinstance(value, date):
        return value.isoformat()
    parsed = parse_date(str(value or "").strip()) if value else None
    return parsed.isoformat() if parsed else ""


def lead_filing_type_name(draft: FilingDraft) -> str:
    lead = FilingDocument.objects.filter(draft=draft, role=FilingDocument.Role.LEAD).first()
    return (lead.filing_type_name if lead else "") or ""


def plan_title_for(draft: FilingDraft) -> str:
    """Name a new plan after the case it gathers documents for."""

    for candidate in (draft.case_type_name, draft.case_category_name, draft.case_title):
        if candidate:
            return candidate[:255]
    return DEFAULT_PLAN_TITLE


def checklist_snapshot(
    checklist: dict[str, dict[str, Any]],
    previous: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Copy resolved guidance into plan shape, keeping any progress already made."""

    previous = previous or {}
    snapshot: dict[str, dict[str, Any]] = {}
    for item_id, item in checklist.items():
        was = previous.get(item_id) or {}
        answered = {"status": item_status(was)}
        if was.get("due_date"):
            answered["due_date"] = was["due_date"]
        snapshot[item_id] = {**item, **answered}
    return snapshot


def resolve_checklist_for_draft(draft: FilingDraft) -> dict[str, dict[str, Any]]:
    return resolve_document_checklist(
        jurisdiction=draft.jurisdiction,
        court_code=draft.court_code,
        case_category_name=draft.case_category_name,
        case_type_name=draft.case_type_name,
        lead_filing_type_name=lead_filing_type_name(draft),
        filer_role=draft.filer_role,
    )


def resolve_guidance_for_draft(draft: FilingDraft) -> dict[str, str]:
    return resolve_plan_guidance(
        jurisdiction=draft.jurisdiction,
        court_code=draft.court_code,
        case_category_name=draft.case_category_name,
        case_type_name=draft.case_type_name,
        filer_role=draft.filer_role,
    )


def filer_roles_for_draft(draft: FilingDraft) -> list[dict[str, Any]]:
    """The sides this case can be filed from, or an empty list for most cases."""

    return resolve_filer_roles(
        jurisdiction=draft.jurisdiction,
        court_code=draft.court_code,
        case_category_name=draft.case_category_name,
        case_type_name=draft.case_type_name,
        lead_filing_type_name=lead_filing_type_name(draft),
    )


def needs_filer_role(draft: FilingDraft) -> bool:
    """Whether this case has sides and the filer has not said which is theirs."""

    return not draft.filer_role and bool(filer_roles_for_draft(draft))


def set_filer_role(draft: FilingDraft, filer_role: str) -> bool:
    """Record which side of the case the filer is on, if it is one on offer."""

    if filer_role not in {role["id"] for role in filer_roles_for_draft(draft)}:
        return False
    if draft.filer_role != filer_role:
        draft.filer_role = filer_role
        draft.save(update_fields=["filer_role", "updated_at"])
    return True


def filer_role_label(draft: FilingDraft) -> str:
    for role in filer_roles_for_draft(draft):
        if role["id"] == draft.filer_role:
            return role["label"]
    return ""


def _plan_case(plan: FilingPlan) -> tuple[str, str, str, str, str]:
    return (
        plan.court_code,
        plan.case_category_name,
        plan.case_type_name,
        plan.filer_role,
        plan.lead_filing_type_name,
    )


def _draft_case(draft: FilingDraft) -> tuple[str, str, str, str, str]:
    return (
        draft.court_code,
        draft.case_category_name,
        draft.case_type_name,
        draft.filer_role,
        lead_filing_type_name(draft),
    )


def _detach(draft: FilingDraft) -> None:
    draft.plan = None
    draft.save(update_fields=["plan", "updated_at"])


def _refresh_plan(plan: FilingPlan, draft: FilingDraft) -> FilingPlan | None:
    """Move a plan onto the case its only filing has become.

    Someone who goes back and picks a different case type is not gathering
    documents for the old one any more. Progress on items that survive the
    change is kept; guidance for a case they are no longer filing is not.
    """

    checklist = resolve_checklist_for_draft(draft)
    if not checklist:
        _detach(draft)
        return None

    auto_title = plan.case_type_name or plan.case_category_name or DEFAULT_PLAN_TITLE
    if plan.title == auto_title:
        plan.title = plan_title_for(draft)
    plan.court_code = draft.court_code
    plan.court_name = draft.court_name
    plan.case_category_name = draft.case_category_name
    plan.case_type_name = draft.case_type_name
    plan.filer_role = draft.filer_role
    plan.lead_filing_type_name = lead_filing_type_name(draft)
    plan.checklist = checklist_snapshot(checklist, plan.checklist)
    plan.guidance = resolve_guidance_for_draft(draft)
    plan.save()
    return plan


@transaction.atomic
def ensure_plan_for_draft(draft: FilingDraft) -> FilingPlan | None:
    """Attach a plan to this draft, creating one from the configured checklist.

    Returns ``None`` when no partner checklist covers this case, so an
    unconfigured case type leaves the filer's experience exactly as it was.
    """

    plan = draft.plan
    if plan is not None:
        if _plan_case(plan) == _draft_case(draft):
            return plan
        settled = FilingDraft.objects.filter(plan=plan).exclude(pk=draft.pk).exists() or plan.is_linked_to_a_case
        if not settled:
            return _refresh_plan(plan, draft)
        if _plan_case(plan)[:3] == _draft_case(draft)[:3] and plan.filer_role != draft.filer_role:
            # Correcting which side you are on changes what the matter needs,
            # however many filings it already has: a tenant who has been shown
            # the landlord's list has been shown the wrong list.
            return _refresh_plan(plan, draft)
        if _plan_case(plan)[:3] == _draft_case(draft)[:3]:
            # Same court case, different lead document: this is simply another
            # filing in the matter, which is what a plan is for. The checklist
            # the matter was set up with stays as it is.
            return plan
        # The matter has other filings, or a court case, behind it, so it keeps
        # the case it was made for. This filing has become a different one and
        # needs its own plan.
        _detach(draft)

    checklist = resolve_checklist_for_draft(draft)
    if not checklist:
        return None

    plan = FilingPlan.objects.create(
        user=draft.user,
        title=plan_title_for(draft),
        jurisdiction=draft.jurisdiction,
        court_code=draft.court_code,
        court_name=draft.court_name,
        case_category_name=draft.case_category_name,
        case_type_name=draft.case_type_name,
        filer_role=draft.filer_role,
        lead_filing_type_name=lead_filing_type_name(draft),
        checklist=checklist_snapshot(checklist),
        guidance=resolve_guidance_for_draft(draft),
    )
    draft.plan = plan
    draft.save(update_fields=["plan", "updated_at"])
    return plan


def set_checklist_answers(plan: FilingPlan, answers: dict[str, dict[str, Any]], *, keep_have=()) -> FilingPlan:
    """Record where the filer is with each document on their list.

    Only items already in the plan can be answered: the controls come from the
    plan's own snapshot, so anything else in the POST is not ours. Items in
    ``keep_have`` are held at "I have it" whatever the form said, because a
    document sitting in the envelope is not something the filer can un-have.
    """

    held = {str(item_id) for item_id in keep_have}
    checklist = dict(plan.checklist or {})
    for item_id, item in checklist.items():
        if not isinstance(item, dict):
            continue
        answer = answers.get(item_id) or {}
        status = STATUS_HAVE if item_id in held else _clean_status(answer.get("status"))
        item["status"] = status
        due_date = _clean_due_date(answer.get("due_date")) if status == STATUS_LATER else ""
        if due_date:
            item["due_date"] = due_date
        else:
            item.pop("due_date", None)
        # "complete" was the whole answer before there was more than one way to
        # be done with a document. Old plans still carry it; new writes do not.
        item.pop("complete", None)
    plan.checklist = checklist
    plan.save(update_fields=["checklist", "updated_at"])
    return plan


def status_choices() -> list[dict[str, str]]:
    """The answers a filer can give about one document, in the order offered."""

    return [
        {"value": status, "label": STATUS_LABELS[status], "short": STATUS_SHORT_LABELS[status]}
        for status in STATUS_ORDER
    ]


def checklist_answers_from_post(post, plan: FilingPlan | None) -> dict[str, dict[str, Any]]:
    """Read one answer per plan item out of a submitted form.

    Item IDs come from the plan rather than the form, so a POST can only answer
    questions this plan actually asked.
    """

    if plan is None:
        return {}
    return {
        item_id: {
            "status": post.get(f"status_{item_id}", ""),
            "due_date": post.get(f"due_{item_id}", ""),
        }
        for item_id in (plan.checklist or {})
    }


def set_checklist_progress(plan: FilingPlan, have_ids, *, keep_have=()) -> FilingPlan:
    """Say that the filer has these documents, and has said nothing about the rest."""

    return set_checklist_answers(
        plan,
        {str(item_id): {"status": STATUS_HAVE} for item_id in have_ids},
        keep_have=keep_have,
    )


# --- What the plan says versus what is actually in this envelope -------------


def attached_documents(draft: FilingDraft | None) -> dict[str, FilingDocument]:
    """Map checklist item ID -> the document in this draft that answers it."""

    if draft is None:
        return {}
    return {
        document.checklist_item_id: document
        for document in FilingDocument.objects.filter(draft=draft).exclude(checklist_item_id="")
    }


@transaction.atomic
def attach_document_to_item(draft: FilingDraft, item_id: str, document: FilingDocument) -> None:
    """Say that ``document`` is the plan item ``item_id``, in this envelope.

    One document answers one item, and one item is answered by one document, so
    claiming an item releases whatever held it before.
    """

    FilingDocument.objects.filter(draft=draft, checklist_item_id=item_id).exclude(pk=document.pk).update(
        checklist_item_id=""
    )
    document.checklist_item_id = item_id
    document.save(update_fields=["checklist_item_id", "updated_at"])


def detach_item(draft: FilingDraft, item_id: str) -> None:
    FilingDocument.objects.filter(draft=draft, checklist_item_id=item_id).update(checklist_item_id="")


def _lead_item_id(plan: FilingPlan) -> str:
    for item_id, item in (plan.checklist or {}).items():
        if isinstance(item, dict) and item.get("role") == "lead":
            return item_id
    return ""


def attach_lead_document(draft: FilingDraft, plan: FilingPlan | None) -> None:
    """Claim the checklist's lead item for the draft's main document.

    The main document is uploaded before the checklist is ever shown, so without
    this the filer would be asked to attach a document that is already the first
    thing in the envelope -- and would be warned about it at review.

    It is a guess, so it is made once. If the filer says "not this file", the
    guess stays rejected instead of coming back on the next page load.
    """

    if plan is None or (draft.supplemental_fields or {}).get(LEAD_MATCHED_FIELD):
        return
    item_id = _lead_item_id(plan)
    if not item_id or item_id in attached_documents(draft):
        return
    lead = FilingDocument.objects.filter(draft=draft, role=FilingDocument.Role.LEAD).first()
    if lead is None or lead.checklist_item_id:
        return

    attach_document_to_item(draft, item_id, lead)
    mark_item_have(plan, item_id)
    draft.supplemental_fields = {**(draft.supplemental_fields or {}), LEAD_MATCHED_FIELD: True}
    draft.save(update_fields=["supplemental_fields", "updated_at"])


def mark_item_have(plan: FilingPlan, item_id: str) -> None:
    """Say the filer has one document, without touching their other answers."""

    item = (plan.checklist or {}).get(item_id)
    if not isinstance(item, dict) or item_status(item) == STATUS_HAVE:
        return
    checklist = dict(plan.checklist)
    updated = {**item, "status": STATUS_HAVE}
    updated.pop("complete", None)
    updated.pop("due_date", None)
    checklist[item_id] = updated
    plan.checklist = checklist
    plan.save(update_fields=["checklist", "updated_at"])


def mark_attached_items_filed(draft: FilingDraft) -> None:
    """Record that this envelope's checklist documents reached the court.

    An attached document is only "I have it" while its envelope is still in
    progress. Once the filing succeeds, the plan must say "I already filed
    this" so a later envelope does not ask for the same document again.
    """

    plan = draft.plan
    if plan is None:
        return

    filed_item_ids = set(
        FilingDocument.objects.filter(draft=draft)
        .exclude(checklist_item_id="")
        .values_list("checklist_item_id", flat=True)
    )
    if not filed_item_ids:
        return

    checklist = dict(plan.checklist or {})
    changed = False
    for item_id in filed_item_ids:
        item = checklist.get(item_id)
        if not isinstance(item, dict) or item_status(item) == STATUS_FILED:
            continue
        updated = {**item, "status": STATUS_FILED}
        updated.pop("complete", None)
        updated.pop("due_date", None)
        checklist[item_id] = updated
        changed = True
    if changed:
        plan.checklist = checklist
        plan.save(update_fields=["checklist", "updated_at"])


def checklist_items(plan: FilingPlan | None, draft: FilingDraft | None = None) -> list[dict[str, Any]]:
    """Flatten a plan's checklist, in requirement order, with envelope state."""

    if plan is None:
        return []

    attached = attached_documents(draft)
    items = []
    for item_id, item in (plan.checklist or {}).items():
        if not isinstance(item, dict) or item.get("requirement") not in REQUIREMENT_ORDER:
            continue
        # A document sitting in this envelope answers the question, whatever the
        # plan last recorded.
        status = STATUS_HAVE if item_id in attached else item_status(item)
        items.append(
            {
                "id": item_id,
                "label": item.get("label") or item_id,
                "description": item.get("description", ""),
                "requirement": item.get("requirement", ""),
                "status": status,
                "status_label": STATUS_LABELS[status],
                "due_date": parse_date(item.get("due_date") or "") if status == STATUS_LATER else None,
                "settled": status in SETTLED_STATUSES,
                "attached": attached.get(item_id),
            }
        )
    items.sort(key=lambda item: REQUIREMENT_ORDER.index(item["requirement"]))
    return items


def grouped_checklist(plan: FilingPlan | None, draft: FilingDraft | None = None) -> list[dict[str, Any]]:
    """Shape a plan's checklist for the page: one group per requirement level."""

    groups: dict[str, list[dict[str, Any]]] = {level: [] for level in REQUIREMENT_ORDER}
    for item in checklist_items(plan, draft):
        groups[item["requirement"]].append(item)

    return [
        {"requirement": level, "label": REQUIREMENT_LABELS[level], "items": items}
        for level, items in groups.items()
        if items
    ]


def plan_progress(plan: FilingPlan) -> dict[str, int]:
    """Count where the filer is with the list, for a one-line summary."""

    items = checklist_items(plan)
    return {
        "total": len(items),
        "complete": sum(1 for item in items if item["settled"]),
        "later": sum(1 for item in items if item["status"] == STATUS_LATER),
        "outstanding": sum(1 for item in items if item["status"] == STATUS_NONE),
    }


def plans_for(user, jurisdiction: str) -> list[dict[str, Any]]:
    """List a filer's matters, most recently worked on first."""

    return [
        {"plan": plan, "progress": plan_progress(plan)}
        for plan in FilingPlan.objects.filter(user=user, jurisdiction=jurisdiction)
    ]


def documents_missing_from_envelope(plan: FilingPlan | None, draft: FilingDraft | None) -> list[dict[str, Any]]:
    """List plan items that are not in this filing but arguably should be.

    Two kinds of gap are worth a word before submitting: a document the filer
    has in hand but never attached, and one the court always wants that has not
    been accounted for at all. Neither blocks the filing.

    A document the filer has already filed, or has deliberately left until
    later, is not a gap. They have told us where it is; repeating the question
    at the last moment would be nagging, not helping.
    """

    if plan is None or draft is None:
        return []

    missing = []
    for item in checklist_items(plan, draft):
        if item["attached"] is not None or item["status"] in (STATUS_FILED, STATUS_LATER):
            continue
        if item["status"] == STATUS_HAVE:
            missing.append({**item, "reason": "have"})
        elif item["requirement"] == "always":
            missing.append({**item, "reason": "always"})
    return missing


# --- The court case a matter has become --------------------------------------


def link_case_to_plan(
    plan: FilingPlan,
    *,
    case_tracking_id: str,
    docket_number: str,
    case_title: str = "",
    court_code: str = "",
    court_name: str = "",
) -> FilingPlan:
    """Point a plan at a real court case, so later filings go into that case."""

    plan.case_tracking_id = str(case_tracking_id or "")[:255]
    plan.docket_number = str(docket_number or "")[:255]
    plan.case_title = str(case_title or "")[:500]
    fields = ["case_tracking_id", "docket_number", "case_title", "updated_at"]
    # A case belongs to the court that heard it; trust that over the court the
    # plan happened to be started in.
    if court_code:
        plan.court_code = str(court_code)[:100]
        plan.court_name = str(court_name or plan.court_name)[:255]
        fields += ["court_code", "court_name"]
    plan.save(update_fields=sorted(set(fields)))
    return plan


def filing_type_for_item(draft: FilingDraft, item_id: str) -> tuple[str, str]:
    """Work out what the court calls the document answering this checklist item.

    A filer who has just added their proposed order should not then have to
    guess which of the court's forty filing types it is. The plan carries the
    names a partner considers right, most preferred first, and the first one
    this court actually publishes wins -- so one configuration works across
    courts that name the same thing differently, and quietly does nothing where
    a court offers none of them.

    Returns ``("", "")`` when nothing matches, which leaves the filer choosing
    on the organize step exactly as they did before.
    """

    plan = draft.plan
    item = (plan.checklist or {}).get(item_id) if plan is not None else None
    if item is not None and item_id == "fee_waiver" and draft.court_code and draft.case_type_code:
        # Use current statewide preferences even for plans saved before an
        # updated hierarchy. Other checklist items keep their configured names.
        from efile.services.waiver_documents import waiver_filing_types

        matches = waiver_filing_types(draft)
        if not matches:
            logger.info("No fee waiver filing type found for court %s", draft.court_code)
        return (str(matches[0]["code"]), matches[0]["name"]) if matches else ("", "")
    wanted = (item or {}).get("filing_type_names") or []
    if not wanted or not (draft.court_code and draft.case_type_code):
        return "", ""

    options = _codes(
        draft.jurisdiction,
        f"{draft.court_code}/filing_types/",
        initial="false" if draft.existing_case == ExistingCase.EXISTING else "true",
        category_id=draft.case_category_code,
        type_id=draft.case_type_code,
    )
    for name in wanted:
        code = _code_for_name(options, name)
        if code:
            return code, next(
                (str(option.get("name")) for option in options if str(option.get("code")) == code),
                str(name),
            )
    logger.info(
        "No filing type on court %s matches any configured name for checklist item %r",
        draft.court_code,
        item_id,
    )
    return "", ""


def remember_case_for_plan(draft: FilingDraft) -> None:
    """Carry the case a filing was confirmed against back onto its plan."""

    plan = draft.plan
    if plan is None or not draft.previous_case_id or not draft.docket_number:
        return
    if plan.case_tracking_id == draft.previous_case_id:
        return
    link_case_to_plan(
        plan,
        case_tracking_id=draft.previous_case_id,
        docket_number=draft.docket_number,
        case_title=draft.case_title,
        court_code=draft.court_code,
        court_name=draft.court_name,
    )


# --- Starting another filing from a saved plan ------------------------------
#
# The plan remembers names. Tyler remembers codes, and changes them. Everything
# below turns the first into the second, at the moment of filing.


def _codes(jurisdiction: str, path: str, **params: Any) -> list[dict[str, Any]]:
    url = f"{settings.EFSP_URL}/jurisdictions/{jurisdiction}/codes/courts/{path}"
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
    except (OSError, ValueError):
        # Every caller wants a name resolved to a code, and can carry on without
        # one by asking the filer. Nothing here is worth failing a request over,
        # so any transport or decoding failure reads as "the court said nothing".
        # (requests' own exceptions are OSErrors; the wider catch also covers a
        # socket giving out underneath it.)
        logger.warning("Could not load %s for jurisdiction %s", url, jurisdiction)
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _code_for_name(options: list[dict[str, Any]], name: str) -> str:
    wanted = normalize_name(name)
    if not wanted:
        return ""
    for option in options:
        if normalize_name(option.get("name")) == wanted:
            return str(option.get("code") or "")
    return ""


def resolve_plan_case_codes(plan: FilingPlan) -> dict[str, str]:
    """Look up today's codes for the names a plan saved.

    Anything the court no longer publishes under that name comes back empty, and
    the filer picks it again on the confirm-filing step -- which is the honest
    outcome, and much better than filing against a stale code.
    """

    codes = {"case_category_code": "", "case_type_code": "", "lead_filing_type_code": ""}
    if not plan.court_code:
        return codes

    categories = _codes(
        plan.jurisdiction,
        f"{plan.court_code}/categories",
        fileable_only=True,
        timing="Initial",
    )
    codes["case_category_code"] = _code_for_name(categories, plan.case_category_name)
    if not codes["case_category_code"]:
        return codes

    case_types = _codes(
        plan.jurisdiction,
        f"{plan.court_code}/case_types/",
        category_id=codes["case_category_code"],
        timing="Initial",
    )
    codes["case_type_code"] = _code_for_name(case_types, plan.case_type_name)
    if not codes["case_type_code"] or not plan.lead_filing_type_name:
        return codes

    filing_types = _codes(
        plan.jurisdiction,
        f"{plan.court_code}/filing_types/",
        initial="true",
        category_id=codes["case_category_code"],
        type_id=codes["case_type_code"],
    )
    codes["lead_filing_type_code"] = _code_for_name(filing_types, plan.lead_filing_type_name)
    return codes


@transaction.atomic
def create_draft_from_plan(user, plan: FilingPlan) -> FilingDraft:
    """Start another filing in an existing matter, using today's court codes."""

    codes = resolve_plan_case_codes(plan)
    draft = create_draft(
        user=user,
        jurisdiction=plan.jurisdiction,
        current_step=WorkflowStepKey.UPLOAD_DOCUMENTS,
    )
    draft.plan = plan
    draft.court_code = plan.court_code
    draft.court_name = plan.court_name
    draft.case_category_name = plan.case_category_name
    draft.case_category_code = codes["case_category_code"]
    draft.case_type_name = plan.case_type_name
    draft.case_type_code = codes["case_type_code"]
    # Which side of the case they are on does not change between filings in one
    # matter, so it is answered once.
    draft.filer_role = plan.filer_role
    if plan.is_linked_to_a_case:
        # The matter already has a court case, so this filing goes into it. The
        # filer still gets to confirm that on the confirm-your-case step, and
        # saying no there clears this and reopens the search.
        draft.existing_case = ExistingCase.EXISTING
        draft.previous_case_id = plan.case_tracking_id
        draft.docket_number = plan.docket_number
        draft.case_title = plan.case_title
    # Otherwise whether this one opens a new case or joins an existing one is
    # the filer's answer to give, on the confirm-filing step.
    draft.save()
    return draft

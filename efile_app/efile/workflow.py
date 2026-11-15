"""Central filing workflow registry and branch-aware navigation.

The reorganized flow is stateful: new and existing cases take different paths,
party details may repeat, and case questions only appear when configured. Keep
those decisions here so templates and JavaScript do not each invent their own
redirect rules.

Legacy step strings remain recognizable so saved bookmarks can be mapped into
the reorganized flow, but there is now one canonical workflow for every draft.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from django.db.models import Q
from django.urls import reverse
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy


class ExistingCase(StrEnum):
    """Controlled vocabulary used by durable drafts and workflow branches."""

    NEW = "new"
    EXISTING = "existing"
    UNSURE = "unsure"


LEGACY_EXISTING_CASE_VALUES = {
    "no": ExistingCase.NEW,
    "yes": ExistingCase.EXISTING,
    "responding": ExistingCase.EXISTING,
}


def normalize_existing_case(value: str | ExistingCase | None) -> str:
    """Normalize old yes/no values without making legacy clients branch incorrectly."""

    if value in (None, ""):
        return ""
    normalized = str(value).strip().lower()
    return str(LEGACY_EXISTING_CASE_VALUES.get(normalized, normalized))


def legacy_existing_case_value(value: str | ExistingCase | None) -> str:
    """Translate normalized state for the old screens during the migration."""

    normalized = normalize_existing_case(value)
    if normalized == ExistingCase.NEW:
        return "no"
    if normalized == ExistingCase.EXISTING:
        return "yes"
    return normalized


class WorkflowStage(StrEnum):
    FILING = "filing"
    UPLOAD = "upload"
    CONFIRM_CASE = "confirm_case"
    CHECK_DOCUMENTS = "check_documents"
    ORGANIZE_DOCUMENTS = "organize_documents"
    PEOPLE = "people"
    SERVICE = "service"
    FEES = "fees"
    REVIEW = "review"


WORKFLOW_STAGE_LABELS = {
    # "Start", not "Filing": filers read "Filing" as the final submit step.
    WorkflowStage.FILING: pgettext_lazy("workflow stage", "Start"),
    WorkflowStage.UPLOAD: _("Upload"),
    WorkflowStage.CONFIRM_CASE: _("Confirm case"),
    WorkflowStage.CHECK_DOCUMENTS: _("Check documents"),
    WorkflowStage.ORGANIZE_DOCUMENTS: _("Organize documents"),
    WorkflowStage.PEOPLE: _("People"),
    WorkflowStage.SERVICE: _("Service"),
    WorkflowStage.FEES: _("Fees"),
    WorkflowStage.REVIEW: _("Review"),
}


class WorkflowStepKey(StrEnum):
    """Stable identifiers for both reorganized and transitional workflow steps."""

    OPTIONS = "options"
    FILING_PATH = "filing_path"
    UPLOAD_DOCUMENTS = "upload_documents"
    PREVIEW_DOCUMENTS = "preview_documents"
    EXTRACTION_REVIEW = "extraction_review"
    CASE_LOOKUP = "case_lookup"
    CASE_CONFIRMATION = "case_confirmation"
    DOCUMENT_CHECKLIST = "document_checklist"
    ORGANIZE_DOCUMENTS = "organize_documents"
    YOUR_INFORMATION = "your_information"
    PARTIES = "parties"
    PARTY_DETAILS = "party_details"
    CASE_QUESTIONS = "case_questions"
    SERVICE = "service"
    PAYMENT = "payment"
    REVIEW = "review"
    CONFIRMATION = "confirmation"

    # Compatibility aliases for pre-migration drafts and URLs. They are not
    # exposed as model choices or active workflow steps.
    UPLOAD_FIRST = "upload_first"
    CASE_INFORMATION = "case_information"
    DOCUMENTS = "documents"


@dataclass(frozen=True)
class WorkflowStep:
    key: WorkflowStepKey
    label: str | Promise
    url_name: str
    stage: WorkflowStage


FILING_WORKFLOW: tuple[WorkflowStep, ...] = (
    WorkflowStep(WorkflowStepKey.OPTIONS, _("Options"), "efile_options", WorkflowStage.FILING),
    WorkflowStep(
        WorkflowStepKey.FILING_PATH, pgettext_lazy("workflow stage", "Start"), "filing_path", WorkflowStage.FILING
    ),
    WorkflowStep(WorkflowStepKey.UPLOAD_DOCUMENTS, _("Upload documents"), "upload_documents", WorkflowStage.UPLOAD),
    WorkflowStep(
        WorkflowStepKey.EXTRACTION_REVIEW,
        _("Confirm filing"),
        "extraction_review",
        WorkflowStage.CONFIRM_CASE,
    ),
    WorkflowStep(WorkflowStepKey.CASE_LOOKUP, _("Find your case"), "case_lookup", WorkflowStage.CONFIRM_CASE),
    WorkflowStep(
        WorkflowStepKey.CASE_CONFIRMATION,
        _("Confirm your case"),
        "case_confirmation",
        WorkflowStage.CONFIRM_CASE,
    ),
    WorkflowStep(
        WorkflowStepKey.DOCUMENT_CHECKLIST,
        _("Check documents"),
        "document_checklist",
        WorkflowStage.CHECK_DOCUMENTS,
    ),
    WorkflowStep(
        WorkflowStepKey.ORGANIZE_DOCUMENTS,
        _("Organize documents"),
        "organize_documents",
        WorkflowStage.ORGANIZE_DOCUMENTS,
    ),
    WorkflowStep(
        WorkflowStepKey.YOUR_INFORMATION,
        _("Your information"),
        "your_information",
        WorkflowStage.PEOPLE,
    ),
    WorkflowStep(WorkflowStepKey.PARTIES, _("People in this filing"), "parties", WorkflowStage.PEOPLE),
    WorkflowStep(WorkflowStepKey.PARTY_DETAILS, _("Person details"), "party_details", WorkflowStage.PEOPLE),
    WorkflowStep(WorkflowStepKey.CASE_QUESTIONS, _("Case questions"), "case_questions", WorkflowStage.PEOPLE),
    WorkflowStep(WorkflowStepKey.SERVICE, _("Serve the other side"), "service", WorkflowStage.SERVICE),
    WorkflowStep(WorkflowStepKey.PAYMENT, _("Fees"), "payment", WorkflowStage.FEES),
    WorkflowStep(WorkflowStepKey.REVIEW, _("Review"), "case_review", WorkflowStage.REVIEW),
    WorkflowStep(WorkflowStepKey.CONFIRMATION, _("Confirmation"), "filing_confirmation", WorkflowStage.REVIEW),
)

_STEPS_BY_KEY = {step.key: step for step in FILING_WORKFLOW}

LEGACY_STEP_TARGETS = {
    WorkflowStepKey.PREVIEW_DOCUMENTS: WorkflowStepKey.EXTRACTION_REVIEW,
    WorkflowStepKey.UPLOAD_FIRST: WorkflowStepKey.UPLOAD_DOCUMENTS,
    WorkflowStepKey.CASE_INFORMATION: WorkflowStepKey.EXTRACTION_REVIEW,
    WorkflowStepKey.DOCUMENTS: WorkflowStepKey.ORGANIZE_DOCUMENTS,
}


def get_workflow_steps() -> tuple[WorkflowStep, ...]:
    """Return the complete target workflow for choices, docs, and tests."""

    return FILING_WORKFLOW


def get_workflow_step_choices() -> tuple[tuple[str, str | Promise], ...]:
    """Return choices for the canonical reorganized workflow."""

    return tuple((step.key.value, step.label) for step in _STEPS_BY_KEY.values())


def get_step(step_key: WorkflowStepKey | str) -> WorkflowStep:
    try:
        key = WorkflowStepKey(step_key)
        return _STEPS_BY_KEY[LEGACY_STEP_TARGETS.get(key, key)]
    except (KeyError, ValueError) as exc:
        raise KeyError(f"Unknown workflow step: {step_key}") from exc


def _draft_value(draft: Any | None, name: str, default: Any = None) -> Any:
    return getattr(draft, name, default) if draft is not None else default


def _has_incomplete_parties(draft: Any | None) -> bool:
    if draft is None:
        return False
    parties = getattr(draft, "parties", None)
    if parties is None:
        return bool(_draft_value(draft, "has_incomplete_parties", False))
    from efile.services.people import get_party_types, party_is_complete

    party_types = []
    if _draft_value(draft, "court_code") and _draft_value(draft, "case_type_code"):
        party_types = get_party_types(draft)
    if hasattr(parties, "filter"):
        return any(not party_is_complete(party, draft=draft, party_types=party_types) for party in parties.all())
    try:
        party_list = list(parties.all())
    except (AttributeError, TypeError):
        party_list = list(parties)

    return any(not party_is_complete(party, draft=draft, party_types=party_types) for party in party_list)


def _has_case_questions(draft: Any | None) -> bool:
    if draft is None:
        return False
    explicit = _draft_value(draft, "case_questions_required", None)
    if explicit is not None:
        return bool(explicit)
    return bool((_draft_value(draft, "supplemental_fields", {}) or {}).get("_case_questions_required"))


def get_visible_workflow(
    draft: Any | None = None,
    *,
    current_step: WorkflowStepKey | str | None = None,
) -> tuple[WorkflowStep, ...]:
    """Resolve the screens visible for this draft's branch."""

    existing_case = normalize_existing_case(_draft_value(draft, "existing_case"))
    current_key = None
    try:
        current_key = WorkflowStepKey(current_step or _draft_value(draft, "current_step"))
    except (TypeError, ValueError):
        pass

    from efile.services.filing_path import filing_path_question_was_asked

    visible: list[WorkflowStep] = []
    for step in FILING_WORKFLOW:
        if step.key == WorkflowStepKey.FILING_PATH:
            # A filer who chose "Start a new case" or "File into an existing
            # case" from the start menu, or started from a plan, never saw this
            # question. Back from Upload goes to where they did start, and the
            # question is only reached by choosing Change.
            if draft is not None and not filing_path_question_was_asked(draft) and step.key != current_key:
                continue
        if step.key in {WorkflowStepKey.CASE_LOOKUP, WorkflowStepKey.CASE_CONFIRMATION}:
            if existing_case != ExistingCase.EXISTING and step.key != current_key:
                continue
        if step.key == WorkflowStepKey.PARTY_DETAILS:
            if not _has_incomplete_parties(draft) and step.key != current_key:
                continue
        if step.key == WorkflowStepKey.CASE_QUESTIONS:
            if not _has_case_questions(draft) and step.key != current_key:
                continue
        visible.append(step)
    return tuple(visible)


def get_step_index(
    step_key: WorkflowStepKey | str,
    draft: Any | None = None,
) -> int:
    workflow = get_visible_workflow(draft, current_step=step_key)
    key = WorkflowStepKey(step_key)
    for index, step in enumerate(workflow):
        if step.key == key:
            return index
    raise KeyError(f"Unknown workflow step: {step_key}")


def get_previous_step(step_key: WorkflowStepKey | str, draft: Any | None = None) -> WorkflowStep | None:
    workflow = get_visible_workflow(draft, current_step=step_key)
    index = get_step_index(step_key, draft)
    if index == 0:
        return None
    return workflow[index - 1]


def get_next_step(step_key: WorkflowStepKey | str, draft: Any | None = None) -> WorkflowStep | None:
    key = WorkflowStepKey(step_key)
    if key == WorkflowStepKey.EXTRACTION_REVIEW:
        existing_case = normalize_existing_case(_draft_value(draft, "existing_case"))
        if existing_case == ExistingCase.NEW:
            return get_step(WorkflowStepKey.DOCUMENT_CHECKLIST)
        if existing_case == ExistingCase.EXISTING:
            return get_step(WorkflowStepKey.CASE_LOOKUP)
        return None

    workflow = get_visible_workflow(draft, current_step=key)
    index = get_step_index(key, draft)
    try:
        return workflow[index + 1]
    except IndexError:
        return None


def get_step_url(step_key: WorkflowStepKey | str, jurisdiction: str) -> str:
    return reverse(get_step(step_key).url_name, kwargs={"jurisdiction": jurisdiction})


# Detours ---------------------------------------------------------------------
#
# A filer sent from Review (or from the interview handoff's list of missing
# details) to change one earlier answer should come back when that step is
# finished, not walk every later screen again. The origin travels as a
# ``return_to`` marker: a hidden field, a query string, or a JSON field.
#
# Only a step's *completion* decides where to go next, through
# ``continue_url``. Intermediate actions on a screen (adding a person, picking
# a role, removing a file) keep the marker and stay on that screen. When a
# change leaves the filing needing something Review cannot accept, the filer
# stops at the step that fixes it, still carrying the marker.

RETURN_TO_REVIEW = "review"
RETURN_TO_HANDOFF = "handoff"
RETURN_TARGETS = frozenset({RETURN_TO_REVIEW, RETURN_TO_HANDOFF})


def clean_return_to(value: Any) -> str:
    """A return marker we act on, or "" for anything else."""

    return value if value in RETURN_TARGETS else ""


def return_target(request: Any) -> str:
    """The checked return marker from a form field or the query string."""

    return clean_return_to(request.POST.get("return_to") or request.GET.get("return_to", ""))


def with_return_to(url: str, return_to: str | None) -> str:
    """Carry the return marker across an intermediate redirect (e.g. to fill
    in one more required party) so it survives to reach the step it names."""

    return_to = clean_return_to(return_to)
    if not return_to:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}return_to={return_to}"


def documents_need_organizing(draft: Any) -> bool:
    """Whether a document has not been through Organize since it changed.

    Checklist attachments can have a filing type before confidentiality is
    answered. A blank document type is valid only after Organize checked that
    the court offers no choices, so it must carry that confirmation.
    """

    return draft.documents.filter(
        Q(filing_type_code="") | Q(document_type_code="", document_type_confirmed=False)
    ).exists()


def continue_step(draft: Any, return_to: str, default_step: WorkflowStepKey | str) -> WorkflowStepKey:
    """The workflow step a finished step leads to, for the draft's resume point.

    Without a marker this is the linear flow's ``default_step``. A handoff
    detour also resumes there: its own list, not a workflow step, is where the
    filer is sent (see ``continue_url``).
    """

    if clean_return_to(return_to) == RETURN_TO_REVIEW:
        return WorkflowStepKey.ORGANIZE_DOCUMENTS if documents_need_organizing(draft) else WorkflowStepKey.REVIEW
    return WorkflowStepKey(default_step)


def continue_url(draft: Any, jurisdiction: str, return_to: str, default_step: WorkflowStepKey | str) -> str:
    """Where a finished step sends the filer: back to the detour's origin, to
    the one step that fixes what the change broke, or on through the flow."""

    return_to = clean_return_to(return_to)
    if return_to == RETURN_TO_HANDOFF:
        # The handoff list re-checks everything and links to whatever is
        # still missing, so it is always the way back.
        return reverse("handoff_review", args=[draft.pk])
    step = continue_step(draft, return_to, default_step)
    url = get_step_url(step, jurisdiction)
    return url if step == WorkflowStepKey.REVIEW else with_return_to(url, return_to)


def get_resume_step_url(
    current_step: WorkflowStepKey | str | None,
    jurisdiction: str,
    draft_id: int | None = None,
) -> str | None:
    """Where "continue where you left off" goes, naming the draft it means.

    The draft is named in the URL because resuming is the filer's decision.
    Screens do not otherwise go looking for a filing to continue, so this is
    what tells them which one.
    """

    if current_step is None:
        return None
    try:
        step_key = WorkflowStepKey(current_step)
    except ValueError:
        step_key = WorkflowStepKey.FILING_PATH
    if step_key == WorkflowStepKey.OPTIONS:
        step_key = WorkflowStepKey.FILING_PATH
    step_key = LEGACY_STEP_TARGETS.get(step_key, step_key)
    url = get_step_url(step_key, jurisdiction)
    if draft_id is None:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}draft={draft_id}"


def get_workflow_context(
    current_step: WorkflowStepKey | str,
    jurisdiction: str,
    draft: Any | None = None,
) -> dict[str, Any]:
    from efile.services.filing_guidance import filing_guidance

    previous_step = get_previous_step(current_step, draft)
    next_step = get_next_step(current_step, draft)
    visible_workflow = get_visible_workflow(draft, current_step=current_step)

    stages = tuple(dict.fromkeys(step.stage for step in visible_workflow))
    current_stage = get_step(current_step).stage
    current_stage_index = stages.index(current_stage)

    return {
        "filing_guidance": filing_guidance(draft, current_step, jurisdiction=jurisdiction),
        "workflow_steps": visible_workflow,
        "workflow_stages": stages,
        "workflow_stage_progress": tuple(
            {
                "key": stage.value,
                "label": WORKFLOW_STAGE_LABELS[stage],
                "state": "complete"
                if index < current_stage_index
                else "current"
                if index == current_stage_index
                else "upcoming",
            }
            for index, stage in enumerate(stages)
        ),
        "workflow_current_step": get_step(current_step),
        "workflow_previous_step": previous_step,
        "workflow_next_step": next_step,
        "workflow_previous_url": get_step_url(previous_step.key, jurisdiction) if previous_step else None,
        "workflow_next_url": get_step_url(next_step.key, jurisdiction) if next_step else None,
    }

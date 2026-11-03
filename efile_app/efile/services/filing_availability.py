"""Deployment-owned restrictions on filing through LITEfile, matched by human-readable type names."""

import re
from urllib.parse import quote, urlencode

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.shortcuts import render
from django.utils.translation import gettext as _

from efile.models import FilingDocument
from efile.services.efsp_payload import _EfspLookups
from efile.utils.config_loader import config_loader
from efile.workflow import ExistingCase, WorkflowStepKey, get_step_url, get_workflow_context

_SELECTORS = ("case_categories", "case_types", "filing_types")


def _court_availability(jurisdiction, court):
    courts = config_loader.load_jurisdiction_config(jurisdiction).get("court_specific_requirements") or {}
    keys = [court]
    if ":" in court:
        keys.append(court.split(":", 1)[0] + ":*")
    availabilities = [(courts.get(key) or {}).get("filing_availability") or {} for key in keys]
    for availability in availabilities:
        _validate(availability)
    return availabilities


def _validate(availability):
    """Reject a malformed rule on every check, not only when a name reaches it.

    Otherwise a draft with blank saved names passes the early checks and the
    rule first fails at submit, after the draft is claimed.
    """
    for rule in availability.get("rules") or []:
        for name in _SELECTORS:
            matchers = rule.get(name)
            if matchers is None:
                continue
            if not isinstance(matchers, list):
                raise ImproperlyConfigured(f"Filing availability {name} must be a list of names.")
            for matcher in matchers:
                if isinstance(matcher, str):
                    continue
                if not (isinstance(matcher, dict) and isinstance(matcher.get("regex"), str)):
                    raise ImproperlyConfigured("Availability selectors must contain names or {regex: pattern} entries.")
                try:
                    re.compile(matcher["regex"])
                except re.error as error:
                    raise ImproperlyConfigured(f"Invalid filing availability regex: {matcher['regex']!r}") from error


def _selectors(availabilities):
    """The selectors this court's rules actually use."""
    return {
        name
        for availability in availabilities
        for rule in availability.get("rules") or []
        for name in _SELECTORS
        if rule.get(name)
    }


def _matches_name(name, matcher):
    """Strings match exactly; explicit regex entries match the entire name."""
    if not name:
        return False
    return name == matcher if isinstance(matcher, str) else re.fullmatch(matcher["regex"], name) is not None


def filing_unavailable_message(jurisdiction, court, *, case_category="", case_type="", filing_types=()):
    """Return a reason, or an empty string when no restriction matches.

    Exact court settings are checked before a county prefix (``cook:*``).
    Rules are additive: an exact court cannot enable a county-wide restriction.
    Values within a selector are alternatives; selectors within a rule must all
    match. Category, case-type, and filing-type values are human-readable names,
    never Tyler numeric IDs. Matching is case-sensitive; regex flags are explicit.
    """
    return _unavailable_message(
        _court_availability(jurisdiction, court),
        case_category=case_category,
        case_type=case_type,
        filing_types=filing_types,
    )


def _unavailable_message(availabilities, *, case_category="", case_type="", filing_types=()):
    selections = {
        "case_categories": {str(case_category or "").strip()} - {""},
        "case_types": {str(case_type or "").strip()} - {""},
        "filing_types": {str(value).strip() for value in filing_types if value},
    }
    for availability in availabilities:
        fallback = availability.get("message") or _(
            "LITEfile cannot submit this filing to this court right now. Contact the court clerk to ask how to file."
        )
        # Specific explanations take precedence over the court's generic one.
        for rule in availability.get("rules") or []:
            # An empty selector (``case_types:``) matches nothing, like ``[]``.
            selectors = [name for name in selections if name in rule]
            if selectors and all(
                any(_matches_name(value, matcher) for value in selections[name] for matcher in rule[name] or [])
                for name in selectors
            ):
                return rule.get("message") or fallback
        if availability.get("enabled") is False:
            return fallback
    return ""


def draft_unavailable_message(draft):
    if draft.deletion_pending:
        return "A verified data deletion is in progress for this filing."
    availabilities = _court_availability(draft.jurisdiction, draft.court_code)
    # Most courts have no filing-type rule; skip the document query for them.
    filing_types = (
        FilingDocument.objects.filter(draft=draft).values_list("filing_type_name", flat=True)
        if "filing_types" in _selectors(availabilities)
        else ()
    )
    return _unavailable_message(
        availabilities,
        case_category=draft.case_category_name,
        case_type=draft.case_type_name,
        filing_types=filing_types,
    )


def outgoing_unavailable_message(jurisdiction, court, case_data, payload):
    """Resolve outgoing IDs to the court's names; never trust client labels at submit.

    Lookups are only needed for selectors configured for this court. A failed
    lookup blocks submission, rather than letting an unknown name evade a rule.
    """
    availabilities = _court_availability(jurisdiction, court)
    selectors = _selectors(availabilities)
    if not selectors:
        return _unavailable_message(availabilities)
    lookups = _EfspLookups()
    base = f"{settings.EFSP_URL}/jurisdictions/{quote(jurisdiction, safe='')}/codes/courts/{quote(court, safe=':')}"
    category = payload.get("efile_case_category") or case_data.get("case_category", "")
    case_type = payload.get("efile_case_type") or case_data.get("case_type", "")
    initial = not (payload.get("previous_case_id") or case_data.get("previous_case_id"))

    def resolve(path, codes):
        choices = lookups.get(f"{base}/{path}")
        if not isinstance(choices, list):
            choices = []
        names = {
            str(item["code"]): item["name"]
            for item in choices
            if isinstance(item, dict) and "code" in item and isinstance(item.get("name"), str)
        }
        if any(not names.get(str(code)) for code in codes):
            raise ValueError(_("We could not confirm this filing's availability with the court. Try again later."))
        return [names[str(code)] for code in codes]

    category_name = resolve("categories", [category])[0] if "case_categories" in selectors else ""
    type_name = (
        resolve("case_types/?" + urlencode({"category_id": category}), [case_type])[0]
        if "case_types" in selectors
        else ""
    )
    filing_names = []
    if "filing_types" in selectors:
        bundles = payload.get("al_court_bundle", [])
        if not isinstance(bundles, list) or not all(isinstance(item, dict) for item in bundles):
            raise ValueError(_("We could not read the filing codes. Reload the review page and try again."))
        query = urlencode({"initial": str(initial).lower(), "category_id": category, "type_id": case_type})
        filing_names = resolve(f"filing_types/?{query}", [item.get("filing_type", "") for item in bundles])
    return _unavailable_message(
        availabilities,
        case_category=category_name,
        case_type=type_name,
        filing_types=filing_names,
    )


def unavailable_response(request, draft, message):
    """Keep the draft intact and offer corrections without suggesting a false court."""
    case_step = (
        WorkflowStepKey.CASE_LOOKUP
        if draft.existing_case == ExistingCase.EXISTING
        else WorkflowStepKey.EXTRACTION_REVIEW
    )
    context = {
        "is_logged_in": True,
        "draft": draft,
        "availability_message": message,
        "change_case_url": get_step_url(case_step, draft.jurisdiction),
        "change_documents_url": get_step_url(WorkflowStepKey.ORGANIZE_DOCUMENTS, draft.jurisdiction),
    }
    context.update(get_workflow_context(draft.current_step, draft.jurisdiction, draft))
    return render(request, "efile/filing_unavailable.html", context, status=403)

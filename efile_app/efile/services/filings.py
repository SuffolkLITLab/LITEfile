"""What the court says about filings this account has already sent.

Everything here reads the EFSP's record rather than ours. Two shapes come back
from it: the *filing list*, which is thin (status, dates, case number) and is
what "My cases" is built from, and the *filing detail*, which is where a clerk's
rejection comment and the links to the actual documents live.

The detail payload is ECF 4 XML rendered into JSON, so it is nested three or
four levels deep in ``{"value": {"value": ...}}`` wrappers and every branch can
be null. ``describe_filing_detail`` flattens exactly the parts a filer needs
into plain dictionaries; screens never walk the raw payload themselves.

The document links come from Tyler, are unauthenticated but unguessable, and
stop working 90 days after filing -- which is why the filer is told that rather
than left to find out. We do not keep copies of filed documents ourselves yet.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from typing import Any

import requests
from django.conf import settings

from efile.api.filing_views import get_tyler_token, list_filing_data
from efile.models import ArchivedCase
from efile.services.filing_timing import add_tracker_timing
from efile.utils.config_loader import config_loader
from efile.utils.efsp_security import validated_code, validated_jurisdiction
from efile.utils.proxy_connection import get_headers

logger = logging.getLogger(__name__)

# How far back "My cases" looks. Tyler requires a start date, and a filer who
# has been with a matter for years should still see where it started.
CASE_HISTORY_YEARS = 5

# Tyler deletes the download links it hands out this long after a filing.
DOCUMENT_LINK_DAYS = 90

# Court code lists change rarely and cost a round trip each time.
_COURT_NAME_TTL_SECONDS = 60 * 60
_court_name_cache: dict[str, tuple[float, dict[str, str]]] = {}


STATUS_PRESENTATION: dict[str, dict[str, str]] = {
    "accepted": {"label": "Accepted", "icon": "fa-file-circle-check", "tone": "accepted"},
    "rejected": {"label": "Returned for changes", "icon": "fa-file-circle-exclamation", "tone": "rejected"},
    "returned": {"label": "Returned for changes", "icon": "fa-file-circle-exclamation", "tone": "rejected"},
    "under-review": {"label": "Under review", "icon": "fa-file-pen", "tone": "pending"},
    "reviewed": {"label": "Reviewed", "icon": "fa-file-pen", "tone": "pending"},
    "submitted": {"label": "Received by the e-filing system", "icon": "fa-file", "tone": "pending"},
    "submitting": {"label": "In progress", "icon": "fa-paper-plane", "tone": "pending"},
    "receipted": {"label": "Received by the court", "icon": "fa-file", "tone": "pending"},
    "served": {"label": "Served", "icon": "fa-envelope-circle-check", "tone": "muted"},
    "cancelled": {"label": "Stopped", "icon": "fa-file-circle-xmark", "tone": "muted"},
    "failed": {"label": "Did not go through", "icon": "fa-file-circle-xmark", "tone": "rejected"},
}

# A code Tyler added that we don't know yet. The court did send it.
UNKNOWN_STATUS = {"label": "Status update from the court", "icon": "fa-file", "tone": "muted"}
# No status at all: a case with no filings listed, or a blank code.
NO_STATUS = {"label": "Sent to the court", "icon": "fa-file", "tone": "pending"}

TRACKER_STAGES = ("Sent", "Received", "Clerk review")


def filing_tracker(
    status_code: Any, *, submitted_at=None, accepted_at=None, status_at=None, timing=None
) -> dict[str, Any]:
    """Return a conservative filing progress summary for the detail screen."""
    code = str(status_code or "").strip().lower()
    stage_by_status = {
        "submitting": (0, "Your filing is being sent to the court. No action is needed right now."),
        "submitted": (1, "The e-filing system received your filing. No action is needed right now."),
        "receipted": (1, "The court received your filing. No action is needed while it waits for clerk review."),
        "under-review": (2, "A court clerk is reviewing your filing. No action is needed right now."),
        "reviewed": (2, "Clerk review is complete. No action is needed while you wait for the court's decision."),
        "accepted": (3, "Download the court copy below if you need it."),
        "rejected": (3, "Review the clerk's comments below."),
        "returned": (3, "Review the clerk's comments below."),
    }
    if code in {"failed", "cancelled"}:
        summary = "This filing did not go through." if code == "failed" else "This filing was cancelled."
        return {
            "state": "stopped",
            "label": status_presentation(code)["label"],
            "summary": summary,
            "stages": [],
        }
    if code == "served":
        return {
            "state": "neutral",
            "label": "Served",
            "summary": "The filing was served. This does not show whether the court accepted it.",
            "stages": [],
        }
    if not code:
        return {
            "state": "neutral",
            "label": NO_STATUS["label"],
            "summary": "The court has not sent a status update yet.",
            "stages": [],
        }
    current = stage_by_status.get(code)
    if current is None:
        return {
            "state": "neutral",
            "label": UNKNOWN_STATUS["label"],
            "summary": "The court sent a status update. Check the details below.",
            "stages": [],
        }
    index, summary = current
    outcome_label = {
        "accepted": "Accepted",
        "rejected": "Returned for changes",
        "returned": "Returned for changes",
    }.get(code, "Court decision")
    stages = (*TRACKER_STAGES, outcome_label)
    tracker = {
        "state": "decision" if index == 3 else "progress",
        "label": status_presentation(code)["label"],
        "summary": summary,
        "stages": [{"label": stage, "complete": i < index, "current": i == index} for i, stage in enumerate(stages)],
    }
    return add_tracker_timing(
        tracker, code, submitted_at=submitted_at, accepted_at=accepted_at, status_at=status_at, timing=timing
    )


def status_presentation(status_code: Any) -> dict[str, str]:
    """How one filing status is named and coloured for a filer.

    Tyler publishes a longer list of codes than any one court uses, and adds to
    it. An unrecognized code still describes a real filing, so it gets neutral
    wording instead of being dropped.
    """

    code = str(status_code or "").strip().lower()
    if not code:
        return NO_STATUS
    return STATUS_PRESENTATION.get(code, UNKNOWN_STATUS)


def _dig(node: Any, *keys: str) -> Any:
    """Follow a path through the payload, stopping at the first missing branch."""

    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _text(node: Any, *keys: str) -> str:
    """Read one ``{"value": ...}``-wrapped string, or "" when it is not there."""

    value = _dig(node, *keys) if keys else node
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, dict):
        value = value.get("value")
    return "" if value is None else str(value)


def _timestamp(node: Any) -> datetime | None:
    """Turn one of the payload's epoch-millisecond dates into a datetime."""

    raw = _dig(node, "dateRepresentation", "value", "value")
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _event_timestamp(node: Any) -> datetime | None:
    # A date-only NIEM value does not establish the time a stage was reached.
    representation = _dig(node, "dateRepresentation") or {}
    if isinstance(representation, dict) and str(representation.get("name", "")).endswith("}Date"):
        return None
    return _timestamp(node)


def history_start_date() -> str:
    """The oldest filing date "My cases" asks the court for."""

    today = date.today()
    try:
        return today.replace(year=today.year - CASE_HISTORY_YEARS).isoformat()
    except ValueError:  # February 29
        return today.replace(year=today.year - CASE_HISTORY_YEARS, day=28).isoformat()


def court_names(jurisdiction: str) -> dict[str, str]:
    """Map court codes to the names a person recognizes.

    The filing list names courts by code ("cook:cd1"). This is display only, so
    a failure here costs the reader a friendly name and nothing else.
    """

    cached = _court_name_cache.get(jurisdiction)
    now = time.monotonic()
    if cached and now - cached[0] < _COURT_NAME_TTL_SECONDS:
        return cached[1]

    url = f"{settings.EFSP_URL}/jurisdictions/{jurisdiction}/codes/courts/"
    try:
        response = requests.get(url, params={"with_names": True}, timeout=10)
        response.raise_for_status()
        data = response.json()
    except (OSError, ValueError):
        logger.warning("Could not load court names for jurisdiction %s", jurisdiction)
        return cached[1] if cached else {}

    names = {
        str(court["code"]): str(court.get("name") or court["code"])
        for court in data
        if isinstance(court, dict) and court.get("code")
    }
    _court_name_cache[jurisdiction] = (now, names)
    return names


def court_contact(jurisdiction: str, court_code: str, config: dict | None = None) -> dict[str, str]:
    """Who to ask about a filing at this court.

    A filer whose filing was rejected needs a person, not a status code. The
    court code lists carry no contact details at all, so this comes from the
    partner's configuration: a ``contact`` block under the court in
    ``court_specific_requirements``, falling back to the jurisdiction-wide help
    line. Nothing is invented -- a court with no configured contact simply shows
    no contact.
    """

    config = config if config is not None else config_loader.load_jurisdiction_config(jurisdiction) or {}
    court = (config.get("court_specific_requirements") or {}).get(court_code) or {}
    contact = court.get("contact") or {}
    # A name alone is not a usable local contact. Never combine a clerk's name
    # with a statewide number or website: fall back as a complete unit.
    if any(contact.get(key) for key in ("phone", "url", "email")):
        return {key: str(contact[key]) for key in ("name", "phone", "url", "email") if contact.get(key)}
    jurisdiction_help = config.get("jurisdiction") or {}
    return {
        key: str(jurisdiction_help[source])
        for key, source in (("name", "help_name"), ("url", "help_url"), ("phone", "help_number"))
        if jurisdiction_help.get(source)
    }


def archived_case_ids(user, jurisdiction: str) -> set[str]:
    return set(
        ArchivedCase.objects.filter(user=user, jurisdiction=jurisdiction).values_list("case_tracking_id", flat=True)
    )


def archive_case(user, jurisdiction: str, case_tracking_id: str, *, docket_number: str = "", case_title: str = ""):
    """Tidy one case away. Archiving twice is not an error."""

    archived, _ = ArchivedCase.objects.get_or_create(
        user=user,
        jurisdiction=jurisdiction,
        case_tracking_id=case_tracking_id,
        defaults={"docket_number": docket_number[:255], "case_title": case_title[:500]},
    )
    return archived


def unarchive_case(user, jurisdiction: str, case_tracking_id: str) -> int:
    deleted, _ = ArchivedCase.objects.filter(
        user=user, jurisdiction=jurisdiction, case_tracking_id=case_tracking_id
    ).delete()
    return deleted


def describe_filing(filing: dict[str, Any], names: dict[str, str] | None = None) -> dict[str, Any]:
    """One row of the filing list, ready to render."""

    names = names or {}
    court_code = str(filing.get("court_code") or "")
    return {
        "filing_id": filing.get("filing_id", ""),
        "envelope_id": filing.get("envelope_id", ""),
        "court_code": court_code,
        "court_name": names.get(court_code, court_code),
        "filing_code": filing.get("filing_code", ""),
        "status": filing.get("filing_status", ""),
        "status_presentation": status_presentation(filing.get("filing_status")),
        "received_at": _epoch_millis_to_datetime(filing.get("received_timestamp")),
        "filed_at": _epoch_millis_to_datetime(filing.get("filed_timestamp")),
    }


def _epoch_millis_to_datetime(raw: Any) -> datetime | None:
    if raw in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _sort_key(entry: dict[str, Any]) -> float:
    latest = entry.get("latest_activity")
    return latest.timestamp() if latest else 0.0


def cases_for_user(
    request,
    jurisdiction: str,
    *,
    start_date: str | None = None,
) -> list[dict[str, Any]]:
    """Group this account's filing history into the cases it belongs to.

    A filer thinks in cases, not envelopes: an eviction with six filings is one
    thing they are dealing with. Filings that never reached a case (rejected
    before the court indexed one) keep their own entry rather than vanishing.
    """

    filings = list_filing_data(request, jurisdiction, start_date=start_date or history_start_date())
    from efile.services.analytics import record_court_statuses

    record_court_statuses(request.user, jurisdiction, filings)
    names = court_names(jurisdiction)
    archived = archived_case_ids(request.user, jurisdiction)

    from efile.services.handoff import matter_keys

    linked_matters = matter_keys(request.user, jurisdiction, filings)
    cases: dict[str, dict[str, Any]] = {}
    for filing in filings:
        tracking_id = str(filing.get("case_tracking_id") or "")
        key = (
            linked_matters.get(str(filing.get("filing_id", "")))
            or tracking_id
            or f"filing:{filing.get('filing_id') or filing.get('envelope_id') or len(cases)}"
        )
        described = describe_filing(filing, names)
        entry = cases.setdefault(
            key,
            {
                "case_tracking_id": tracking_id,
                "case_title": filing.get("case_title", ""),
                "docket_number": filing.get("case_number", ""),
                "court_code": described["court_code"],
                "court_name": described["court_name"],
                "filings": [],
                "latest_activity": None,
                "is_archived": bool(tracking_id) and tracking_id in archived,
            },
        )
        # The court fills in a case title and number once it indexes the case,
        # so later filings in the same case know more than the first one did.
        entry["case_tracking_id"] = entry["case_tracking_id"] or tracking_id
        entry["is_archived"] = bool(entry["case_tracking_id"]) and entry["case_tracking_id"] in archived
        entry["case_title"] = entry["case_title"] or filing.get("case_title", "")
        entry["docket_number"] = entry["docket_number"] or filing.get("case_number", "")
        entry["filings"].append(described)
        activity = described["received_at"] or described["filed_at"]
        if activity and (entry["latest_activity"] is None or activity > entry["latest_activity"]):
            entry["latest_activity"] = activity

    for entry in cases.values():
        entry["filings"].sort(
            key=lambda filing: filing["received_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True
        )
        entry["filing_count"] = len(entry["filings"])
        entry["latest_status"] = entry["filings"][0]["status_presentation"] if entry["filings"] else NO_STATUS

    return sorted(cases.values(), key=_sort_key, reverse=True)


def fetch_filing_detail(request, jurisdiction: str, court_code: str, filing_id: str) -> dict[str, Any] | None:
    """Ask the EFSP for everything the court knows about one filing.

    The filing ID is Tyler's FILINGID (a GUID), not the envelope number: the
    detail endpoint rejects the envelope number with a 422.
    """

    jurisdiction = validated_jurisdiction(jurisdiction)
    court_code = validated_code(court_code)
    filing_id = validated_code(filing_id)
    url = f"{settings.EFSP_URL}/jurisdictions/{jurisdiction}/filingreview/courts/{court_code}/filings/{filing_id}"
    headers = get_headers()
    tyler_token = get_tyler_token(request, jurisdiction)
    if tyler_token:
        headers[f"tyler-token-{jurisdiction}"] = tyler_token
    else:
        logger.info("No Tyler token for jurisdiction %s in filing-detail request", jurisdiction)
        return None

    response = requests.get(url, headers=headers, timeout=30, allow_redirects=False)
    if not response.ok:
        logger.info(
            "Filing detail request failed: status=%s court=%s jurisdiction=%s",
            response.status_code,
            court_code,
            jurisdiction,
        )
        return None
    payload = response.json()
    return payload if isinstance(payload, dict) else None


# What Tyler calls each copy of a document it hands back, and what that copy is
# to the filer. "Original" is what they uploaded; anything the court transmitted
# or filed is the court's own copy, which carries the file stamp once accepted.
_SUBMITTED_PREFIXES = ("original",)
_COURT_PREFIXES = ("transmitted", "filed", "stamped", "service")


def _attachment_kind(description: str) -> str:
    label = description.strip().lower()
    if label.startswith(_SUBMITTED_PREFIXES):
        return "submitted"
    if label.startswith(_COURT_PREFIXES):
        return "court"
    return "other"


def _describe_attachment(attachment: Any, *, accepted: bool) -> dict[str, str] | None:
    url = _text(attachment, "binaryLocationURI")
    description = _text(attachment, "binaryDescriptionText")
    if not url:
        return None
    kind = _attachment_kind(description)
    # Tyler writes "Original - petition.pdf"; the half after the dash is the file.
    filename = description.split(" - ", 1)[1] if " - " in description else description
    if kind == "submitted":
        label = "The copy you sent"
    elif kind == "court":
        label = "The court's file-stamped copy" if accepted else "The copy the court received"
    else:
        label = description or "Document"
    return {"kind": kind, "label": label, "filename": filename, "description": description, "url": url}


def _describe_document_comments(document: Any) -> list[dict[str, str]]:
    """The clerk's own words about one document, when there are any.

    Tyler puts the comment in ``statusText`` and the *kind* of comment in
    ``statusDescriptionText`` ("RejectComments", "AcceptComments"), and sends
    the kind with an empty comment far more often than not.
    """

    status = _dig(document, "documentStatus") or {}
    text = _text(status, "statusText").strip()
    if not text:
        return []
    kinds = [_text(entry).strip().lower() for entry in (_dig(status, "statusDescriptionText") or [])]
    if any("reject" in kind for kind in kinds):
        kind, heading = "rejection", "Why the court rejected this"
    elif any("accept" in kind for kind in kinds):
        kind, heading = "acceptance", "Note from the court"
    else:
        kind, heading = "other", "Note from the court"
    comment = {"kind": kind, "heading": heading, "text": text}
    description = _text(document, "documentDescriptionText")
    if description:
        comment["document_name"] = description
    return [comment]


def _describe_fees(payload: dict[str, Any]) -> list[dict[str, str]]:
    """The charges on the envelope, keeping only the lines that are not zero.

    Tyler returns a dozen totals for every filing, almost all of them $0.00. A
    filer wants to know what they were charged, so a zero-fee filing says that
    once rather than twelve times.
    """

    charges: list[dict[str, str]] = []
    for fee_group in payload.get("envelopeFees") or []:
        for charge in _dig(fee_group, "allowanceCharge") or []:
            reason = _text(charge, "allowanceChargeReason")
            amount = _dig(charge, "amount", "value")
            if reason and isinstance(amount, int | float) and amount:
                charges.append({"reason": reason, "amount": f"{amount:.2f}"})
    return charges


def describe_filing_detail(
    payload: dict[str, Any] | None, names: dict[str, str] | None = None, *, timing=None
) -> dict[str, Any] | None:
    """Flatten the EFSP's filing detail into what one screen needs.

    Every field here is optional in the payload, and several of them are absent
    on filings the court has not looked at yet. Nothing raises: a missing branch
    reads as "the court did not say".
    """

    if not payload:
        return None

    names = names or {}
    status_code = _text(payload, "filingStatus", "filingStatusCode")
    accepted = status_code.strip().lower() == "accepted"
    identifiers = {
        _text(entry, "identificationCategory", "value").strip().upper(): _text(entry, "identificationID")
        for entry in payload.get("documentIdentification") or []
    }
    court_code = _text(_dig(payload, "caseCourt", "organizationIdentification", "value"), "identificationID")
    case = _dig(payload, "case", "value") or {}

    documents = []
    for document in payload.get("filingLeadDocument") or []:
        attachments = [
            described
            for rendition in document.get("documentRendition") or []
            for attachment in _dig(rendition, "documentRenditionMetadata", "documentAttachment") or []
            if (described := _describe_attachment(attachment, accepted=accepted))
        ]
        documents.append(
            {
                "description": _text(document, "documentDescriptionText"),
                "comments": _describe_document_comments(document),
                "attachments": attachments,
            }
        )

    submitter = _dig(payload, "documentSubmitter", "entityRepresentation", "value") or {}
    submitter_name = " ".join(
        part
        for part in (
            _text(submitter, "personName", "personGivenName"),
            _text(submitter, "personName", "personSurName"),
        )
        if part
    )

    return {
        "filing_id": identifiers.get("FILINGID", ""),
        "envelope_id": identifiers.get("ENVELOPEID", ""),
        "status": status_code,
        "can_correct": str(status_code or "").strip().lower() in {"rejected", "returned"},
        "status_presentation": status_presentation(status_code),
        "tracker": filing_tracker(
            status_code,
            submitted_at=_event_timestamp(payload.get("filingSubmissionDate")),
            accepted_at=_event_timestamp(payload.get("filingAcceptDate")),
            status_at=_event_timestamp(_dig(payload, "filingStatus", "statusDate")),
            timing=timing,
        ),
        "status_description": _text(next(iter(_dig(payload, "filingStatus", "statusDescriptionText") or []), None)),
        "court_code": court_code,
        "court_name": names.get(court_code, court_code),
        "case_title": _text(case, "caseTitleText"),
        "docket_number": _text(case, "caseDocketID"),
        "submitted_at": _timestamp(payload.get("filingSubmissionDate")),
        "accepted_at": _timestamp(payload.get("filingAcceptDate")),
        "submitter_name": submitter_name,
        "submitter_firm": _text(submitter, "firmName"),
        "fees": _describe_fees(payload),
        "fee_waiver": bool(_dig(payload, "payment", "waiverIndicator", "value")),
        "documents": documents,
        "comments": [comment for document in documents for comment in document["comments"]],
    }

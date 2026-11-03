"""The complete, reviewable set of details requested from a lead document."""

import re

from efile.utils.prompt_config import load_prompt

DOCUMENT_EXTRACTION_PROMPT = load_prompt("document_evidence_extraction")
COMMON_EXTRACTION_FIELDS: dict[str, str] = dict(DOCUMENT_EXTRACTION_PROMPT["fields"])

EXTRACTION_FIELDS: dict[str, dict[str, str]] = {
    jurisdiction: dict(COMMON_EXTRACTION_FIELDS) for jurisdiction in ("illinois", "massachusetts", "vermont", "default")
}

EXTRACTION_HINTS: dict[str, str] = {
    jurisdiction: "Extract source evidence without guessing an e-filing taxonomy value."
    for jurisdiction in ("illinois", "massachusetts", "vermont", "default")
}

EXTRACTION_LABELS = {
    "document title": "Document title",
    "form name": "Form name",
    "form identifier": "Form number or ID",
    "form revision": "Form revision",
    "form purpose": "What the form asks the court to do",
    "court": "Court or county",
    "filing type": "Filing code",
    "case category": "Case category",
    "case type": "Case type",
    "docket number": "Case number",
    "case title": "Case title",
    "plaintiff or petitioner names": "Plaintiff or petitioner names",
    "defendant or respondent names": "Defendant or respondent names",
    "other party names": "Other names mentioned in the document",
    "document date": "Document date",
    "filing phase": "Filing phase",
    "requested relief": "Requested relief",
    "monetary amounts": "Monetary amounts",
    "selected options": "Selected options",
    "docket code evidence": "Docket code evidence",
    "classification evidence": "Classification evidence",
}

EXTRACTION_KEY_ALIASES = {
    "plaintiff name": "plaintiff or petitioner names",
    "plaintiff names": "plaintiff or petitioner names",
    "petitioner name": "plaintiff or petitioner names",
    "petitioner names": "plaintiff or petitioner names",
    "plaintiff/petitioner names": "plaintiff or petitioner names",
    "plaintiff and petitioner names": "plaintiff or petitioner names",
    "defendant name": "defendant or respondent names",
    "defendant names": "defendant or respondent names",
    "respondent name": "defendant or respondent names",
    "respondent names": "defendant or respondent names",
    "defendant/respondent names": "defendant or respondent names",
    "defendant and respondent names": "defendant or respondent names",
    "other parties": "other party names",
    "other party name": "other party names",
}


def _normalized_key(raw_key):
    key = str(raw_key).strip().lower()
    return EXTRACTION_KEY_ALIASES.get(key, key)


_PLACEHOLDERS = frozenset(
    {
        "unknown",
        "none",
        "null",
        "n a",
        "na",
        "not applicable",
        "not available",
        "not provided",
        "not specified",
        "not stated",
        "not found",
        "not listed",
        "unspecified",
        "blank",
        "empty",
        "tbd",
        "to be determined",
    }
)


def clean_extracted_value(value):
    """Discard missing-value answers, recursively, without matching inside facts.

    Keep meaningful zero/false answers and names such as All Unknown Occupants.
    This also runs at display time so older saved extractions are covered.
    """
    if isinstance(value, str):
        value = value.strip()
        comparable = re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
        return None if not value or comparable in _PLACEHOLDERS else value
    if isinstance(value, dict):
        return {
            key: cleaned for key, item in value.items() if (cleaned := clean_extracted_value(item)) is not None
        } or None
    if isinstance(value, list | tuple | set):
        return [cleaned for item in value if (cleaned := clean_extracted_value(item)) is not None] or None
    return value


def normalize_extracted_fields(found_fields):
    """Keep supported extracted values and normalize keys used by the workflow."""
    if not isinstance(found_fields, dict):
        return {}

    normalized = {}
    for raw_key, value in found_fields.items():
        value = clean_extracted_value(value)
        if value in (None, "", [], {}):
            continue
        key = _normalized_key(raw_key)
        if key in {"court name", "court unit", "court or county"}:
            key = "court"
        elif key in {"docker number", "case number"}:
            key = "docket number"
        if isinstance(value, dict):
            value = "; ".join(f"{item_key}: {item_value}" for item_key, item_value in value.items())
        elif isinstance(value, list | tuple | set):
            value = "; ".join(str(item) for item in value)
        elif not isinstance(value, str):
            value = str(value)
        normalized[key] = value.strip()
    return normalized


def normalize_document_evidence(found_fields):
    """Normalize evidence keys without destroying arrays and evidence objects."""
    if not isinstance(found_fields, dict):
        return {}
    normalized = {}
    for raw_key, value in found_fields.items():
        value = clean_extracted_value(value)
        if value in (None, "", [], {}):
            continue
        key = _normalized_key(raw_key)
        if key in {"court unit", "court or county"}:
            key = "court name"
        elif key in {"monetary amount", "amount in controversy"}:
            key = "monetary amounts"
        normalized[key] = value
    return normalized


def display_extracted_fields(found_fields):
    """Flatten structured evidence only at the user-interface boundary."""
    if not isinstance(found_fields, dict):
        return {}
    display = {}
    for raw_key, value in found_fields.items():
        value = clean_extracted_value(value)
        if value in (None, "", [], {}):
            continue
        key = _normalized_key(raw_key)
        if key in {"court name", "court unit", "court or county"}:
            key = "court"
        if isinstance(value, list):
            rendered = []
            for item in value:
                if isinstance(item, dict):
                    label = item.get("label")
                    raw = item.get("raw") or item.get("amount")
                    rendered.append(": ".join(str(part) for part in (label, raw) if part))
                else:
                    rendered.append(str(item))
            display[key] = "; ".join(part for part in rendered if part)
        elif isinstance(value, dict):
            display[key] = "; ".join(f"{item_key}: {item_value}" for item_key, item_value in value.items())
        else:
            display[key] = str(value).strip()
    return {key: value for key, value in display.items() if value}


# The few items that answer the only question this part of the screen is for:
# did we read the document you meant to send? Nothing else is shown as
# read-only text: a value the filing uses is asked for by the form on the same
# screen, where it can be corrected, and one it does not use is left out.
DOCUMENT_SUMMARY_KEYS: tuple[str, ...] = (
    "document title",
    "form name",
    "form identifier",
    "form purpose",
)


def extracted_details(guesses):
    """Return every extracted item in a stable, user-facing order."""
    guesses = display_extracted_fields(guesses or {})
    ordered_keys = [*EXTRACTION_LABELS, *(key for key in guesses if key not in EXTRACTION_LABELS)]
    return [
        {
            "key": key,
            "label": EXTRACTION_LABELS.get(key, key.replace("_", " ").capitalize()),
            "value": guesses[key],
        }
        for key in ordered_keys
        if guesses.get(key) not in (None, "", [], {})
    ]


def document_summary_details(guesses):
    """The handful of items that identify which document was read."""

    by_key = {detail["key"]: detail for detail in extracted_details(guesses)}
    return [by_key[key] for key in DOCUMENT_SUMMARY_KEYS if key in by_key]

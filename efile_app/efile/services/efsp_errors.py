"""Turn an EFSP error response into one line worth showing a filer.

The proxy answers a bad payload with a machine-readable description of which
fields are wrong rather than with an ``error`` string::

    {"required_vars": [],
     "optional_vars": [...],
     "wrong_vars": [{"name": "al_court_bundle.elements[0].filing_type",
                     "description": "What filing type is this??",
                     "datatype": "choice", "currentVal": "", "choices": [...]}]}

Reading only ``error`` out of that leaves the filer with "API returned status
400" and no way to act, while the response says exactly which document is
missing exactly which field. Shared by the fee quote and the submission so both
describe the same rejection the same way.

Some rejections instead arrive as a single free-text sentence (a "Malformed
Interview" body, or a plain ``error`` string) written for a developer reading
the proxy's logs, not a filer. ``_KNOWN_MESSAGE_HINTS`` recognizes the ones
that come up in practice -- lifted from the literal strings the proxy raises,
in ~/EfileProxyServer (see e.g. ``Ecf4Filer.java`` and
``FilingInformationDocassembleJacksonDeserializer.java``) -- and appends a
sentence saying what to actually do about it. Unrecognized messages still pass
through unchanged rather than being hidden.
"""

import json
import re
from collections.abc import Callable
from urllib.parse import urlencode

from django.urls import reverse

from efile.services.efsp_validation import FIELD_LABELS

# Field names the EFSP uses, in the words the UI uses for them.
_FIELD_LABELS = {
    "document_type": "document type",
    "efile_case_category": "case category",
    "efile_case_subtype": "case subtype",
    "efile_case_type": "case type",
    "filing_component": "filing component",
    "filing_parties": "filing parties",
    "filing_type": "filing code",
    "party_type": "party type",
    "previous_case_id": "case number",
    "tyler_payment_id": "payment account",
}

# "al_court_bundle.elements[0].filing_type" -> document 1, field filing_type
_BUNDLE_FIELD = re.compile(r"^al_court_bundle\.elements\[(\d+)\]\.(.+)$")
_OTHER_PARTY_ADDRESS_FIELD = re.compile(r"^other_parties\[(\d+)\]\.address\.(address|city|state|zip)$")

_MAX_RAW_BODY = 300

# (pattern, hint builder) pairs checked in order against a free-text EFSP
# message; the first match wins. Each hint tells the filer what to actually do,
# not just what went wrong. Patterns are deliberately specific substrings of
# the proxy's own wording so an unrelated message never matches by accident.
_KNOWN_MESSAGE_HINTS: list[tuple[re.Pattern, Callable[[re.Match], str]]] = [
    (
        re.compile(r"(?:doesn't|dosesn't|does not) support a state named", re.IGNORECASE),
        lambda m: (
            "Check the party's state or location against the address dropdown. "
            "If the address is not supported, contact the court for filing instructions. "
            "Do not choose a different state."
        ),
    ),
    (
        re.compile(r"PersonSurName is required or does not match regular expression", re.IGNORECASE),
        lambda m: (
            "Check the last names in the party information. If a party is a company or other organization, "
            "choose Organization and enter its organization name."
        ),
    ),
    (
        re.compile(r"doesn't allow subsequent filing into non-indexed cases", re.IGNORECASE),
        lambda m: (
            "Go back to the case details step: choose New case and remove the case number, "
            "or choose Existing case and look up the case instead."
        ),
    ),
    (
        re.compile(r"needs docket number, but not present", re.IGNORECASE),
        lambda m: "Go back to the case details step and provide the court's case number for this existing case.",
    ),
    (
        re.compile(r"Document .*? is too big! Must be max (\d+)", re.IGNORECASE),
        lambda m: (
            f"One of your PDFs is over the court's {int(m.group(1)):,}-byte limit. "
            "Compress it or split it into smaller files, then re-upload."
        ),
    ),
    (
        re.compile(r"All Documents combined are too big! Must be max\s*(\d+)", re.IGNORECASE),
        lambda m: (
            f"Your documents add up to more than the court's {int(m.group(1)):,}-byte combined limit. "
            "Remove or compress some documents and try again."
        ),
    ),
    (
        re.compile(r"Need a filing type! FilingTypes are empty", re.IGNORECASE),
        lambda m: (
            "This court doesn't offer any filing codes for that case category and case type "
            "together. Go back and double-check the case category, case type, and whether "
            "this is a new or existing case."
        ),
    ),
    (
        re.compile(r"Amount in controversy required", re.IGNORECASE),
        lambda m: (
            "This case type requires an amount in controversy, which this tool doesn't collect yet. "
            "Contact the court about filing this case another way."
        ),
    ),
]


def _actionable_hint(message: str) -> str | None:
    for pattern, build_hint in _KNOWN_MESSAGE_HINTS:
        match = pattern.search(message)
        if match:
            return build_hint(match)
    return None


def _with_hint(message: str) -> str:
    hint = _actionable_hint(message)
    return f"{message} {hint}" if hint else message


def describe_efsp_error(response) -> str:
    """Describe why the EFSP refused ``response``'s request.

    Never raises: a bad message is still better than a traceback on a path whose
    only job is reporting someone else's failure.
    """
    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError):
        text = (response.text or "").strip()
        if text:
            return f"the court's filing service returned status {response.status_code}: {text[:_MAX_RAW_BODY]}"
        return f"the court's filing service returned status {response.status_code}"

    if not isinstance(body, dict):
        return f"the court's filing service returned status {response.status_code}: {str(body)[:_MAX_RAW_BODY]}"

    body = _unwrap_error(body)

    problems = [
        *(_describe_var(var, missing=False) for var in _var_list(body, "wrong_vars")),
        *(_describe_var(var, missing=True) for var in _var_list(body, "required_vars")),
    ]
    problems = [problem for problem in problems if problem]
    if problems:
        return "the court could not accept this filing: " + "; ".join(problems)

    if body.get("name"):
        return _describe_var(body, missing=False)

    # Some errors do arrive as a plain message.
    error = body.get("error") or body.get("message") or body.get("detail")
    if error:
        message = str(error)
        validation_errors = body.get("validation_errors") or body.get("errors")
        if validation_errors:
            message += f" - Validation errors: {validation_errors}"
        return _with_hint(message)

    # "Malformed Interview" errors (e.g. a docket number on a case the court has
    # no record of) arrive as {"type": ..., "description": ...} instead.
    description = body.get("description")
    if description:
        error_type = str(body.get("type") or "").strip()
        message = f"{error_type}: {description}" if error_type else str(description)
        return _with_hint(message)

    # The proxy's collector includes general errors alongside wrong_vars.
    errors = body.get("errors")
    if isinstance(errors, list) and errors:
        return "; ".join(
            _with_hint(str(item.get("description") or item)) if isinstance(item, dict) else _with_hint(str(item))
            for item in errors
        )

    return f"the court's filing service returned status {response.status_code}"


def _var_list(body, key):
    value = body.get(key)
    return [var for var in value if isinstance(var, dict)] if isinstance(value, list) else []


def _describe_var(var, *, missing: bool) -> str:
    name = str(var.get("name") or "").strip()
    if not name:
        return ""
    current = str(var.get("currentVal") or "").strip()

    party_match = _PARTY_FIELD.fullmatch(name)
    if party_match and party_match[3] in _PARTY_FIELDS:
        collection, index, path = party_match.groups()
        field = _PARTY_FIELDS[path]
        label = FIELD_LABELS.get(field, field.replace("_", " "))
        who = f"{'filing' if collection == 'users' else 'other'} party {int(index) + 1}"
        description = str(var.get("description") or "")
        length = re.search(r"can't exceed (\d+) characters", description)
        regex_length = re.search(r"must match regex: \^\.\{0,(\d+)\}\$", description)
        if not length:
            length = regex_length
        if length:
            return f"{label} for {who} must be {length[1]} characters or fewer"
        if missing or not current:
            return f"{label.lower()} is required for {who}"
        return f"{current!r} is not a {label.lower()} the court accepts for {who}"

    match = _BUNDLE_FIELD.match(name)
    if match:
        index, name = match.groups()
        where = f" on document {int(index) + 1}"
    else:
        where = ""

    address_match = _OTHER_PARTY_ADDRESS_FIELD.match(name)
    if address_match:
        index, field = address_match.groups()
        field_label = {"address": "street address", "zip": "ZIP code"}.get(field, field)
        if current:
            return f"{current!r} is not a {field_label} the court accepts for other party {int(index) + 1}"
        return f"{field_label} is required for other party {int(index) + 1}'s mailing address"

    label = _FIELD_LABELS.get(name, name.replace("_", " "))
    if missing or not current:
        return f"no {label} was given{where}"
    return f"{current!r} is not a {label} this court accepts{where}"


_PARTY_FIELD = re.compile(r"^(users|other_parties)\[(\d+)\]\.(.+)$")
_PARTY_FIELDS = {
    "name.first": "first_name",
    "name.middle": "middle_name",
    "name.last": "last_name",
    "name.suffix": "suffix",
    # NameDocassembleDeserializer adds name.suffix while already inside name.
    "name.name.suffix": "suffix",
    "email": "email",
    "phone_number": "phone",
    "mobile_number": "phone",
    "address.address": "address_line_1",
    "address.unit": "address_line_2",
    "address.city": "city",
    "address.state": "state",
    "address.zip": "zip_code",
    "party_type": "party_type",
}
_STATE_MESSAGE = re.compile(
    r"(?:Opposing party|Filing party).*?(?:doesn't|dosesn't|does not) support a state named\s+['\"]?([^\s.'\"]+)",
    re.IGNORECASE,
)


def efsp_error_problems(response):
    """Retain field paths instead of making the browser parse rendered prose."""
    try:
        body = response.json()
    except (ValueError, TypeError):
        return []
    if not isinstance(body, dict):
        return []
    body = _unwrap_error(body)
    problems = []
    for key in ("wrong_vars", "required_vars"):
        for var in _var_list(body, key):
            problems.append(
                {"name": str(var.get("name") or ""), "message": _describe_var(var, missing=key == "required_vars")}
            )
    if body.get("name"):
        problems.append({"name": str(body["name"]), "message": _describe_var(body, missing=False)})
    return problems


def _unwrap_error(body):
    """InfoCollector wraps a single FilingError as {error: {...}}."""
    nested = body.get("error")
    return nested if isinstance(nested, dict) else body


def _matching_party(wire, parties):
    """A link is safe only when the rejected payload identifies exactly one row.

    Array order is not draft order: users can be someone the filer acts for,
    and other_parties excludes those users. Duplicate names remain ambiguous.
    """
    name = wire.get("name")
    if not isinstance(name, dict) or not name.get("first"):
        return None
    organization = str(wire.get("person_type") or "").lower() in {"business", "organization"}
    matches = []
    for party in parties:
        if organization:
            same = party.organization_name == name.get("first")
        else:
            same = (
                party.first_name == name.get("first")
                and party.last_name == (name.get("last") or "")
                and party.middle_name == (name.get("middle") or "")
                and not party.organization_name
            )
        if same and party.party_type == (wire.get("party_type") or ""):
            matches.append(party)
    return matches[0] if len(matches) == 1 else None


def error_actions(draft, payload, problems, *, message=""):
    """Build local, draft-bound edit links only for confidently located fields."""
    if draft is None or not isinstance(payload, dict):
        return []
    parties = list(draft.parties.all())
    candidates = list(problems)
    # Older proxy wording contains the rejected state, but no field path.
    # Locate it only when exactly one outgoing party has that value. The
    # message may already carry describe_efsp_error's hint after the state.
    match = _STATE_MESSAGE.search(message)
    if match:
        state = match[1]
        locations = []
        for collection in ("users", "other_parties"):
            rows = payload.get(collection)
            if not isinstance(rows, list):
                continue
            for index, wire in enumerate(rows):
                if (
                    isinstance(wire, dict)
                    and isinstance(wire.get("address"), dict)
                    and wire["address"].get("state") == state
                ):
                    locations.append(f"{collection}[{index}].address.state")
        if len(locations) == 1:
            candidates.append({"name": locations[0], "message": "Check this party's state or location."})
    actions = []
    seen = set()
    for problem in candidates:
        match = _PARTY_FIELD.fullmatch(str(problem.get("name") or ""))
        if not match or match[3] not in _PARTY_FIELDS:
            continue
        collection, index, path = match.groups()
        rows = payload.get(collection)
        if not isinstance(rows, list) or int(index) >= len(rows) or not isinstance(rows[int(index)], dict):
            continue
        wire = rows[int(index)]
        party = _matching_party(wire, parties)
        if party is None or party.role not in {"filer", "other"}:
            continue
        field = _PARTY_FIELDS[path]
        # The filer's court role is collected on a separate screen, not on
        # Your information. Do not offer a field link that cannot focus it.
        if party.role == "filer" and field == "party_type":
            continue
        if field == "first_name" and party.organization_name:
            field = "organization_name"
        if (party.pk, field) in seen:
            continue
        seen.add((party.pk, field))
        view = "your_information" if party.role == "filer" else "party_details"
        params = {"draft": draft.pk, "return_to": "review", "focus": field}
        if party.role != "filer":
            params["party"] = party.pk
        url = reverse(view, kwargs={"jurisdiction": draft.jurisdiction}) + "?" + urlencode(params)
        label = FIELD_LABELS.get(field, field.replace("_", " "))
        display_name = party.organization_name or " ".join(filter(None, [party.first_name, party.last_name]))
        actions.append(
            {
                "url": url,
                "label": f"Edit {label.lower()} for {display_name}",
                "message": problem.get("message") or f"Check {label.lower()}.",
            }
        )
    return actions

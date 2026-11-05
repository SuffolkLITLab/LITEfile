"""Shared validation for document choices on confirmation and organization."""

from urllib.parse import quote

import requests
from django.conf import settings

from efile.services.case_filing_types import permitted_filing_types
from efile.services.efsp_payload import parse_optional_services
from efile.workflow import ExistingCase


def filing_choices(draft):
    if draft.existing_case == ExistingCase.EXISTING:
        return permitted_filing_types(draft)
    url = (
        f"{settings.EFSP_URL}/jurisdictions/{draft.jurisdiction}/codes/courts/"
        f"{quote(draft.court_code, safe='')}/filing_types/"
    )
    try:
        response = requests.get(
            url,
            params={"initial": "true", "category_id": draft.case_category_code, "type_id": draft.case_type_code},
            timeout=30,
        )
        response.raise_for_status()
        choices = response.json()
        if not isinstance(choices, list):
            raise ValueError
        return [{**item, "value": str(item["code"]), "text": str(item["name"])} for item in choices]
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        raise ValueError("We could not check the document types. Try again.") from exc


def validate_document_types(draft, selections, documents, *, allow_blank=False):
    """Validate identity and court choices without trusting posted display names."""
    ids = [item.get("id") for item in selections if isinstance(item, dict)]
    if (
        len(ids) != len(selections)
        or any(type(value) is not int or value < 1 for value in ids)
        or len(set(ids)) != len(ids)
        or set(ids) != {doc.pk for doc in documents}
    ):
        raise ValueError("The document list changed. Reload this page before continuing.")
    choices = (
        {item["value"]: item for item in filing_choices(draft)}
        if any(item.get("filing_type") for item in selections)
        else {}
    )
    validated = []
    for item in selections:
        code = str(item.get("filing_type") or "")
        if not code and allow_blank:
            validated.append({"id": item["id"], "filing_type": "", "filing_type_name": ""})
            continue
        if code not in choices:
            raise ValueError("Choose a type of document (filing code) offered for this case.")
        choice = choices[code]
        validated.append(
            {
                **item,
                "filing_type": code,
                "filing_type_name": choice["text"],
                "requires_amount_in_controversy": str(choice.get("amountincontroversy", "")).lower() == "required",
            }
        )
    return validated


def save_document_type(document, code, name, *, requires_amount=False):
    if document.filing_type_code != code:
        document.document_type_confirmed = False
        document.document_type_code = ""
        document.document_type_name = ""
        document.filing_component_code = ""
        document.filing_component_name = ""
        document.requested_optional_services = []
    document.filing_type_code = code
    document.filing_type_name = name
    document.filing_requires_amount_in_controversy = requires_amount
    document.save()


def service_choices(draft, filing_type):
    url = (
        f"{settings.EFSP_URL}/jurisdictions/{draft.jurisdiction}/codes/courts/"
        f"{quote(draft.court_code, safe='')}/filing_types/{quote(filing_type, safe='')}/optional_services"
    )
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return {item["code"]: item for item in parse_optional_services(response.json())}
    except (requests.RequestException, ValueError) as exc:
        raise ValueError("We could not load the optional services. Try again.") from exc


def validate_services(draft, filing_type, selections):
    """Keep legacy code lists readable; new quantity entries must be exact integers."""
    if not isinstance(selections, list):
        raise ValueError("Choose optional services from the list.")
    if not selections:
        return []
    metadata = service_choices(draft, filing_type)
    result = []
    seen = set()
    for selection in selections:
        code = str(selection.get("code", "") if isinstance(selection, dict) else selection)
        if code not in metadata or code in seen:
            raise ValueError("Choose optional services available for this document type.")
        seen.add(code)
        item = {"code": code}
        if metadata[code].get("name"):
            item["name"] = metadata[code]["name"]
        quantity = selection.get("multiplier", 1) if isinstance(selection, dict) else 1
        if metadata[code]["multiplier"]:
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
                raise ValueError("Enter a whole number of 1 or more.")
            item["multiplier"] = quantity
        elif isinstance(selection, dict) and "multiplier" in selection:
            raise ValueError("This service does not allow a quantity.")
        result.append(item)
    for code, service in metadata.items():
        if service["required"] and code not in seen:
            raise ValueError("Include the services required by the court.")
    return result

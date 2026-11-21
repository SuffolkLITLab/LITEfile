"""Saved electronic-service recipients, shared by fee quotes and submission."""

import requests
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from efile.utils.efsp_security import validated_code, validated_jurisdiction
from efile.utils.proxy_connection import get_headers

KEY = "_electronic_service"


class ServiceContactError(ValueError):
    pass


def scope(draft):
    return [
        draft.jurisdiction,
        draft.court_code,
        draft.case_type_code,
        draft.previous_case_id,
        draft.existing_case,
        getattr(draft, "correction_of_id", None),
    ]


def saved_service(draft):
    return (draft.supplemental_fields or {}).get(KEY) or {}


def proxy_request(draft, token, path, *, body=None, method=None, timeout=15):
    jurisdiction = validated_jurisdiction(draft.jurisdiction)
    headers = get_headers()
    headers[f"tyler-token-{jurisdiction}"] = token
    url = f"{settings.EFSP_URL}/jurisdictions/{jurisdiction}/{path}"
    try:
        if method == "GET":
            response = requests.get(url, headers=headers, json=body, timeout=timeout)
        elif method == "PUT":
            response = requests.put(url, headers=headers, json=body, timeout=timeout)
        elif body is None:
            response = requests.get(url, headers=headers, timeout=15)
        else:
            response = requests.post(url, headers=headers, json=body, timeout=15)
        if response.status_code in {401, 403}:
            raise ServiceContactError("Sign in again to check the court's email contacts and service options.")
        if response.status_code == 400 and method == "GET" and body is not None:
            raise ServiceContactError(
                "The court needs more filing details to check electronic service. Finish your filing details, then return to Service and try again."
            )
        if response.status_code == 400 and body is not None:
            raise ServiceContactError("The court could not add this contact. Check the name and email address.")
        response.raise_for_status()
        if method == "PUT" and response.status_code in {201, 204}:
            return None
        return response.json()
    except ServiceContactError:
        raise
    except (requests.RequestException, ValueError) as error:
        raise ServiceContactError("We could not reach the court's contact list. Try again later.") from error


def _nested(value, *keys):
    for key in keys:
        if not isinstance(value, dict):
            return ""
        value = value.get(key)
    return value if isinstance(value, str) else ""


def parse_contacts(items):
    from efile.services.existing_cases import contacts as entity_contacts
    from efile.services.existing_cases import field, objects

    if not isinstance(items, list):
        raise ServiceContactError("We could not read the court's contact list. Try again later.")
    result = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        if any(
            (item.get(key).get("value") if isinstance(item.get(key), dict) else item.get(key)) is True
            for key in ("isUndeliverable", "isMailServiceContact")
        ):
            continue
        contact_id = item.get("serviceContactID")
        email = item.get("email", "")
        name = " ".join(str(item.get(key) or "") for key in ("firstName", "middleName", "lastName")).strip()
        if not contact_id:
            person = (item.get("entityRepresentation") or {}).get("value") or {}
            if not isinstance(person, dict):
                continue
            contact_id = _nested(
                person,
                "personAugmentation",
                "electronicServiceInformation",
                "serviceRecipientID",
                "identificationID",
                "value",
            )
            if not contact_id:
                contact_id = _nested(
                    person,
                    "organizationAugmentation",
                    "electronicServiceInformation",
                    "serviceRecipientID",
                    "identificationID",
                    "value",
                )
            if not contact_id:
                contact_id = next(
                    (
                        _nested(part, "electronicServiceInformation", "serviceRecipientID", "identificationID", "value")
                        for part in objects(person.get("rest", []))
                        if _nested(
                            part, "electronicServiceInformation", "serviceRecipientID", "identificationID", "value"
                        )
                    ),
                    "",
                )
            email = entity_contacts(person).get("email", "")
            person_name = person.get("personName") or {}
            name = " ".join(
                _nested(person_name, key, "value") for key in ("personGivenName", "personMiddleName", "personSurName")
            ).strip()
        if contact_id and not name:
            name = field(item, "organizationName") or "Court service contact"
        if contact_id and name:
            result[str(contact_id)] = {"contact_id": str(contact_id), "name": name, "email": str(email or "")}
    return list(result.values())


def electronic_service_types(draft, *, lookups=None, token=None, payload=None):
    from efile.services.efsp_payload import _EfspLookups

    court = validated_code(draft.court_code)
    jurisdiction = validated_jurisdiction(draft.jurisdiction)
    base = f"{settings.EFSP_URL}/jurisdictions/{jurisdiction}/codes/courts/{court}"
    lookups = lookups or _EfspLookups(budget=20)
    initial = draft.existing_case != "existing"
    allowed = None
    location = lookups.get(f"{base}/codes")
    if (
        initial
        and isinstance(location, dict)
        and str(location.get("disallowelectronicserviceonnewcontacts")).lower() == "true"
    ):
        raise ServiceContactError("This court does not allow electronic service to new contacts when starting a case.")
    if initial:
        flag = str((location or {}).get("allowserviceoninitial", "")).lower() if isinstance(location, dict) else ""
        if flag in {"true", "false"}:
            allowed = flag == "true"
    if allowed is None:
        field = "FilingServiceCheckBoxInitial" if initial else "FilingServiceCheckBoxSubsequent"
        row = lookups.get(f"{base}/datafields/{field}")
        if isinstance(row, dict) and str(row.get("isvisible")).lower() in {"true", "false"}:
            allowed = str(row["isvisible"]).lower() == "true"
    if allowed is False:
        raise ServiceContactError(
            "This court does not allow electronic service for this filing. Check its rules for how to serve copies."
        )
    if allowed is None:
        raise ServiceContactError("We could not check whether this court allows electronic service. Try again later.")
    types = lookups.get(f"{base}/service_types")
    if not isinstance(types, list):
        raise ServiceContactError("We could not check the court's service options. Try again later.")
    choices = [
        item
        for item in types
        if isinstance(item, dict) and item.get("code") and item.get("servicemethod") == "ESERVICE"
    ]
    if not choices:
        raise ServiceContactError("Electronic service is not available for this court.")
    if isinstance(location, dict) and str(location.get("hasconditionalservicetypes")).lower() == "true":
        if not token:
            raise ServiceContactError("Sign in again to check the court's electronic service options.")
        from copy import deepcopy

        from efile.services.service_query import service_query_payload

        query = deepcopy(payload) if payload is not None else service_query_payload(draft, lookups)
        query.pop("service_contacts", None)
        for bundle in query.get("al_court_bundle", []):
            bundle["filing_action"] = "efile"
        # This proxy endpoint is a read-only GET with a JSON request body.
        remaining = lookups.remaining_seconds()
        if remaining <= 0:
            raise ServiceContactError("The court's service-options check took too long. Try again later.")
        offered = proxy_request(
            draft,
            token,
            f"filingreview/courts/{court}/filing/servicetypes",
            body=query,
            method="GET",
            timeout=min(20, remaining),
        )
        if not isinstance(offered, list):
            raise ServiceContactError("We could not read the court's service options. Try again later.")
        codes = {_nested(item, "serviceCodeID", "value") for item in offered if isinstance(item, dict)}
        choices = [item for item in choices if str(item["code"]) in codes]
        if not choices:
            raise ServiceContactError("The court does not offer electronic service for these documents.")
    return [{**item, "code": str(item["code"])} for item in choices]


def service_options(draft, token):
    types = electronic_service_types(draft, token=token)
    court = validated_code(draft.court_code)
    contacts = parse_contacts(proxy_request(draft, token, "firmattorneyservice/service-contacts"))
    if draft.existing_case == "existing" and draft.previous_case_id:
        case = validated_code(draft.previous_case_id)
        contacts += parse_contacts(
            proxy_request(draft, token, f"cases/courts/{court}/cases/{case}/service-information")
        )
    return list({item["contact_id"]: item for item in contacts}.values()), types


def create_contact(draft, token, data):
    electronic_service_types(draft, token=token)
    from efile.services.efsp_payload import _EfspLookups

    court = validated_code(draft.court_code)
    jurisdiction = validated_jurisdiction(draft.jurisdiction)
    location = _EfspLookups().get(f"{settings.EFSP_URL}/jurisdictions/{jurisdiction}/codes/courts/{court}/codes")
    if (
        draft.existing_case != "existing"
        and isinstance(location, dict)
        and location.get("allowaddservicecontactsoninitial") is False
    ):
        raise ServiceContactError(
            "This court does not allow new service contacts when starting a case. Choose an existing contact."
        )
    return get_or_create_contact(draft, token, data)


def get_or_create_contact(draft, token, data):
    """Reuse a firm contact or create one; callers decide the permitted use."""
    first = data.get("contact_first_name", "").strip()
    last = data.get("contact_last_name", "").strip()
    email = data.get("contact_email", "").strip()
    if not first or not last or len(first) > 100 or len(last) > 100:
        raise ServiceContactError("Enter the person's first and last name.")
    if len(email) > 254:
        raise ServiceContactError("Enter an email address with 254 characters or fewer.")
    try:
        validate_email(email)
    except ValidationError as error:
        raise ServiceContactError("Enter a valid email address.") from error
    existing = parse_contacts(proxy_request(draft, token, "firmattorneyservice/service-contacts"))
    for contact in existing:
        if (
            contact["email"].casefold() == email.casefold()
            and " ".join(contact["name"].split()).casefold() == f"{first} {last}".casefold()
        ):
            return contact["contact_id"]
    contact_id = proxy_request(
        draft,
        token,
        "firmattorneyservice/service-contacts",
        body={
            "firstName": first,
            "lastName": last,
            "email": email,
            "isPublic": False,
            "isInFirmMaster": True,
        },
    )
    if not isinstance(contact_id, str) or not contact_id or contact_id == "null":
        raise ServiceContactError(
            "We could not confirm that the contact was added. Check the list before trying again."
        )
    return contact_id


def save_contacts(draft, token, data, *, persist=True):
    ids = set(data.getlist("service_contact"))
    if not ids:
        selection = {}
    else:
        if data.get("service_rules_checked") != "yes":
            raise ServiceContactError("Check the court's rules before choosing electronic service.")
        contacts, types = service_options(draft, token)
        allowed = {item["contact_id"]: item for item in contacts}
        chosen_type = next((item for item in types if str(item["code"]) == data.get("service_type")), None)
        if not ids <= allowed.keys() or chosen_type is None:
            raise ServiceContactError("The court's contact list or service options changed. Choose again.")
        selection = {
            "scope": scope(draft),
            "rules_checked": True,
            "contacts": [allowed[contact_id] for contact_id in sorted(ids)],
            "service_type": str(chosen_type["code"]),
            "service_name": chosen_type.get("name", ""),
        }
    if persist:
        draft.supplemental_fields = {**(draft.supplemental_fields or {}), KEY: selection}
        draft.save(update_fields=["supplemental_fields", "updated_at"])
    return selection


def bind_service(payload, draft, *, lookups=None, token=None):
    from efile.services.efsp_payload import PayloadValidationError

    saved = saved_service(draft)
    contacts = saved.get("contacts") or []
    if contacts and (saved.get("scope") != scope(draft) or not saved.get("rules_checked")):
        raise PayloadValidationError(
            "Your case details changed. Check service recipients on the Service page before filing."
        )
    payload.pop("service_contacts", None)
    if contacts:
        try:
            types = electronic_service_types(draft, lookups=lookups, token=token, payload=payload)
        except (ServiceContactError, ValueError) as error:
            raise PayloadValidationError(str(error)) from error
        if saved.get("service_type") not in {str(item["code"]) for item in types}:
            raise PayloadValidationError(
                "The court's electronic service options changed. Choose recipients again on the Service page."
            )
        payload["service_contacts"] = [
            {"contact_id": item["contact_id"], "service_type": saved["service_type"]} for item in contacts
        ]
    for bundle in payload.get("al_court_bundle", []):
        bundle["filing_action"] = "efile_and_serve" if contacts else "efile"
        bundle["preliminary_copies"] = []

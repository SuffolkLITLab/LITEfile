"""Authenticated court-case import and authoritative envelope identity.

The raw ECF response is intentional: JSON-V1 currently loses contacts and can
choose a non-CASEPARTYID identification for an individual. Only supported fields
enter the bounded snapshot; raw records and credentials are never persisted.
"""

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import quote

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from efile.models import FilingDraft, FilingParty
from efile.party_sides import side_for_party_type_name
from efile.services.drafts import ACTIVE_DRAFT_STATUSES
from efile.services.fee_quotes import FEE_QUOTE_FIELDS, fee_inputs_token, invalidate_fee_quote
from efile.utils.efsp_security import validated_code, validated_jurisdiction
from efile.utils.proxy_connection import get_headers
from efile.workflow import ExistingCase


class CaseImportError(ValueError):
    pass


# Case fields the court's record decides; the draft mirrors them.
SNAPSHOT_CASE_FIELDS = ("docket_number", "case_title", "case_category_code", "case_type_code")
PARTY_FIELDS = (
    "first_name",
    "middle_name",
    "last_name",
    "suffix",
    "organization_name",
    "email",
    "phone",
    "address_line_1",
    "address_line_2",
    "city",
    "state",
    "zip_code",
    "country",
)
PARTY_FIELD_LIMITS = {
    key: FilingParty._meta.get_field(key).max_length
    for key in (*PARTY_FIELDS, "external_party_id", "party_type", "party_type_name")
}


def docket_key(value):
    return re.sub(r"[^a-z0-9]", "", value.lower())


def unwrap(value):
    while isinstance(value, dict) and "value" in value:
        value = value["value"]
    return value


def text(value):
    value = unwrap(value)
    return str(value).strip() if isinstance(value, str | int) and not isinstance(value, bool) else ""


def objects(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from objects(item)
    elif isinstance(value, list):
        for item in value:
            yield from objects(item)


def field(value, key):
    return next((text(item[key]) for item in objects(value) if item.get(key) is not None), "")


def country_code(value):
    value = value.upper()
    if value in {"USA", "UNITED STATES", "UNITED STATES OF AMERICA"}:
        return "US"
    # FilingParty.country holds an ISO code; anything else is not identity.
    return value if re.fullmatch(r"[A-Z]{2}", value) else ""


def entity_id(entity, category="CASEPARTYID"):
    ids = {
        text(item.get("identificationID"))
        for item in objects(entity)
        if text(item.get("identificationCategory")).upper() == category
    }
    ids.discard("")
    return next(iter(ids)) if len(ids) == 1 else ""


def contacts(entity):
    result = {}
    # Search only this entity, never attorney/represented-party references.
    for item in objects(entity):
        kind = str(item.get("name") or "").split("}")[-1]
        value = unwrap(item)
        if kind == "ContactEmailID":
            result.setdefault("email", text(value))
        elif kind == "ContactTelephoneNumber":
            result.setdefault("phone", field(value, "telephoneNumberFullID"))
        elif kind in {"ContactMailingAddress", "ContactAddress"}:
            address = unwrap(value.get("addressRepresentation")) if isinstance(value, dict) else None
            if not isinstance(address, dict):
                continue
            delivery = address.get("addressDeliveryPoint") or []
            lines = [field(line, "streetFullText") or text(line) for line in delivery]
            result.update(
                {
                    "address_line_1": lines[0] if lines else "",
                    "address_line_2": lines[1] if len(lines) > 1 else "",
                    "city": text(address.get("locationCityName")),
                    "state": text(address.get("locationState")),
                    "zip_code": text(address.get("locationPostalCode")),
                    "country": country_code(text(address.get("locationCountry"))),
                }
            )
    return result


def entity_fields(entity):
    if "organizationName" in entity:
        return {"organization_name": text(entity.get("organizationName")), **contacts(entity)}
    name = entity.get("personName") or {}
    if not isinstance(name, dict):
        raise CaseImportError("The court returned an unreadable party name. Retry loading the case.")
    return {
        "first_name": text(name.get("personGivenName")),
        "middle_name": text(name.get("personMiddleName")),
        "last_name": text(name.get("personSurName")),
        "suffix": text(name.get("personNameSuffixText")),
        **contacts(entity),
    }


def normalize_case(raw, *, court, tracking_id, party_types, docket_number=""):
    case = unwrap(raw)
    lineage_ids = {
        text(lineage.get("caseTrackingID"))
        for item in objects(case)
        if isinstance(item.get("caseLineageCase"), list)
        for lineage in map(unwrap, item["caseLineageCase"])
        if isinstance(lineage, dict)
    }
    if not isinstance(case, dict) or tracking_id not in {text(case.get("caseTrackingID")), *lineage_ids}:
        raise CaseImportError("The court returned a different case. Search for your case again.")
    if docket_number and docket_key(text(case.get("caseDocketID"))) != docket_key(docket_number):
        raise CaseImportError("The court returned a different case number. Search for your case again.")
    returned_court = field(
        next((x for x in objects(case) if "caseCourt" in x), {}).get("caseCourt"), "identificationID"
    )
    if not returned_court or returned_court != court:
        raise CaseImportError("The court returned a different court. Search for your case again.")
    augmentation = next((x for x in objects(case) if "caseParticipant" in x), None)
    if augmentation is None or not isinstance(augmentation["caseParticipant"], list):
        raise CaseImportError("The court did not return a party roster. Retry loading the case or sign in again.")
    if not isinstance(party_types, list) or any(
        not isinstance(p, dict) or not p.get("code") or not isinstance(p.get("name"), str) for p in party_types
    ):
        raise CaseImportError("We could not verify the court's party roles. Retry loading the case.")
    if len(augmentation["caseParticipant"]) > 1000:
        raise CaseImportError("This case has more parties than can be loaded here. Contact support.")
    types = {str(p["code"]): p["name"] for p in party_types}
    attorneys = {}
    associations = {}
    attorney_records = augmentation.get("caseOtherEntityAttorney") or []
    if (
        not isinstance(attorney_records, list)
        or len(attorney_records) > 1000
        or any(not isinstance(a, dict) for a in attorney_records)
    ):
        raise CaseImportError("The court returned unreadable attorney information. Retry loading the case.")
    for attorney in attorney_records:
        reference = attorney.get("roleOfPersonReference") or {}
        references = attorney.get("caseRepresentedPartyReference") or []
        if (
            not isinstance(reference, dict)
            or not isinstance(references, list)
            or len(references) > 1000
            or any(not isinstance(ref, dict) for ref in references)
        ):
            raise CaseImportError("The court returned unreadable attorney information. Retry loading the case.")
        entity = reference.get("ref")
        if not isinstance(entity, dict):
            continue
        for id_category in ("CASEPARTYATTORNEYID", "ATTORNEYID"):
            if attorney_id := entity_id(entity, id_category):
                break
        else:
            continue
        attorneys[attorney_id] = {
            "id": attorney_id,
            "id_category": id_category,
            **entity_fields(entity),
            "bar_number": field(attorney.get("judicialOfficialBarMembership"), "identificationID"),
        }
        for reference in references:
            ref = reference.get("ref")
            # An unidentified reference would match every party without an ID.
            if isinstance(ref, dict) and (represented_id := entity_id(ref)):
                associations.setdefault(represented_id, []).append(attorney_id)
    parties, problems, seen = [], [], set()
    for index, wrapped in enumerate(augmentation["caseParticipant"]):
        participant = unwrap(wrapped)
        if not isinstance(participant, dict):
            raise CaseImportError("The court returned an unreadable party. Retry loading the case.")
        role = text(participant.get("caseParticipantRoleCode"))
        if role.upper() == "ATTY":
            continue
        entity = unwrap(participant.get("entityRepresentation"))
        if not isinstance(entity, dict):
            raise CaseImportError("The court returned an unreadable party. Retry loading the case.")
        party_id = entity_id(entity)
        if party_id and party_id in seen:
            raise CaseImportError("The court returned conflicting party IDs. Retry loading the case.")
        seen.add(party_id)
        values: dict[str, str] = {key: "" for key in PARTY_FIELDS}
        values.update(entity_fields(entity))
        values.update(external_party_id=party_id, party_type=role, party_type_name=types.get(role, ""))
        for key, value in values.items():
            limit = PARTY_FIELD_LIMITS[key]
            if limit and len(value) > limit:
                raise CaseImportError(
                    "The court returned party information that is too long to import. Contact support."
                )
        if not party_id:
            problems.append(f"Party {index + 1} has no court party ID.")
        if role not in types:
            problems.append(f"Party {index + 1} has an unrecognized court role ({role or 'missing'}).")
        if not (values["organization_name"] or (values["first_name"] and values["last_name"])):
            problems.append(f"Party {index + 1} has an incomplete court name.")
        attorney_ids = associations.get(party_id, [])
        parties.append(
            {
                **values,
                "representation": {
                    "status": "represented" if attorney_ids else "unknown",
                    "attorneys": [attorneys[i] for i in attorney_ids],
                },
            }
        )
    if len(parties) > 500:
        raise CaseImportError("This case has more parties than can be loaded here. Contact support.")
    case_fields = {
        "docket_number": text(case.get("caseDocketID")),
        "case_title": text(case.get("caseTitleText")),
        "case_category_code": text(case.get("caseCategoryText")),
        "case_type_code": text(augmentation.get("caseTypeText")),
    }
    for key, value in case_fields.items():
        if len(value) > FilingDraft._meta.get_field(key).max_length:
            raise CaseImportError("The court returned case information that is too long to import. Contact support.")
        if not value and key != "case_title":
            problems.append(f"The court did not return the case's {key.replace('_', ' ')}.")
    return {
        "schema_version": 1,
        "court_local_case_id": text(case.get("caseTrackingID")),
        "court": court,
        "tracking_id": tracking_id,
        "status": "partial" if problems else "loaded",
        "problems": problems,
        **case_fields,
        "parties": parties,
    }


def fetch_case(draft, token):
    if not token:
        raise CaseImportError("Sign in again to load your court case.")
    try:
        validated_jurisdiction(draft.jurisdiction)
        validated_code(draft.court_code)
        validated_code(draft.previous_case_id)
    except ValueError as error:
        raise CaseImportError("Choose a valid court and case before loading parties.") from error
    url = (
        f"{settings.EFSP_URL}/jurisdictions/{quote(draft.jurisdiction, safe='')}/cases/"
        f"courts/{quote(draft.court_code, safe='')}/cases/{quote(draft.previous_case_id, safe='')}"
    )
    codes_url = (
        f"{settings.EFSP_URL}/jurisdictions/{quote(draft.jurisdiction, safe='')}/codes/courts/"
        f"{quote(draft.court_code, safe='')}/party_types"
    )
    headers = get_headers()
    headers[f"tyler-token-{draft.jurisdiction}"] = token
    # The role list does not depend on the case, so fetch both at once.
    pool = ThreadPoolExecutor(max_workers=1)
    pending_codes = pool.submit(requests.get, codes_url, headers=headers, timeout=15, allow_redirects=False)
    pool.shutdown(wait=False)
    try:
        response = requests.get(url, headers=headers, timeout=30, allow_redirects=False)
        response.raise_for_status()
        raw = response.json()
    except (requests.RequestException, ValueError) as error:
        raise CaseImportError("We could not load the court's parties. Retry or sign in again.") from error
    # Classification and roles come from this response, not browser assertions.
    try:
        codes = pending_codes.result()
        codes.raise_for_status()
        types = codes.json()
        if not isinstance(types, list):
            raise ValueError
        snapshot = normalize_case(
            raw,
            court=draft.court_code,
            tracking_id=draft.previous_case_id,
            party_types=types,
            docket_number=draft.docket_number,
        )
    except CaseImportError:
        raise
    except (requests.RequestException, ValueError) as error:
        raise CaseImportError("We could not verify the court's party roles. Retry loading the case.") from error
    except (AttributeError, TypeError, KeyError) as error:
        # The court's record had a shape normalize_case did not expect.
        raise CaseImportError("We could not read the court's case. Retry loading the case.") from error
    snapshot.update(
        source_environment=settings.EFSP_URL, jurisdiction=draft.jurisdiction, retrieved_at=timezone.now().isoformat()
    )
    return snapshot


@transaction.atomic
def clear_case_import(draft):
    """Forget an imported court roster. Returns whether there was one.

    New-case drafts are left alone: their filing-party and self choices
    belong to the filer, not to an import.
    """

    roster = draft.parties.filter(source__in=FilingParty.CASE_ROSTER_SOURCES)
    if not draft.existing_case_snapshot and not roster.exists():
        return False
    roster.delete()
    draft.parties.update(is_filing_party=False, is_self=False)
    draft.existing_case_snapshot = {}
    invalidate_fee_quote(draft, save=False)
    draft.save(update_fields=["existing_case_snapshot", *FEE_QUOTE_FIELDS, "updated_at"])
    return True


def _lock_case(draft, court, tracking_id):
    """Lock the draft, which must be editable and still point at this case."""

    current = FilingDraft.objects.select_for_update().get(pk=draft.pk)
    if current.status not in ACTIVE_DRAFT_STATUSES:
        raise CaseImportError("This filing can no longer be edited.")
    if (current.court_code, current.previous_case_id, current.existing_case) != (
        court,
        tracking_id,
        ExistingCase.EXISTING,
    ):
        raise CaseImportError("The selected case changed while loading. Reload your case.")
    return current


@transaction.atomic
def apply_case(draft, snapshot):
    _apply_locked(_lock_case(draft, snapshot["court"], snapshot["tracking_id"]), snapshot)
    draft.refresh_from_db()


def _apply_locked(current, snapshot):
    if current.existing_case_snapshot.get("tracking_id") not in {None, snapshot["tracking_id"]}:
        clear_case_import(current)
    previous_fee_inputs = fee_inputs_token(current)
    # Extracted suggestions stay in extracted_guesses, not as duplicate parties.
    current.parties.exclude(role="filer").exclude(source__in=FilingParty.CASE_ROSTER_SOURCES).delete()
    current.parties.filter(role="filer").update(party_type="", party_type_name="", is_filing_party=False)
    existing = {p.external_party_id: p for p in current.parties.filter(source="court") if p.external_party_id}
    keep = []
    next_order = max(current.parties.values_list("sort_order", flat=True), default=-1) + 1
    for index, values in enumerate(snapshot["parties"]):
        party = existing.get(values["external_party_id"])
        if party is None:
            party = FilingParty(draft=current, role="other", sort_order=next_order + index)
        for key, value in values.items():
            setattr(party, key, value)
        party.source = "court"
        party.source_case_id = snapshot["tracking_id"]
        party.party_side = side_for_party_type_name(party.party_type_name)
        party.save()
        keep.append(party.pk)
    current.parties.filter(source="court").exclude(pk__in=keep).delete()
    current.existing_case_snapshot = snapshot
    for key in SNAPSHOT_CASE_FIELDS:
        setattr(current, key, snapshot[key])
    if fee_inputs_token(current) != previous_fee_inputs:
        invalidate_fee_quote(current, save=False)
    current.save()


def import_ready(draft):
    snapshot = draft.existing_case_snapshot or {}
    return (
        snapshot.get("status") == "loaded"
        and snapshot.get("confirmed") is True
        and snapshot.get("court") == draft.court_code
        and snapshot.get("tracking_id") == draft.previous_case_id
        and snapshot.get("source_environment") == settings.EFSP_URL
    )


SNAPSHOT_REUSE_WINDOW = timedelta(minutes=15)


def reusable_snapshot(draft):
    """An unconfirmed snapshot of this case recent enough to show or confirm."""

    snapshot = draft.existing_case_snapshot or {}
    if (
        snapshot.get("status") not in {"loaded", "partial"}
        or snapshot.get("court") != draft.court_code
        or snapshot.get("tracking_id") != draft.previous_case_id
        or snapshot.get("source_environment") != settings.EFSP_URL
    ):
        return False
    try:
        retrieved_at = datetime.fromisoformat(snapshot.get("retrieved_at", ""))
    except (TypeError, ValueError):
        return False
    return timezone.now() - retrieved_at < SNAPSHOT_REUSE_WINDOW


def load_case(draft, token):
    """Fetch the court's case and store it as an unconfirmed preview."""

    try:
        snapshot = fetch_case(draft, token)
        with transaction.atomic():
            current = _lock_case(draft, snapshot["court"], snapshot["tracking_id"])
            current.existing_case_snapshot = snapshot
            current.save(update_fields=["existing_case_snapshot", "updated_at"])
        draft.refresh_from_db()
    except CaseImportError:
        try:
            with transaction.atomic():
                current = _lock_case(draft, draft.court_code, draft.previous_case_id)
                current.existing_case_snapshot = {
                    "status": "failed",
                    "court": current.court_code,
                    "tracking_id": current.previous_case_id,
                }
                invalidate_fee_quote(current, save=False)
                current.save(update_fields=["existing_case_snapshot", *FEE_QUOTE_FIELDS, "updated_at"])
        except CaseImportError:
            pass  # The draft moved on; there is no failed load of it to record.
        draft.refresh_from_db()
        raise


def reconcile_case_parties(payload, draft, jurisdiction, court):
    # efsp_payload imports this module; import its error lazily.
    from efile.services.efsp_payload import PayloadValidationError

    existing = bool(payload.get("previous_case_id")) or (draft and draft.existing_case == ExistingCase.EXISTING)
    if not existing:
        if any(p.get("tyler_id") for key in ("users", "other_parties") for p in payload.get(key, [])):
            raise PayloadValidationError("A new case cannot use another case's party IDs.")
        return
    if draft is None or not import_ready(draft):
        raise PayloadValidationError("Confirm your court case and load its parties before calculating fees or filing.")
    if (jurisdiction, court, payload.get("previous_case_id"), payload.get("user_started_case")) != (
        draft.jurisdiction,
        draft.court_code,
        draft.previous_case_id,
        False,
    ):
        raise PayloadValidationError("The filing does not match the confirmed court case.")
    for wire, attr in (
        ("efile_case_category", "case_category_code"),
        ("efile_case_type", "case_type_code"),
        ("docket_number", "docket_number"),
    ):
        payload[wire] = getattr(draft, attr)
    expected = {
        p.external_party_id if p.source == "court" else f"draft:{p.pk}": p
        for p in draft.parties.filter(source__in=FilingParty.CASE_ROSTER_SOURCES)
    }
    verified_parties = {p["external_party_id"]: p for p in draft.existing_case_snapshot["parties"]}
    selected = {key for key, p in expected.items() if p.is_filing_party}
    if not selected:
        raise PayloadValidationError("Choose the existing party you are filing for.")
    seen = set()
    for collection in ("users", "other_parties"):
        for party in payload.get(collection, []):
            party_id = party.get("tyler_id")
            if not party_id and party.get("is_new") is True:
                party_id = f"draft:{party.get('_draft_party_id')}"
            if not party_id or party_id not in expected or party_id in seen:
                raise PayloadValidationError(
                    "The filing contains a missing, duplicate, or substituted court party ID. Reload the parties."
                )
            stored = expected[party_id]
            if stored.source == "court":
                verified = verified_parties.get(party_id)
                if verified is None or any(
                    getattr(stored, key) != verified.get(key, "")
                    for key in ("party_type", "first_name", "middle_name", "last_name", "suffix", "organization_name")
                ):
                    raise PayloadValidationError("The court party roster needs to be reloaded before filing.")
            if party.get("is_new") is not (stored.source == "added") or (
                stored.source_case_id != draft.previous_case_id
                or (stored.source == "added" and bool(party.get("tyler_id")))
            ):
                raise PayloadValidationError("The party's court identity cannot be changed.")
            name = {
                "first": stored.organization_name or stored.first_name,
                "middle": stored.middle_name,
                "last": stored.last_name,
                "suffix": stored.suffix,
            }
            if party.get("name") != name or party.get("party_type") != stored.party_type:
                raise PayloadValidationError("Court party names and roles cannot be changed in this filing.")
            if (collection == "users") != (party_id in selected):
                raise PayloadValidationError("The selected filing parties changed. Reload the parties.")
            # Ignore client additions to court identity/representation fields.
            email = (stored.email or draft.notice_email or draft.user.email) if collection == "users" else stored.email
            party.clear()
            party.update(
                is_new=stored.source == "added",
                name=name,
                party_type=stored.party_type,
                person_type="business" if stored.organization_name else "individual",
                email=email or "",
            )
            if stored.source == "court":
                party["tyler_id"] = stored.external_party_id
            if stored.phone:
                party["phone_number"] = stored.phone
            if stored.address_line_1 and stored.city and stored.state and stored.zip_code:
                party["address"] = {
                    "address": stored.address_line_1,
                    "unit": stored.address_line_2,
                    "city": stored.city,
                    "state": stored.state,
                    "zip": stored.zip_code,
                    "country": stored.country or "US",
                }
            if collection == "users":
                party["is_form_filler"] = False
            seen.add(party_id)
    if seen != set(expected):
        raise PayloadValidationError("Some existing court parties are missing. Reload the parties.")
    references = [f"users[{i}]" for i in range(len(payload.get("users", [])))]
    for bundle in payload.get("al_court_bundle", []):
        if bundle.get("filing_parties") != references:
            raise PayloadValidationError("A document does not reference the selected existing filing parties.")


@transaction.atomic
def confirm_case(draft):
    current = _lock_case(draft, draft.court_code, draft.previous_case_id)
    if current.existing_case_snapshot.get("status") != "loaded":
        raise CaseImportError("The selected case changed while loading. Reload your case.")
    _apply_locked(current, {**current.existing_case_snapshot, "confirmed": True})
    draft.refresh_from_db()

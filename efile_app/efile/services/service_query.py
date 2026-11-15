"""Build a read-only service-options query from the saved filing."""

from efile.models import FilingParty
from efile.services.efsp_payload import (
    PayloadValidationError,
    add_cook_srl_reference,
    bind_filing_documents,
    normalize_efile_payload,
)
from efile.services.existing_cases import reconcile_case_parties


def party_payload(party, notice_email=""):
    value = {
        "party_type": party.party_type,
        "name": {
            "first": party.organization_name or party.first_name,
            "middle": party.middle_name,
            "last": party.last_name,
            "suffix": party.suffix,
        },
        "email": party.email or notice_email,
        "phone_number": party.phone,
        "mobile_number": party.phone,
        "is_new": not bool(party.external_party_id),
        "is_form_filler": False,
    }
    if party.organization_name:
        value["person_type"] = "business"
    if party.external_party_id:
        value["tyler_id"] = party.external_party_id
    if party.source == "added":
        value["_draft_party_id"] = party.pk
    if any((party.address_line_1, party.address_line_2, party.city, party.state, party.zip_code)):
        value["address"] = {
            "address": party.address_line_1,
            "unit": party.address_line_2,
            "city": party.city,
            "state": party.state,
            "zip": party.zip_code,
            "country": party.country or "US",
        }
    return value


def service_query_payload(draft, lookups):
    parties = list(FilingParty.objects.filter(draft=draft).order_by("sort_order", "pk"))
    filer = next((party for party in parties if party.role == "filer"), None)
    filing = [party for party in parties if party.is_filing_party and party.party_type]
    if not filing and filer and filer.party_type:
        filing = [filer]
    if not filer or not filing:
        raise PayloadValidationError("Save your role and the people in this case before choosing email recipients.")
    notice = draft.notice_email or filer.email
    payload = {
        "efile_case_category": draft.case_category_code,
        "efile_case_type": draft.case_type_code,
        "efile_case_subtype": draft.case_subtype_code,
        "users": [party_payload(party, notice) for party in filing],
        "other_parties": [
            party_payload(party)
            for party in parties
            if party not in filing and party.role != "filer" and party.party_type
        ],
        "user_started_case": draft.existing_case != "existing",
        "al_court_bundle": [
            {"document_id": document.pk} for document in draft.documents.order_by("role", "sort_order", "pk")
        ],
        "lead_contact": {
            "name": {"first": filer.first_name, "middle": filer.middle_name, "last": filer.last_name},
            "email": notice,
        },
        "tyler_payment_id": draft.selected_payment_account_id,
        "comments_to_clerk": "",
        "return_date": "",
    }
    if draft.existing_case == "existing":
        payload.update(previous_case_id=draft.previous_case_id, docket_number=draft.docket_number)
    if draft.amount_in_controversy:
        payload["amount_in_controversy"] = str(draft.amount_in_controversy)
    fields = draft.supplemental_fields or {}
    if fields.get("lower_court_code") and draft.existing_case != "existing":
        payload["lower_court_case"] = {
            "title": fields.get("lower_court_title", ""),
            "docket_number": fields.get("lower_court_docket_number", ""),
            "judge": fields.get("lower_court_judge", ""),
        }
        payload["trial_court"] = {
            "name": fields.get("lower_court_name", ""),
            "tyler_lower_court_code": fields["lower_court_code"],
            "tyler_prod_lower_court_code": fields.get("lower_court_prod_code", ""),
        }
    bind_filing_documents(payload, draft)
    reconcile_case_parties(payload, draft, draft.jurisdiction, draft.court_code)
    add_cook_srl_reference(payload, draft, lookups=lookups)
    return normalize_efile_payload(payload, draft.jurisdiction, draft.court_code, lookups=lookups)

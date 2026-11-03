"""Court identity regressions, independent of staging availability."""

import copy
import json
from pathlib import Path

import pytest
from django.conf import settings
from django.db import IntegrityError, transaction
from django.urls import reverse

from efile.models import FilingDraft, FilingParty
from efile.services.drafts import read_case_data, write_case_data
from efile.services.efsp_payload import PayloadValidationError
from efile.services.existing_cases import (
    CaseImportError,
    apply_case,
    entity_id,
    import_ready,
    normalize_case,
    reconcile_case_parties,
)
from efile.services.extracted_parties import save_reviewed_parties
from efile.services.filing_path import change_filing_path
from efile.services.people import (
    absorb_filer_duplicates,
    claim_party_as_filer,
    discard_empty_parties,
    ensure_required_parties,
    party_is_complete,
    set_filing_parties,
)
from efile.tests.test_people_flow import authorize
from efile.workflow import ExistingCase

FIXTURES = Path(__file__).parent / "fixtures" / "existing_cases"


def normalized(filename="2025SC5-raw.json"):
    raw = json.loads((FIXTURES / filename).read_text())
    case = raw["value"]
    court = next(item["value"]["caseCourt"] for item in case["rest"] if "caseCourt" in item["value"])
    court = court["organizationIdentification"]["value"]["identificationID"]["value"]
    types = json.loads((FIXTURES / (court + "-party-types.json")).read_text())
    lineage = next(item["value"]["caseLineageCase"] for item in case["rest"] if "caseCourt" in item["value"])
    tracking = lineage[0]["caseTrackingID"]["value"]
    snapshot = normalize_case(
        raw, court=court, tracking_id=tracking, party_types=types, docket_number=case["caseDocketID"]["value"]
    )
    snapshot.update(confirmed=True, source_environment=settings.EFSP_URL)
    return snapshot


@pytest.fixture
def imported(client, django_user_model):
    snapshot = normalized()
    user = django_user_model.objects.create_user(
        username="existing-owner", email="contact@example.com", tyler_jurisdiction="illinois"
    )
    draft = FilingDraft.objects.create(
        user=user,
        jurisdiction="illinois",
        existing_case=ExistingCase.EXISTING,
        court_code=snapshot["court"],
        previous_case_id=snapshot["tracking_id"],
        docket_number=snapshot["docket_number"],
    )
    apply_case(draft, snapshot)
    FilingParty.objects.create(
        draft=draft, role="filer", first_name="Account", last_name="Contact", email="contact@example.com"
    )
    client.force_login(user)
    authorize(client, draft)
    return draft


@pytest.mark.parametrize("filename", sorted(p.name for p in FIXTURES.glob("*-raw.json")))
def test_sanitized_staging_variations(filename):
    snapshot = normalized(filename)
    assert snapshot["status"] == "loaded"
    counts = {
        "2025SC5-raw.json": 3,
        "2024EV001752-raw.json": 4,
        "2005SC000985-raw.json": 4,
        "2019-D-0000655-raw.json": 5,
        "2019-SC-0001642-raw.json": 2,
        "2024SC00003388-raw.json": 2,
        "2024DC00000572-raw.json": 2,
    }
    assert len(snapshot["parties"]) == counts[filename]
    assert all(p["external_party_id"] for p in snapshot["parties"])
    assert len({p["external_party_id"] for p in snapshot["parties"]}) == len(snapshot["parties"])
    assert all(p["representation"]["status"] in {"unknown", "represented"} for p in snapshot["parties"])


def test_id_selects_category_not_last_identification():
    assert (
        entity_id(
            {
                "personOtherIdentification": [
                    {
                        "identificationCategory": {"value": {"value": "CASEPARTYID"}},
                        "identificationID": {"value": "court-id"},
                    },
                    {
                        "identificationCategory": {"value": {"value": "OTHER"}},
                        "identificationID": {"value": "wrong-id"},
                    },
                ]
            }
        )
        == "court-id"
    )


def test_case_attorney_associations_names_and_contacts_are_preserved():
    snapshot = normalized()
    represented = [p for p in snapshot["parties"] if p["representation"]["status"] == "represented"]
    assert len(represented) == 1
    attorneys = [a for p in represented for a in p["representation"]["attorneys"]]
    assert len(attorneys) == 1
    assert all(a["id_category"] == "CASEPARTYATTORNEYID" for a in attorneys)
    assert all(a["id"] and a["bar_number"] == "123456" for a in attorneys)
    assert all(a["first_name"] == "Sample" and a["email"] == "sample@example.com" for a in attorneys)
    assert len({a["id"] for a in attorneys}) == 1


@pytest.mark.django_db
def test_reimport_preserves_rows_selections_and_equal_names(imported):
    selected = imported.parties.filter(source="court").first()
    set_filing_parties(imported, [selected])
    ids = list(imported.parties.filter(source="court").values_list("pk", flat=True))
    apply_case(imported, normalized())
    assert list(imported.parties.filter(source="court").values_list("pk", flat=True)) == ids
    selected.refresh_from_db()
    assert selected.is_filing_party
    assert imported.parties.filter(source="court").count() == 3
    assert import_ready(imported)
    assert read_case_data(imported)["filing_parties"][1]["external_party_id"]
    with pytest.raises(IntegrityError), transaction.atomic():
        FilingParty.objects.create(
            draft=imported,
            role="other",
            sort_order=99,
            source="court",
            source_case_id=selected.source_case_id,
            external_party_id=selected.external_party_id,
        )


@pytest.mark.django_db
def test_claim_and_cleanup_preserve_court_identity(imported):
    party = imported.parties.filter(source="court").first()
    original = (party.pk, party.first_name, party.external_party_id)
    claim_party_as_filer(imported, party)
    absorb_filer_duplicates(imported)
    discard_empty_parties(imported)
    party.refresh_from_db()
    assert (party.pk, party.first_name, party.external_party_id) == original
    assert party.is_self and party.is_filing_party
    assert not imported.parties.get(role="filer").party_type
    assert party_is_complete(party)
    types = [{"code": p.party_type, "name": "Role", "required": True} for p in imported.parties.filter(source="court")]
    ensure_required_parties(imported, types)
    assert imported.parties.count() == 4


@pytest.mark.django_db
def test_stale_import_cannot_restore_another_case(imported):
    snapshot = normalized()
    write_case_data(imported, {"previous_case_id": "case-B"})
    assert not imported.parties.filter(source="court").exists()
    with pytest.raises(CaseImportError, match="changed"):
        apply_case(imported, snapshot)
    imported.refresh_from_db()
    assert imported.previous_case_id == "case-B"
    assert not imported.existing_case_snapshot


@pytest.mark.django_db
def test_switch_to_new_clears_import_and_quote(imported):
    imported.quoted_fee_total = "12"
    imported.save()
    change_filing_path(imported, ExistingCase.NEW)
    assert not imported.parties.filter(source="court").exists()
    assert imported.parties.get(role="filer").email == "contact@example.com"
    assert not imported.existing_case_snapshot
    assert not imported.quoted_fee_total


@pytest.mark.django_db
def test_crafted_party_edit_remove_and_extraction_do_not_change_court(imported, client, monkeypatch):
    monkeypatch.setattr(
        "efile.views.parties.get_party_types", lambda draft: [{"code": "1", "name": "Role", "required": False}]
    )
    party = imported.parties.filter(source="court").first()
    before = read_case_data(imported)["filing_parties"]
    url = reverse("parties", kwargs={"jurisdiction": "illinois"})
    assert client.post(url, {"action": "remove", "party_id": party.pk}).status_code == 302
    details = reverse("party_details", kwargs={"jurisdiction": "illinois"})
    assert client.post(f"{details}?party={party.pk}", {"first_name": "Tampered"}).status_code == 302
    save_reviewed_parties(imported, [{"id": party.pk, "name": "Tampered", "side": "other"}])
    write_case_data(imported, {"other_first_name": "Tampered", "other_party_type": "wrong"})
    assert read_case_data(imported)["filing_parties"] == before
    response = client.get(url)
    assert response.status_code == 200
    assert b"Already on the court case" in response.content
    assert f"?party={party.pk}".encode() not in response.content


def payload_for(draft):
    from efile.services.drafts import read_case_data

    rows = read_case_data(draft)["filing_parties"]
    parties = [
        {
            "tyler_id": p["external_party_id"],
            "is_new": False,
            "party_type": p["party_type"],
            "name": {
                "first": p["organization_name"] or p["first_name"],
                "middle": p["middle_name"],
                "last": p["last_name"],
                "suffix": p["suffix"],
            },
        }
        for p in rows
        if p["source"] == "court"
    ]
    set_filing_parties(draft, [draft.parties.filter(source="court").first()])
    return {
        "previous_case_id": draft.previous_case_id,
        "user_started_case": False,
        "users": parties[:1],
        "other_parties": parties[1:],
        "al_court_bundle": [{"filing_parties": ["users[0]"]}],
    }


@pytest.mark.django_db
@pytest.mark.parametrize("mutation", ["foreign", "rename", "new", "duplicate", "missing", "reference", "case"])
def test_reject_substituted_identity(imported, mutation):
    payload = payload_for(imported)
    if mutation == "foreign":
        payload["users"][0]["tyler_id"] = "foreign-id"
    if mutation == "rename":
        payload["users"][0]["name"]["first"] = "Tampered"
    if mutation == "new":
        payload["users"][0]["is_new"] = True
    if mutation == "duplicate":
        payload["other_parties"].append(copy.deepcopy(payload["users"][0]))
    if mutation == "missing":
        payload["other_parties"].pop()
    if mutation == "reference":
        payload["al_court_bundle"][0]["filing_parties"] = ["users[99]"]
    if mutation == "case":
        payload["previous_case_id"] = "different-case"
    with pytest.raises(PayloadValidationError):
        reconcile_case_parties(payload, imported, "illinois", imported.court_code)


@pytest.mark.django_db
def test_authoritative_payload_and_incomplete_import(imported):
    payload = payload_for(imported)
    payload["users"][0]["attorneys"] = ["substituted"]
    reconcile_case_parties(payload, imported, "illinois", imported.court_code)
    assert all(p["is_new"] is False and p["tyler_id"] for p in payload["users"] + payload["other_parties"])
    assert "attorneys" not in payload["users"][0]
    imported.existing_case_snapshot["status"] = "partial"
    imported.save()
    with pytest.raises(PayloadValidationError, match="load its parties"):
        reconcile_case_parties(payload, imported, "illinois", imported.court_code)


@pytest.mark.django_db
def test_unimported_resume_returns_to_confirmation(imported, client):
    imported.existing_case_snapshot = {}
    imported.save()
    for step in ("parties", "payment", "case_review"):
        response = client.get(reverse(step, kwargs={"jurisdiction": "illinois"}))
        assert response.status_code == 302
        assert "/case-confirmation/" in response.url


@pytest.mark.django_db
def test_confirmation_preview_and_confirm_import_idempotently(imported, client, monkeypatch):
    imported.parties.filter(source="court").delete()
    imported.existing_case_snapshot = {}
    imported.save()
    snapshot = normalized()
    snapshot.pop("confirmed")
    monkeypatch.setattr("efile.services.existing_cases.fetch_case", lambda *args: copy.deepcopy(snapshot))
    url = reverse("case_confirmation", kwargs={"jurisdiction": "illinois"})
    response = client.get(url)
    assert response.status_code == 200
    assert b"Already on the court case" in response.content
    assert not imported.parties.filter(source="court").exists()
    response = client.post(url, {"confirmed": "yes"})
    assert response.status_code == 302
    imported.refresh_from_db()
    assert import_ready(imported)
    ids = list(imported.parties.filter(source="court").values_list("pk", flat=True))
    response = client.post(url, {"confirmed": "yes"})
    assert response.status_code == 302
    assert list(imported.parties.filter(source="court").values_list("pk", flat=True)) == ids


@pytest.mark.django_db
def test_failed_confirmation_blocks_and_offers_retry(imported, client, monkeypatch):
    imported.existing_case_snapshot = {}
    imported.save()

    def unavailable(*args):
        raise CaseImportError("Retry or sign in again.")

    monkeypatch.setattr("efile.services.existing_cases.fetch_case", unavailable)
    response = client.post(reverse("case_confirmation", kwargs={"jurisdiction": "illinois"}), {"confirmed": "yes"})
    assert response.status_code == 422
    assert b"Retry loading the case" in response.content
    imported.refresh_from_db()
    assert imported.existing_case_snapshot["status"] == "failed"
    assert not import_ready(imported)


@pytest.mark.integration
@pytest.mark.django_db(transaction=True)
def test_browser_lookup_confirmation_party_selection_review(imported, client, live_server, monkeypatch):
    import os
    import shutil
    import subprocess

    from efile.models import FilingDocument
    from efile.tests.helpers import reviewed_document

    app = Path(__file__).resolve().parents[2]
    if not shutil.which("node") or not (app / "node_modules/@playwright/test").exists():
        pytest.skip("Install browser test dependencies to run the existing-case browser flow.")
    snapshot = normalized()
    snapshot.pop("confirmed")
    imported.parties.filter(source="court").delete()
    imported.previous_case_id = ""
    imported.docket_number = ""
    imported.existing_case_snapshot = {}
    imported.selected_payment_account_id = "synthetic-payment"
    imported.save()
    reviewed_document(
        draft=imported,
        role=FilingDocument.Role.LEAD,
        sort_order=0,
        name="motion.pdf",
        public_url="https://example.com/motion.pdf",
        filing_type_code="synthetic-motion",
        document_type_code="synthetic-nonconfidential",
        document_type_confirmed=True,
        filing_component_code="synthetic-lead",
    )
    monkeypatch.setattr("efile.services.existing_cases.fetch_case", lambda *args: copy.deepcopy(snapshot))
    types = [{"code": p["party_type"], "name": p["party_type_name"], "required": False} for p in snapshot["parties"]]
    monkeypatch.setattr("efile.views.parties.get_party_types", lambda draft: types)
    monkeypatch.setattr("efile.views.parties.get_case_questions", lambda draft: [])
    monkeypatch.setattr("efile.views.review.get_case_questions", lambda draft: [])
    config = {
        "base": live_server.url,
        "session": client.session.session_key,
        "draft": imported.pk,
        "court": snapshot["court"],
        "tracking": snapshot["tracking_id"],
        "docket": snapshot["docket_number"],
        "count": len(snapshot["parties"]),
    }
    result = subprocess.run(
        ["node", str(app / "tests/existing-case-flow.js")],
        env={**os.environ, "EXISTING_CASE_BROWSER_CONFIG": json.dumps(config)},
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.django_db
def test_explicit_new_party_stays_new_and_survives_reconfirmation(imported, client, monkeypatch):
    monkeypatch.setattr(
        "efile.views.parties.get_party_types", lambda draft: [{"code": "20641", "name": "Defendant", "required": False}]
    )
    response = client.post(reverse("parties", kwargs={"jurisdiction": "illinois"}), {"action": "add"})
    assert response.status_code == 302
    added = imported.parties.get(source="added")
    added.first_name, added.last_name, added.party_type = "New", "Person", "20641"
    added.save()
    apply_case(imported, normalized())
    added.refresh_from_db()
    payload = payload_for(imported)
    payload["other_parties"].append(
        {
            "_draft_party_id": added.pk,
            "is_new": True,
            "party_type": added.party_type,
            "name": {"first": "New", "middle": "", "last": "Person", "suffix": ""},
        }
    )
    reconcile_case_parties(payload, imported, "illinois", imported.court_code)
    new_party = payload["other_parties"][-1]
    assert new_party["is_new"] is True
    assert "tyler_id" not in new_party
    assert "_draft_party_id" not in new_party


def test_detail_local_id_never_replaces_the_search_uuid():
    snapshot = normalized("2024EV001752-raw.json")
    assert snapshot["tracking_id"] != snapshot["court_local_case_id"]
    assert snapshot["tracking_id"] == "8534ec52-0fa8-4e77-91ca-55af362742c3"


@pytest.mark.django_db
def test_missing_import_identity_is_partial_and_not_a_new_placeholder(imported):
    snapshot = normalized()
    snapshot["parties"][0]["external_party_id"] = ""
    snapshot["status"] = "partial"
    apply_case(imported, snapshot)
    missing = imported.parties.get(source="court", external_party_id="")
    assert not party_is_complete(missing)
    count = imported.parties.count()
    ensure_required_parties(imported, [{"code": "unknown", "name": "Unresolved", "required": True}])
    assert imported.parties.count() == count
    assert not import_ready(imported)


@pytest.mark.django_db
def test_fetch_uses_selected_uuid_authenticated_headers_and_full_role_catalog(imported, monkeypatch):
    from efile.services.existing_cases import fetch_case

    raw = json.loads((FIXTURES / "2025SC5-raw.json").read_text())
    types = json.loads((FIXTURES / "marion-party-types.json").read_text())
    calls = []

    class Response:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Response(types if url.endswith("/party_types") else raw)

    monkeypatch.setattr("efile.services.existing_cases.requests.get", get)
    snapshot = fetch_case(imported, "test-token")
    assert snapshot["status"] == "loaded"
    assert snapshot["tracking_id"] == imported.previous_case_id
    # The two requests run concurrently, so find each by its URL.
    case_call = next(call for call in calls if call[0].endswith("/" + imported.previous_case_id))
    codes_call = next(call for call in calls if call[0].endswith("/party_types"))
    assert case_call[1]["headers"]["tyler-token-illinois"] == "test-token"
    assert "/case_types/" not in codes_call[0]
    assert snapshot["retrieved_at"]


def test_absent_roster_differs_from_successful_empty_roster():
    raw = json.loads((FIXTURES / "2025SC5-raw.json").read_text())
    snapshot = normalized()
    augmentation = next(i["value"] for i in raw["value"]["rest"] if "caseParticipant" in i["value"])
    augmentation["caseParticipant"] = []
    assert (
        normalize_case(raw, court=snapshot["court"], tracking_id=snapshot["tracking_id"], party_types=[])["status"]
        == "loaded"
    )
    del augmentation["caseParticipant"]
    with pytest.raises(CaseImportError, match="did not return a party roster"):
        normalize_case(raw, court=snapshot["court"], tracking_id=snapshot["tracking_id"], party_types=[])


@pytest.mark.parametrize("invalid", ["attorney_reference", "represented_reference", "party_id", "case_title"])
def test_malformed_detail_is_an_actionable_import_error(invalid):
    raw = json.loads((FIXTURES / "2025SC5-raw.json").read_text())
    snapshot = normalized()
    augmentation = next(i["value"] for i in raw["value"]["rest"] if "caseParticipant" in i["value"])
    if invalid == "attorney_reference":
        augmentation["caseOtherEntityAttorney"] = [{"roleOfPersonReference": "unreadable"}]
    elif invalid == "represented_reference":
        augmentation["caseOtherEntityAttorney"] = [{"caseRepresentedPartyReference": ["unreadable"]}]
    elif invalid == "party_id":
        entity = next(
            p["value"]["entityRepresentation"]["value"]
            for p in augmentation["caseParticipant"]
            if entity_id(p["value"]["entityRepresentation"]["value"])
        )
        identification = next(p for p in entity["personOtherIdentification"] if entity_id({"id": p}))
        identification["identificationID"] = {"value": "x" * 256}
    else:
        raw["value"]["caseTitleText"] = {"value": "x" * 501}
    with pytest.raises(CaseImportError, match="Retry loading|too long to import"):
        normalize_case(
            raw,
            court=snapshot["court"],
            tracking_id=snapshot["tracking_id"],
            party_types=json.loads((FIXTURES / "marion-party-types.json").read_text()),
        )


@pytest.mark.django_db
def test_identical_reimport_keeps_current_quote(imported):
    from efile.services.fee_quotes import fee_quote_summary, record_fee_quote

    set_filing_parties(imported, [imported.parties.filter(source="court").first()])
    record_fee_quote(imported, "5.00", [])
    apply_case(imported, normalized())
    assert fee_quote_summary(imported)["state"] == "current"
    assert imported.quoted_fee_total == "5.00"


@pytest.mark.django_db
def test_account_link_does_not_merge_an_added_party_by_name(imported):
    added = FilingParty.objects.create(
        draft=imported,
        role="other",
        sort_order=100,
        source="added",
        source_case_id=imported.previous_case_id,
        first_name="Account",
        last_name="Contact",
    )
    claim_party_as_filer(imported, imported.parties.filter(source="court").first())
    absorb_filer_duplicates(imported)
    assert imported.parties.filter(pk=added.pk).exists()
    claim_party_as_filer(imported, added)
    added.refresh_from_db()
    assert added.is_self and added.is_filing_party
    assert imported.parties.get(role="filer").first_name == "Account"


@pytest.mark.django_db
def test_revisiting_people_preserves_selection_separately_from_account_link(imported, client, monkeypatch):
    monkeypatch.setattr("efile.views.parties.get_party_types", lambda draft: [])
    party, other = list(imported.parties.filter(source="court"))[:2]
    filer = imported.parties.get(role="filer")
    filer.first_name, filer.last_name = party.first_name, party.last_name
    filer.save()
    claim_party_as_filer(imported, party)
    set_filing_parties(imported, [other])
    response = client.get(reverse("parties", kwargs={"jurisdiction": "illinois"}))
    assert response.status_code == 200
    party.refresh_from_db()
    other.refresh_from_db()
    assert party.is_self and not party.is_filing_party
    assert other.is_filing_party


@pytest.mark.django_db
def test_missing_required_role_requires_explicit_addition(imported, client, monkeypatch):
    types = [{"code": "missing-required-role", "name": "Required role", "required": True}]
    monkeypatch.setattr("efile.views.parties.get_party_types", lambda draft: types)
    before = imported.parties.count()
    ensure_required_parties(imported, types)
    selected = imported.parties.filter(source="court").first()
    url = reverse("parties", kwargs={"jurisdiction": "illinois"})
    response = client.post(url, {"action": "continue", "filing_for": [selected.pk]}, follow=True)
    assert response.status_code == 200
    assert b"The court requires these roles" in response.content
    assert imported.parties.count() == before


def _augmentation(raw):
    return next(i["value"] for i in raw["value"]["rest"] if "caseParticipant" in i["value"])


def _normalize(raw, snapshot):
    return normalize_case(
        raw,
        court=snapshot["court"],
        tracking_id=snapshot["tracking_id"],
        party_types=json.loads((FIXTURES / "marion-party-types.json").read_text()),
    )


@pytest.mark.django_db
def test_classification_change_keeps_new_case_filing_choices(django_user_model):
    from efile.services.filing_path import clear_changed_classification

    user = django_user_model.objects.create_user(username="new-case-owner", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(
        user=user, jurisdiction="illinois", existing_case=ExistingCase.NEW, court_code="old", case_type_code="1"
    )
    filer = FilingParty.objects.create(draft=draft, role="filer", first_name="Account", last_name="Owner")
    other = FilingParty.objects.create(draft=draft, role="other", first_name="Someone", last_name="Else")
    set_filing_parties(draft, [other])
    filer.is_self = True
    filer.save()
    clear_changed_classification(draft, "new", "", "")
    other.refresh_from_db()
    filer.refresh_from_db()
    assert other.is_filing_party
    assert filer.is_self


def test_wrapped_and_unreadable_lineage_entries_still_match():
    raw = json.loads((FIXTURES / "2025SC5-raw.json").read_text())
    snapshot = normalized()
    holder = next(i["value"] for i in raw["value"]["rest"] if "caseLineageCase" in i["value"])
    holder["caseLineageCase"] = ["unreadable", *({"value": entry} for entry in holder["caseLineageCase"])]
    assert _normalize(raw, snapshot)["tracking_id"] == snapshot["tracking_id"]


@pytest.mark.parametrize(("country", "expected"), [("USA", "US"), ("us", "US"), ("United Kingdom", "")])
def test_country_is_normalized_rather_than_failing_the_import(country, expected):
    raw = json.loads((FIXTURES / "2025SC5-raw.json").read_text())
    snapshot = normalized()

    def visit(value):
        if isinstance(value, dict):
            if "locationCountry" in value:
                value["locationCountry"] = {"value": country}
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(_augmentation(raw)["caseParticipant"])
    countries = {p["country"] for p in _normalize(raw, snapshot)["parties"] if p["address_line_1"]}
    assert countries == {expected}


def test_unidentified_attorney_reference_is_not_shared_by_parties_without_ids():
    raw = json.loads((FIXTURES / "2025SC5-raw.json").read_text())
    snapshot = normalized()
    augmentation = _augmentation(raw)
    unrepresented = {p["external_party_id"] for p in snapshot["parties"] if p["representation"]["status"] == "unknown"}
    for participant in augmentation["caseParticipant"]:
        entity = participant["value"]["entityRepresentation"]["value"]
        if entity_id(entity) in unrepresented:
            entity["personOtherIdentification"] = []
    augmentation["caseOtherEntityAttorney"][0]["caseRepresentedPartyReference"].append({"ref": {"personName": {}}})
    result = _normalize(raw, snapshot)
    assert all(p["representation"]["status"] == "unknown" for p in result["parties"] if not p["external_party_id"])


@pytest.mark.django_db
def test_linking_yourself_keeps_co_parties_already_chosen(imported):
    mine, co_party = list(imported.parties.filter(source="court"))[:2]
    set_filing_parties(imported, [mine, co_party])
    claim_party_as_filer(imported, mine)
    mine.refresh_from_db()
    co_party.refresh_from_db()
    assert mine.is_self and mine.is_filing_party
    assert co_party.is_filing_party


@pytest.mark.django_db
def test_added_organization_name_wins_over_a_stale_first_name(imported):
    added = FilingParty.objects.create(
        draft=imported,
        role="other",
        sort_order=100,
        source="added",
        source_case_id=imported.previous_case_id,
        first_name="Stale",
        organization_name="Example LLC",
        party_type=imported.parties.filter(source="court").first().party_type,
    )
    court = list(imported.parties.filter(source="court"))
    set_filing_parties(imported, [court[0]])
    imported.refresh_from_db()

    def entry(party, **extra):
        name = {"first": party.first_name, "middle": party.middle_name, "last": party.last_name, "suffix": party.suffix}
        return {"name": name, "party_type": party.party_type, "tyler_id": party.external_party_id, "is_new": False}

    added_entry = {
        "name": {"first": "Example LLC", "middle": "", "last": "", "suffix": ""},
        "party_type": added.party_type,
        "is_new": True,
        "_draft_party_id": added.pk,
    }
    payload = {
        "previous_case_id": imported.previous_case_id,
        "user_started_case": False,
        "users": [entry(court[0])],
        "other_parties": [*(entry(p) for p in court[1:]), added_entry],
    }
    reconcile_case_parties(payload, imported, "illinois", imported.court_code)
    assert payload["other_parties"][-1]["name"]["first"] == "Example LLC"


@pytest.mark.django_db
def test_confirmation_reuses_a_recent_preview(imported, client, monkeypatch):
    from django.utils import timezone

    imported.parties.filter(source="court").delete()
    imported.existing_case_snapshot = {}
    imported.save()
    snapshot = normalized()
    snapshot.pop("confirmed")
    calls = []

    def fetch(*args):
        calls.append(args)
        return {**copy.deepcopy(snapshot), "retrieved_at": timezone.now().isoformat()}

    monkeypatch.setattr("efile.services.existing_cases.fetch_case", fetch)
    url = reverse("case_confirmation", kwargs={"jurisdiction": "illinois"})
    assert client.get(url).status_code == 200
    assert client.get(url).status_code == 200
    assert client.post(url, {"confirmed": "yes"}).status_code == 302
    assert len(calls) == 1
    imported.refresh_from_db()
    assert import_ready(imported)


@pytest.mark.django_db
def test_blocked_confirmation_still_shows_the_court_roster(imported, client, monkeypatch):
    from django.utils import timezone

    imported.existing_case_snapshot = {}
    imported.save()
    snapshot = normalized()
    snapshot.pop("confirmed")
    partial = {
        **snapshot,
        "status": "partial",
        "problems": ["Party 1 has an unrecognized court role (X)."],
        "retrieved_at": timezone.now().isoformat(),
    }
    monkeypatch.setattr("efile.services.existing_cases.fetch_case", lambda *args: copy.deepcopy(partial))
    response = client.post(reverse("case_confirmation", kwargs={"jurisdiction": "illinois"}), {"confirmed": "yes"})
    assert response.status_code == 422
    assert b"unrecognized court role" in response.content
    assert b"Already on the court case" in response.content


@pytest.mark.django_db
def test_claiming_a_court_party_hides_only_the_account_roster_row(imported, client, monkeypatch):
    monkeypatch.setattr("efile.views.parties.get_party_types", lambda draft: [])
    url = reverse("parties", kwargs={"jurisdiction": "illinois"})
    filer = imported.parties.get(role="filer")
    before = client.get(url)
    assert filer.pk in [item["party"].pk for item in before.context["roster"]]
    party = imported.parties.filter(source="court").first()
    original_identity = (party.pk, party.first_name, party.external_party_id)
    response = client.post(url, {"action": "claim_party", "party_id": party.pk}, follow=True)
    assert response.status_code == 200
    shown = [item["party"].pk for item in response.context["roster"]]
    assert filer.pk not in shown
    assert response.context["has_linked_party"]
    assert not next(item for item in response.context["roster"] if item["party"].pk == party.pk)["claimable"]
    assert set(shown) == set(imported.parties.exclude(role="filer").values_list("pk", flat=True))
    party.refresh_from_db()
    assert (party.pk, party.first_name, party.external_party_id) == original_identity
    assert party.is_self and party.is_filing_party
    filer.refresh_from_db()
    assert filer.email == "contact@example.com"

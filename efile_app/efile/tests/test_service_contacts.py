from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.http import QueryDict

from efile.services.efsp_payload import PayloadValidationError, add_cook_srl_reference
from efile.services.service_contacts import (
    KEY,
    ServiceContactError,
    bind_service,
    electronic_service_types,
    parse_contacts,
    save_contacts,
    scope,
)


@pytest.fixture
def draft():
    return SimpleNamespace(
        jurisdiction="illinois",
        court_code="adams",
        case_type_code="civil",
        previous_case_id="",
        existing_case="new",
        supplemental_fields={},
        save=Mock(),
    )


@pytest.fixture
def court_codes(monkeypatch):
    replies = {
        "codes": {"allowserviceoninitial": "TRUE"},
        "FilingServiceCheckBoxInitial": {"isvisible": True},
        "FilingServiceCheckBoxSubsequent": {"isvisible": True},
        "service_types": [{"code": "-580", "name": "Email", "servicemethod": "ESERVICE"}],
    }
    monkeypatch.setattr(
        "efile.services.efsp_payload._EfspLookups.get", lambda self, url: replies.get(url.rsplit("/", 1)[-1])
    )
    return replies


def selection(draft):
    return {
        "scope": scope(draft),
        "rules_checked": True,
        "service_type": "-580",
        "contacts": [{"contact_id": "person", "name": "Taylor Example", "email": "taylor@example.com"}],
    }


def test_payload_uses_saved_selection_and_serves_every_document(draft, court_codes):
    draft.supplemental_fields[KEY] = selection(draft)
    payload = {"service_contacts": [{"contact_id": "injected"}], "al_court_bundle": [{}, {}]}
    bind_service(payload, draft)
    assert payload["service_contacts"] == [{"contact_id": "person", "service_type": "-580"}]
    assert all(item["filing_action"] == "efile_and_serve" for item in payload["al_court_bundle"])
    assert all(item["preliminary_copies"] == [] for item in payload["al_court_bundle"])


def test_no_saved_selection_strips_client_service(draft):
    payload = {"service_contacts": [{"contact_id": "injected"}], "al_court_bundle": [{"filing_action": "serve"}]}
    bind_service(payload, draft)
    assert "service_contacts" not in payload
    assert payload["al_court_bundle"][0]["filing_action"] == "efile"


@pytest.mark.parametrize(
    "field,value",
    [
        ("court_code", "dupage"),
        ("previous_case_id", "new-case"),
        ("case_type_code", "family"),
        ("existing_case", "existing"),
    ],
)
def test_case_changes_require_recipient_review(draft, field, value):
    draft.supplemental_fields[KEY] = selection(draft)
    setattr(draft, field, value)
    with pytest.raises(PayloadValidationError, match="case details changed"):
        bind_service({}, draft)


@pytest.mark.parametrize(
    "initial,flag,visible", [(True, "FALSE", True), (True, "DEFAULT", False), (False, "TRUE", False)]
)
def test_explicit_court_service_denial(draft, court_codes, initial, flag, visible):
    draft.existing_case = "new" if initial else "existing"
    court_codes["codes"] = {"allowserviceoninitial": flag}
    court_codes["FilingServiceCheckBoxInitial"] = {"isvisible": visible}
    court_codes["FilingServiceCheckBoxSubsequent"] = {"isvisible": visible}
    with pytest.raises(ServiceContactError, match="does not allow"):
        electronic_service_types(draft)


def test_unknown_court_rules_do_not_enable_service(draft, court_codes):
    court_codes.clear()
    with pytest.raises(ServiceContactError, match="could not check"):
        electronic_service_types(draft)


def test_changed_service_type_blocks_submission(draft, court_codes):
    draft.supplemental_fields[KEY] = selection(draft)
    court_codes["service_types"][0]["code"] = "new-code"
    with pytest.raises(PayloadValidationError, match="options changed"):
        bind_service({}, draft)


def test_save_checks_ids_and_acknowledgement(draft, monkeypatch):
    monkeypatch.setattr(
        "efile.services.service_contacts.service_options",
        lambda *args: ([{"contact_id": "person", "name": "Taylor"}], [{"code": "-580"}]),
    )
    with pytest.raises(ServiceContactError, match="rules"):
        save_contacts(draft, "token", QueryDict("service_contact=person&service_type=-580"))
    with pytest.raises(ServiceContactError, match="changed"):
        save_contacts(draft, "token", QueryDict("service_contact=stranger&service_type=-580&service_rules_checked=yes"))
    save_contacts(draft, "token", QueryDict("service_contact=person&service_type=-580&service_rules_checked=yes"))
    assert draft.supplemental_fields[KEY]["contacts"][0]["contact_id"] == "person"
    save_contacts(draft, "token", QueryDict())
    assert draft.supplemental_fields[KEY] == {}


def test_case_contact_response_and_firm_response():
    firm = {"serviceContactID": "firm-id", "firstName": "Taylor", "lastName": "Example", "email": "t@example.com"}
    case = {
        "entityRepresentation": {
            "value": {
                "personName": {"personGivenName": {"value": "Alex"}},
                "personAugmentation": {
                    "electronicServiceInformation": {"serviceRecipientID": {"identificationID": {"value": "case-id"}}}
                },
            }
        }
    }
    assert [item["contact_id"] for item in parse_contacts([firm, case])] == ["firm-id", "case-id"]


def test_cook_reference_uses_environment_code(draft, court_codes):
    draft.court_code = "cook:cvd1"
    court_codes["cross_references"] = [{"code": "production-code", "efspcode": "COOKCOUNTYATTORNEY"}]
    payload = {"cross_references": {"254500": "99500"}}
    add_cook_srl_reference(payload, draft)
    assert payload["cross_references"] == {"production-code": "99500"}


def test_cook_lookup_failure_blocks_instead_of_guessing(draft, court_codes):
    draft.court_code = "cook:cvd1"
    with pytest.raises(PayloadValidationError, match="could not check"):
        add_cook_srl_reference({}, draft)


@pytest.fixture
def db_draft(client, django_user_model):
    from efile.models import FilingDraft, FilingParty
    from efile.services.current_drafts import CURRENT_DRAFT_SESSION_KEY

    user = django_user_model.objects.create_user(username="service-filer", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(
        user=user,
        jurisdiction="illinois",
        workflow_version=2,
        existing_case="new",
        court_code="adams",
        case_type_code="civil",
    )
    FilingParty.objects.create(
        draft=draft, role="filer", first_name="Taylor", last_name="Example", email="taylor@example.com"
    )
    client.force_login(user)
    session = client.session
    session[CURRENT_DRAFT_SESSION_KEY] = draft.pk
    session["jurisdiction"] = "illinois"
    session["auth_tokens"] = {"TYLER-TOKEN-ILLINOIS": "token"}
    session.save()
    return draft


@pytest.mark.django_db
def test_save_view_checks_scope_and_invalidates_fee_inputs(client, db_draft, monkeypatch):
    from django.core import signing
    from django.urls import reverse

    from efile.services.fee_quotes import fee_fingerprint

    monkeypatch.setattr(
        "efile.services.service_contacts.service_options",
        lambda *args: ([{"contact_id": "person", "name": "Alex Example"}], [{"code": "-580"}]),
    )
    before = fee_fingerprint(db_draft)
    data = {
        "draft": db_draft.pk,
        "service_scope": signing.dumps([db_draft.pk, scope(db_draft)], salt="service-contacts"),
        "service_contact": "person",
        "service_type": "-580",
        "service_rules_checked": "yes",
    }
    response = client.post(reverse("update_service_contacts", args=["illinois"]), data)
    assert response.status_code == 302
    db_draft.refresh_from_db()
    assert db_draft.supplemental_fields[KEY]["contacts"][0]["contact_id"] == "person"
    assert before != fee_fingerprint(db_draft)
    db_draft.court_code = "dupage"
    db_draft.save()
    client.post(reverse("update_service_contacts", args=["illinois"]), data)
    db_draft.refresh_from_db()
    assert db_draft.supplemental_fields[KEY]["scope"][1] == "adams"


@pytest.mark.django_db
def test_save_view_rejects_other_users_draft(client, db_draft, django_user_model):
    from django.core import signing
    from django.urls import reverse

    user = django_user_model.objects.create_user(username="other-filer", tyler_jurisdiction="illinois")
    db_draft.user = user
    db_draft.save()
    response = client.post(
        reverse("update_service_contacts", args=["illinois"]),
        {"draft": db_draft.pk, "service_scope": signing.dumps([db_draft.pk, scope(db_draft)], salt="service-contacts")},
    )
    assert response.status_code == 409


@pytest.mark.integration
@pytest.mark.django_db(transaction=True)
def test_service_picker_browser(client, db_draft, live_server, monkeypatch, tmp_path):
    import json
    import os
    import subprocess

    from django.conf import settings
    from django.urls import reverse

    if not os.getenv("CORE_FILING_UX_BROWSER_TESTS"):
        pytest.skip("Opt-in: requires Chromium")
    options = (
        [{"contact_id": "person", "name": "Alex Example", "email": "alex@example.com"}],
        [{"code": "-580", "name": "Email service", "servicemethod": "ESERVICE"}],
    )

    def add_browser_contact(draft, token, data):
        contact = {
            "contact_id": "new-person",
            "name": data["contact_first_name"] + " " + data["contact_last_name"],
            "email": data["contact_email"],
        }
        options[0][:] = [item for item in options[0] if item["contact_id"] != "new-person"]
        options[0].append(contact)
        return contact["contact_id"]

    monkeypatch.setattr("efile.views.service_contacts.create_contact", add_browser_contact)
    monkeypatch.setattr("efile.views.service_contacts.service_options", lambda *args: options)
    monkeypatch.setattr("efile.services.service_contacts.service_options", lambda *args: options)
    monkeypatch.setattr(
        "efile.views.parties.get_party_types", lambda *args: [{"code": "plaintiff", "name": "Plaintiff"}]
    )
    config = tmp_path / "service.json"
    config.write_text(
        json.dumps(
            {
                "baseUrl": live_server.url,
                "url": reverse("parties", args=["illinois"]) + f"?draft={db_draft.pk}",
                "uploadUrl": reverse("upload_documents", args=["illinois"]) + f"?draft={db_draft.pk}",
                "cookie": client.cookies[settings.SESSION_COOKIE_NAME].value,
            }
        )
    )
    result = subprocess.run(
        ["node", "tests/service-contacts-browser.js", str(config)],
        cwd=settings.BASE_DIR,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_conditional_types_query_uses_full_payload_and_filters_catalog(draft, court_codes, monkeypatch):
    court_codes["codes"]["hasconditionalservicetypes"] = True
    court_codes["service_types"].append({"code": "other", "name": "Other email", "servicemethod": "ESERVICE"})
    payload = {
        "users": [{"name": {"first": "Taylor"}}],
        "al_court_bundle": [{"filing_type": "motion", "filing_action": "efile_and_serve"}],
        "service_contacts": [{"contact_id": "person"}],
    }
    calls = []

    def query(draft, token, path, *, body, method, timeout):
        calls.append((token, path, body, method))
        return [{"serviceCodeID": {"value": "-580"}, "description": {"value": "Email"}}]

    monkeypatch.setattr("efile.services.service_contacts.proxy_request", query)
    types = electronic_service_types(draft, token="private-token", payload=payload)
    assert [item["code"] for item in types] == ["-580"]
    token, path, body, method = calls[0]
    assert method == "GET"
    assert path == "filingreview/courts/adams/filing/servicetypes"
    assert body["users"] == payload["users"]
    assert body["al_court_bundle"][0]["filing_action"] == "efile"
    assert "service_contacts" not in body
    assert payload["al_court_bundle"][0]["filing_action"] == "efile_and_serve"


def test_conditional_options_rechecked_at_submission(draft, court_codes, monkeypatch):
    court_codes["codes"]["hasconditionalservicetypes"] = True
    draft.supplemental_fields[KEY] = selection(draft)
    monkeypatch.setattr("efile.services.service_contacts.proxy_request", lambda *args, **kwargs: [])
    with pytest.raises(PayloadValidationError, match="does not offer"):
        bind_service({"al_court_bundle": [{}]}, draft, token="token")


def test_correction_needs_new_service_acknowledgement(draft):
    draft.supplemental_fields[KEY] = selection(draft)
    draft.correction_of_id = 1
    with pytest.raises(PayloadValidationError, match="case details changed"):
        bind_service({}, draft)


def test_organization_recipient_in_jaxb_rest():
    item = {
        "entityRepresentation": {
            "value": {
                "organizationName": {"value": "Example Legal Aid"},
                "rest": [
                    {
                        "value": {
                            "electronicServiceInformation": {
                                "serviceRecipientID": {"identificationID": {"value": "org-id"}}
                            }
                        }
                    }
                ],
            }
        }
    }
    assert parse_contacts([item])[0] == {"contact_id": "org-id", "name": "Example Legal Aid", "email": ""}


@pytest.mark.parametrize("flag", [True, {"value": True}])
def test_undeliverable_contacts_not_offered(flag):
    item = {"serviceContactID": "id", "firstName": "Alex", "lastName": "Example", "isUndeliverable": flag}
    assert parse_contacts([item]) == []


def test_cook_case_type_without_reference_does_not_get_a_guessed_code(draft, court_codes):
    draft.court_code = "cook:cvd1"
    court_codes["cross_references"] = []
    payload = {"cross_references": {"254500": "99500"}}
    add_cook_srl_reference(payload, draft)
    assert "cross_references" not in payload


@pytest.mark.django_db
def test_conditional_query_rebuilds_documents_and_people_from_draft(db_draft, court_codes, monkeypatch):
    from efile.models import FilingDocument
    from efile.services.efsp_payload import _EfspLookups
    from efile.services.service_query import service_query_payload

    filer = db_draft.parties.get(role="filer")
    filer.party_type = "plaintiff"
    filer.is_filing_party = True
    filer.save()
    document = FilingDocument.objects.create(
        draft=db_draft,
        role="lead",
        name="Motion.pdf",
        s3_key="owned/motion.pdf",
        filing_type_code="motion",
        document_type_code="public",
        filing_component_code="lead",
    )
    monkeypatch.setattr(
        "efile.services.efsp_payload.S3UploadHandler.get_public_url",
        lambda *args, **kwargs: "https://storage.invalid/owned.pdf",
    )
    monkeypatch.setattr("efile.services.efsp_payload.validate_party_formats", lambda *args: None)
    payload = service_query_payload(db_draft, _EfspLookups())
    assert payload["users"][0]["name"]["first"] == "Taylor"
    assert payload["users"][0]["party_type"] == "plaintiff"
    assert payload["lead_contact"]["email"] == "taylor@example.com"
    assert payload["al_court_bundle"][0]["data_url"] == "https://storage.invalid/owned.pdf"
    assert payload["al_court_bundle"][0]["filing_type"] == document.filing_type_code
    assert payload["al_court_bundle"][0]["filing_action"] == "efile"
    assert "service_contacts" not in payload


@pytest.mark.django_db
def test_correction_review_keeps_feedback_visible_with_stale_recipients(client, db_draft, monkeypatch):
    from django.urls import reverse

    from efile.models import FilingDraft

    db_draft.supplemental_fields[KEY] = selection(db_draft)
    original = FilingDraft.objects.create(user=db_draft.user, jurisdiction="illinois")
    db_draft.correction_of = original
    db_draft.save()
    monkeypatch.setattr("efile.views.review.get_case_questions", lambda draft: [])
    monkeypatch.setattr(
        "efile.services.corrections.correction_readiness",
        lambda draft: [{"message": "Fix the court choice.", "url": "/case/"}],
    )
    response = client.get(reverse("case_review", args=["illinois"]) + f"?draft={db_draft.pk}")
    assert response.status_code == 200
    assert b"Fix the court choice." in response.content
    assert b"Edit and save your email recipients before filing." in response.content


@pytest.mark.django_db
def test_modal_save_returns_summary_and_errors_preserve_saved_selection(client, db_draft, monkeypatch):
    from django.core import signing
    from django.urls import reverse

    monkeypatch.setattr(
        "efile.services.service_contacts.service_options",
        lambda *args: (
            [{"contact_id": "person", "name": "Alex Example", "email": "alex@example.com"}],
            [{"code": "-580"}],
        ),
    )
    data = {
        "draft": db_draft.pk,
        "service_scope": signing.dumps([db_draft.pk, scope(db_draft)], salt="service-contacts"),
        "service_contact": "person",
        "service_type": "-580",
        "service_rules_checked": "yes",
    }
    url = reverse("update_service_contacts", args=["illinois"])
    response = client.post(url, data, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    assert response.status_code == 200
    assert "Alex Example" in response.json()["summary_html"]
    assert 'id="service-editor"' not in response.json()["summary_html"]
    data["service_contact"] = "not-allowed"
    response = client.post(url, data, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    assert response.status_code == 400
    assert response.json()["success"] is False
    db_draft.refresh_from_db()
    assert db_draft.supplemental_fields[KEY]["contacts"][0]["contact_id"] == "person"

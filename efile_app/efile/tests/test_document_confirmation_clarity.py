"""Litigants can correct each document without losing confirmed case answers."""

import pytest
from django.urls import reverse

from efile.models import DocumentExtraction, FilingDraft, FilingParty
from efile.services.case_filing_types import case_fingerprint
from efile.services.current_drafts import CURRENT_DRAFT_SESSION_KEY
from efile.services.document_choices import validate_services
from efile.services.document_previews import preview_fingerprint
from efile.tests.helpers import reviewed_document
from efile.views.organize_documents import _save_document_details
from efile.workflow import ExistingCase

pytestmark = pytest.mark.django_db


@pytest.fixture
def draft(client, django_user_model, monkeypatch):
    user = django_user_model.objects.create_user(username="clarity", email="jamie@example.com")
    draft = FilingDraft.objects.create(
        user=user,
        jurisdiction="illinois",
        workflow_version=2,
        existing_case=ExistingCase.NEW,
        court_code="court",
        case_category_code="civil",
        case_type_code="contract",
    )
    reviewed_document(
        draft=draft,
        role="lead",
        sort_order=0,
        name="Complaint.pdf",
        filing_type_code="complaint",
        filing_type_name="Complaint",
    )
    reviewed_document(
        draft=draft,
        role="supporting",
        sort_order=0,
        name="Exhibit.pdf",
        filing_type_code="exhibit",
        filing_type_name="Exhibit",
    )
    monkeypatch.setattr(
        "efile.services.document_choices.filing_choices",
        lambda draft: [{"value": "complaint", "text": "Complaint"}, {"value": "exhibit", "text": "Exhibit"}],
    )
    monkeypatch.setattr(
        "efile.services.document_choices.service_choices",
        lambda *args: {
            "copy": {"code": "copy", "multiplier": True, "required": False},
            "jury": {"code": "jury", "multiplier": False, "required": False},
        },
    )
    monkeypatch.setattr("efile.views.extraction_review.cached_account_profile", lambda *args: None)
    monkeypatch.setattr("efile.workflow._has_incomplete_parties", lambda draft: False)
    monkeypatch.setattr("efile.views.extraction_review._offered_filer_roles", lambda *args: [])
    client.force_login(user)
    session = client.session
    session[CURRENT_DRAFT_SESSION_KEY] = draft.pk
    session["auth_tokens"] = {"TYLER-TOKEN-ILLINOIS": "token"}
    session.save()
    return draft


def confirmation_data(draft):
    documents = list(draft.documents.all())
    return {
        "existing_case": "new",
        "court_code": "court",
        "case_category_code": "civil",
        "case_type_code": "contract",
        "filing_type_code": "complaint",
        "filing_type_name": "Untrusted name",
        "preview_fingerprint": preview_fingerprint(documents),
        "case_fingerprint": case_fingerprint(draft),
        "document_id": [doc.pk for doc in documents],
        **{f"document_type_{doc.pk}": doc.filing_type_code for doc in documents},
    }


def test_confirmation_saves_each_document_and_uses_court_names(client, draft):
    supporting = draft.documents.get(role="supporting")
    data = confirmation_data(draft)
    data[f"document_type_{supporting.pk}"] = "complaint"
    response = client.post(reverse("extraction_review", args=[draft.jurisdiction]), data)
    assert response.status_code == 302
    supporting.refresh_from_db()
    assert (supporting.filing_type_code, supporting.filing_type_name) == ("complaint", "Complaint")
    assert not supporting.document_type_confirmed
    assert draft.documents.get(role="lead").filing_type_name == "Complaint"


@pytest.mark.parametrize("bad_ids", ["duplicate", "foreign"])
def test_confirmation_rejects_changed_document_list_without_saving(client, draft, bad_ids):
    data = confirmation_data(draft)
    ids = data["document_id"]
    data["document_id"] = [ids[0], ids[0] if bad_ids == "duplicate" else 999999]
    response = client.post(reverse("extraction_review", args=[draft.jurisdiction]), data)
    assert response.status_code == 200
    assert b"document list changed" in response.content
    assert draft.documents.get(role="supporting").filing_type_code == "exhibit"


def test_confirmation_rejects_stale_case(client, draft):
    data = confirmation_data(draft)
    data["case_fingerprint"] = "old"
    response = client.post(reverse("extraction_review", args=[draft.jurisdiction]), data)
    assert b"Your case changed" in response.content


def test_detected_number_defaults_to_existing_but_preserves_explicit_new(client, draft):
    draft.existing_case = ""
    draft.extracted_guesses = {"docket number": "26-CV-123"}
    draft.save()
    url = reverse("extraction_review", args=[draft.jurisdiction])
    response = client.get(url)
    assert response.context["chosen_existing_case"] == "existing"
    assert response.context["saved_path"] == "existing"
    assert response.context["path_question_open"] is False
    draft.existing_case = "new"
    draft.save()
    assert client.get(url).context["chosen_existing_case"] == "new"


@pytest.mark.parametrize("quantity", [0, -1, 1.5, "2", True, None])
def test_service_quantities_reject_invalid_values(draft, quantity):
    with pytest.raises(ValueError, match="whole number"):
        validate_services(draft, "complaint", [{"code": "copy", "multiplier": quantity}])


def test_service_quantities_and_legacy_codes(draft):
    assert validate_services(draft, "complaint", ["copy"]) == [{"code": "copy", "multiplier": 1}]
    assert validate_services(draft, "complaint", [{"code": "copy", "multiplier": 3}]) == [
        {"code": "copy", "multiplier": 3}
    ]
    with pytest.raises(ValueError, match="does not allow"):
        validate_services(draft, "complaint", [{"code": "jury", "multiplier": 2}])
    with pytest.raises(ValueError, match="available"):
        validate_services(draft, "complaint", ["not-offered"])


def test_organize_saves_quantity_and_refreshes_changed_source(draft, django_capture_on_commit_callbacks):
    documents = list(draft.documents.all())
    new_source = next(doc for doc in documents if doc.role == "supporting")
    details = [
        {
            "id": doc.pk,
            "filing_type": doc.filing_type_code,
            "document_type": "public",
            "requested_optional_services": [{"code": "copy", "multiplier": 3}],
        }
        for doc in documents
    ]
    with django_capture_on_commit_callbacks(execute=True):
        _save_document_details(draft, details, new_source.pk)
    draft.refresh_from_db()
    assert draft.court_code == "court"
    assert draft.case_type_code == "contract"
    assert draft.documents.get(pk=new_source.pk).requested_optional_services == [{"code": "copy", "multiplier": 3}]
    assert DocumentExtraction.objects.get(document=new_source).status == DocumentExtraction.Status.PENDING


def test_valid_account_details_skip_but_explicit_edit_still_opens(client, draft, monkeypatch):
    profile = {
        "first_name": "Jamie",
        "last_name": "Rivera",
        "address": "123 Main Street",
        "city": "Chicago",
        "state": "IL",
        "zip": "60601",
    }
    monkeypatch.setattr("efile.views.your_information.cached_account_profile", lambda *args: profile)
    url = reverse("your_information", args=[draft.jurisdiction])
    assert client.get(url).url.partition("?")[0] == reverse("parties", args=[draft.jurisdiction])
    filer = FilingParty.objects.get(draft=draft, role="filer")
    assert filer.email == "jamie@example.com"
    filer.city = "Evanston"
    filer.save()
    response = client.get(url + "?edit=1")
    assert response.status_code == 200
    assert response.context["filer"].city == "Evanston"


def test_missing_contact_fields_do_not_skip(client, draft, monkeypatch):
    monkeypatch.setattr("efile.views.your_information.cached_account_profile", lambda *args: {"first_name": "Jamie"})
    assert client.get(reverse("your_information", args=[draft.jurisdiction])).status_code == 200


def test_unknown_occupant_role_is_available_under_more_roles(client, draft, monkeypatch):
    FilingParty.objects.create(draft=draft, role="filer", first_name="Jamie")
    roles = [
        {"code": "plaintiff", "name": "Plaintiff", "required": True},
        {"code": "occupant", "name": "Unknown occupant", "required": False},
    ]
    monkeypatch.setattr("efile.views.parties.get_party_types", lambda draft: roles)
    response = client.get(reverse("parties", args=[draft.jurisdiction]))
    assert response.status_code == 200
    assert response.context["other_party_types"] == [roles[1]]
    assert b"More roles" in response.content
    assert b"Unknown occupant" in response.content


def test_selected_optional_role_remains_visible(client, draft, monkeypatch):
    roles = [
        {"code": "plaintiff", "name": "Plaintiff", "required": True},
        {"code": "occupant", "name": "Unknown occupant", "required": False},
    ]
    FilingParty.objects.create(draft=draft, role="filer", party_type="occupant")
    monkeypatch.setattr("efile.views.parties.get_party_types", lambda draft: roles)
    response = client.get(reverse("parties", args=[draft.jurisdiction]))
    assert response.context["other_party_types"] == []
    assert roles[1] in response.context["primary_party_types"]


@pytest.mark.integration
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("inferred", [False, True])
def test_confirmation_browser_on_desktop_and_mobile(client, draft, live_server, tmp_path, inferred, monkeypatch):
    import json
    import os
    import subprocess

    from django.conf import settings

    if not os.getenv("CORE_FILING_UX_BROWSER_TESTS"):
        pytest.skip("Opt-in: requires Chromium")
    draft.extracted_guesses = {
        "document title": "Complaint",
        "plaintiff or petitioner names": "Jordan Rivera",
        "defendant or respondent names": "Sam Parker",
    }
    if inferred:
        draft.existing_case = ""
        draft.extracted_guesses["docket number"] = "26-CV-123"
    draft.save()
    monkeypatch.setattr(
        "efile.views.parties.get_party_types",
        lambda draft: [
            {"code": "plaintiff", "name": "Plaintiff", "required": True},
            {"code": "defendant", "name": "Defendant", "required": True},
            {"code": "occupant", "name": "Unknown occupant", "required": False},
        ],
    )
    FilingParty.objects.create(draft=draft, role="filer", first_name="Jamie", last_name="Rivera")
    config = tmp_path / "confirmation.json"
    config.write_text(
        json.dumps(
            {
                "inferred": inferred,
                "partiesUrl": reverse("parties", args=[draft.jurisdiction]) + f"?draft={draft.pk}",
                "baseUrl": live_server.url,
                "url": reverse("extraction_review", args=[draft.jurisdiction]) + f"?draft={draft.pk}",
                "cookie": client.cookies[settings.SESSION_COOKIE_NAME].value,
            }
        )
    )
    result = subprocess.run(
        ["node", "tests/document-confirmation-clarity-browser.js", str(config)],
        cwd=settings.BASE_DIR,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_refreshing_source_does_not_restore_removed_people(draft):
    from efile.services.extracted_parties import review_rows

    draft.extracted_guesses = {"plaintiff or petitioner names": "Jamie Rivera"}
    draft.supplemental_fields = {"_suggestion_source_changed": True}
    assert review_rows(draft) == []


def test_organize_rejects_stale_new_case(draft):
    with pytest.raises(ValueError, match="case changed"):
        _save_document_details(draft, [], draft.documents.first().pk, confirmed_case_fingerprint="old")

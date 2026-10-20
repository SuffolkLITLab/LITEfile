"""Regression coverage for extraction and simultaneous filings (#217, #218)."""

from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from django.test import Client
from django.urls import reverse

from efile.models import FilingDraft, FilingParty
from efile.services.current_drafts import CURRENT_DRAFT_SESSION_KEY
from efile.services.drafts import read_case_data, write_case_data
from efile.services.extracted_parties import review_rows, save_reviewed_parties
from efile.services.extraction_fields import (
    display_extracted_fields,
    normalize_document_evidence,
    normalize_extracted_fields,
)
from efile.services.fee_quotes import record_fee_quote
from efile.tests.helpers import accepted_submission, reviewed_document


@pytest.mark.parametrize(
    "normalize", [normalize_document_evidence, normalize_extracted_fields, display_extracted_fields]
)
def test_extraction_discards_missing_answers_without_discarding_facts(normalize):
    result = normalize(
        {
            "form revision": " Unknown. ",
            "docket number": "N/A",
            "document date": "not provided",
            "filing phase": "unknown",
            "case title": "All Unknown Occupants",
            "requested relief": ["unknown", "Possession", "N/A"],
            "selected options": {"unknown answer": "Unknown", "has children": False, "amount": 0},
        }
    )
    assert not {"form revision", "docket number", "document date", "filing phase"}.intersection(result)
    assert result["case title"] == "All Unknown Occupants"
    assert "Possession" in result["requested relief"]
    assert "unknown" not in str(result["requested relief"])
    assert "unknown answer" not in result["selected options"]
    assert "False" in str(result["selected options"])
    assert "0" in str(result["selected options"])


@pytest.fixture
def draft(db, django_user_model):
    user = django_user_model.objects.create_user(username="integrity-user", tyler_jurisdiction="illinois")
    return FilingDraft.objects.create(user=user, jurisdiction="illinois", existing_case="new")


def signed_in(user):
    client = Client()
    client.force_login(user)
    session = client.session
    session["jurisdiction"] = "illinois"
    session["auth_tokens"] = {"TYLER-TOKEN-ILLINOIS": "test-token"}
    session.save()
    return client


def route(name, draft):
    return reverse(name, kwargs={"jurisdiction": draft.jurisdiction}) + f"?draft={draft.pk}"


def test_family_form_children_remain_evidence_until_explicitly_added(draft):
    draft.case_type_name = "Dissolution of marriage with children"
    draft.extracted_guesses = {
        "plaintiff or petitioner names": "Dana Kim",
        "defendant or respondent names": "Elliot Kim",
        "other party names": "Jamie Kim (Child); Robin Kim (Child)",
    }
    draft.save()
    rows = review_rows(draft)
    assert [row["name"] for row in rows] == ["Dana Kim", "Elliot Kim"]
    save_reviewed_parties(draft, rows)
    assert list(draft.parties.values_list("first_name", flat=True)) == ["Dana", "Elliot"]

    # Explicitly adding a party remains possible, including a minor petitioner.
    rows = review_rows(draft)
    rows.append({"name": "Jamie Kim", "side": "initiating"})
    save_reviewed_parties(draft, rows)
    assert FilingParty.objects.filter(draft=draft, first_name="Jamie").exists()


def test_old_extraction_placeholders_are_not_prefilled_or_displayed(draft):
    draft.extracted_guesses = {"case title": "unknown", "docket number": "N/A", "form revision": "Not provided"}
    draft.save()
    reviewed_document(draft=draft, role="lead", name="petition.pdf")
    response = signed_in(draft.user).get(route("extraction_review", draft))
    assert response.status_code == 200
    assert response.context["document_summary_details"] == []
    assert not response.context["docket_number"]
    assert response.context["extraction_context"]["guesses"] == {}


@pytest.mark.parametrize("shared_session", [False, True], ids=["two-browsers", "two-tabs"])
def test_each_context_keeps_its_draft_after_another_starts_or_resumes(draft, shared_session):
    first = signed_in(draft.user)
    second = signed_in(draft.user)
    if shared_session:
        second.cookies = first.cookies.copy()
    start_url = reverse("start_filing", kwargs={"jurisdiction": "illinois"})
    first_url = first.post(start_url, {"existing_case": "new"}).url
    first_id = int(parse_qs(urlsplit(first_url).query)["draft"][0])
    second_url = second.post(start_url, {"existing_case": "existing"}).url
    second_id = int(parse_qs(urlsplit(second_url).query)["draft"][0])
    assert first_id != second_id

    # The second tab can even resume a third matter without changing either URL.
    assert second.get(route("upload_documents", draft)).status_code == 200
    for client, url, draft_id, title in [
        (first, first_url, first_id, "Dana's filing"),
        (second, second_url, second_id, "Elliot's filing"),
    ]:
        assert client.get(url).context["filing_draft"]["id"] == draft_id
        response = client.post(
            reverse("save_case_data_api") + f"?draft={draft_id}",
            {"case_title": title},
            content_type="application/json",
        )
        assert response.status_code == 200
        assert FilingDraft.objects.get(pk=draft_id).case_title == title
        data = client.get(reverse("get_current_draft") + f"?draft={draft_id}").json()
        assert data["data"]["filing_draft"]["id"] == draft_id


@pytest.mark.parametrize("identity", ["", "unknown", "0", "999999999", "1&draft=2", "9" * 100])
def test_invalid_identity_never_writes_the_session_draft(draft, identity):
    client = signed_in(draft.user)
    session = client.session
    session[CURRENT_DRAFT_SESSION_KEY] = draft.pk
    session.save()
    response = client.post(
        reverse("save_case_data_api") + f"?draft={identity}",
        {"case_title": "wrong draft"},
        content_type="application/json",
    )
    assert response.status_code == 409
    draft.refresh_from_db()
    assert draft.case_title == ""


@pytest.mark.parametrize(
    "status", [FilingDraft.Status.SUBMITTED, FilingDraft.Status.ABANDONED, FilingDraft.Status.SUBMITTING]
)
def test_stale_identity_is_rejected_without_creating_another_draft(draft, status):
    draft.status = status
    draft.save()
    client = signed_in(draft.user)
    before = FilingDraft.objects.count()
    assert client.post(route("filing_path", draft), {"existing_case": "existing"}).status_code == 409
    assert FilingDraft.objects.count() == before


def test_no_session_pointer_does_not_read_another_contexts_latest_draft(draft):
    assert signed_in(draft.user).get(reverse("get_current_draft")).json()["data"]["filing_draft"] is None


def test_header_identity_and_conflicting_form_identity(draft):
    client = signed_in(draft.user)
    response = client.post(
        reverse("save_case_data_api"),
        {"case_title": "Named in header"},
        content_type="application/json",
        HTTP_X_FILING_DRAFT=str(draft.pk),
    )
    assert response.status_code == 200
    response = client.post(route("filing_path", draft), {"draft": str(draft.pk + 1), "existing_case": "existing"})
    assert response.status_code == 409
    draft.refresh_from_db()
    assert draft.case_title == "Named in header"
    assert draft.existing_case == "new"


def test_backfill_repairs_existing_submitted_draft_summary(draft):
    from importlib import import_module
    from types import SimpleNamespace

    from django.apps import apps
    from django.db import connection

    reviewed_document(draft=draft, role="lead", filing_type_code="27959", filing_type_name="Complaint")
    FilingDraft.objects.filter(pk=draft.pk).update(
        status=FilingDraft.Status.SUBMITTED, filing_type_code="", filing_type_name=""
    )
    migration = vars(import_module("efile.migrations.0022_sync_primary_filing_type"))
    migration["synchronize_primary_types"](apps, SimpleNamespace(connection=connection))
    draft.refresh_from_db()
    assert (draft.filing_type_code, draft.filing_type_name) == ("27959", "Complaint")


def test_primary_type_tracks_edits_clearing_and_lead_deletion(draft):
    lead = reviewed_document(draft=draft, role="lead", filing_type_code="27959", filing_type_name="Complaint")
    stale_draft = FilingDraft.objects.get(pk=draft.pk)
    lead.filing_type_code = "123"
    lead.filing_type_name = "Petition"
    lead.save(update_fields=["filing_type_code", "filing_type_name"])
    stale_draft.case_title = "A corrected caption"
    stale_draft.save()
    draft.refresh_from_db()
    assert (draft.filing_type_code, draft.filing_type_name) == ("123", "Petition")
    write_case_data(draft, {"filing_type": "456", "filing_type_name": "Motion"})
    lead.refresh_from_db()
    assert (lead.filing_type_code, lead.filing_type_name) == ("456", "Motion")
    assert read_case_data(draft)["filing_type"] == "456"
    lead.filing_type_code = lead.filing_type_name = ""
    lead.save()
    draft.refresh_from_db()
    assert (draft.filing_type_code, draft.filing_type_name) == ("", "")
    lead.filing_type_code, lead.filing_type_name = "27959", "Complaint"
    lead.save()
    draft.documents.all().delete()
    draft.refresh_from_db()
    assert (draft.filing_type_code, draft.filing_type_name) == ("", "")


def test_organization_and_submission_preserve_primary_type_and_confirmation_identity(draft, monkeypatch):
    from efile.tests.test_durable_drafts import FakeApiResponse, _prepare_submission

    client = signed_in(draft.user)
    _prepare_submission(client, draft, monkeypatch)
    draft.refresh_from_db()
    draft.document_checklist_acknowledged = True
    draft.save()
    lead = draft.documents.get(role="lead")
    details = [{"id": lead.pk, "filing_type": "27959", "filing_type_name": "Complaint", "document_type": "public"}]
    response = client.post(
        route("organize_documents", draft),
        {"documents": details, "main_document_id": lead.pk},
        content_type="application/json",
    )
    assert response.status_code == 200
    assert parse_qs(urlsplit(response.json()["redirect_url"]).query)["draft"] == [str(draft.pk)]
    draft.refresh_from_db()
    assert (draft.filing_type_code, draft.filing_type_name) == ("27959", "Complaint")

    record_fee_quote(draft, "0.00", [])
    # Starting another filing in the shared session cannot redirect submission.
    other = FilingDraft.objects.create(user=draft.user, jurisdiction="illinois")
    session = client.session
    session[CURRENT_DRAFT_SESSION_KEY] = other.pk
    session.save()
    with patch("requests.post", return_value=FakeApiResponse(201, {"filing_id": "adoption-123"})):
        response = client.post(
            reverse("submit_final_filing") + f"?draft={draft.pk}",
            accepted_submission(draft, efile_data={"al_court_bundle": [{}]}),
            content_type="application/json",
        )
    assert response.status_code == 200
    draft.refresh_from_db()
    lead.refresh_from_db()
    assert draft.status == FilingDraft.Status.SUBMITTED
    assert (draft.filing_type_code, draft.filing_type_name) == (lead.filing_type_code, lead.filing_type_name)
    assert client.session[CURRENT_DRAFT_SESSION_KEY] == other.pk
    other.mark_submitted({"filing_id": "different-confirmation"})
    confirmation = client.get(response.json()["redirect_url"])
    assert confirmation.context["confirmation_number"] == "adoption-123"
    assert client.post(route("filing_path", draft), {"existing_case": "new"}).status_code == 409

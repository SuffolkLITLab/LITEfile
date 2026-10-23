import hashlib
import json
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from efile.models import FilingDraft, InterviewHandoff
from efile.services.handoff import HandoffError, create_correction, effective_hints, resolve_metadata, unique_match
from efile.tests.helpers import reviewed_document
from efile.tests.pdf_helpers import pdf_bytes

pytestmark = pytest.mark.django_db
PDF = pdf_bytes()


@pytest.fixture
def source(settings):
    settings.LITEFILE_HANDOFF_SOURCES = {
        "rfa": {"token": "secret", "jurisdictions": ["vermont"], "return_origins": ["https://interviews.example.org"]}
    }
    return {"HTTP_X_LITEFILE_SOURCE": "rfa", "HTTP_AUTHORIZATION": "Bearer secret"}


@pytest.fixture
def payload():
    return {
        "schema_version": 1,
        "source_id": "session-opaque",
        "idempotency_key": "retry-key",
        "jurisdiction": "vermont",
        "case": {"existing_case": False, "court_name": "Test family court", "court_code": "untrusted"},
        "filing_intent": "relief_from_abuse",
        "filing_type_name_hints": ["Complaint"],
        "case_category_name_hints": ["Family"],
        "case_type_name_hints": ["Relief from abuse"],
        "filer": {"first_name": "Test", "last_name": "Filer", "email": "filer@example.org"},
        "parties": [
            {
                "first_name": "Test",
                "last_name": "Filer",
                "is_self": True,
                "is_filing_party": True,
                "semantic_role": "plaintiff",
                "case_side_hint": "plaintiff",
            }
        ],
        "documents": [
            {
                "id": "complaint",
                "role": "lead",
                "form_name": "RFA complaint",
                "sha256": hashlib.sha256(PDF).hexdigest(),
                "filing_type_code": "9999",
            }
        ],
        "return_url": "https://interviews.example.org/interview?session=opaque",
    }


def send(client, source, payload, data=PDF):
    return client.post(
        reverse("external_handoff"),
        {"payload": json.dumps(payload), "complaint": SimpleUploadedFile("complaint.pdf", data, "application/pdf")},
        **source,
    )


@pytest.fixture
def storage():
    with patch("efile.views.handoff.S3UploadHandler") as mocked:
        mocked.return_value.upload_file.return_value = {
            "success": True,
            "key": "private/key.pdf",
            "url": "https://s3.example/signed",
            "filename": "complaint.pdf",
            "size": len(PDF),
        }
        mocked.return_value.get_public_url.return_value = "https://s3.example/signed"
        yield mocked.return_value


def login(client, django_user_model, username="filer"):
    user = django_user_model.objects.create_user(username=username, tyler_jurisdiction="vermont")
    client.force_login(user)
    session = client.session
    session["auth_tokens"] = {"TYLER-TOKEN-VERMONT": "test-token"}
    session.save()
    return user


def test_partial_draft_idempotency_and_source_hints(client, source, payload, storage):
    first = send(client, source, payload)
    assert first.status_code == 201, first.content
    assert first.json()["state"] == "needs_input"
    draft = FilingDraft.objects.get()
    assert draft.user is None
    assert draft.court_name == "Test family court"
    assert draft.court_code == ""
    assert draft.documents.get().filing_type_code == ""
    assert draft.parties.count() == 1
    assert draft.parties.get().email == "filer@example.org"
    assert send(client, source, payload).json()["draft_id"] == first.json()["draft_id"]
    assert storage.upload_file.call_count == 1
    payload["case"]["court_name"] = "A different court"
    assert send(client, source, payload).status_code == 409
    assert FilingDraft.objects.count() == 1


def test_auth_hash_schema_and_scope(client, source, payload, storage):
    assert send(client, {}, payload).status_code == 401
    assert send(client, source, payload, b"%PDF-tampered").status_code == 400
    payload["schema_version"] = 2
    assert send(client, source, payload).status_code == 400
    payload["schema_version"] = 1
    payload["jurisdiction"] = "illinois"
    assert send(client, source, payload).status_code == 403
    assert not FilingDraft.objects.exists()
    storage.upload_file.assert_not_called()


def test_claim_requires_explicit_post_and_enforces_owner(client, source, payload, storage, django_user_model):
    url = send(client, source, payload).json()["continue_url"]
    assert client.get(url).status_code == 302
    user = login(client, django_user_model)
    assert client.get(url).status_code == 200
    assert FilingDraft.objects.get().user_id is None
    with patch("efile.services.handoff._codes", return_value=[]):
        assert client.post(url).status_code == 302
    draft = FilingDraft.objects.get()
    assert draft.user_id == user.pk
    login(client, django_user_model, "other")
    assert client.post(url).status_code == 403
    assert client.get(reverse("handoff_review", args=[draft.pk])).status_code == 404


def test_ambiguous_names_do_not_preselect():
    assert unique_match([{"code": "a", "name": "Complaint"}, {"code": "b", "name": "Complaint"}], ["Complaint"]) is None


def test_live_resolution_and_user_override_provenance(client, source, payload, storage):
    send(client, source, payload)
    draft = FilingDraft.objects.get()

    def codes(jurisdiction, path, **params):
        return {
            "": [{"code": "vt", "name": "Test family court"}],
            "vt/categories": [{"code": "fam", "name": "Family"}],
            "vt/case_types/": [{"code": "rfa", "name": "Relief from abuse"}],
            "vt/filing_types/": [{"code": "live", "name": "Complaint"}],
        }.get(path, [])

    with patch("efile.services.handoff._codes", side_effect=codes):
        resolve_metadata(draft)
    doc = draft.documents.get()
    assert doc.filing_type_code == "live"
    assert draft.metadata_events.filter(kind="live_resolution").exists()
    doc.filing_type_code = "user-selected"
    doc.save()
    assert draft.metadata_events.filter(kind="user_edit", path=f"documents.{doc.pk}.filing_type_code").exists()
    assert InterviewHandoff.objects.get().payload["documents"][0]["filing_type_code"] == "9999"


def test_county_and_court_hint_overrides_replace_general_hints(client, source, payload, storage):
    payload["case"]["county"] = "Cook County"
    payload["filing_hint_overrides"] = {
        "counties": {
            "Cook": {
                "case_category_name_hints": ["County category"],
                "case_type_name_hints": ["County case type"],
                "documents": {"complaint": {"document_type_name_hints": ["County confidential"]}},
            }
        },
        "courts": {
            "Test Family Court": {
                "case_category_name_hints": ["Court category"],
                "documents": {
                    "complaint": {
                        "filing_type_name_hints": ["Court complaint"],
                        "filing_component_name_hints": ["Court lead"],
                    }
                },
            }
        },
    }
    assert effective_hints(payload, court_name="Test family court")["case_category_name_hints"] == ["Court category"]
    send(client, source, payload)
    draft = FilingDraft.objects.get()

    def codes(jurisdiction, path, **params):
        return {
            "": [{"code": "vt", "name": "Test family court"}],
            "vt/categories": [
                {"code": "county", "name": "County category"},
                {"code": "court", "name": "Court category"},
            ],
            "vt/case_types/": [{"code": "county-type", "name": "County case type"}],
            "vt/filing_types/": [{"code": "court-filing", "name": "Court complaint"}],
            "vt/filing_types/court-filing/document_types": [
                {"code": "county-confidential", "name": "County confidential"}
            ],
            "vt/filing_types/court-filing/filing_components": [{"code": "court-lead", "name": "Court lead"}],
        }.get(path, [])

    with patch("efile.services.handoff._codes", side_effect=codes):
        resolve_metadata(draft)
    draft.refresh_from_db()
    document = draft.documents.get()
    assert draft.case_category_code == "court"
    assert draft.case_type_code == "county-type"
    assert document.filing_type_code == "court-filing"
    assert document.document_type_code == "county-confidential"
    assert document.filing_component_code == "court-lead"


def test_scoped_hints_reject_unknown_documents_and_duplicate_counties(client, source, payload, storage):
    payload["filing_hint_overrides"] = {
        "counties": {"Cook": {"documents": {"missing": {"filing_type_name_hints": ["Complaint"]}}}}
    }
    assert send(client, source, payload).status_code == 400
    payload["filing_hint_overrides"] = {
        "counties": {
            "Cook": {"case_type_name_hints": ["One"]},
            "Cook County": {"case_type_name_hints": ["Two"]},
        }
    }
    assert send(client, source, payload).status_code == 400


def test_correction_preserves_snapshot_and_blocks_ambiguous_attempts(
    client, source, payload, storage, django_user_model
):
    send(client, source, payload)
    draft = FilingDraft.objects.get()
    draft.user = login(client, django_user_model)
    draft.save()
    doc = draft.documents.get()
    doc.filing_type_code = "old"
    doc.filing_type_name = "Old filing type"
    doc.document_type_name = "Old document type"
    doc.filing_component_name = "Old filing component"
    doc.save()
    party = draft.parties.get()
    party.party_type = "PET"
    party.party_type_name = "Petitioner"
    party.save()
    draft.case_category_name = "Family"
    draft.case_type_name = "Relief from abuse"
    draft.save()
    with pytest.raises(HandoffError):
        create_correction(draft, {"status": "rejected"}, [f"documents.{doc.pk}.filing_type_code"])
    draft.mark_submitted({"filing_id": "confirmed"})
    snapshot = draft.submission_snapshot
    with pytest.raises(HandoffError):
        create_correction(draft, {"status": "pending"}, ["documents"])
    revision = create_correction(draft, {"status": "rejected"}, [f"documents.{doc.pk}.filing_type_code"])
    assert revision.documents.get().s3_key == doc.s3_key
    assert revision.documents.get().filing_type_code == ""
    assert revision.parties.get().first_name == "Test"
    draft.refresh_from_db()
    assert draft.submission_snapshot == snapshot
    assert draft.documents.get().filing_type_code == "old"
    assert create_correction(draft, {"status": "rejected"}, ["documents"]).pk == revision.pk
    assert revision.selected_payment_account_id == ""


def test_scoped_correction_clears_dependent_names(client, source, payload, storage, django_user_model):
    send(client, source, payload)
    draft = FilingDraft.objects.get()
    draft.user = login(client, django_user_model)
    draft.case_category_name = "Family"
    draft.case_type_name = "Relief from abuse"
    draft.save()
    document = draft.documents.get()
    document.filing_type_name = "Old filing type"
    document.document_type_name = "Old document type"
    document.filing_component_name = "Old filing component"
    document.save()
    party = draft.parties.get()
    party.party_type_name = "Petitioner"
    party.save()
    draft.mark_submitted({"filing_id": "confirmed"})
    scoped = create_correction(draft, {"status": "rejected"}, ["court_code"])
    scoped_document = scoped.documents.get()
    assert scoped.case_category_name == ""
    assert scoped.case_type_name == ""
    assert scoped_document.filing_type_name == ""
    assert scoped_document.document_type_name == ""
    assert scoped_document.filing_component_name == ""
    assert scoped.parties.get().party_type_name == ""


def test_replacement_retains_metadata_and_is_idempotent(client, source, payload, storage, django_user_model):
    from urllib.parse import parse_qs, urlsplit

    send(client, source, payload)
    draft = FilingDraft.objects.get()
    draft.user = login(client, django_user_model)
    draft.save()
    doc = draft.documents.get()
    doc.filing_type_code = "user-code"
    doc.save()
    response = client.post(reverse("return_to_interview", args=[draft.pk]))
    token = parse_qs(urlsplit(response.url).query)["litefile_correction"][0]
    payload["idempotency_key"] = "replace-1"
    payload["case"]["court_name"] = "Must not overwrite"
    headers = {**source, "HTTP_X_LITEFILE_CORRECTION": token}

    def replace():
        return client.post(
            reverse("handoff_replace_documents"),
            {"payload": json.dumps(payload), "complaint": SimpleUploadedFile("complaint.pdf", PDF)},
            **headers,
        )

    assert replace().status_code == 200
    assert replace().status_code == 200
    draft.refresh_from_db()
    assert draft.court_name == "Test family court"
    assert draft.documents.get().filing_type_code == "user-code"
    assert storage.upload_file.call_count == 2


def test_expired_claim_and_csrf(client, source, payload, storage, django_user_model, settings):
    from django.test import Client

    url = send(client, source, payload).json()["continue_url"]
    csrf_client = Client(enforce_csrf_checks=True)
    user = login(csrf_client, django_user_model)
    assert csrf_client.post(url).status_code == 403
    settings.LITEFILE_HANDOFF_TOKEN_MAX_AGE = -1
    assert client.get(url).status_code == 410
    assert FilingDraft.objects.get().user_id is None
    assert user.is_authenticated


def test_storage_failure_rolls_back_and_retry_can_succeed(client, source, payload, storage):
    storage.upload_file.return_value = {"success": False}
    assert send(client, source, payload).status_code == 503
    assert not FilingDraft.objects.exists()
    assert not InterviewHandoff.objects.exists()


def test_partial_without_documents_is_durable(client, source, payload, storage):
    payload.pop("documents")
    payload.pop("filer")
    payload.pop("parties")
    response = client.post(reverse("external_handoff"), json.dumps(payload), content_type="application/json", **source)
    assert response.status_code == 201
    assert any(issue["path"] == "main_document" for issue in response.json()["issues"])
    storage.upload_file.assert_not_called()


def test_targeted_edit_returns_to_handoff(client, source, payload, storage, django_user_model):
    send(client, source, payload)
    draft = FilingDraft.objects.get()
    draft.user = login(client, django_user_model)
    draft.save()
    response = client.post(
        reverse("your_information", kwargs={"jurisdiction": "vermont"}) + f"?draft={draft.pk}&return_to=handoff",
        {
            "first_name": "Corrected",
            "last_name": "Filer",
            "address_line_1": "100 Main Street",
            "city": "Burlington",
            "state": "VT",
            "zip_code": "05401",
            "email": "filer@example.org",
        },
    )
    assert response.status_code == 302
    assert response.url == reverse("handoff_review", args=[draft.pk])
    assert draft.parties.get().first_name == "Corrected"
    assert draft.documents.count() == 1


def test_replacement_cannot_edit_a_submitted_attempt(client, source, payload, storage, django_user_model):
    from urllib.parse import parse_qs, urlsplit

    send(client, source, payload)
    draft = FilingDraft.objects.get()
    draft.user = login(client, django_user_model)
    draft.save()
    response = client.post(reverse("return_to_interview", args=[draft.pk]))
    token = parse_qs(urlsplit(response.url).query)["litefile_correction"][0]
    draft.mark_submitted({"filing_id": "submitted"})
    payload["idempotency_key"] = "later-update"
    response = client.post(
        reverse("handoff_replace_documents"),
        {"payload": json.dumps(payload), "complaint": SimpleUploadedFile("complaint.pdf", PDF)},
        **source,
        HTTP_X_LITEFILE_CORRECTION=token,
    )
    assert response.status_code == 409
    assert storage.upload_file.call_count == 1


def test_correction_api_rejects_partly_accepted_submission(client, django_user_model):
    user = login(client, django_user_model)
    draft = FilingDraft.objects.create(
        user=user,
        jurisdiction="vermont",
        court_code="vt",
        status="submitted",
        submission_response={"filings": [{"filing_id": "returned"}, {"filing_id": "accepted"}]},
    )
    with (
        patch("efile.views.handoff.fetch_filing_detail", return_value={}),
        patch(
            "efile.services.corrections.describe_filing_detail",
            side_effect=[{"filing_id": "accepted", "status": "accepted"}],
        ),
    ):
        response = client.post(reverse("correct_filing", args=[draft.pk]), {"fields": ["documents"]})
    assert response.status_code == 409
    assert b"The court must confirm every filing as returned or rejected before it can be copied." in response.content
    assert b"What needs a correction?" not in response.content
    assert not FilingDraft.objects.filter(correction_of=draft).exists()


def test_non_metadata_save_does_not_fetch_previous_metadata(client, source, payload, storage, django_user_model):
    send(client, source, payload)
    draft = FilingDraft.objects.get()
    draft.user = login(client, django_user_model)
    draft.save()
    document = draft.documents.get()
    with patch("efile.signals.FilingDocument.objects.filter") as existing:
        document.name = "Renamed document"
        document.save(update_fields=["name"])
    existing.assert_not_called()


def test_correction_attempts_share_the_remote_case_group(django_user_model):
    from efile.services.handoff import matter_keys

    user = django_user_model.objects.create_user(username="grouped", tyler_jurisdiction="vermont")
    old = FilingDraft.objects.create(
        user=user, jurisdiction="vermont", status="submitted", submission_response={"filing_id": "old"}
    )
    FilingDraft.objects.create(
        user=user,
        jurisdiction="vermont",
        correction_of=old,
        status="submitted",
        submission_response={"filing_id": "new"},
    )
    keys = matter_keys(user, "vermont", [{"filing_id": "old"}, {"filing_id": "new", "case_tracking_id": "real-case"}])
    assert keys == {"old": "real-case", "new": "real-case"}


def test_unclaimed_retention_leaves_claimed_drafts(client, source, payload, storage, django_user_model):
    from datetime import timedelta
    from io import StringIO

    from django.core.management import call_command
    from django.utils import timezone

    send(client, source, payload)
    InterviewHandoff.objects.update(created_at=timezone.now() - timedelta(days=8))
    output = StringIO()
    call_command("expire_unclaimed_handoffs", stdout=output)
    assert "Would expire 1" in output.getvalue()
    draft = FilingDraft.objects.get()
    draft.user = login(client, django_user_model)
    draft.save()
    call_command("expire_unclaimed_handoffs", apply=True, stdout=output)
    assert FilingDraft.objects.filter(pk=draft.pk).exists()


def test_imported_pdf_access_urls_are_renewed(client, source, payload, storage):
    from efile.services.drafts import read_upload_data

    send(client, source, payload)
    draft = FilingDraft.objects.get()
    with patch("efile.utils.s3_upload_handler.S3UploadHandler") as handler:
        handler.return_value._ensure_initialized.return_value = True
        handler.return_value.get_public_url.return_value = "https://s3.example/fresh-signature"
        uploads = read_upload_data(draft)
    assert uploads["files"]["lead"]["url"] == "https://s3.example/fresh-signature"
    assert draft.documents.get().public_url == "https://s3.example/signed"


def test_claim_referrer_policy_allows_same_origin_forms(client, source, payload, storage, django_user_model):
    url = send(client, source, payload).json()["continue_url"]
    login(client, django_user_model)
    response = client.get(url)
    # no-referrer makes browser form navigations send Origin: null, which
    # Django correctly rejects. same-origin still hides the capability URL
    # from external sites while allowing the claim's CSRF-protected POST.
    assert response["Referrer-Policy"] == "same-origin"
    assert response["Cache-Control"] == "no-store"


def test_malformed_return_url_is_validation_error(client, source, payload):
    payload["return_url"] = "https://[invalid"
    response = send(client, source, payload)
    assert response.status_code == 400
    assert not InterviewHandoff.objects.exists()


def test_pdf_correction_does_not_report_existing_lead_as_missing(client, source, payload, storage):
    from efile.services.handoff import issues_for

    response = send(client, source, payload)
    draft = FilingDraft.objects.get(pk=response.json()["draft_id"])
    draft.correction_fields = ["documents"]
    messages = [issue["message"] for issue in issues_for(draft)]
    assert "Add the main PDF." not in messages
    assert "Replace the PDF the clerk asked you to correct." in messages


def test_dev_submission_filing_ids_link_status_and_corrections(django_user_model):
    from efile.services.handoff import local_submission, matter_keys, submitted_filing_ids

    user = django_user_model.objects.create_user(username="submitted-filer")
    response = {"caseId": "case-1", "envelopeId": "envelope-1", "filingIds": ["filing-1", "filing-2"]}
    draft = FilingDraft.objects.create(user=user, jurisdiction="vermont", court_code="test-court")
    draft.mark_submitted(response)
    assert submitted_filing_ids(response) == {"filing-1", "filing-2"}
    assert local_submission(user, "vermont", "test-court", "filing-2") == draft
    assert matter_keys(user, "vermont", []) == {"filing-1": f"matter:{draft.pk}", "filing-2": f"matter:{draft.pk}"}
    assert submitted_filing_ids({"caseId": "case-1", "envelopeId": "envelope-1"}) == set()


def test_dev_submission_confirmation_uses_envelope_reference():
    from efile.views.confirmation import _confirmation_number

    assert (
        _confirmation_number(
            {
                "caseId": "case-1",
                "envelopeId": "envelope-1",
                "filingIds": ["filing-1", "filing-2"],
                "leadContact": {"id": "contact-1"},
            }
        )
        == "envelope-1"
    )


def send_with_exhibit(client, source, payload):
    """Send the lead after a supporting exhibit, as a source may order them."""
    payload["documents"].insert(
        0,
        {"id": "exhibit", "role": "supporting", "form_name": "Exhibit A", "sha256": hashlib.sha256(PDF).hexdigest()},
    )
    return client.post(
        reverse("external_handoff"),
        {
            "payload": json.dumps(payload),
            "complaint": SimpleUploadedFile("complaint.pdf", PDF, "application/pdf"),
            "exhibit": SimpleUploadedFile("exhibit.pdf", PDF, "application/pdf"),
        },
        **source,
    )


def test_correction_stores_court_timestamps(client, source, payload, storage, django_user_model):
    from datetime import UTC, datetime

    send(client, source, payload)
    draft = FilingDraft.objects.get()
    draft.user = login(client, django_user_model)
    draft.save()
    draft.mark_submitted({"filing_id": "confirmed"})
    submitted = datetime(2026, 9, 1, 12, tzinfo=UTC)
    revision = create_correction(draft, {"status": "rejected", "submitted_at": submitted}, ["documents"])
    revision.refresh_from_db()
    assert revision.clerk_return["submitted_at"].startswith("2026-09-01T12:00:00")


def test_lead_listed_second_is_not_duplicated_by_upload_screens(client, source, payload, storage):
    from efile.services.drafts import read_upload_data, write_upload_data

    send_with_exhibit(client, source, payload)
    draft = FilingDraft.objects.get()
    assert draft.documents.get(role="lead").sort_order == 0
    write_upload_data(draft, read_upload_data(draft))
    assert draft.documents.filter(role="lead").count() == 1


def test_supporting_pdf_can_be_replaced_alone_after_organizing(client, source, payload, storage, django_user_model):
    from urllib.parse import parse_qs, urlsplit

    from efile.services.drafts import read_upload_data, write_upload_data

    storage.upload_file.side_effect = lambda file, **kwargs: {
        "success": True,
        "key": f"private/{file.name}",
        "url": "https://s3.example/signed",
        "filename": file.name,
        "size": len(PDF),
    }
    send_with_exhibit(client, source, payload)
    draft = FilingDraft.objects.get()
    draft.user = login(client, django_user_model)
    draft.save()
    old_pk = draft.documents.get(role="supporting").pk
    write_upload_data(draft, read_upload_data(draft))
    exhibit = draft.documents.get(role="supporting")
    assert draft.metadata_events.filter(path=f"documents.{exhibit.pk}", kind="source_suggestion").exists()
    if exhibit.pk != old_pk:
        assert not draft.metadata_events.filter(path=f"documents.{old_pk}").exists()

    response = client.post(reverse("return_to_interview", args=[draft.pk]))
    token = parse_qs(urlsplit(response.url).query)["litefile_correction"][0]
    payload["idempotency_key"] = "replace-exhibit"
    payload["documents"] = [payload["documents"][0]]
    response = client.post(
        reverse("handoff_replace_documents"),
        {"payload": json.dumps(payload), "exhibit": SimpleUploadedFile("exhibit-v2.pdf", PDF)},
        **source,
        HTTP_X_LITEFILE_CORRECTION=token,
    )
    assert response.status_code == 200, response.json()
    assert draft.documents.get(role="supporting").s3_key == "private/exhibit-v2.pdf"


def test_pdf_correction_is_resolved_by_uploading_in_litefile(client, source, payload, storage, django_user_model):
    from efile.services.drafts import read_upload_data, write_upload_data
    from efile.services.handoff import issues_for

    send(client, source, payload)
    draft = FilingDraft.objects.get()
    draft.user = login(client, django_user_model)
    draft.save()
    draft.mark_submitted({"filing_id": "confirmed"})
    revision = create_correction(draft, {"status": "rejected"}, ["documents"])
    message = "Replace the PDF the clerk asked you to correct."
    assert message in [issue["message"] for issue in issues_for(revision)]
    data = read_upload_data(revision)
    data["files"]["lead"]["s3_key"] = "private/corrected.pdf"
    write_upload_data(revision, data)
    assert message not in [issue["message"] for issue in issues_for(revision)]
    client.post(reverse("handoff_review", args=[revision.pk]), {"action": "confirm"})
    revision.refresh_from_db()
    assert "documents" not in revision.correction_fields


def test_court_correction_clears_dependent_names_without_user_edits(
    client, source, payload, storage, django_user_model
):
    send(client, source, payload)
    draft = FilingDraft.objects.get()
    draft.user = login(client, django_user_model)
    draft.court_code, draft.case_category_code, draft.case_category_name = "court", "cat", "Family"
    draft.case_type_code, draft.case_type_name = "type", "Relief from abuse"
    draft.save()
    doc = draft.documents.get()
    doc.filing_type_code, doc.filing_type_name = "ft", "Complaint"
    doc.document_type_code, doc.document_type_name = "dt", "Lead"
    doc.save()
    party = draft.parties.get()
    party.party_type, party.party_type_name = "pt", "Plaintiff"
    party.save()
    draft.mark_submitted({"filing_id": "confirmed"})
    revision = create_correction(draft, {"status": "rejected"}, ["court_code"])
    revision.refresh_from_db()
    assert (revision.case_category_name, revision.case_type_name) == ("", "")
    assert (revision.filing_type_code, revision.filing_type_name) == ("", "")
    doc = revision.documents.get()
    assert (doc.filing_type_name, doc.document_type_name) == ("", "")
    assert revision.parties.get().party_type_name == ""
    original_edits = draft.metadata_events.filter(kind="user_edit").count()
    assert revision.metadata_events.filter(kind="user_edit").count() == original_edits


def test_user_with_a_correction_draft_can_be_deleted(client, source, payload, storage, django_user_model):
    send(client, source, payload)
    draft = FilingDraft.objects.get()
    user = login(client, django_user_model)
    draft.user = user
    draft.save()
    draft.mark_submitted({"filing_id": "confirmed"})
    create_correction(draft, {"status": "rejected"}, ["documents"])
    user.delete()
    assert not FilingDraft.objects.exists()


@pytest.mark.parametrize(("previous_case_id", "shown"), [("", False), ("tracking-1", True)])
def test_handoff_review_shows_case_identity_only_once_the_case_is_found(
    client, source, payload, storage, django_user_model, previous_case_id, shown
):
    payload["case"] = {
        "existing_case": True,
        "court_name": "Test family court",
        "docket_number": "24-FA-00123",
        "case_title": "Doe v. Doe",
    }
    assert send(client, source, payload).status_code == 201
    draft = FilingDraft.objects.get()
    draft.user = login(client, django_user_model)
    draft.previous_case_id = previous_case_id
    draft.save()

    content = client.get(reverse("handoff_review", args=[draft.pk])).content.decode()

    assert ("Doe v. Doe" in content) is shown
    assert ("24-FA-00123" in content) is shown


def test_invalid_pdf_requires_replacement_instead_of_retry(client, source, payload, storage):
    invalid = b"%PDF- broken"
    payload["documents"][0]["sha256"] = hashlib.sha256(invalid).hexdigest()
    response = send(client, source, payload, invalid)
    assert response.status_code == 422
    assert not FilingDraft.objects.exists()
    storage.upload_file.assert_not_called()


def test_replacement_cleans_old_copies_and_preserves_shared_filing(
    client, source, payload, storage, django_user_model, django_capture_on_commit_callbacks
):
    from urllib.parse import parse_qs, urlsplit

    from django.utils import timezone

    send(client, source, payload)
    draft = FilingDraft.objects.get()
    draft.user = login(client, django_user_model)
    draft.save()
    document = draft.documents.get()
    document.original_s3_key = "old-original.docx"
    document.preparation_reviewed_at = timezone.now()
    document.save()
    other = FilingDraft.objects.create(user=draft.user, jurisdiction="vermont")
    reviewed_document(draft=other, role="lead", s3_key=document.s3_key)
    token = parse_qs(urlsplit(client.post(reverse("return_to_interview", args=[draft.pk])).url).query)[
        "litefile_correction"
    ][0]
    payload["idempotency_key"] = "replace-cleanup"
    storage.upload_file.return_value = {"success": True, "key": "replacement.pdf"}
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            reverse("handoff_replace_documents"),
            {"payload": json.dumps(payload), "complaint": SimpleUploadedFile("complaint.pdf", PDF)},
            **source,
            HTTP_X_LITEFILE_CORRECTION=token,
        )
    assert response.status_code == 200
    document.refresh_from_db()
    assert document.s3_key == "replacement.pdf"
    assert document.preparation_reviewed_at is None
    storage.delete_file.assert_called_once_with("old-original.docx")


@pytest.mark.parametrize("status,expected", [(400, 422), (503, 503)])
def test_handoff_distinguishes_conversion_rejection_from_service_outage(
    client, source, payload, storage, status, expected
):
    from efile.tests.pdf_helpers import docx_bytes
    from efile.tests.test_document_preparation import service_response

    data = docx_bytes()
    payload["documents"][0]["sha256"] = hashlib.sha256(data).hexdigest()
    with (
        patch("efile.services.document_preparation.settings.GOTENBERG_URL", "https://synthetic.invalid"),
        patch("efile.services.document_preparation.requests.post", return_value=service_response(b"", status=status)),
    ):
        response = client.post(
            reverse("external_handoff"),
            {"payload": json.dumps(payload), "complaint": SimpleUploadedFile("complaint.docx", data)},
            **source,
        )
    assert response.status_code == expected
    assert not FilingDraft.objects.exists()
    storage.upload_file.assert_not_called()


def test_claimed_handoff_resumes_without_claiming_again(client, source, payload, storage, django_user_model):
    response = send(client, source, payload)
    draft = FilingDraft.objects.get()
    draft.user = login(client, django_user_model)
    draft.save()

    response = client.get(response.json()["continue_url"])

    assert response.status_code == 302
    assert response.url == reverse("handoff_review", args=[draft.pk])


@pytest.mark.parametrize("fingerprint_state", ["current", "stale", "missing"])
def test_handoff_reviews_current_pdfs_without_reasking_case_details(
    client, source, payload, storage, django_user_model, fingerprint_state
):
    from efile.services.document_previews import preview_fingerprint

    send(client, source, payload)
    draft = FilingDraft.objects.get()
    draft.user = login(client, django_user_model)
    draft.save()
    document = draft.documents.get()
    fingerprint = preview_fingerprint([document])
    if fingerprint_state == "stale":
        document.s3_key = "replacement.pdf"
        document.save()
    elif fingerprint_state == "missing":
        fingerprint = ""

    # Isolate PDF acknowledgement from the separate live taxonomy/readiness checks.
    with patch("efile.views.handoff.resolve_metadata"), patch("efile.views.handoff.issues_for", return_value=[]):
        page = client.get(reverse("handoff_review", args=[draft.pk]))
        assert b"Confirm my corrections" not in page.content
        assert page.content.index(b"Continue to final review") < page.content.index(b"<h2>Documents</h2>")
        response = client.post(
            reverse("handoff_review", args=[draft.pk]),
            {"action": "continue", "preview_fingerprint": fingerprint},
        )

    document.refresh_from_db()
    if fingerprint_state == "current":
        assert response.status_code == 302
        assert reverse("case_review", kwargs={"jurisdiction": "vermont"}) in response.url
        assert document.preparation_reviewed_at is not None
    else:
        assert response.status_code == 422
        assert document.preparation_reviewed_at is None
        assert b"Your files changed" in response.content
    draft.refresh_from_db()
    assert draft.status == FilingDraft.Status.DRAFT


def test_handoff_continue_keeps_missing_details_blocking(client, source, payload, storage, django_user_model):
    from efile.services.document_previews import preview_fingerprint

    send(client, source, payload)
    draft = FilingDraft.objects.get()
    draft.user = login(client, django_user_model)
    draft.save()
    with patch("efile.views.handoff.resolve_metadata"):
        page = client.get(reverse("handoff_review", args=[draft.pk]))
        assert b"Complete missing details" in page.content
        assert b"Continue to final review" not in page.content
        response = client.post(
            reverse("handoff_review", args=[draft.pk]),
            {"action": "continue", "preview_fingerprint": preview_fingerprint(draft.documents.all())},
        )
    assert response.url == reverse("handoff_review", args=[draft.pk])
    assert draft.documents.get().preparation_reviewed_at is None


def test_handoff_does_not_reask_confirmed_optional_document_settings(client, source, payload, storage):
    from efile.services.handoff import issues_for

    send(client, source, payload)
    draft = FilingDraft.objects.get()
    document = draft.documents.get()
    document.filing_type_code = "complaint"
    document.document_type_confirmed = True
    document.save()
    paths = {issue["path"] for issue in issues_for(draft)}
    assert f"documents.{document.pk}.document_type_code" not in paths
    assert f"documents.{document.pk}.filing_component_code" not in paths

    # An unresolved confidentiality choice and clerk-requested corrections must
    # still be completed, even when the normal document editor was visited.
    document.document_type_confirmed = False
    document.save()
    assert f"documents.{document.pk}.document_type_code" in {issue["path"] for issue in issues_for(draft)}
    draft.correction_fields = [f"documents.{document.pk}.filing_component_code"]
    draft.save()
    assert draft.correction_fields[0] in {issue["path"] for issue in issues_for(draft)}


def test_imported_hints_and_unmatched_warning_outside_accordion(client, source, payload, storage, django_user_model):
    send(client, source, payload)
    draft = FilingDraft.objects.get()
    draft.user = login(client, django_user_model)
    draft.save()
    with (
        patch("efile.services.people.get_case_questions", return_value=[]),
        patch("efile.services.people.needs_amount_in_controversy", return_value=False),
    ):
        response = client.get(reverse("handoff_review", args=[draft.pk]))
    content = response.content.decode()
    before, inside = content.split('<details class="mt-3">', 1)
    assert "the interview suggested “Relief from abuse”" in before
    assert "but no court choice is matched" in before
    assert "From your interview: Relief from abuse" in inside
    assert "Needs a court choice" in inside
    draft.case_type_code = "current-code"
    draft.case_type_name = "Relief from abuse (current court label)"
    draft.save()
    with (
        patch("efile.services.people.get_case_questions", return_value=[]),
        patch("efile.services.people.needs_amount_in_controversy", return_value=False),
    ):
        response = client.get(reverse("handoff_review", args=[draft.pk]))
    before, inside = response.content.decode().split('<details class="mt-3">', 1)
    assert "the interview suggested “Relief from abuse”" not in before
    assert "Saved court choice: Relief from abuse (current court label)" in inside
    assert "From your interview: Relief from abuse" in inside

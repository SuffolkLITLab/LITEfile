"""Copy-to-Review and final-submit gates, with no live court/storage calls."""

import json
from unittest.mock import Mock

import pytest
import requests
from django.http import JsonResponse
from django.test import RequestFactory
from django.urls import reverse

from efile.models import FilingDocument, FilingDraft, InterviewHandoff
from efile.services.corrections import correction_readiness, source_eligibility, storage_issues
from efile.services.fee_quotes import record_fee_quote
from efile.services.handoff import create_correction, documents_replaced
from efile.tests.test_review_submit_flow import submission_draft

__all__ = ["submission_draft"]
pytestmark = pytest.mark.django_db


@pytest.fixture
def original(submission_draft, monkeypatch):
    draft = submission_draft
    draft.parties.update(is_filing_party=True)
    draft.documents.update(s3_key="owned/original.pdf")
    draft.mark_submitted({"filing_id": "old", "envelopeId": "old-envelope"})
    detail = {
        "filing_id": "old",
        "status": "returned",
        "envelope_id": "old-envelope",
        "comments": [
            {"text": "Fix <the caption>.", "heading": "Petition.pdf"},
        ],
    }
    monkeypatch.setattr("efile.views.handoff.fetch_filing_detail", lambda *args: detail)
    monkeypatch.setattr("efile.services.corrections.fetch_filing_detail", lambda *args: detail)
    monkeypatch.setattr("efile.views.my_cases.fetch_filing_detail", lambda *args: detail)
    monkeypatch.setattr("efile.views.my_cases.describe_filing_detail", lambda detail, *args, **kwargs: detail)
    monkeypatch.setattr("efile.views.my_cases.court_names", lambda *args: {})
    monkeypatch.setattr("efile.services.corrections.describe_filing_detail", lambda detail: detail)
    monkeypatch.setattr("efile.services.corrections.storage_issues", lambda draft: [])
    monkeypatch.setattr(
        "efile.services.efsp_payload._EfspLookups.get",
        lambda self, url: [
            {"code": code} for code in ["cook:law1", "civil", "contract", "petition", "public", "lead", "PLA"]
        ],
    )
    return draft


def copy(client, original):
    response = client.post(reverse("correct_filing", args=[original.pk]))
    assert response.status_code == 302
    revision = FilingDraft.objects.get(correction_of=original)
    assert response.url == reverse("case_review", args=[original.jurisdiction]) + f"?draft={revision.pk}"
    return revision, response.url


@pytest.mark.parametrize("interview", [False, True])
def test_copy_review_edit_and_submit(client, original, monkeypatch, interview):
    if interview:
        InterviewHandoff.objects.create(
            draft=original, source="example", source_id="source-1", idempotency_key="source-1", payload={}
        )
    detail = client.get(reverse("filing_detail", args=[original.jurisdiction, original.court_code, "old"]))
    assert b"Copy and review filing" in detail.content
    revision, url = copy(client, original)
    page = client.get(url)
    assert page.status_code == 200
    assert b"Review your corrected filing" in page.content
    assert b"Fix &lt;the caption&gt;." in page.content
    assert b"Choose a payment method" in page.content
    if not interview:
        assert b"interview answers" not in page.content
    assert not revision.selected_payment_account_id
    assert revision.documents.get().preparation_reviewed_at
    assert revision.documents.get().public_url == ""
    assert revision.documents.get().s3_key == original.documents.get().s3_key
    assert not revision.submission_response and not revision.submission_operation
    assert not revision.submitted_at and not revision.disclaimer_acceptance
    assert not revision.correction_fields
    assert revision.documents.get().pk != original.documents.get().pk
    assert revision.parties.get().pk != original.parties.get().pk
    # A saved owned correction remains reachable during a court outage.
    monkeypatch.setattr("efile.views.handoff.fetch_filing_detail", Mock(side_effect=requests.Timeout))
    assert client.post(reverse("correct_filing", args=[original.pk])).url == url
    assert FilingDraft.objects.filter(correction_of=original).count() == 1
    # An earlier section uses the existing draft-scoped return-to-Review path.
    monkeypatch.setattr(
        "efile.views.payment.payment_accounts",
        lambda *args: [
            {"paymentAccountID": "pay", "accountName": "Card", "paymentAccountTypeCode": "CC"},
        ],
    )
    response = client.post(
        reverse("payment", args=[original.jurisdiction]) + f"?draft={revision.pk}&return_to=review",
        {
            "selected_payment_account": "pay",
            "return_to": "review",
        },
    )
    assert response.status_code == 302 and reverse("case_review", args=[original.jurisdiction]) in response.url
    revision.refresh_from_db()
    record_fee_quote(revision, "0", [])
    assert correction_readiness(revision) == []
    monkeypatch.setattr(
        "efile.views.submission.forward_final_filing",
        Mock(
            return_value=JsonResponse(
                {
                    "success": True,
                    "api_response": {"filing_id": "new", "envelopeId": "new-envelope"},
                }
            )
        ),
    )
    response = client.post(
        reverse("submit_final_filing") + f"?draft={revision.pk}",
        json.dumps(
            {
                "confirm_submission": True,
                "disclaimer_token": client.get(
                    reverse("case_review", args=[revision.jurisdiction]) + f"?draft={revision.pk}"
                ).context["disclaimer_token"],
            }
        ),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    revision.refresh_from_db()
    assert revision.status == "submitted" and revision.submission_response["filing_id"] == "new"
    original.refresh_from_db()
    assert original.submission_response["filing_id"] == "old"
    assert original.documents.get().s3_key == "owned/original.pdf"
    assert "new" in client.post(reverse("correct_filing", args=[original.pk])).url
    confirmation = client.get(reverse("filing_confirmation", args=[original.jurisdiction]) + f"?draft={revision.pk}")
    assert b"View the original filing" in confirmation.content


@pytest.mark.parametrize("status", ["accepted", "pending", "failed", "", "unknown"])
def test_mixed_or_unknown_source_cannot_copy(client, original, monkeypatch, status):
    original.submission_response = {"filing_ids": ["old", "other"]}
    original.save()

    def detail(request, jurisdiction, court, filing):
        return {"filing_id": filing, "status": "returned" if filing == "old" else status}

    monkeypatch.setattr("efile.views.handoff.fetch_filing_detail", detail)
    monkeypatch.setattr("efile.services.corrections.fetch_filing_detail", detail)
    page = client.get(reverse("filing_detail", args=[original.jurisdiction, original.court_code, "old"]))
    assert b"Copy and review filing" not in page.content
    assert b"every filing" in page.content
    response = client.post(reverse("correct_filing", args=[original.pk]))
    assert response.status_code == 409
    assert not FilingDraft.objects.filter(correction_of=original).exists()


def test_get_is_read_only_and_post_requires_csrf(client, original):
    assert client.get(reverse("correct_filing", args=[original.pk])).status_code == 200
    assert not FilingDraft.objects.filter(correction_of=original).exists()
    client.handler.enforce_csrf_checks = True
    assert client.post(reverse("correct_filing", args=[original.pk])).status_code == 403


def test_empty_comments_and_missing_documents_remain_visible(client, original, monkeypatch):
    revision = create_correction(original, {"status": "returned", "comments": []})
    monkeypatch.setattr("efile.services.corrections.storage_issues", storage_issues)
    monkeypatch.setattr("efile.utils.s3_upload_handler.S3UploadHandler._ensure_initialized", lambda self: False)
    response = client.get(reverse("case_review", args=[revision.jurisdiction]) + f"?draft={revision.pk}")
    assert response.status_code == 200
    assert b"No clerk comments were provided" in response.content
    assert b"We could not access Petition.pdf" in response.content
    assert revision.documents.count() == 1


def test_legacy_pdf_requirement_is_not_satisfied_by_an_attachment(original):
    revision = create_correction(original, {"status": "returned"}, ["documents"])
    extra = FilingDocument.objects.create(draft=revision, role="supporting", s3_key="unrelated.pdf")
    assert not documents_replaced(revision)
    assert any(issue.get("path") == "documents" for issue in correction_readiness(revision))
    source = revision.documents.get(role="lead")
    source.s3_key = "replacement.pdf"
    source.save()
    assert documents_replaced(revision)
    assert not any(issue.get("path") == "documents" for issue in correction_readiness(revision))
    extra.delete()


@pytest.mark.parametrize("legacy", [False, True])
def test_valid_empty_court_choices_do_not_block(original, monkeypatch, legacy):
    revision = create_correction(original, {"status": "returned"})
    revision.documents.update(document_type_code="", filing_component_code="", document_type_confirmed=True)
    if legacy:
        doc = revision.documents.get()
        revision.correction_fields = [
            f"documents.{doc.pk}.document_type_code",
            f"documents.{doc.pk}.filing_component_code",
        ]
        revision.save()
    monkeypatch.setattr(
        "efile.services.efsp_payload._EfspLookups.get",
        lambda self, url: []
        if (url.endswith("document_types") or url.endswith("filing_components"))
        else [{"code": code} for code in ["cook:law1", "civil", "contract", "petition", "PLA"]],
    )
    issues = correction_readiness(revision)
    assert len(issues) == 1 and issues[0]["view"] == "payment"


@pytest.mark.parametrize("problem", ["legacy", "metadata", "storage", "source"])
def test_direct_submit_never_forwards_unready_correction(client, original, monkeypatch, problem):
    revision, _ = copy(client, original)
    revision.selected_payment_account_id = "pay"
    revision.save()
    record_fee_quote(revision, "0", [])
    if problem == "legacy":
        revision.correction_fields = ["documents"]
        revision.save()
    elif problem == "metadata":
        monkeypatch.setattr("efile.services.efsp_payload._EfspLookups.get", lambda *args: None)
    elif problem == "storage":
        monkeypatch.setattr(
            "efile.services.corrections.storage_issues",
            lambda draft: [{"message": "Missing PDF", "view": "upload_documents"}],
        )
    else:
        monkeypatch.setattr("efile.services.corrections.fetch_filing_detail", Mock(side_effect=requests.Timeout))
    forward = Mock()
    monkeypatch.setattr("efile.views.submission.forward_final_filing", forward)
    response = client.post(
        reverse("submit_final_filing") + f"?draft={revision.pk}",
        json.dumps(
            {
                "confirm_submission": True,
                "disclaimer_token": client.get(
                    reverse("case_review", args=[revision.jurisdiction]) + f"?draft={revision.pk}"
                ).context["disclaimer_token"],
            }
        ),
        content_type="application/json",
    )
    assert response.status_code in (409, 412)
    forward.assert_not_called()
    revision.refresh_from_db()
    assert revision.status == "draft"


def test_eligibility_checks_owner_identifiers_and_local_status(original, django_user_model):
    request = RequestFactory().get("/")
    request.user = django_user_model.objects.create_user(username="not-owner")
    assert not source_eligibility(request, original)["eligible"]
    request.user = original.user
    original.status = "error"
    assert not source_eligibility(request, original)["eligible"]
    original.status = "submitted"
    original.submission_response = {"envelope_id": "no-filing-ids"}
    assert not source_eligibility(request, original)["eligible"]


def test_replacement_preserves_metadata_and_original_bytes(original, monkeypatch, django_capture_on_commit_callbacks):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from efile.services.document_uploads import upload_files
    from efile.tests.pdf_helpers import pdf_bytes

    revision = create_correction(original, {"status": "returned"}, ["documents"])
    document = revision.documents.get()
    document.courtesy_copy_email = "copy@example.com"
    document.requested_optional_services = ["certified"]
    document.save()
    handler = Mock()
    handler.upload_file.return_value = {"success": True, "key": "owned/replacement.pdf"}
    handler.get_public_url.return_value = "https://example.invalid/fresh"
    monkeypatch.setattr("efile.services.document_uploads.S3UploadHandler", lambda: handler)
    monkeypatch.setattr("efile.services.document_uploads.queue_document_extraction", lambda doc: None)
    with django_capture_on_commit_callbacks(execute=True):
        upload_files(
            revision,
            [SimpleUploadedFile("corrected.pdf", pdf_bytes())],
            revision.jurisdiction,
            replace_document_id=document.pk,
        )
    document.refresh_from_db()
    assert document.s3_key == "owned/replacement.pdf"
    assert document.filing_type_code == "petition"
    assert document.document_type_code == "public"
    assert document.courtesy_copy_email == "copy@example.com"
    assert document.requested_optional_services == ["certified"]
    assert document.preparation_reviewed_at is None
    assert original.documents.get().s3_key == "owned/original.pdf"
    handler.delete_file.assert_not_called()
    assert documents_replaced(revision)


def test_copy_contract_preserves_order_plan_and_remaps_provenance(original):
    from efile.models import FilingMetadataEvent, FilingPlan
    from efile.services.extraction_confirmation import extraction_is_confirmed, extraction_review_fingerprint

    original.plan = FilingPlan.objects.create(
        user=original.user, jurisdiction=original.jurisdiction, title="Matter", checklist={"waiver": {"status": "have"}}
    )
    original.documents.update(checklist_item_id="waiver", courtesy_copy_email="copy@example.com")
    original.extraction_review_fingerprint = extraction_review_fingerprint(original)
    original.save()
    FilingMetadataEvent.objects.create(
        draft=original,
        path=f"documents.{original.documents.get().pk}",
        kind="document_purpose",
        value={"purpose": "fee_waiver"},
    )
    revision = create_correction(original, {"status": "returned"})
    assert revision.plan_id == original.plan_id
    assert revision.documents.get().checklist_item_id == "waiver"
    assert revision.metadata_events.get(kind="document_purpose").path == f"documents.{revision.documents.get().pk}"
    assert extraction_is_confirmed(revision)
    assert revision.public_id != original.public_id


def test_changed_bytes_after_readiness_cannot_win_submission_claim(original):
    from efile.services.corrections import readiness_fingerprint
    from efile.views.submission import _claim_for_submission

    revision = create_correction(original, {"status": "returned"})
    checked = readiness_fingerprint(revision)
    revision.documents.update(s3_key="changed-in-another-tab")
    with pytest.raises(ValueError, match="correction changed"):
        _claim_for_submission(revision, {}, checked_correction=checked)


@pytest.mark.integration
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("interview", [False, True])
def test_correction_browser(live_server, client, original, monkeypatch, tmp_path, interview):
    import os
    import subprocess

    from django.conf import settings

    if not os.getenv("CORRECTION_BROWSER_TESTS"):
        pytest.skip("Opt-in: requires Chromium")
    if interview:
        InterviewHandoff.objects.create(
            draft=original, source="browser", source_id="1", idempotency_key="1", payload={}
        )
    accounts = [{"paymentAccountID": "pay", "accountName": "Test card", "paymentAccountTypeCode": "CC", "active": True}]
    monkeypatch.setattr("efile.views.payment.payment_accounts", lambda *args: accounts)
    monkeypatch.setattr(
        "efile.views.organize_documents._court_document_types", lambda *args: [{"code": "public", "name": "Public"}]
    )
    from efile.tests.test_fee_quotes import FakeResponse, efsp_fee_response

    monkeypatch.setattr(
        "efile.api.filing_views.requests.post", lambda *args, **kwargs: FakeResponse(200, efsp_fee_response("0"))
    )
    monkeypatch.setattr("efile.api.filing_views.prepare_efile_payload", lambda *args, **kwargs: None)
    monkeypatch.setattr("efile.views.payment.estimate_fees", lambda draft: {"known": False})
    forward = Mock(
        return_value=JsonResponse(
            {
                "success": True,
                "api_response": {"filing_id": "new-browser"},
                "redirect_url": reverse("filing_confirmation", args=[original.jurisdiction]),
            }
        )
    )
    monkeypatch.setattr("efile.views.submission.forward_final_filing", forward)
    config = tmp_path / "correction-browser.json"
    config.write_text(
        json.dumps(
            {
                "baseUrl": live_server.url,
                "cookie": client.cookies[settings.SESSION_COOKIE_NAME].value,
                "url": reverse("filing_detail", args=[original.jurisdiction, original.court_code, "old"]),
                "accounts": accounts,
                "screenshotDir": os.getenv("CORRECTION_SCREENSHOT_DIR", ""),
                "scenario": "interview" if interview else "native",
            }
        )
    )
    result = subprocess.run(
        ["node", "tests/correction-browser.js", str(config)],
        cwd=settings.BASE_DIR,
        text=True,
        capture_output=True,
        timeout=100,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    revision = FilingDraft.objects.get(correction_of=original)
    assert revision.status == "submitted"
    assert revision.documents.get().name == "Corrected petition.pdf"
    assert original.documents.get().name == "Petition.pdf"
    forward.assert_called_once()


def test_native_copy_renews_urls_from_owned_keys(original, monkeypatch):
    from efile.services.corrections import refresh_correction_documents
    from efile.services.drafts import read_upload_data

    revision = create_correction(original, {"status": "returned"})
    monkeypatch.setattr("efile.utils.s3_upload_handler.S3UploadHandler._ensure_initialized", lambda self: True)
    calls = []

    def renewed(self, key, expiration=604800):
        calls.append(key)
        return "https://example.invalid/renewed"

    monkeypatch.setattr("efile.utils.s3_upload_handler.S3UploadHandler.get_public_url", renewed)
    assert read_upload_data(revision)["files"]["lead"]["url"] == "https://example.invalid/renewed"
    payload = {"al_court_bundle": [{"data_url": "expired", "filing_type": "stale"}]}
    refresh_correction_documents(payload, revision)
    assert payload["al_court_bundle"][0]["data_url"] == "https://example.invalid/renewed"
    assert payload["al_court_bundle"][0]["filing_type"] == "petition"
    assert calls == ["owned/original.pdf", "owned/original.pdf"]


def test_removed_children_do_not_leave_impossible_legacy_requirements(original):
    revision = create_correction(original, {"status": "returned"})
    attachment = FilingDocument.objects.create(draft=revision, role="supporting", name="Removed.pdf")
    revision.correction_fields = [f"documents.{attachment.pk}.filing_type_code", "parties.999999.party_type"]
    revision.save()
    attachment.delete()
    assert not any("saved correction requirement" in issue["message"] for issue in correction_readiness(revision))


@pytest.mark.parametrize("field", ["case_category_code", "case_type_code", "filing_type_code"])
def test_blank_parent_does_not_query_unscoped_child_choices(original, monkeypatch, field):
    from efile.services.corrections import metadata_issues

    revision = create_correction(original, {"status": "returned"})
    if field == "filing_type_code":
        revision.documents.update(filing_type_code="")
    else:
        setattr(revision, field, "")
        revision.save()
    urls = []

    def lookup(self, url):
        urls.append(url)
        return [{"code": code} for code in ["cook:law1", "civil", "contract", "petition", "public", "lead", "PLA"]]

    monkeypatch.setattr("efile.services.efsp_payload._EfspLookups.get", lookup)
    assert metadata_issues(revision)
    assert not any("filing_types//" in url or "case_types//" in url for url in urls)
    if field == "case_category_code":
        assert not any("case_types" in url or "filing_types" in url for url in urls)
    if field == "case_type_code":
        assert not any("filing_types" in url or "party_types" in url for url in urls)


def test_availability_checks_the_refreshed_correction_payload(client, original, monkeypatch):
    from efile.services.corrections import refresh_correction_documents
    from efile.views.session_api import forward_final_filing

    revision, _ = copy(client, original)
    request = RequestFactory().post("/")
    request.user = revision.user
    request.session = client.session
    monkeypatch.setattr("efile.views.session_api.get_current_draft", lambda *args, **kwargs: revision)
    monkeypatch.setattr("efile.views.session_api.get_case_data", lambda *args: {"court": revision.court_code})
    monkeypatch.setattr("efile.views.session_api.get_upload_data", lambda *args: {"files": {"lead": {"name": "x"}}})
    monkeypatch.setattr(
        "efile.utils.s3_upload_handler.S3UploadHandler.get_public_url",
        lambda *args, **kwargs: "https://example.invalid/new",
    )
    monkeypatch.setattr(
        "efile.views.session_api.prepare_efile_payload",
        lambda data, *args, **kwargs: refresh_correction_documents(data, kwargs["draft"]),
    )
    seen = []

    def availability(jurisdiction, court, case, data):
        seen.append(data["al_court_bundle"][0]["filing_type"])
        return "Unavailable" if seen[-1] == "petition" else ""

    monkeypatch.setattr("efile.views.session_api.outgoing_unavailable_message", availability)
    upstream = Mock()
    monkeypatch.setattr("requests.post", upstream)
    response = forward_final_filing(request, {"efile_data": {"al_court_bundle": [{"filing_type": "stale"}]}})
    assert response.status_code == 403
    assert seen == ["petition"]
    upstream.assert_not_called()

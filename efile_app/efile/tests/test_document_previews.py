import io
from unittest.mock import MagicMock, patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from efile.models import DocumentExtraction, FilingDocument, FilingDraft
from efile.services.document_previews import preview_fingerprint
from efile.services.document_uploads import upload_files
from efile.services.drafts import read_upload_data, write_upload_data
from efile.tests.pdf_helpers import pdf_bytes
from efile.tests.test_document_extractions import authorize

pytestmark = pytest.mark.django_db


@pytest.fixture
def preview_draft(client, django_user_model):
    user = django_user_model.objects.create_user(username="preview-filer", tyler_jurisdiction="vermont")
    draft = FilingDraft.objects.create(user=user, jurisdiction="vermont", workflow_version=2)
    authorize(client, draft)
    FilingDocument.objects.create(
        draft=draft,
        role="lead",
        name="filing.pdf",
        s3_key="private/filing.pdf",
        original_s3_key="private/original.docx",
        original_filename="original.docx",
        preparation="converted",
    )
    return draft


def url(name, draft, **kwargs):
    return reverse(name, kwargs={"jurisdiction": draft.jurisdiction, **kwargs}) + f"?draft={draft.pk}"


def approve(client, draft, **overrides):
    documents = list(draft.documents.all())
    return client.post(
        url("extraction_review", draft),
        {
            "preview_fingerprint": preview_fingerprint(documents),
            "existing_case": "existing",
            **overrides,
        },
    )


def test_continue_records_preview_without_checkboxes_for_current_documents(client, preview_draft):
    doc = preview_draft.documents.get()
    with patch("efile.views.extraction_review.prepare_document_review"):
        page = client.get(url("extraction_review", preview_draft))
    assert b'type="checkbox"' not in page.content
    assert b"preview_fingerprint" in page.content
    doc.refresh_from_db()
    assert doc.preparation_reviewed_at is None
    assert approve(client, preview_draft, preview_fingerprint="stale").status_code == 422
    doc.refresh_from_db()
    assert doc.preparation_reviewed_at is None
    assert approve(client, preview_draft).status_code == 302
    doc.refresh_from_db()
    assert doc.preparation_reviewed_at is not None
    assert client.get(url("extraction_review", preview_draft)).status_code == 200


def test_original_and_filing_bytes_are_separate_and_not_cached(client, preview_draft):
    doc = preview_draft.documents.get()
    content = pdf_bytes()
    handler = MagicMock()
    handler.bucket_name = "private"
    handler.s3_client.get_object.side_effect = lambda **kw: {
        "Body": io.BytesIO(content if kw["Key"] == doc.s3_key else b"original Word bytes")
    }
    with patch("efile.views.document_previews.S3UploadHandler", return_value=handler):
        filing = client.get(url("document_content", preview_draft, document_id=doc.pk))
        assert filing.status_code == 200
        assert b"".join(filing.streaming_content) == content
        assert filing["Content-Type"] == "application/pdf"
        assert filing["Cache-Control"] == "private, no-store"
        original = client.get(url("document_content", preview_draft, document_id=doc.pk) + "&original=1")
        assert b"".join(original.streaming_content) == b"original Word bytes"
        assert original["Content-Disposition"].startswith("attachment;")


def test_preview_cannot_read_another_users_or_another_drafts_document(client, preview_draft, django_user_model):
    doc = preview_draft.documents.get()
    other = FilingDraft.objects.create(user=preview_draft.user, jurisdiction="vermont")
    with patch("efile.views.document_previews.S3UploadHandler") as storage:
        response = client.get(url("document_content", other, document_id=doc.pk))
        assert response.status_code == 404
        other_user = django_user_model.objects.create_user(username="other-filer", tyler_jurisdiction="vermont")
        client.force_login(other_user)
        assert client.get(url("document_content", preview_draft, document_id=doc.pk)).status_code in {403, 409}
    storage.assert_not_called()


def test_previews_require_sign_in(client, preview_draft):
    doc = preview_draft.documents.get()
    client.logout()
    response = client.get(reverse("document_content", kwargs={"jurisdiction": "vermont", "document_id": doc.pk}))
    assert response.status_code == 401


def test_submission_cannot_bypass_preview(client, preview_draft):
    with patch("efile.views.submission.forward_final_filing") as forward:
        response = client.post(reverse("submit_final_filing"), data="{}", content_type="application/json")
    assert response.status_code == 412
    assert "Review your PDFs" in response.json()["error"]
    forward.assert_not_called()
    preview_draft.refresh_from_db()
    assert preview_draft.status == FilingDraft.Status.DRAFT


def test_later_uploads_are_all_or_nothing_and_leave_existing_approvals(preview_draft):
    doc = preview_draft.documents.get()
    doc.preparation_reviewed_at = timezone.now()
    doc.save()
    handler = MagicMock()
    handler.upload_file.return_value = {"success": True, "key": "new.pdf"}
    handler.get_public_url.return_value = "https://s3.example/new.pdf"
    with patch("efile.services.document_uploads.S3UploadHandler", return_value=handler):
        with pytest.raises(ValueError, match="could not be read"):
            upload_files(
                preview_draft,
                [SimpleUploadedFile("valid.pdf", pdf_bytes()), SimpleUploadedFile("bad.pdf", b"invalid")],
                "vermont",
            )
    assert preview_draft.documents.count() == 1
    doc.refresh_from_db()
    assert doc.preparation_reviewed_at is not None
    handler.delete_file.assert_called_once_with("new.pdf")


def test_original_and_approval_survive_supporting_row_rebuilds(preview_draft):
    supporting = FilingDocument.objects.create(
        draft=preview_draft,
        role="supporting",
        name="support.pdf",
        original_filename="support.docx",
        s3_key="support.pdf",
        original_s3_key="support.docx",
        preparation="converted",
        preparation_reviewed_at=timezone.now(),
    )
    wire = read_upload_data(preview_draft)
    # Browser-supplied preparation approval and original key must be ignored.
    wire["files"]["supporting"][0].update(preparation_reviewed_at="forged", original_s3_key="forged")
    write_upload_data(preview_draft, wire)
    saved = preview_draft.documents.get(role="supporting")
    assert saved.original_s3_key == "support.docx"
    assert saved.original_filename == "support.docx"
    assert saved.preparation_reviewed_at == supporting.preparation_reviewed_at


def organized(draft):
    draft.existing_case = "existing"
    draft.previous_case_id = "verified-case"
    draft.save()
    draft.documents.update(filing_type_code="complaint", document_type_code="public")


def test_preview_return_destinations_are_restricted(client, preview_draft):
    organized(preview_draft)
    for unknown in ("https://attacker.example", "payment", "document_checklist"):
        response = approve(client, preview_draft, return_to=unknown)
        assert response.url.partition("?")[0].endswith("/case-lookup/")
        assert "return_to" not in response.url


def test_lead_key_swap_resets_approval_and_cleans_only_unused_copies(preview_draft, django_capture_on_commit_callbacks):
    lead = preview_draft.documents.get()
    lead.preparation_reviewed_at = timezone.now()
    lead.save()
    old_key, original_key = lead.s3_key, lead.original_s3_key
    shared = FilingDraft.objects.create(user=preview_draft.user, jurisdiction="vermont")
    FilingDocument.objects.create(draft=shared, role="lead", s3_key=old_key)
    handler = MagicMock()
    with (
        patch("efile.utils.s3_upload_handler.S3UploadHandler", return_value=handler),
        django_capture_on_commit_callbacks(execute=True),
    ):
        write_upload_data(preview_draft, {"files": {"lead": {"s3_key": "replacement.pdf", "name": "new.pdf"}}})
    lead.refresh_from_db()
    assert lead.preparation_reviewed_at is None
    assert lead.preparation == ""
    assert lead.original_s3_key == ""
    assert lead.original_filename == "new.pdf"
    handler.delete_file.assert_called_once_with(original_key)


def test_legacy_word_support_is_prepared_before_it_can_be_approved(client, preview_draft):
    from efile.tests.pdf_helpers import docx_bytes
    from efile.tests.test_document_preparation import service_response

    write_upload_data(preview_draft, {"files": {"supporting": [{"s3_key": "legacy.docx", "name": "legacy.docx"}]}})
    supporting = preview_draft.documents.get(role="supporting")
    assert approve(client, preview_draft).status_code == 422
    supporting.refresh_from_db()
    assert supporting.preparation_reviewed_at is None
    handler = MagicMock()
    handler.bucket_name = "private"
    handler.get_public_url.return_value = "https://synthetic.invalid/prepared.pdf"
    handler.s3_client.get_object.return_value = {"Body": io.BytesIO(docx_bytes())}
    handler.upload_file.side_effect = [
        {"success": True, "key": "retained.docx"},
        {"success": True, "key": "prepared.pdf"},
    ]
    with (
        patch("efile.views.extraction_review.S3UploadHandler", return_value=handler),
        patch("efile.services.document_preparation.requests.post", return_value=service_response(pdf_bytes())),
        patch("efile.services.document_preparation.settings.GOTENBERG_URL", "https://synthetic.invalid"),
    ):
        assert client.get(url("extraction_review", preview_draft)).status_code == 200
    supporting.refresh_from_db()
    assert supporting.preparation == "converted"
    assert supporting.s3_key == "prepared.pdf"
    assert supporting.original_s3_key == "retained.docx"
    assert supporting.preparation_reviewed_at is None
    assert approve(client, preview_draft).status_code == 302


def test_legacy_rows_and_changed_preparation_cannot_bypass_submission(client, preview_draft):
    doc = preview_draft.documents.get()
    doc.preparation_reviewed_at = timezone.now()
    doc.save()
    fingerprint = preview_fingerprint([doc])
    doc.original_s3_key = "new-original"
    doc.save()
    assert preview_fingerprint([doc]) != fingerprint
    assert approve(client, preview_draft, preview_fingerprint=fingerprint).status_code == 422
    doc.preparation = ""
    doc.save()
    with patch("efile.views.submission.forward_final_filing") as forward:
        assert (
            client.post(reverse("submit_final_filing"), data="{}", content_type="application/json").status_code == 412
        )
    forward.assert_not_called()


def test_extraction_job_is_discarded_when_outer_transaction_rolls_back(preview_draft):
    from django.db import transaction

    preview_draft.documents.all().delete()
    handler = MagicMock()
    handler.upload_file.return_value = {"success": True, "key": "new.pdf"}
    handler.get_public_url.return_value = "https://synthetic.invalid/new.pdf"
    with (
        patch("efile.services.document_uploads.S3UploadHandler", return_value=handler),
    ):
        with pytest.raises(RuntimeError):
            with transaction.atomic():
                upload_files(preview_draft, [SimpleUploadedFile("new.pdf", pdf_bytes())], "vermont")
                assert DocumentExtraction.objects.filter(document__draft=preview_draft).exists()
                raise RuntimeError("Rollback")
    assert not DocumentExtraction.objects.filter(document__draft=preview_draft).exists()
    assert not preview_draft.documents.exists()


def test_legacy_pdf_is_flattened_and_cannot_be_acknowledged_after_preparation_failure(client, preview_draft):
    from efile.tests.test_document_preparation import service_response

    doc = preview_draft.documents.get()
    doc.preparation = ""
    doc.original_s3_key = ""
    doc.save()
    source = pdf_bytes(form_value="Stored answer")
    handler = MagicMock()
    handler.bucket_name = "private"
    handler.get_public_url.return_value = "https://synthetic.invalid/prepared.pdf"
    handler.s3_client.get_object.side_effect = lambda **kwargs: {"Body": io.BytesIO(source)}
    handler.upload_file.side_effect = [{"success": True, "key": "retained.pdf"}, {"success": True, "key": "flat.pdf"}]
    doc.original_filename = "source.pdf"
    doc.save()
    with (
        patch("efile.views.extraction_review.S3UploadHandler", return_value=handler),
        patch("efile.services.document_preparation.settings.GOTENBERG_URL", "https://synthetic.invalid"),
        patch(
            "efile.services.document_preparation.requests.post",
            return_value=service_response(pdf_bytes("Stored answer")),
        ),
    ):
        response = client.get(url("extraction_review", preview_draft))
        assert response.status_code == 302
        assert "/upload-documents/" in response.url
        assert DocumentExtraction.objects.get(document=doc).status == "pending"
    doc.refresh_from_db()
    assert doc.preparation == "flattened"
    assert doc.s3_key == "flat.pdf"
    assert doc.original_s3_key == "retained.pdf"
    assert doc.preparation_reviewed_at is None
    DocumentExtraction.objects.filter(document=doc).update(status="complete")
    # A structurally valid response that drops text remains blocked.
    doc.preparation = ""
    doc.preparation_reviewed_at = None
    doc.save()
    with (
        patch("efile.views.extraction_review.S3UploadHandler", return_value=handler),
        patch("efile.services.document_preparation.settings.GOTENBERG_URL", "https://synthetic.invalid"),
        patch(
            "efile.services.document_preparation.requests.post",
            return_value=service_response(pdf_bytes("Dropped answer")),
        ),
    ):
        response = client.get(url("extraction_review", preview_draft))
    assert response.status_code == 422
    assert b"Some answers are missing" in response.content
    assert approve(client, preview_draft).status_code == 422
    doc.refresh_from_db()
    assert doc.preparation_reviewed_at is None


def test_accessibility_seed_starts_with_a_prepared_acknowledged_document(tmp_path):
    from django.core.management import call_command

    from efile.services.document_previews import require_document_previews

    call_command("seed_accessibility_session", output=str(tmp_path / "browser-state.json"))
    draft = FilingDraft.objects.get(user__username="accessibility-checker")
    require_document_previews(draft)
    assert (tmp_path / "browser-state.json").exists()


def test_change_files_keeps_where_the_filer_came_from(client, preview_draft):
    with patch("efile.views.extraction_review.prepare_document_review"):
        preview = client.get(url("extraction_review", preview_draft) + "&return_to=review").content.decode()
    assert 'upload-documents/?return_to=review"' in preview
    upload = client.get(url("upload_documents", preview_draft) + "&return_to=review").content.decode()
    # Both ways off the upload page lead back toward Review, not the start.
    assert upload.count('href="/jurisdiction/vermont/extraction-review/?return_to=review"') == 2
    unknown = client.get(url("upload_documents", preview_draft) + "&return_to=elsewhere").content.decode()
    assert "return_to" not in unknown


def test_upload_from_a_detour_continues_to_its_preview(client, preview_draft):
    with patch("efile.views.upload_documents.upload_files", return_value={}):
        response = client.post(
            url("upload_documents", preview_draft) + "&return_to=review",
            {"documents": SimpleUploadedFile("new.pdf", pdf_bytes())},
        )
    assert "extraction-review/?return_to=review" in response.json()["redirect_url"]


def test_unchanged_files_return_straight_to_review(client, preview_draft):
    organized(preview_draft)
    response = approve(client, preview_draft, return_to="review")
    assert response.url.startswith(reverse("case_review", kwargs={"jurisdiction": "vermont"}))


def test_a_new_file_from_review_stops_only_at_organize(client, preview_draft):
    organized(preview_draft)
    FilingDocument.objects.create(draft=preview_draft, role="supporting", name="new.pdf", preparation="unchanged")
    response = approve(client, preview_draft, return_to="review")
    assert "/organize-documents/?return_to=review" in response.url
    # Without a return target, new files still follow the full flow.
    assert "case-lookup" in approve(client, preview_draft).url


def test_handoff_detour_returns_to_its_list(client, preview_draft):
    organized(preview_draft)
    response = approve(client, preview_draft, return_to="handoff")
    assert response.url == reverse("handoff_review", args=[preview_draft.pk])


@pytest.mark.parametrize("state", ["empty", "unprepared", "deleting", "stale"])
def test_shared_review_rejects_unsafe_approval(preview_draft, state):
    from efile.services.document_previews import DocumentReviewError, approve_document_review

    fingerprint = preview_fingerprint(list(preview_draft.documents.all()))
    if state == "empty":
        preview_draft.documents.all().delete()
    elif state == "unprepared":
        preview_draft.documents.update(preparation="")
    elif state == "deleting":
        preview_draft.deletion_pending = True
        preview_draft.save()
    else:
        preview_draft.documents.update(s3_key="replacement.pdf")
    with pytest.raises(DocumentReviewError):
        approve_document_review(preview_draft, fingerprint)
    assert not preview_draft.documents.filter(preparation_reviewed_at__isnull=False).exists()


def test_legacy_preview_redirects_without_approving(client, preview_draft):
    response = client.post(url("preview_documents", preview_draft) + "&return_to=review")
    assert response.status_code == 302
    assert "extraction-review/" in response.url
    assert "return_to=review" in response.url
    assert preview_draft.documents.get().preparation_reviewed_at is None


def test_all_documents_are_available_on_confirm(client, preview_draft):
    supporting = FilingDocument.objects.create(
        draft=preview_draft, role="supporting", name="second.pdf", s3_key="second.pdf", preparation="original"
    )
    page = client.get(url("extraction_review", preview_draft))
    assert page.status_code == 200
    assert set(doc.pk for doc in page.context["documents"]) == set(preview_draft.documents.values_list("pk", flat=True))
    assert str(supporting.pk).encode() in page.content


def test_a_rejected_form_leaves_the_files_unchecked(client, preview_draft):
    response = approve(client, preview_draft, existing_case="")
    assert response.status_code == 200
    assert preview_draft.documents.get().preparation_reviewed_at is None

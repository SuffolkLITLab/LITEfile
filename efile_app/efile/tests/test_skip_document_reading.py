"""A filer can stop waiting for their document to be read and type the details."""

from unittest.mock import MagicMock, patch

import pytest
from django.urls import reverse
from pypdf import PdfWriter

from efile.models import DocumentExtraction, FilingDocument, FilingDraft
from efile.services.document_extractions import (
    EXTRACTION_SKIPPED,
    claim_next_extraction,
    process_document_extraction,
    queue_document_extraction,
    skip_document_extraction,
)
from efile.tests.helpers import reviewed_document
from efile.tests.test_document_extractions import authorize

UPLOAD = reverse("upload_documents", kwargs={"jurisdiction": "illinois"})


@pytest.fixture
def extraction_draft(db, django_user_model):
    user = django_user_model.objects.create_user(username="skip-reading-user", tyler_jurisdiction="illinois")
    return FilingDraft.objects.create(user=user, jurisdiction="illinois", workflow_version=2)


def _lead(draft):
    document = reviewed_document(draft=draft, role=FilingDocument.Role.LEAD, name="petition.pdf")
    return queue_document_extraction(document)


@pytest.mark.django_db
def test_skipping_opens_review_with_skip_copy(client, extraction_draft):
    authorize(client, extraction_draft)
    job = _lead(extraction_draft)
    review_url = reverse("extraction_review", kwargs={"jurisdiction": "illinois"})
    assert client.get(review_url).status_code == 302

    result = client.post(UPLOAD, {"action": "skip_reading"})

    assert result.json() == {"success": True, "skipped": True}
    job.refresh_from_db()
    assert job.status == DocumentExtraction.Status.FAILED
    assert job.error == EXTRACTION_SKIPPED
    assert job.claim_token is None
    review = client.get(review_url)
    assert review.status_code == 200
    content = review.content.decode()
    assert "You chose not to wait for us to read your document." in content
    assert "We could not read the details." not in content


@pytest.mark.django_db
def test_a_worker_already_reading_drops_its_result(extraction_draft):
    _lead(extraction_draft)
    claimed = claim_next_extraction()
    assert claimed is not None
    assert skip_document_extraction(extraction_draft) is True

    handler = MagicMock()

    def download(_key, destination):
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        with open(destination, "wb") as pdf_file:
            writer.write(pdf_file)
        return {"success": True}

    handler.download_file.side_effect = download
    analyze = MagicMock(return_value={"document title": "Complaint"})
    with (
        patch("efile.services.document_extractions.S3UploadHandler", return_value=handler),
        patch("efile.services.document_extractions.analyze_document", analyze),
    ):
        assert process_document_extraction(claimed.pk, claimed.claim_token) is None

    # Nothing was sent out for reading, and nothing was saved over the skip.
    analyze.assert_not_called()
    claimed.refresh_from_db()
    extraction_draft.refresh_from_db()
    assert claimed.status == DocumentExtraction.Status.FAILED
    assert claimed.error == EXTRACTION_SKIPPED
    assert extraction_draft.extracted_guesses == {}
    assert claim_next_extraction() is None


@pytest.mark.django_db
def test_skipping_a_finished_reading_changes_nothing(client, extraction_draft):
    authorize(client, extraction_draft)
    job = _lead(extraction_draft)
    DocumentExtraction.objects.filter(pk=job.pk).update(status=DocumentExtraction.Status.COMPLETE)

    assert client.post(UPLOAD, {"action": "skip_reading"}).json() == {"success": True, "skipped": False}
    job.refresh_from_db()
    assert job.status == DocumentExtraction.Status.COMPLETE
    assert job.error == ""


@pytest.mark.django_db
def test_upload_window_offers_skip_and_cancel(client, extraction_draft):
    authorize(client, extraction_draft)
    _lead(extraction_draft)
    content = client.get(UPLOAD).content.decode()
    assert 'id="processing-skip-reading"' in content
    assert 'id="processing-cancel-upload"' in content
    assert 'data-phase="reading"' in content

import re
from unittest.mock import MagicMock, patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from efile.models import DocumentExtraction, FilingDocument, FilingDraft
from efile.services.current_drafts import CURRENT_DRAFT_SESSION_KEY
from efile.tests.helpers import reviewed_document
from efile.tests.pdf_helpers import pdf_bytes
from efile.workflow import ExistingCase, WorkflowStepKey


def authorize(client, draft):
    session = client.session
    session[CURRENT_DRAFT_SESSION_KEY] = draft.pk
    session["auth_tokens"] = {"TYLER-TOKEN-ILLINOIS": "token"}
    session["jurisdiction"] = "illinois"
    session.save()


@pytest.fixture
def reorganized_draft(client, django_user_model):
    user = django_user_model.objects.create_user(username="flow-user", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(user=user, jurisdiction="illinois", workflow_version=2)
    client.force_login(user)
    authorize(client, draft)
    return draft


@pytest.mark.django_db
def test_filing_path_saves_normalized_branch(client, reorganized_draft):
    response = client.post(
        reverse("filing_path", kwargs={"jurisdiction": "illinois"}),
        {"existing_case": ExistingCase.EXISTING},
    )

    reorganized_draft.refresh_from_db()
    assert response.status_code == 302
    assert response.url.partition("?")[0] == reverse("upload_documents", kwargs={"jurisdiction": "illinois"})
    assert reorganized_draft.existing_case == ExistingCase.EXISTING
    assert reorganized_draft.current_step == WorkflowStepKey.UPLOAD_DOCUMENTS


@pytest.mark.django_db(transaction=True)
def test_upload_documents_persists_files_and_queues_analysis(client, reorganized_draft):
    handler = MagicMock()
    handler._ensure_initialized.return_value = True
    handler.validate_file.return_value = {"valid": True}
    handler.upload_file.side_effect = [
        {"success": True, "key": "lead.pdf"},
        {"success": True, "key": "supporting.pdf"},
    ]
    handler.get_public_url.side_effect = ["https://example.com/lead.pdf", "https://example.com/supporting.pdf"]
    lead = SimpleUploadedFile("petition.pdf", pdf_bytes(), content_type="application/pdf")
    supporting = SimpleUploadedFile("exhibit.pdf", pdf_bytes(), content_type="application/pdf")

    with patch("efile.services.document_uploads.S3UploadHandler", return_value=handler):
        response = client.post(
            reverse("upload_documents", kwargs={"jurisdiction": "illinois"}),
            {"documents": [lead, supporting]},
        )

    assert response.status_code == 200
    assert response.json()["success"] is True
    reorganized_draft.refresh_from_db()
    assert reorganized_draft.extracted_guesses == {}
    lead_document = reorganized_draft.documents.get(role=FilingDocument.Role.LEAD)
    assert lead_document.name == "petition.pdf"
    assert lead_document.extraction.status == DocumentExtraction.Status.PENDING
    assert response.json()["extraction_pending"] is True
    assert reorganized_draft.documents.get(role=FilingDocument.Role.SUPPORTING).name == "exhibit.pdf"


@pytest.mark.django_db
def test_removing_analyzed_document_cleans_storage_and_stale_guesses(client, reorganized_draft):
    lead = reviewed_document(
        draft=reorganized_draft,
        role=FilingDocument.Role.LEAD,
        sort_order=0,
        name="petition.pdf",
        s3_key="lead/petition.pdf",
    )
    supporting = reviewed_document(
        draft=reorganized_draft,
        role=FilingDocument.Role.SUPPORTING,
        sort_order=0,
        name="exhibit.pdf",
    )
    reorganized_draft.extracted_guesses = {"court": "Cook County"}
    reorganized_draft.save(update_fields=["extracted_guesses", "updated_at"])
    handler = MagicMock()
    handler._ensure_initialized.return_value = True
    handler.delete_file.return_value = {"success": True}

    with patch("efile.views.upload_documents.S3UploadHandler", return_value=handler):
        response = client.post(
            reverse("upload_documents", kwargs={"jurisdiction": "illinois"}),
            {"action": "remove", "document_id": lead.pk},
        )

    reorganized_draft.refresh_from_db()
    supporting.refresh_from_db()
    assert response.status_code == 200
    assert response.json()["success"] is True
    handler.delete_file.assert_called_once_with("lead/petition.pdf")
    assert reorganized_draft.extracted_guesses == {}
    assert supporting.role == FilingDocument.Role.LEAD
    assert supporting.extraction.status == DocumentExtraction.Status.PENDING


@pytest.mark.django_db
def test_extraction_review_branches_new_case_to_checklist(client, reorganized_draft):
    reviewed_document(
        draft=reorganized_draft,
        role=FilingDocument.Role.LEAD,
        name="petition.pdf",
    )

    response = client.post(
        reverse("extraction_review", kwargs={"jurisdiction": "illinois"}),
        {
            "existing_case": ExistingCase.NEW,
            "court_code": "cook",
            "court_name": "Cook County Circuit Court",
            "case_category_code": "MR",
            "case_category_name": "Miscellaneous Remedy",
            "case_type_code": "NC",
            "case_type_name": "Name Change",
        },
    )

    reorganized_draft.refresh_from_db()
    assert response.status_code == 302
    assert response.url.partition("?")[0] == reverse("document_checklist", kwargs={"jurisdiction": "illinois"})
    assert reorganized_draft.existing_case == ExistingCase.NEW
    assert reorganized_draft.court_code == "cook"
    assert reorganized_draft.case_type_code == "NC"
    assert reorganized_draft.case_type_name == "Name Change"
    assert reorganized_draft.current_step == WorkflowStepKey.DOCUMENT_CHECKLIST


@pytest.mark.django_db
def test_extraction_review_returns_to_review_when_edited_from_there(client, reorganized_draft):
    reviewed_document(
        draft=reorganized_draft,
        role=FilingDocument.Role.LEAD,
        name="petition.pdf",
        # Organized already, as every document on Review has been.
        filing_type_code="petition",
        document_type_code="public",
    )

    response = client.post(
        reverse("extraction_review", kwargs={"jurisdiction": "illinois"}),
        {
            "existing_case": ExistingCase.NEW,
            "court_code": "cook",
            "court_name": "Cook County Circuit Court",
            "case_category_code": "MR",
            "case_category_name": "Miscellaneous Remedy",
            "case_type_code": "NC",
            "case_type_name": "Name Change",
            "return_to": "review",
        },
    )

    reorganized_draft.refresh_from_db()
    assert response.status_code == 302
    assert response.url.partition("?")[0] == reverse("case_review", kwargs={"jurisdiction": "illinois"})
    assert reorganized_draft.current_step == WorkflowStepKey.REVIEW


@pytest.mark.django_db
def test_extraction_review_new_case_requires_matched_court_and_type(client, reorganized_draft):
    reviewed_document(
        draft=reorganized_draft,
        role=FilingDocument.Role.LEAD,
        name="petition.pdf",
    )

    response = client.post(
        reverse("extraction_review", kwargs={"jurisdiction": "illinois"}),
        {
            "existing_case": ExistingCase.NEW,
            "court_name": "Cook County Circuit Court",
            "case_category_name": "Miscellaneous Remedy",
            "case_type_name": "Name Change",
        },
    )

    reorganized_draft.refresh_from_db()
    assert response.status_code == 200
    assert b"Choose a court, case category, and case type" in response.content
    assert reorganized_draft.current_step == WorkflowStepKey.EXTRACTION_REVIEW
    assert reorganized_draft.court_code == ""


@pytest.mark.django_db
def test_extraction_review_requires_a_case_path(client, reorganized_draft):
    reviewed_document(
        draft=reorganized_draft,
        role=FilingDocument.Role.LEAD,
        name="petition.pdf",
    )

    response = client.post(
        reverse("extraction_review", kwargs={"jurisdiction": "illinois"}),
        {"existing_case": ExistingCase.UNSURE},
    )

    assert response.status_code == 200
    assert b"Choose whether this is a new or existing court case" in response.content


@pytest.mark.django_db
def test_extraction_review_does_not_offer_case_number_or_title_for_a_new_case(client, reorganized_draft):
    """Tyler rejects a docket number on a new case, and a new case has no
    title until the court opens it -- neither is asked for."""
    reorganized_draft.existing_case = ExistingCase.NEW
    reorganized_draft.extracted_guesses = {"case title": "Rivera v. Example", "docket number": "2024-L-1"}
    reorganized_draft.save(update_fields=["existing_case", "extracted_guesses"])
    reviewed_document(
        draft=reorganized_draft,
        role=FilingDocument.Role.LEAD,
        name="petition.pdf",
    )

    response = client.get(reverse("extraction_review", kwargs={"jurisdiction": "illinois"}))

    assert response.status_code == 200
    content = response.content.decode()
    assert 'name="case_title"' not in content
    docket_field = re.search(r'<div class="form-field"\s+id="docket-number-field"[^>]*>', content)
    assert docket_field is not None
    assert "hidden" in docket_field.group()


@pytest.mark.django_db
def test_extraction_review_shows_existing_case_number_before_lookup(client, reorganized_draft):
    reorganized_draft.existing_case = ExistingCase.EXISTING
    reorganized_draft.save(update_fields=["existing_case"])
    reviewed_document(draft=reorganized_draft, role=FilingDocument.Role.LEAD, name="motion.pdf")

    content = client.get(reverse("extraction_review", kwargs={"jurisdiction": "illinois"})).content.decode()

    docket_field = re.search(r'<div class="form-field"\s+id="docket-number-field"[^>]*>', content)
    assert docket_field is not None
    assert "hidden" not in docket_field.group()
    assert 'name="case_title"' not in content


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("existing_case", "expected_docket", "expected_title"),
    [
        (ExistingCase.NEW, "", ""),
        # An existing case's title is the court's, set by the case lookup.
        (ExistingCase.EXISTING, "2024-L-1", "Court's own title"),
    ],
)
def test_extraction_review_saves_case_identity_only_for_existing_cases(
    client, reorganized_draft, existing_case, expected_docket, expected_title
):
    reorganized_draft.case_title = "Court's own title"
    reorganized_draft.docket_number = "stale"
    reorganized_draft.save(update_fields=["case_title", "docket_number"])
    reviewed_document(draft=reorganized_draft, role=FilingDocument.Role.LEAD, name="petition.pdf")

    response = client.post(
        reverse("extraction_review", kwargs={"jurisdiction": "illinois"}),
        {
            "existing_case": existing_case,
            "court_code": "cook",
            "case_category_code": "civil",
            "case_type_code": "contract",
            "docket_number": "2024-L-1",
            "case_title": "Typed by the filer",
        },
    )

    assert response.status_code == 302
    reorganized_draft.refresh_from_db()
    assert reorganized_draft.docket_number == expected_docket
    assert reorganized_draft.case_title == expected_title


@pytest.mark.django_db
@pytest.mark.parametrize("jurisdiction", ["illinois", "vermont"])
def test_first_progress_stage_reads_start_not_filing(client, reorganized_draft, jurisdiction):
    # "Filing" read as the final submit step, so the first stage is "Start".
    reorganized_draft.jurisdiction = jurisdiction
    reorganized_draft.save(update_fields=["jurisdiction"])
    reorganized_draft.user.tyler_jurisdiction = jurisdiction
    reorganized_draft.user.save(update_fields=["tyler_jurisdiction"])
    session = client.session
    session["auth_tokens"] = {f"TYLER-TOKEN-{jurisdiction.upper()}": "token"}
    session["jurisdiction"] = jurisdiction
    session.save()

    response = client.get(reverse("filing_path", kwargs={"jurisdiction": jurisdiction}))

    progress = re.search(r'<nav class="workflow-progress".*?</nav>', response.content.decode(), re.S)
    assert progress is not None
    labels = re.findall(r'<span class="workflow-progress__label">([^<]+)</span>', progress.group(0))
    assert labels[0] == "Start"
    assert "Filing" not in labels
    assert labels[-1] == "Review"

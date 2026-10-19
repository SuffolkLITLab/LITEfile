"""The filing path a filer gets when they ask us not to use AI (issue #104).

Two promises are tested here. The first is that opting out really does stop
every model call for that filing. The second is that the filer is not dropped
into a dead end: the document is still read locally for the identifiers printed
on it, and everything else is theirs to type.
"""

import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from efile.models import DocumentExtraction, FilingDocument, FilingDraft
from efile.services.current_drafts import CURRENT_DRAFT_SESSION_KEY
from efile.services.document_extractions import (
    keyword_case_number,
    keyword_document_analysis,
    process_document_extraction,
    queue_document_extraction,
)
from efile.services.drafts import create_draft
from efile.services.taxonomy_classification import HierarchicalDocumentClassifier
from efile.tests.helpers import reviewed_document
from efile.tests.pdf_helpers import pdf_bytes

SYNTHETIC_PDFS = Path(__file__).resolve().parents[3] / "benchmarking/synthetic/filled_pdfs/flattened"

# A Massachusetts complaint whose printed form number is in the form registry,
# so the keyword pass alone can say which form this is.
IDENTIFIABLE_PDF = SYNTHETIC_PDFS / "MA-03.pdf"


def no_model_calls(*_args, **_kwargs):
    raise AssertionError("A filer who opted out of AI must not have a model called on their document")


@pytest.fixture
def opted_out_draft(client, django_user_model):
    user = django_user_model.objects.create_user(username="opted-out-user", tyler_jurisdiction="massachusetts")
    draft = FilingDraft.objects.create(
        user=user,
        jurisdiction="massachusetts",
        workflow_version=2,
        ai_assistance_opted_out=True,
    )
    client.force_login(user)
    session = client.session
    session[CURRENT_DRAFT_SESSION_KEY] = draft.pk
    session["auth_tokens"] = {"TYLER-TOKEN-MASSACHUSETTS": "token"}
    session["jurisdiction"] = "massachusetts"
    session.save()
    return draft


def upload_url(jurisdiction="massachusetts"):
    return reverse("upload_documents", kwargs={"jurisdiction": jurisdiction})


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Docket No. 24-CV-00123", "24-CV-00123"),
        ("CASE NUMBER: 2024 SC 000456", "2024 SC 000456"),
        # OCR and typing both turn the label into something close but wrong.
        ("Docker number  25D1234", "25D1234"),
        ("Case #: 24-D-98", "24-D-98"),
        # Court captions print the label above the number, sometimes with a
        # blank line between them.
        ("DOCKET NO.\n\n2601CV01234", "2601CV01234"),
        ("Civil Action No. 1984CV01234 Plaintiff", "1984CV01234"),
        # An unfilled form, and a form's own instructions about the label.
        ("Case No. ________", ""),
        ("Enter the Case Number given by the Circuit Clerk:____", ""),
        ("Case Number The Circuit Clerk will add one", ""),
        ("The case number will be assigned by the clerk.", ""),
    ],
)
def test_case_number_is_read_from_its_label_without_reading_prose(text, expected):
    assert keyword_case_number(text) == expected


def test_keyword_analysis_identifies_a_form_without_calling_a_model():
    with (
        patch("efile.services.document_extractions.extract_fields_from_file", side_effect=no_model_calls),
        patch("efile.services.taxonomy_classification.chat_completion", side_effect=no_model_calls),
    ):
        analysis = keyword_document_analysis(str(IDENTIFIABLE_PDF), "massachusetts")

    assert analysis["evidence"]["form identifier"] == "CJD 109"
    assert analysis["guesses"]["form name"] == "Complaint for Support, Custody, Parenting Time"
    # The form's own crosswalk entry names one category, so the review screen's
    # dropdown has something to recommend even with no model involved.
    assert analysis["guesses"]["case category"] == "Custody, Support, Parenting Time"
    # Nothing was selected against the live taxonomy: that needs a court, and
    # choosing one is the filer's job on the next screen.
    assert analysis["classification"] == {}
    assert analysis["metadata"]["analysis_mode"] == "keyword"
    assert analysis["metadata"]["ai_assistance"] == "opted_out"


@pytest.mark.django_db
def test_worker_reads_an_opted_out_document_with_keywords_only(opted_out_draft):
    document = reviewed_document(
        draft=opted_out_draft,
        role=FilingDocument.Role.LEAD,
        name="MA-03.pdf",
        s3_key="lead/MA-03.pdf",
    )
    job = queue_document_extraction(document)
    handler = MagicMock()
    handler.download_file.side_effect = lambda _key, destination: (
        shutil.copyfile(IDENTIFIABLE_PDF, destination),
        {"success": True},
    )[1]

    with (
        patch("efile.services.document_extractions.S3UploadHandler", return_value=handler),
        patch("efile.services.document_extractions.extract_fields_from_file", side_effect=no_model_calls),
        patch.object(HierarchicalDocumentClassifier, "classify", no_model_calls),
    ):
        from efile.services.document_extractions import claim_next_extraction

        claimed = claim_next_extraction()
        process_document_extraction(job.pk, claimed.claim_token)

    job.refresh_from_db()
    opted_out_draft.refresh_from_db()
    assert job.status == DocumentExtraction.Status.COMPLETE
    assert job.analysis_metadata["analysis_mode"] == "keyword"
    assert job.evidence["form identifier"] == "CJD 109"
    assert opted_out_draft.extracted_guesses["form identifier"] == "CJD 109"


@pytest.mark.django_db
def test_upload_page_offers_the_choice_and_says_what_still_happens(client, opted_out_draft):
    response = client.get(upload_url())

    page = response.content.decode()
    assert response.status_code == 200
    assert 'name="ai_opt_out"' in page
    assert "How do we use AI?" in page
    assert "never used to train AI" in page
    # The saved choice comes back checked, with the keyword warning showing.
    assert 'id="ai-opt-out"' in page
    assert "AI is off." in page


@pytest.mark.django_db(transaction=True)
def test_uploading_saves_the_choice_before_analysis_is_queued(client, opted_out_draft):
    opted_out_draft.ai_assistance_opted_out = False
    opted_out_draft.save(update_fields=["ai_assistance_opted_out", "updated_at"])
    handler = MagicMock()
    handler._ensure_initialized.return_value = True
    handler.validate_file.return_value = {"valid": True}
    handler.upload_file.return_value = {"success": True, "key": "lead.pdf"}
    handler.get_public_url.return_value = "https://example.com/lead.pdf"
    lead = SimpleUploadedFile("complaint.pdf", pdf_bytes(), content_type="application/pdf")

    with patch("efile.services.document_uploads.S3UploadHandler", return_value=handler):
        response = client.post(upload_url(), {"documents": [lead], "ai_opt_out": "yes"})

    opted_out_draft.refresh_from_db()
    assert response.status_code == 200
    assert opted_out_draft.ai_assistance_opted_out is True
    assert opted_out_draft.documents.get(role=FilingDocument.Role.LEAD).extraction.status == (
        DocumentExtraction.Status.PENDING
    )


@pytest.mark.django_db
def test_changing_the_choice_after_upload_drops_the_old_reading_and_re_runs(client, opted_out_draft):
    document = reviewed_document(
        draft=opted_out_draft,
        role=FilingDocument.Role.LEAD,
        name="complaint.pdf",
        s3_key="lead/complaint.pdf",
    )
    job = queue_document_extraction(document)
    job.status = DocumentExtraction.Status.COMPLETE
    job.save(update_fields=["status", "updated_at"])
    opted_out_draft.extracted_guesses = {"court": "Middlesex Probate and Family Court"}
    opted_out_draft.save(update_fields=["extracted_guesses", "updated_at"])

    response = client.post(upload_url(), {"action": "ai_preference", "ai_opt_out": ""})

    opted_out_draft.refresh_from_db()
    job.refresh_from_db()
    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "ai_opted_out": False,
        "remembered": False,
        "reanalyzing": True,
    }
    assert opted_out_draft.ai_assistance_opted_out is False
    # What the other mode read is gone, and the document is queued to be read
    # again the way the filer has now asked for.
    assert opted_out_draft.extracted_guesses == {}
    assert job.status == DocumentExtraction.Status.PENDING


@pytest.mark.django_db
def test_choosing_before_any_upload_saves_without_queueing_anything(client, opted_out_draft):
    opted_out_draft.ai_assistance_opted_out = False
    opted_out_draft.save(update_fields=["ai_assistance_opted_out", "updated_at"])

    response = client.post(upload_url(), {"action": "ai_preference", "ai_opt_out": "yes"})

    opted_out_draft.refresh_from_db()
    assert response.json() == {
        "success": True,
        "ai_opted_out": True,
        "remembered": False,
        "reanalyzing": False,
    }
    assert opted_out_draft.ai_assistance_opted_out is True
    assert not DocumentExtraction.objects.filter(document__draft=opted_out_draft).exists()


@pytest.mark.django_db
def test_review_screen_credits_the_keyword_search_rather_than_a_reading(client, opted_out_draft):
    reviewed_document(
        draft=opted_out_draft,
        role=FilingDocument.Role.LEAD,
        name="complaint.pdf",
        s3_key="lead/complaint.pdf",
    )
    opted_out_draft.extracted_guesses = {"form identifier": "CJD 109"}
    opted_out_draft.save(update_fields=["extracted_guesses", "updated_at"])

    response = client.get(reverse("extraction_review", kwargs={"jurisdiction": "massachusetts"}))

    page = response.content.decode()
    assert response.status_code == 200
    assert "AI is off." in page
    assert "We used form and case numbers to suggest answers." in page


@pytest.mark.django_db
def test_saved_ai_preference_is_available_in_a_closed_disclosure(client, opted_out_draft):
    page = client.get(upload_url()).content.decode()

    # The optional preference is discoverable without changing the filing choice.
    assert 'id="ai-remember"' in page
    assert '<details class="ai-remember" id="ai-remember">' in page
    assert "AI preferences for future filings" in page
    assert "Remember this choice" in page
    assert 'id="ai-remember-choice" />' in page


@pytest.mark.django_db
def test_remembering_the_choice_saves_it_to_the_account(client, opted_out_draft):
    user = opted_out_draft.user
    assert user.ai_assistance_opted_out is False

    response = client.post(upload_url(), {"action": "ai_preference", "ai_opt_out": "yes", "remember_ai_choice": "yes"})

    user.refresh_from_db()
    assert response.json()["remembered"] is True
    assert user.ai_assistance_opted_out is True


@pytest.mark.django_db
def test_clearing_the_remember_box_takes_the_standing_preference_back_off(client, opted_out_draft):
    user = opted_out_draft.user
    user.ai_assistance_opted_out = True
    user.save(update_fields=["ai_assistance_opted_out", "updated_at"])

    response = client.post(upload_url(), {"action": "ai_preference", "ai_opt_out": "yes", "remember_ai_choice": "no"})

    user.refresh_from_db()
    assert response.json()["remembered"] is False
    assert user.ai_assistance_opted_out is False


@pytest.mark.django_db
def test_a_filing_choice_alone_leaves_the_account_preference_untouched(client, opted_out_draft):
    user = opted_out_draft.user
    user.ai_assistance_opted_out = True
    user.save(update_fields=["ai_assistance_opted_out", "updated_at"])

    # No remember field at all: the row was never opened, so this request has
    # nothing to say about the account.
    client.post(upload_url(), {"action": "ai_preference", "ai_opt_out": ""})

    user.refresh_from_db()
    opted_out_draft.refresh_from_db()
    assert user.ai_assistance_opted_out is True
    assert opted_out_draft.ai_assistance_opted_out is False


@pytest.mark.django_db
def test_a_remembered_choice_starts_every_new_filing_opted_out(client, opted_out_draft):
    user = opted_out_draft.user
    user.ai_assistance_opted_out = True
    user.save(update_fields=["ai_assistance_opted_out", "updated_at"])

    new_draft = create_draft(user=user, jurisdiction="massachusetts")

    assert new_draft.ai_assistance_opted_out is True


@pytest.mark.django_db
def test_the_upload_page_says_when_a_standing_preference_is_in_force(client, opted_out_draft):
    user = opted_out_draft.user
    user.ai_assistance_opted_out = True
    user.save(update_fields=["ai_assistance_opted_out", "updated_at"])

    page = client.get(upload_url()).content.decode()

    assert "Saved: new filings start with AI off" in page

import os
import re
import shutil
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from pypdf import PdfReader, PdfWriter

from efile.models import DocumentExtraction, FilingDocument, FilingDraft
from efile.services.current_drafts import CURRENT_DRAFT_SESSION_KEY
from efile.services.document_extractions import (
    EXTRACTION_WAIT_LIMIT,
    claim_next_extraction,
    extraction_is_waiting,
    process_document_extraction,
    queue_document_extraction,
)
from efile.services.extraction_fields import normalize_document_evidence, normalize_extracted_fields
from efile.services.fee_quotes import fee_fingerprint
from efile.services.taxonomy_classification import ClassificationRun, HierarchicalDocumentClassifier
from efile.tests.helpers import reviewed_document
from efile.workflow import ExistingCase


def authorize(client, draft):
    client.force_login(draft.user)
    session = client.session
    session[CURRENT_DRAFT_SESSION_KEY] = draft.pk
    session["auth_tokens"] = {f"TYLER-TOKEN-{draft.jurisdiction.upper()}": "token"}
    session["jurisdiction"] = draft.jurisdiction
    session.save()


@pytest.fixture
def extraction_draft(db, django_user_model):
    user = django_user_model.objects.create_user(username="extraction-user", tyler_jurisdiction="illinois")
    return FilingDraft.objects.create(user=user, jurisdiction="illinois", workflow_version=2)


def test_normalization_keeps_every_extracted_field():
    guesses = normalize_extracted_fields(
        {
            "court name": "Washington County",
            "docker number": "24-CV-123",
            "plaintiff or petitioner names": "Alex Rivera; Sam Rivera",
            "other party names": ["Guardian: Pat Lee", "Minor: Casey Lee"],
            "unexpected useful detail": "Shown on page two",
        }
    )

    assert guesses == {
        "court": "Washington County",
        "docket number": "24-CV-123",
        "plaintiff or petitioner names": "Alex Rivera; Sam Rivera",
        "other party names": "Guardian: Pat Lee; Minor: Casey Lee",
        "unexpected useful detail": "Shown on page two",
    }


@pytest.mark.parametrize("normalizer", [normalize_extracted_fields, normalize_document_evidence])
def test_normalization_canonicalizes_common_party_name_keys(normalizer):
    normalized = normalizer(
        {
            "petitioner names": "Dana Kim",
            "respondent name": "Elliot Kim",
            "other parties": "Ari Kim (Child)",
        }
    )

    assert normalized["plaintiff or petitioner names"] == "Dana Kim"
    assert normalized["defendant or respondent names"] == "Elliot Kim"
    assert normalized["other party names"] == "Ari Kim (Child)"


@pytest.mark.django_db
def test_worker_caps_pages_and_persists_the_complete_payload(extraction_draft):
    document = reviewed_document(
        draft=extraction_draft,
        role=FilingDocument.Role.LEAD,
        name="petition.pdf",
        s3_key="lead/petition.pdf",
    )
    job = queue_document_extraction(document)
    claimed = claim_next_extraction()
    assert claimed.pk == job.pk

    handler = MagicMock()

    def create_download(_key, destination):
        writer = PdfWriter()
        for _index in range(5):
            writer.add_blank_page(width=612, height=792)
        with open(destination, "wb") as pdf_file:
            writer.write(pdf_file)
        return {"success": True}

    handler.download_file.side_effect = create_download

    def inspect_limited_pdf(file_path, _jurisdiction, *, use_ai=True, before_outbound=None):
        assert len(PdfReader(file_path).pages) == 2
        assert use_ai is True
        return {
            "court": "Washington County",
            "document title": "Complaint",
            "plaintiff or petitioner names": "Alex Rivera",
        }

    with (
        override_settings(DOCUMENT_EXTRACTION_MAX_PAGES=2),
        patch("efile.services.document_extractions.S3UploadHandler", return_value=handler),
        patch("efile.services.document_extractions.analyze_document", side_effect=inspect_limited_pdf),
    ):
        process_document_extraction(job.pk, claimed.claim_token)

    job.refresh_from_db()
    extraction_draft.refresh_from_db()
    assert job.status == DocumentExtraction.Status.COMPLETE
    assert job.total_pages == 5
    assert job.pages_analyzed == 2
    assert extraction_draft.extracted_guesses["document title"] == "Complaint"
    assert extraction_draft.extracted_guesses["plaintiff or petitioner names"] == "Alex Rivera"


@pytest.mark.django_db
def test_worker_reads_a_real_uploaded_pdf_before_classification(extraction_draft, settings):
    settings.DOCUMENT_EVIDENCE_STRATEGY = "single"
    """Do not let the standard worker test regress to a fully mocked document."""
    source_pdf = Path(__file__).resolve().parents[3] / "benchmarking/synthetic/filled_pdfs/flattened/MA-01.pdf"
    assert source_pdf.is_file()
    document = reviewed_document(
        draft=extraction_draft,
        role=FilingDocument.Role.LEAD,
        name="MA-01.pdf",
        s3_key="lead/MA-01.pdf",
    )
    extraction_draft.jurisdiction = "massachusetts"
    extraction_draft.save(update_fields=["jurisdiction", "updated_at"])
    job = queue_document_extraction(document)
    handler = MagicMock()

    def download_real_pdf(_key, destination):
        shutil.copyfile(source_pdf, destination)
        return {"success": True}

    handler.download_file.side_effect = download_real_pdf

    def classify_source(_classifier, jurisdiction, evidence, source_text):
        assert jurisdiction == "massachusetts"
        # CJD 101B is not retained in the downloaded-form ID index, so the
        # deterministic pass must not replace the model's partial evidence.
        assert evidence["form identifier"] == "CJD 101"
        assert "COMPLAINT FOR DIVORCE" in source_text
        assert "Middlesex" in source_text
        assert "Division" in source_text
        return ClassificationRun(
            selections={
                "court": {
                    "status": "selected",
                    "name": "Middlesex Probate and Family Court",
                    "route_key": "current-court-key",
                }
            },
            metadata={"prompt_version": "v2", "model": "test-model"},
        )

    with (
        patch("efile.services.document_extractions.S3UploadHandler", return_value=handler),
        patch(
            "efile.services.document_extractions.extract_fields_from_file",
            return_value={
                "form identifier": "CJD 101",
                "form name": "Complaint for Divorce under G.L. c. 208, § 1B",
                "court name": "Middlesex Division",
                "filing phase": "initial",
                "monetary amounts": [{"label": "Amount in controversy", "raw": "$1,275", "amount": "1275"}],
            },
        ),
        patch("efile.services.document_extractions.get_default_model", return_value="test-evidence-model"),
        patch.object(HierarchicalDocumentClassifier, "classify", classify_source),
        patch.object(HierarchicalDocumentClassifier, "__init__", return_value=None),
    ):
        claimed = claim_next_extraction()
        process_document_extraction(job.pk, claimed.claim_token)

    job.refresh_from_db()
    extraction_draft.refresh_from_db()
    assert job.evidence["form identifier"] == "CJD 101"
    assert job.classification["court"]["route_key"] == "current-court-key"
    assert job.analysis_metadata["evidence_prompt_version"] == "v1"
    assert job.analysis_metadata["ai_form_identifier"] == "CJD 101"
    assert job.analysis_metadata["form_identifier_scan"]["status"] == "unmatched"
    assert job.analysis_metadata["form_identifier_scan_source"] == "pypdf"
    assert job.analysis_metadata["form_identifier_scan_ms"] < 2000
    assert extraction_draft.extracted_guesses["court"] == "Middlesex Probate and Family Court"
    assert extraction_draft.amount_in_controversy == ""
    assert job.evidence["monetary amounts"][0]["amount"] == "1275"


@pytest.mark.django_db
def test_review_waits_for_background_analysis(client, extraction_draft):
    authorize(client, extraction_draft)
    document = reviewed_document(
        draft=extraction_draft,
        role=FilingDocument.Role.LEAD,
        name="petition.pdf",
    )
    queue_document_extraction(document)

    response = client.get(reverse("extraction_review", kwargs={"jurisdiction": "illinois"}))

    assert response.status_code == 302
    assert response.url.partition("?")[0] == reverse("upload_documents", kwargs={"jurisdiction": "illinois"})


@pytest.mark.django_db
def test_status_endpoint_reports_when_review_is_ready(client, extraction_draft):
    authorize(client, extraction_draft)
    document = reviewed_document(
        draft=extraction_draft,
        role=FilingDocument.Role.LEAD,
        name="petition.pdf",
    )
    job = queue_document_extraction(document)
    job.status = DocumentExtraction.Status.COMPLETE
    job.total_pages = 30
    job.pages_analyzed = 20
    job.save(update_fields=["status", "total_pages", "pages_analyzed", "updated_at"])

    response = client.get(reverse("document_extraction_status", kwargs={"jurisdiction": "illinois"}))

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "status": "complete",
        "ai_opted_out": False,
        "ready": True,
        "wait_seconds": 0,
        "pages_analyzed": 20,
        "total_pages": 30,
        "review_url": reverse("extraction_review", kwargs={"jurisdiction": "illinois"}),
    }


@pytest.mark.django_db
def test_review_shows_only_the_document_summary_and_fields_the_filer_can_edit(client, extraction_draft):
    """Read-only text is limited to what identifies the document. Values the
    filing uses are shown in the form below, where they can be corrected, and
    values it does not use (like amounts) are not shown at all."""

    authorize(client, extraction_draft)
    reviewed_document(
        draft=extraction_draft,
        role=FilingDocument.Role.LEAD,
        name="petition.pdf",
    )
    extraction_draft.extracted_guesses = {
        "court": "Washington County",
        "document title": "Complaint",
        "plaintiff or petitioner names": "Alex Rivera",
        "unexpected useful detail": "Shown on page two",
        "amounts": [{"label": "Amount claimed", "raw": "$1,200"}],
    }
    extraction_draft.save(update_fields=["extracted_guesses", "updated_at"])

    page = client.get(reverse("extraction_review", kwargs={"jurisdiction": "illinois"}))
    content = page.content.decode()
    summary = content.partition('class="extracted-details"')[2].partition("</section>")[0]
    assert "Document title" in summary
    assert "See everything else" not in content
    assert "Shown on page two" not in summary
    assert "$1,200" not in summary
    # The court is asked for by the form's own court picker further down.
    assert "Washington County" not in summary
    # The names are asked for by the party editor, not printed as evidence.
    assert re.search(r'name="party_name"\s+value="Alex Rivera"', content)
    assert 'name="reviewed_extraction"' in content

    response = client.post(
        reverse("extraction_review", kwargs={"jurisdiction": "illinois"}),
        {
            "existing_case": ExistingCase.NEW,
            "court_code": "washington",
            "case_category_code": "civil",
            "case_type_code": "small-claims",
        },
    )
    assert response.status_code == 200
    assert "Check these details against your document, then check the box." in response.content.decode()


def _post_review(client, jurisdiction="illinois", **fields):
    data = {
        "existing_case": ExistingCase.NEW,
        "court_code": "washington",
        "court_name": "Washington County",
        "case_category_code": "civil",
        "case_category_name": "Civil",
        "case_type_code": "small-claims",
        "case_type_name": "Small Claims",
        "case_title": "Rivera v. Example",
        "party_name": ["Alex Rivera"],
        "party_side": ["plaintiff"],
        "party_is_me": ["0"],
        "party_kind": ["person"],
    }
    data.update(fields)
    return client.post(reverse("extraction_review", kwargs={"jurisdiction": jurisdiction}), data)


@pytest.mark.django_db
def test_missing_acknowledgement_shows_an_error_beside_the_checkbox_and_keeps_edits(client, extraction_draft):
    authorize(client, extraction_draft)
    reviewed_document(draft=extraction_draft, role=FilingDocument.Role.LEAD, name="petition.pdf")
    extraction_draft.extracted_guesses = {"document title": "Complaint", "case title": "Old title"}
    extraction_draft.save(update_fields=["extracted_guesses", "updated_at"])

    response = _post_review(client, case_title="Rivera v. Example")

    assert response.status_code == 200
    content = response.content.decode()
    checkbox = re.search(r'<input[^>]*name="reviewed_extraction"[^>]*>', content, re.S)
    assert checkbox is not None
    assert 'aria-invalid="true"' in checkbox.group(0)
    assert 'aria-describedby="reviewed-extraction-error"' in checkbox.group(0)
    error = re.search(r'<p class="field-error"\s+id="reviewed-extraction-error"\s*>', content)
    assert error is not None, "the error must be rendered visible, not hidden"
    # What the filer submitted comes back, not what was saved or guessed.
    # A new case has no case title field to refill (#196).
    assert 'name="case_title"' not in content
    assert re.search(r'value="new"\s+checked', content)
    assert '"court_code": "washington"' in content
    assert '"case_type_code": "small-claims"' in content
    assert re.search(r'name="party_name"\s+value="Alex Rivera"', content)
    extraction_draft.refresh_from_db()
    assert extraction_draft.case_title == ""


@pytest.mark.django_db
def test_acknowledging_after_the_error_lets_the_filer_continue(client, extraction_draft):
    authorize(client, extraction_draft)
    reviewed_document(draft=extraction_draft, role=FilingDocument.Role.LEAD, name="petition.pdf")
    extraction_draft.extracted_guesses = {"document title": "Complaint"}
    extraction_draft.save(update_fields=["extracted_guesses", "updated_at"])

    assert _post_review(client).status_code == 200
    response = _post_review(client, reviewed_extraction="yes")

    assert response.status_code == 302
    extraction_draft.refresh_from_db()
    assert extraction_draft.court_code == "washington"
    # A new case has no court title yet, so none is saved (#196).
    assert extraction_draft.case_title == ""


@pytest.mark.django_db
@pytest.mark.parametrize(
    "extracted_guesses",
    [
        {},
        # Found, but nothing worth showing: no checkbox is rendered, so none may be demanded.
        {"document title": "", "case title": None},
    ],
)
def test_no_acknowledgement_is_demanded_when_none_is_shown(client, extraction_draft, extracted_guesses):
    authorize(client, extraction_draft)
    reviewed_document(draft=extraction_draft, role=FilingDocument.Role.LEAD, name="petition.pdf")
    extraction_draft.extracted_guesses = extracted_guesses
    extraction_draft.save(update_fields=["extracted_guesses", "updated_at"])

    page = client.get(reverse("extraction_review", kwargs={"jurisdiction": "illinois"})).content.decode()
    assert 'name="reviewed_extraction"' not in page

    response = _post_review(client)
    assert response.status_code == 302


@pytest.mark.django_db
def test_vermont_review_uses_court_form_terms_and_neutral_copy(client, django_user_model):
    user = django_user_model.objects.create_user(username="vermont-reviewer", tyler_jurisdiction="vermont")
    draft = FilingDraft.objects.create(user=user, jurisdiction="vermont", workflow_version=2)
    reviewed_document(draft=draft, role=FilingDocument.Role.LEAD, name="complaint.pdf")
    authorize(client, draft)

    content = client.get(reverse("extraction_review", kwargs={"jurisdiction": "vermont"})).content.decode()

    assert "Court unit or county" in content
    assert "Court division" in content
    assert "Civil includes Small Claims" in content
    assert "Family includes Relief from Abuse" in content
    assert "Enter the details below, or" in content
    assert "We could not find any case details" not in content


@pytest.mark.django_db
def test_failed_extraction_allows_review_with_neutral_failure_copy(client, extraction_draft):
    authorize(client, extraction_draft)
    document = reviewed_document(
        draft=extraction_draft,
        role=FilingDocument.Role.LEAD,
        name="petition.pdf",
    )
    job = queue_document_extraction(document)
    job.status = DocumentExtraction.Status.FAILED
    job.error = "Corrupted PDF"
    job.save(update_fields=["status", "error", "updated_at"])

    response = client.get(reverse("extraction_review", kwargs={"jurisdiction": "illinois"}))

    assert response.status_code == 200
    content = response.content.decode()
    assert "We could not read the details." in content
    assert "try a different document" in content


@pytest.mark.django_db
def test_management_command_runs_once_with_no_jobs():
    from django.core.management import call_command

    call_command("process_document_extractions", once=True)


@pytest.mark.django_db
def test_management_command_processes_and_retries_failures(extraction_draft):
    from django.core.management import call_command

    document = reviewed_document(
        draft=extraction_draft,
        role=FilingDocument.Role.LEAD,
        name="petition.pdf",
        s3_key="lead/petition.pdf",
    )
    job = queue_document_extraction(document)

    # A child killed by a signal exits without recording a Python exception.
    child = MagicMock()
    child.is_alive.return_value = False
    with (
        override_settings(DOCUMENT_EXTRACTION_MAX_ATTEMPTS=2),
        patch(
            "efile.management.commands.process_document_extractions.multiprocessing.get_context",
            return_value=MagicMock(Process=MagicMock(return_value=child)),
        ),
    ):
        # 1st attempt: should fail and reset to PENDING for retry
        call_command("process_document_extractions", once=True)
        job.refresh_from_db()
        assert job.status == DocumentExtraction.Status.PENDING
        assert job.attempts == 1
        assert "Document analysis failed" in job.error

        # 2nd attempt: reaches max_attempts (2) -> status becomes FAILED
        from django.utils import timezone

        DocumentExtraction.objects.filter(pk=job.pk).update(available_at=timezone.now())
        call_command("process_document_extractions", once=True)
        job.refresh_from_db()
        assert job.status == DocumentExtraction.Status.FAILED
        assert job.attempts == 2
        assert "Document analysis failed" in job.error


@pytest.mark.django_db
@pytest.mark.parametrize("status", [None, "pending", "processing", "complete", "failed"])
def test_upload_continue_waits_for_analysis(client, extraction_draft, status):
    authorize(client, extraction_draft)
    if status is not None:
        document = reviewed_document(draft=extraction_draft, role=FilingDocument.Role.LEAD, name="petition.pdf")
        DocumentExtraction.objects.create(document=document, status=status)
    response = client.get(reverse("upload_documents", kwargs={"jurisdiction": "illinois"}))
    assert response.status_code == 200
    content = response.content.decode()
    match = re.search(r'<a\b[^>]*id="continue-to-analysis"[^>]*>', content)
    assert match is not None
    link = match.group()
    if status in {"complete", "failed"}:
        assert ' href="' in link
        assert 'aria-disabled="true"' not in link
    else:
        assert ' href="' not in link
        assert 'aria-disabled="true"' in link
    assert "You can leave this page and come back while we work." not in content


def _queued_lead(draft, *, waited):
    document = reviewed_document(draft=draft, role=FilingDocument.Role.LEAD, name="petition.pdf")
    job = queue_document_extraction(document)
    DocumentExtraction.objects.filter(pk=job.pk).update(created_at=timezone.now() - waited)
    return document


@pytest.mark.django_db
@pytest.mark.parametrize(("waited", "may_continue"), [(timedelta(0), False), (EXTRACTION_WAIT_LIMIT, True)])
def test_filer_may_go_on_without_analysis_after_the_wait_limit(client, extraction_draft, waited, may_continue):
    authorize(client, extraction_draft)
    _queued_lead(extraction_draft, waited=waited + timedelta(seconds=1) if may_continue else waited)

    page = client.get(reverse("upload_documents", kwargs={"jurisdiction": "illinois"})).content.decode()
    link = re.search(r'<a\b[^>]*id="continue-to-analysis"[^>]*>', page)
    assert link is not None
    assert (' href="' in link.group()) is may_continue
    wait = client.get(reverse("document_extraction_status", kwargs={"jurisdiction": "illinois"})).json()
    assert wait["ready"] is False
    assert (wait["wait_seconds"] == 0) is may_continue
    assert 0 <= wait["wait_seconds"] <= EXTRACTION_WAIT_LIMIT.total_seconds()
    review = client.get(reverse("extraction_review", kwargs={"jurisdiction": "illinois"}))
    assert (review.status_code == 200) is may_continue


@pytest.mark.django_db
def test_replacing_the_lead_restarts_the_wait(extraction_draft):
    document = _queued_lead(extraction_draft, waited=EXTRACTION_WAIT_LIMIT * 2)
    job = queue_document_extraction(document)
    assert job.created_at > timezone.now() - timedelta(seconds=5)


@pytest.mark.django_db
@pytest.mark.parametrize("saved_amount", ["", "500.00"])
def test_late_analysis_keeps_fee_inputs_unchanged(extraction_draft, saved_amount):
    _queued_lead(extraction_draft, waited=EXTRACTION_WAIT_LIMIT * 2)
    claimed = claim_next_extraction()
    assert not extraction_is_waiting(claimed)
    extraction_draft.current_step = "review"
    extraction_draft.amount_in_controversy = saved_amount
    extraction_draft.save(update_fields=["current_step", "amount_in_controversy"])
    fingerprint = fee_fingerprint(extraction_draft)

    handler = MagicMock()

    def download(_key, destination):
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        with open(destination, "wb") as pdf_file:
            writer.write(pdf_file)
        return {"success": True}

    handler.download_file.side_effect = download
    evidence = {"monetary amounts": [{"label": "Amount in controversy", "amount": "1275.00"}]}
    with (
        patch("efile.services.document_extractions.S3UploadHandler", return_value=handler),
        patch(
            "efile.services.document_extractions.analyze_document",
            return_value={"guesses": {"document title": "Complaint"}, "evidence": evidence},
        ),
    ):
        process_document_extraction(claimed.pk, claimed.claim_token)

    extraction_draft.refresh_from_db()
    claimed.refresh_from_db()
    assert claimed.status == DocumentExtraction.Status.COMPLETE
    assert claimed.evidence == evidence
    assert extraction_draft.extracted_guesses == {"document title": "Complaint"}
    assert extraction_draft.amount_in_controversy == saved_amount
    assert fee_fingerprint(extraction_draft) == fingerprint


@pytest.mark.django_db
@pytest.mark.parametrize(("configured", "expected"), [(None, "2"), ("4", "4")])
def test_worker_children_start_with_few_malloc_arenas(extraction_draft, monkeypatch, configured, expected):
    from django.core.management import call_command

    if configured is None:
        monkeypatch.delenv("MALLOC_ARENA_MAX", raising=False)
    else:
        monkeypatch.setenv("MALLOC_ARENA_MAX", configured)
    document = reviewed_document(draft=extraction_draft, role=FilingDocument.Role.LEAD, name="petition.pdf")
    queue_document_extraction(document)
    seen = []
    child = MagicMock()
    child.is_alive.return_value = False
    child.start.side_effect = lambda: seen.append(os.environ.get("MALLOC_ARENA_MAX"))
    with patch(
        "efile.management.commands.process_document_extractions.multiprocessing.get_context",
        return_value=MagicMock(Process=MagicMock(return_value=child)),
    ):
        call_command("process_document_extractions", once=True)
    assert seen == [expected]

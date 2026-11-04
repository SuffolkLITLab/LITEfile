import json
from unittest.mock import MagicMock, patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from pypdf import PdfWriter

from efile.models import DocumentExtraction, FilingDocument, FilingDraft
from efile.services.current_drafts import CURRENT_DRAFT_SESSION_KEY
from efile.tests.helpers import accepted_submission
from efile.workflow import ExistingCase, WorkflowStepKey


class FakeApiResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)
        self.headers = {}

    def json(self):
        return self._payload


def authorize_session(client, draft, jurisdiction):
    client.force_login(draft.user)
    session = client.session
    session[CURRENT_DRAFT_SESSION_KEY] = draft.pk
    session["auth_tokens"] = {f"TYLER-TOKEN-{jurisdiction.upper()}": f"token-{jurisdiction}"}
    session["jurisdiction"] = jurisdiction
    session.save()


def make_dummy_pdf():
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    import io

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    (
        "jurisdiction",
        "court_code",
        "court_name",
        "category_code",
        "category_name",
        "case_type_code",
        "case_type_name",
        "filing_type_code",
        "filing_type_name",
        "filer_party_code",
        "filer_party_name",
        "other_party_code",
        "other_party_name",
    ),
    [
        (
            "vermont",
            "vt:chittenden",
            "Chittenden Superior Court",
            "civil",
            "Civil",
            "small-claims",
            "Small Claims",
            "complaint",
            "Complaint",
            "PLA",
            "Plaintiff",
            "DEF",
            "Defendant",
        ),
        (
            "illinois",
            "cook:law1",
            "Cook County Circuit Court - Law Division",
            "civil",
            "Civil",
            "contract",
            "Contract",
            "petition",
            "Petition",
            "PLA",
            "Plaintiff",
            "DEF",
            "Defendant",
        ),
        (
            "massachusetts",
            "suffolk:superior",
            "Suffolk Superior Court",
            "civil",
            "Civil",
            "tort",
            "Tort",
            "complaint",
            "Complaint",
            "PLA",
            "Plaintiff",
            "DEF",
            "Defendant",
        ),
    ],
)
def test_complete_new_filing_flow_by_jurisdiction(
    client,
    django_user_model,
    jurisdiction,
    court_code,
    court_name,
    category_code,
    category_name,
    case_type_code,
    case_type_name,
    filing_type_code,
    filing_type_name,
    filer_party_code,
    filer_party_name,
    other_party_code,
    other_party_name,
):
    """Walk step-by-step through the entire new flow for each state."""

    # 0. Create user and draft
    user = django_user_model.objects.create_user(
        username=f"flow-user-{jurisdiction}",
        email=f"filer@{jurisdiction}.example.org",
        tyler_jurisdiction=jurisdiction,
    )
    draft = FilingDraft.objects.create(
        user=user,
        jurisdiction=jurisdiction,
        workflow_version=2,
    )
    authorize_session(client, draft, jurisdiction)

    # 1. Step: filing-path
    path_response = client.post(
        reverse("filing_path", kwargs={"jurisdiction": jurisdiction}),
        {"existing_case": ExistingCase.NEW},
    )
    assert path_response.status_code == 302
    assert path_response.url.partition("?")[0] == reverse("upload_documents", kwargs={"jurisdiction": jurisdiction})
    draft.refresh_from_db()
    assert draft.existing_case == ExistingCase.NEW
    assert draft.current_step == WorkflowStepKey.UPLOAD_DOCUMENTS

    # 2. Step: upload-documents
    pdf_bytes = make_dummy_pdf()
    lead_file = SimpleUploadedFile("main_document.pdf", pdf_bytes, content_type="application/pdf")
    supp_file = SimpleUploadedFile("exhibit_a.pdf", pdf_bytes, content_type="application/pdf")

    s3_handler = MagicMock()
    s3_handler._ensure_initialized.return_value = True
    s3_handler.validate_file.return_value = {"valid": True}
    s3_handler.upload_file.side_effect = [
        {"success": True, "key": f"lead/{jurisdiction}/main_document.pdf"},
        {"success": True, "key": f"supporting/{jurisdiction}/exhibit_a.pdf"},
    ]
    s3_handler.get_public_url.side_effect = [
        f"https://s3.example.com/{jurisdiction}/main_document.pdf",
        f"https://s3.example.com/{jurisdiction}/exhibit_a.pdf",
    ]

    with patch("efile.services.document_uploads.S3UploadHandler", return_value=s3_handler):
        upload_resp = client.post(
            reverse("upload_documents", kwargs={"jurisdiction": jurisdiction}),
            {"documents": [lead_file, supp_file]},
        )
    assert upload_resp.status_code == 200
    assert upload_resp.json()["success"] is True
    assert upload_resp.json()["extraction_pending"] is True

    draft.refresh_from_db()
    lead_doc = draft.documents.get(role=FilingDocument.Role.LEAD)
    supp_doc = draft.documents.get(role=FilingDocument.Role.SUPPORTING)
    assert lead_doc.name == "main_document.pdf"
    assert supp_doc.name == "exhibit_a.pdf"
    assert lead_doc.extraction.status == DocumentExtraction.Status.PENDING

    # 2b. Check extraction status endpoint while pending
    status_resp = client.get(reverse("document_extraction_status", kwargs={"jurisdiction": jurisdiction}))
    assert status_resp.status_code == 200
    assert status_resp.json()["ready"] is False
    assert status_resp.json()["status"] == "pending"

    # 2c. Background worker runs and processes extraction
    def fake_download(_key, destination):
        with open(destination, "wb") as f:
            f.write(pdf_bytes)
        return {"success": True}

    s3_handler.download_file.side_effect = fake_download

    with (
        patch("efile.services.document_extractions.S3UploadHandler", return_value=s3_handler),
        patch(
            "efile.services.document_extractions.analyze_document",
            return_value={
                "court": court_name,
                "case category": category_name,
                "case type": case_type_name,
                "filing type": filing_type_name,
                "document title": "Initial Pleading",
                "plaintiff or petitioner names": f"Jane Filer ({jurisdiction})",
            },
        ),
    ):
        # Exercise processing in this test's transaction; spawned production
        # workers use a separate connection and cannot see uncommitted fixtures.
        from efile.services.document_extractions import claim_next_extraction, process_document_extraction

        claimed = claim_next_extraction()
        process_document_extraction(claimed.pk, claimed.claim_token)

    # Extraction completed
    lead_doc.extraction.refresh_from_db()
    assert lead_doc.extraction.status == DocumentExtraction.Status.COMPLETE
    draft.refresh_from_db()
    assert draft.extracted_guesses["document title"] == "Initial Pleading"

    # Status endpoint now reports ready
    status_resp2 = client.get(reverse("document_extraction_status", kwargs={"jurisdiction": jurisdiction}))
    assert status_resp2.status_code == 200
    assert status_resp2.json()["ready"] is True
    assert status_resp2.json()["status"] == "complete"

    # 3. Step: extraction-review
    review_page = client.get(reverse("extraction_review", kwargs={"jurisdiction": jurisdiction}))
    assert review_page.status_code == 200
    if jurisdiction == "vermont":
        assert "Court unit or county" in review_page.content.decode()
        assert "Court division" in review_page.content.decode()
    else:
        assert "Court or county" in review_page.content.decode()
        assert "Case category" in review_page.content.decode()

    ext_post = client.post(
        reverse("extraction_review", kwargs={"jurisdiction": jurisdiction}),
        {
            "preview_fingerprint": review_page.context["preview_fingerprint"],
            "existing_case": ExistingCase.NEW,
            "court_code": court_code,
            "court_name": court_name,
            "case_category_code": category_code,
            "case_category_name": category_name,
            "case_type_code": case_type_code,
            "case_type_name": case_type_name,
            "filing_type_code": filing_type_code,
            "filing_type_name": filing_type_name,
        },
    )
    assert ext_post.status_code == 302
    assert ext_post.url.partition("?")[0] == reverse("document_checklist", kwargs={"jurisdiction": jurisdiction})

    draft.refresh_from_db()
    assert draft.court_code == court_code
    assert draft.case_type_code == case_type_code

    # 4. Step: document-checklist
    checklist_resp = client.post(
        reverse("document_checklist", kwargs={"jurisdiction": jurisdiction}),
        {},
    )
    assert checklist_resp.status_code == 302
    assert checklist_resp.url.partition("?")[0] == reverse("organize_documents", kwargs={"jurisdiction": jurisdiction})

    draft.refresh_from_db()
    assert draft.current_step == WorkflowStepKey.ORGANIZE_DOCUMENTS

    # 5. Step: organize-documents
    organize_payload = {
        "main_document_id": lead_doc.pk,
        "documents": [
            {
                "id": lead_doc.pk,
                "name": "Main Document",
                "filing_type": filing_type_code,
                "filing_type_name": filing_type_name,
                "document_type": "public",
                "document_type_name": "Public Document",
                "filing_component": "lead",
                "filing_component_name": "Lead Document",
            },
            {
                "id": supp_doc.pk,
                "name": "Exhibit A",
                "filing_type": filing_type_code,
                "filing_type_name": filing_type_name,
                "document_type": "public",
                "document_type_name": "Public Document",
                "filing_component": "supporting",
                "filing_component_name": "Supporting Document",
            },
        ],
    }
    org_resp = client.post(
        reverse("organize_documents", kwargs={"jurisdiction": jurisdiction}),
        data=json.dumps(organize_payload),
        content_type="application/json",
    )
    assert org_resp.status_code == 200
    assert org_resp.json()["success"] is True
    assert org_resp.json()["redirect_url"].partition("?")[0] == reverse(
        "your_information", kwargs={"jurisdiction": jurisdiction}
    )

    draft.refresh_from_db()
    lead_doc.refresh_from_db()
    supp_doc.refresh_from_db()
    assert lead_doc.role == FilingDocument.Role.LEAD
    assert supp_doc.role == FilingDocument.Role.SUPPORTING
    assert draft.current_step == WorkflowStepKey.YOUR_INFORMATION

    # 6. Step: your-information
    your_info_resp = client.post(
        reverse("your_information", kwargs={"jurisdiction": jurisdiction}),
        {
            "first_name": "Jane",
            "last_name": "Doe",
            "address_line_1": "100 Main Street",
            "city": "Capital City",
            "state": "VT" if jurisdiction == "vermont" else ("IL" if jurisdiction == "illinois" else "MA"),
            "zip_code": "05401" if jurisdiction == "vermont" else ("60601" if jurisdiction == "illinois" else "02108"),
            "email": f"jane.doe@{jurisdiction}.example.org",
            "phone": "802-555-0100" if jurisdiction == "vermont" else "312-555-0100",
        },
    )
    assert your_info_resp.status_code == 302
    assert your_info_resp.url.partition("?")[0] == reverse("parties", kwargs={"jurisdiction": jurisdiction})

    draft.refresh_from_db()
    filer_party = draft.parties.get(role="filer")
    assert filer_party.first_name == "Jane"
    assert filer_party.last_name == "Doe"
    assert draft.current_step == WorkflowStepKey.PARTIES

    # 7. Step: parties
    mock_party_types = [
        {"code": filer_party_code, "name": filer_party_name, "required": True},
        {"code": other_party_code, "name": other_party_name, "required": True},
    ]

    with (
        patch("efile.views.parties.get_party_types", return_value=mock_party_types),
        patch("efile.views.party_details.get_party_types", return_value=mock_party_types),
    ):
        parties_resp = client.post(
            reverse("parties", kwargs={"jurisdiction": jurisdiction}),
            {"filer_party_type": filer_party_code},
        )

        # Since other required party (Defendant) was automatically added and is incomplete,
        # it redirects to party_details to fill in their name and address.
        assert parties_resp.status_code == 302
        assert "party-details" in parties_resp.url

        other_party = draft.parties.get(role="other")
        assert other_party.party_type == other_party_code

        # 7b. Step: party-details (fill in Defendant)
        party_details_resp = client.post(
            f"{reverse('party_details', kwargs={'jurisdiction': jurisdiction})}?party={other_party.pk}",
            {
                "party_type": other_party_code,
                "first_name": "John",
                "last_name": "Smith",
                "address_line_1": "200 State Street",
                "city": "Othertown",
                "state": "VT" if jurisdiction == "vermont" else ("IL" if jurisdiction == "illinois" else "MA"),
                "zip_code": "05401"
                if jurisdiction == "vermont"
                else ("60601" if jurisdiction == "illinois" else "02108"),
            },
        )
        assert party_details_resp.status_code == 302
        assert party_details_resp.url.partition("?")[0] == reverse("payment", kwargs={"jurisdiction": jurisdiction})

    draft.refresh_from_db()
    other_party.refresh_from_db()
    assert other_party.first_name == "John"
    assert other_party.last_name == "Smith"
    assert draft.current_step == WorkflowStepKey.PAYMENT

    # 8. Step: payment
    with patch(
        "efile.views.payment.payment_accounts",
        return_value=[
            {"paymentAccountID": "waiver-account-1", "accountName": "Fee Waiver", "paymentAccountTypeCode": "WV"}
        ],
    ):
        pay_resp = client.post(
            reverse("payment", kwargs={"jurisdiction": jurisdiction}),
            {
                "selected_payment_account": "waiver-account-1",
                "selected_payment_account_name": "Fee Waiver",
                "selected_payment_account_type": "WV",
            },
        )
    assert pay_resp.status_code == 302
    assert pay_resp.url.partition("?")[0] == reverse("case_review", kwargs={"jurisdiction": jurisdiction})

    draft.refresh_from_db()
    assert draft.selected_payment_account_id == "waiver-account-1"
    assert draft.current_step == WorkflowStepKey.REVIEW

    # 9. Step: case_review page
    review_page = client.get(reverse("case_review", kwargs={"jurisdiction": jurisdiction}))
    assert review_page.status_code == 200
    assert "Review your filing" in review_page.content.decode()
    assert "Jane" in review_page.content.decode()
    assert "John" in review_page.content.decode()

    # 10. Step: submit_final_filing
    fake_efsp_response = FakeApiResponse(
        200,
        {
            "success": True,
            "filing_id": f"ENV-{jurisdiction.upper()}-998877",
            "envelope_id": f"ENV-{jurisdiction.upper()}-998877",
            "status": "submitted",
        },
    )

    with (
        patch("requests.post", return_value=fake_efsp_response),
        patch("efile.views.session_api.prepare_efile_payload", return_value={"al_court_bundle": {"lead_document": {}}}),
    ):
        submit_resp = client.post(
            reverse("submit_final_filing"),
            data=json.dumps(accepted_submission(draft, efile_data={"al_court_bundle": {}})),
            content_type="application/json",
        )

    assert submit_resp.status_code == 200
    body = submit_resp.json()
    assert body["success"] is True

    draft.refresh_from_db()
    assert draft.status == FilingDraft.Status.SUBMITTED
    assert draft.submission_response.get("envelope_id") == f"ENV-{jurisdiction.upper()}-998877"

"""Files added on the checklist are checked there, not after a detour from Review."""

from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from efile.models import FilingDocument
from efile.services.document_previews import preview_fingerprint
from efile.tests import test_filing_plan_actions as plan_actions
from efile.tests.pdf_helpers import pdf_bytes

CHECKLIST_URL = plan_actions.CHECKLIST_URL
user = plan_actions.user
draft = plan_actions.draft
signed_in = plan_actions.signed_in

pytestmark = pytest.mark.django_db

CHECKS_URL = reverse("document_checks", kwargs={"jurisdiction": "illinois"})


def new_file(draft, **fields):
    """A file as the upload leaves it: prepared, but its copy not yet confirmed."""
    return FilingDocument.objects.create(
        draft=draft,
        role=FilingDocument.Role.SUPPORTING,
        sort_order=1,
        name="fee-waiver.pdf",
        preparation="unchanged",
        **fields,
    )


def confirm(client, draft, document):
    document.refresh_from_db()
    return client.post(
        f"{CHECKS_URL}?draft={draft.pk}",
        {"action": "confirm", "document_id": document.pk, "preview_fingerprint": preview_fingerprint([document])},
    )


@pytest.mark.usefixtures("enabled_document_checklists")
def test_uploading_for_an_item_lands_on_its_check(client, signed_in):
    def upload(draft, files, jurisdiction, **kwargs):
        new_file(draft)

    with patch("efile.views.document_checklist.upload_files", side_effect=upload):
        response = client.post(
            CHECKLIST_URL,
            {"action": "attach_item", "item_id": "fee_waiver", "document": SimpleUploadedFile("w.pdf", pdf_bytes())},
        )
    assert response.status_code == 302
    assert response.url.endswith("#document-checks")
    added = signed_in.documents.get(role=FilingDocument.Role.SUPPORTING)
    page = client.get(CHECKLIST_URL)
    content = page.content.decode()
    assert f'data-document-id="{added.pk}"' in content
    assert "This copy looks right" in content
    assert 'aria-describedby="checks-pending"' in content
    # The confirmed lead is listed as added; the new copy is only in its check.
    assert [doc.pk for doc in page.context["checked_documents"]] == [
        signed_in.documents.get(role=FilingDocument.Role.LEAD).pk
    ]


def test_continue_waits_for_new_files_to_be_checked(client, signed_in):
    added = new_file(signed_in, filing_type_code="78690", document_type_code="public")
    blocked = client.post(CHECKLIST_URL, {"documents_complete": "yes"})
    assert blocked.url.partition("#")[0].partition("?")[0] == CHECKLIST_URL
    assert blocked.url.endswith("#document-checks")
    assert confirm(client, signed_in, added).status_code == 200
    page = client.get(CHECKLIST_URL).content.decode()
    assert "checks-pending" not in page
    moved_on = client.post(CHECKLIST_URL, {"documents_complete": "yes"})
    assert "organize-documents" in moved_on.url


def test_a_file_added_on_the_way_back_from_review_needs_no_second_preview(client, signed_in):
    added = new_file(signed_in, filing_type_code="78690", document_type_code="public")
    assert confirm(client, signed_in, added).status_code == 200
    signed_in.selected_payment_account_id = "account"
    signed_in.save()
    response = client.post(
        f"{CHECKLIST_URL}?return_to=review",
        {"documents_complete": "yes", "return_to": "review", "status_petition": "have"},
    )
    assert response.url.partition("?")[0] == reverse("case_review", kwargs={"jurisdiction": "illinois"})
    with patch("efile.views.review.get_case_questions", return_value=[]):
        review = client.get(response.url)
    # Review used to bounce here to the preview step and back.
    assert review.status_code == 200

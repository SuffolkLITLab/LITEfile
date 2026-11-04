"""Confirming or removing a newly prepared copy on the page it was added to."""

import io
from unittest.mock import MagicMock, patch

import pytest
from django.test import Client
from django.urls import reverse

from efile.models import FilingDocument, FilingDraft
from efile.services.document_previews import preview_fingerprint
from efile.services.fee_quotes import fee_inputs_token
from efile.tests.pdf_helpers import pdf_bytes
from efile.tests.test_review_submit_flow import submission_draft as _submission_draft

draft = _submission_draft

pytestmark = pytest.mark.django_db


def checks(draft):
    return reverse("document_checks", kwargs={"jurisdiction": draft.jurisdiction}) + f"?draft={draft.pk}"


@pytest.fixture
def added(draft):
    return FilingDocument.objects.create(
        draft=draft,
        role=FilingDocument.Role.SUPPORTING,
        sort_order=1,
        name="exhibit.pdf",
        s3_key="supporting/exhibit.pdf",
        preparation="unchanged",
    )


def confirm(document, **changes):
    document.refresh_from_db()
    return {
        "action": "confirm",
        "document_id": document.pk,
        "preview_fingerprint": preview_fingerprint([document]),
        **changes,
    }


def test_confirming_marks_only_that_copy_checked(client, draft, added):
    response = client.post(checks(draft), confirm(added))
    assert response.status_code == 200
    added.refresh_from_db()
    assert added.preparation_reviewed_at is not None


@pytest.mark.parametrize("action", ["confirm", "remove"])
def test_payment_checks_reject_unrelated_fee_input_changes(client, draft, added, action):
    token = fee_inputs_token(draft)
    # Another tab changes the case, but this document's preview is unchanged.
    draft.case_type_code = "another-case-type"
    draft.save(update_fields=["case_type_code", "updated_at"])
    response = client.post(checks(draft), confirm(added, action=action, fee_inputs_token=token))
    assert response.status_code == 409
    assert "Reload this page" in response.json()["error"]
    assert "fee_inputs_token" not in response.json()
    added.refresh_from_db()
    assert added.preparation_reviewed_at is None


def test_payment_confirmation_returns_only_a_token_matching_the_page(client, draft, added):
    token = fee_inputs_token(draft)
    response = client.post(checks(draft), confirm(added, fee_inputs_token=token))
    assert response.status_code == 200
    assert response.json()["fee_inputs_token"] == token
    added.refresh_from_db()
    assert added.preparation_reviewed_at is not None


def test_a_changed_unprepared_or_foreign_copy_is_not_confirmed(client, draft, added):
    assert client.post(checks(draft), confirm(added, preview_fingerprint="old")).status_code == 409
    FilingDocument.objects.filter(pk=added.pk).update(preparation="")
    assert client.post(checks(draft), confirm(added)).status_code == 409
    other = FilingDraft.objects.create(user=draft.user, jurisdiction="illinois")
    foreign = FilingDocument.objects.create(draft=other, role="lead", name="x.pdf", preparation="unchanged")
    assert client.post(checks(draft), confirm(foreign)).status_code == 409
    added.refresh_from_db()
    foreign.refresh_from_db()
    assert added.preparation_reviewed_at is None
    assert foreign.preparation_reviewed_at is None


def test_removing_takes_out_a_supporting_file_and_reprices(client, draft, added, django_capture_on_commit_callbacks):
    lead = draft.documents.get(role=FilingDocument.Role.LEAD)
    draft.quoted_fee_total = "100"
    draft.save()
    remove = {"action": "remove", "fee_inputs_token": fee_inputs_token(draft)}
    assert client.post(checks(draft), remove | {"document_id": lead.pk}).status_code == 400
    storage = MagicMock()
    storage.delete_file.return_value = {"success": True}
    with (
        patch("efile.utils.s3_upload_handler.S3UploadHandler", return_value=storage),
        django_capture_on_commit_callbacks(execute=True),
    ):
        response = client.post(checks(draft), remove | {"document_id": added.pk})
    assert response.status_code == 200
    assert list(draft.documents.all()) == [lead]
    draft.refresh_from_db()
    assert draft.quoted_fee_total == ""
    assert response.json()["fee_inputs_token"] == fee_inputs_token(draft)
    assert response.json()["fee_inputs_token"] != remove["fee_inputs_token"]
    storage.delete_file.assert_called_with("supporting/exhibit.pdf")


def test_checks_need_a_signed_in_named_filing_and_csrf(client, draft, added):
    unscoped = reverse("document_checks", kwargs={"jurisdiction": "illinois"})
    assert Client().post(unscoped, confirm(added)).status_code == 401
    # Someone else's draft is refused before the view, by the draft scope.
    assert Client().post(checks(draft), confirm(added)).status_code == 409
    assert client.post(unscoped, confirm(added)).status_code == 409
    assert client.get(checks(draft)).status_code == 405
    protected = Client(enforce_csrf_checks=True)
    protected.force_login(draft.user)
    assert protected.post(checks(draft), confirm(added)).status_code == 403


@pytest.mark.parametrize("page", ["payment", "document_checklist"])
@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("return_to", ["", "review", "handoff"])
def test_legacy_main_documents_recover_through_preparation(client, draft, page, method, return_to):
    lead = draft.documents.get(role="lead")
    draft.documents.update(preparation="", preparation_reviewed_at=None, s3_key="legacy/filing.pdf")
    url = reverse(page, kwargs={"jurisdiction": draft.jurisdiction}) + f"?draft={draft.pk}"
    if return_to:
        url += f"&return_to={return_to}"
    with patch("efile.views.document_checklist.draft_unavailable_message", return_value=""):
        response = getattr(client, method)(url)
    assert response.status_code == 302
    assert response.url.partition("?")[0] == reverse("extraction_review", kwargs={"jurisdiction": draft.jurisdiction})
    assert f"draft={draft.pk}" in response.url
    if return_to:
        assert f"return_to={return_to}" in response.url

    storage = MagicMock()
    storage.bucket_name = "private"
    storage.s3_client.get_object.side_effect = lambda **_kwargs: {"Body": io.BytesIO(pdf_bytes())}
    storage.upload_file.return_value = {"success": True, "key": "prepared/filing.pdf"}
    storage.get_public_url.return_value = "https://synthetic.invalid/prepared.pdf"
    with patch("efile.views.extraction_review.S3UploadHandler", return_value=storage):
        prepared = client.get(response.url)
    assert prepared.status_code == 200
    lead.refresh_from_db()
    assert lead.preparation == "unchanged"
    assert lead.preparation_reviewed_at is None
    approved = client.post(
        response.url,
        {
            "preview_fingerprint": preview_fingerprint(list(draft.documents.all())),
            "return_to": return_to,
            "existing_case": draft.existing_case,
            "court_code": draft.court_code,
            "case_category_code": draft.case_category_code,
            "case_type_code": draft.case_type_code,
        },
    )
    assert approved.status_code == 302
    lead.refresh_from_db()
    assert lead.preparation_reviewed_at is not None
    # The original screen is now reachable and the filer can continue there.
    with (
        patch("efile.views.document_checklist.draft_unavailable_message", return_value=""),
        patch("efile.views.payment.estimate_fees", return_value={}),
    ):
        assert client.get(url).status_code == 200


@pytest.mark.parametrize("page", ["payment", "document_checklist"])
def test_prepared_unchecked_files_stay_inline(client, draft, added, page):
    url = reverse(page, kwargs={"jurisdiction": draft.jurisdiction}) + f"?draft={draft.pk}"
    with (
        patch("efile.views.document_checklist.draft_unavailable_message", return_value=""),
        patch("efile.views.payment.estimate_fees", return_value={}),
    ):
        response = client.get(url)
    assert response.status_code == 200
    assert f'data-document-id="{added.pk}"' in response.content.decode()

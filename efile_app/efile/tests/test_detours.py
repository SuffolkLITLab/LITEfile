"""A filer sent to an earlier step from Review or the handoff list comes back.

See the "Detours" section of efile.workflow: only a step's completion decides
where to go next, and an intermediate action keeps the marker on its screen.
"""

import json
from unittest.mock import patch

import pytest
from django.urls import reverse

from efile.models import FilingDocument, FilingParty
from efile.services.document_previews import preview_fingerprint
from efile.services.drafts import read_upload_data, write_upload_data
from efile.services.filing_path import change_filing_path
from efile.tests.test_review_submit_flow import submission_draft as _submission_draft
from efile.views.organize_documents import _save_document_details
from efile.workflow import (
    WorkflowStepKey,
    clean_return_to,
    continue_step,
    continue_url,
    documents_need_organizing,
    with_return_to,
)

draft = _submission_draft

pytestmark = pytest.mark.django_db

J = {"jurisdiction": "illinois"}


def path(url):
    return url.partition("?")[0]


def step(name, draft, return_to=""):
    url = reverse(name, kwargs=J) + f"?draft={draft.pk}"
    return f"{url}&return_to={return_to}" if return_to else url


def test_only_known_origins_are_markers():
    assert clean_return_to("review") == "review"
    assert clean_return_to("handoff") == "handoff"
    for value in ("payment", "document_checklist", "https://example.com", "", None, '"><script>'):
        assert clean_return_to(value) == ""
        assert with_return_to("/x/", value) == "/x/"
    assert with_return_to("/x/?a=1", "review") == "/x/?a=1&return_to=review"


def test_finished_steps_go_back_or_stop_where_the_change_needs_it(draft):
    review = reverse("case_review", kwargs=J)
    assert continue_url(draft, "illinois", "", WorkflowStepKey.PARTIES) == reverse("parties", kwargs=J)
    assert continue_url(draft, "illinois", "review", WorkflowStepKey.PARTIES) == review
    assert continue_url(draft, "illinois", "handoff", WorkflowStepKey.PARTIES) == reverse(
        "handoff_review", args=[draft.pk]
    )
    FilingDocument.objects.create(draft=draft, role="supporting", name="new.pdf")
    assert documents_need_organizing(draft)
    assert continue_step(draft, "review", WorkflowStepKey.PARTIES) == WorkflowStepKey.ORGANIZE_DOCUMENTS
    assert continue_url(draft, "illinois", "review", WorkflowStepKey.PARTIES) == (
        reverse("organize_documents", kwargs=J) + "?return_to=review"
    )
    # Without a detour the linear flow is unchanged; Organize comes in order.
    assert continue_step(draft, "", WorkflowStepKey.PARTIES) == WorkflowStepKey.PARTIES


def test_an_empty_document_type_is_a_finished_answer(draft):
    # Organize saves no document type when the court offers no confidentiality
    # choices. Treating that as unorganized would send Organize back to itself.
    draft.documents.update(document_type_code="", document_type_name="")
    assert documents_need_organizing(draft)
    lead = draft.documents.get()

    with patch("efile.views.organize_documents._court_document_types", return_value=[]):
        _save_document_details(
            draft,
            [{"id": lead.pk, "filing_type": lead.filing_type_code, "document_type": ""}],
            lead.pk,
        )
    assert not documents_need_organizing(draft)
    assert continue_url(draft, "illinois", "review", WorkflowStepKey.YOUR_INFORMATION) == reverse(
        "case_review", kwargs=J
    )


def test_empty_confidentiality_confirmation_is_reset_when_the_filing_path_changes(draft):
    draft.documents.update(document_type_code="", document_type_confirmed=True)
    change_filing_path(draft, "existing")
    lead = draft.documents.get()
    assert not lead.document_type_confirmed
    lead.filing_type_code = "automatically-assigned"
    lead.save()
    assert documents_need_organizing(draft)


def test_empty_confidentiality_confirmation_survives_rebuild_only_for_the_same_types(draft):
    document = FilingDocument.objects.create(
        draft=draft,
        role="supporting",
        s3_key="supporting.pdf",
        filing_type_code="motion",
        document_type_confirmed=True,
    )
    wire = read_upload_data(draft)
    write_upload_data(draft, wire)
    document = draft.documents.get(s3_key=document.s3_key)
    assert document.document_type_confirmed
    assert not documents_need_organizing(draft)

    wire["supporting_documents"][0]["filing_type"] = "different-type"
    write_upload_data(draft, wire)
    assert not draft.documents.get(s3_key=document.s3_key).document_type_confirmed
    assert documents_need_organizing(draft)


def test_adding_a_person_on_a_handoff_detour_stays_on_the_people_screens(client, draft):
    response = client.post(step("parties", draft, "handoff"), {"action": "add", "return_to": "handoff"})
    assert response.status_code == 302
    added = draft.parties.exclude(role="filer").get()
    assert path(response.url) == reverse("party_details", kwargs=J)
    assert f"party={added.pk}" in response.url and "return_to=handoff" in response.url


def test_finding_a_case_on_a_handoff_detour_still_asks_to_confirm_it(client, draft, monkeypatch):
    draft.existing_case = "existing"
    draft.save()
    response = client.post(
        step("case_lookup", draft, "handoff"),
        json.dumps({"court": "cook:law1", "case_docket_id": "24-L-1", "case_tracking_id": "T-1"}),
        content_type="application/json",
    )
    redirect_url = response.json()["redirect_url"]
    assert path(redirect_url) == reverse("case_confirmation", kwargs=J)
    assert "return_to=handoff" in redirect_url
    page = client.get(redirect_url)
    assert b'name="return_to" value="handoff"' in page.content
    from efile.tests.helpers import loaded_case_snapshot

    monkeypatch.setattr(
        "efile.services.existing_cases.fetch_case", lambda draft, token: loaded_case_snapshot(draft, confirmed=False)
    )

    with patch("efile.views.case_confirmation.draft_unavailable_message", return_value=""):
        confirmed = client.post(redirect_url, {"confirmed": "yes", "return_to": "handoff"})
    assert confirmed.url == reverse("handoff_review", args=[draft.pk])


def test_unchecked_files_on_a_detour_return_through_the_preview(client, draft):
    draft.documents.update(preparation_reviewed_at=None)
    response = client.get(step("extraction_review", draft, "handoff"))
    assert response.status_code == 200
    assert response.context["return_to"] == "handoff"


def test_switching_to_an_existing_case_from_review_finds_it_then_returns(client, draft):
    with patch("efile.views.extraction_review.filing_unavailable_message", return_value=""):
        response = client.post(
            step("extraction_review", draft),
            {
                "existing_case": "existing",
                "docket_number": "24-L-1",
                "court_code": "cook:law1",
                "court_name": "Circuit Court of Cook County",
                "case_category_code": "civil",
                "case_category_name": "Civil",
                "case_type_code": "contract",
                "case_type_name": "Contract",
                "preview_fingerprint": preview_fingerprint(list(draft.documents.all())),
                "return_to": "review",
            },
        )
    assert response.status_code == 302
    assert path(response.url) == reverse("case_lookup", kwargs=J)
    assert "return_to=review" in response.url
    draft.refresh_from_db()
    assert draft.current_step == WorkflowStepKey.CASE_LOOKUP


def test_fees_on_a_handoff_detour_return_to_the_list(client, draft):
    with patch(
        "efile.views.payment.payment_accounts",
        return_value=[{"paymentAccountID": "cc", "paymentAccountTypeCode": "CC", "accountName": "Card"}],
    ):
        response = client.post(step("payment", draft, "handoff"), {"selected_payment_account": "cc"})
    assert response.url == reverse("handoff_review", args=[draft.pk])
    draft.refresh_from_db()
    assert draft.selected_payment_account_id == "cc"


@pytest.mark.parametrize(
    "name", ["your_information", "parties", "upload_documents", "organize_documents", "document_checklist"]
)
def test_pages_never_echo_an_unknown_marker(client, draft, name):
    FilingParty.objects.filter(draft=draft).update(is_filing_party=True)
    with (
        patch("efile.views.organize_documents.draft_unavailable_message", return_value=""),
        patch("efile.views.document_checklist.draft_unavailable_message", return_value=""),
    ):
        response = client.get(step(name, draft, "evil%22%3E"))
    assert response.status_code == 200
    assert b"evil" not in response.content

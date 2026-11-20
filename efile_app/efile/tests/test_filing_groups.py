"""Lead and attachment PDFs must be children of one proxy filing."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from django.urls import reverse
from django.utils import timezone

from efile.models import FilingDocument, FilingDraft
from efile.services.efsp_payload import PayloadValidationError, bind_filing_documents, prepare_efile_payload
from efile.services.fee_quotes import fee_fingerprint
from efile.services.filing_groups import group_filing_documents
from efile.tests.test_document_prep import document_draft

__all__ = ["document_draft"]
pytestmark = pytest.mark.django_db
COMPONENTS = [
    {"code": "332", "name": "Lead Document", "required": True, "allowmultiple": False, "efspcode": "LEAD"},
    {"code": "331", "name": "Attachments", "required": False, "allowmultiple": True, "efspcode": "ATTACH"},
]


@pytest.fixture
def grouped(document_draft):
    lead = document_draft.documents.get()
    lead.filing_type_code = "petition"
    lead.filing_component_code = "332"
    lead.document_type_code = "public"
    lead.s3_key = "owned/lead.pdf"
    lead.save()
    child = FilingDocument.objects.create(
        draft=document_draft,
        role="supporting",
        name="Exhibit.pdf",
        s3_key="owned/exhibit.pdf",
        filing_type_code="petition",
        filing_component_code="331",
        document_type_code="public",
        attached_to=lead,
        preparation="unchanged",
        preparation_reviewed_at=timezone.now(),
    )
    return document_draft, lead, child


def payload(draft, monkeypatch):
    monkeypatch.setattr(
        "efile.services.efsp_payload.S3UploadHandler.get_public_url",
        lambda self, key, **kw: f"https://storage.example/{key}",
    )
    data = {
        "users": [],
        "al_court_bundle": [{"elements": [{"data_url": "https://untrusted.example"}]} for _ in draft.documents.all()],
    }
    bind_filing_documents(data, draft)
    return data


def test_group_uses_owned_urls_and_matches_efsp_integration(grouped, monkeypatch):
    draft, lead, child = grouped
    data = payload(draft, monkeypatch)
    group_filing_documents(data, draft, lookups=Mock(get=Mock(return_value=COMPONENTS)))
    assert len(data["al_court_bundle"]) == 1
    filing = data["al_court_bundle"][0]
    assert filing["filing_type"] == "petition"
    assert filing["tyler_merge_attachments"] is False
    assert "data_url" not in filing
    assert [item["filing_component"] for item in filing["elements"]] == ["332", "331"]
    assert [item["data_url"] for item in filing["elements"]] == [
        "https://storage.example/owned/lead.pdf",
        "https://storage.example/owned/exhibit.pdf",
    ]


def test_two_leads_remain_two_filings(grouped, monkeypatch):
    draft, lead, child = grouped
    child.attached_to = None
    child.filing_component_code = "332"
    child.save()
    data = payload(draft, monkeypatch)
    group_filing_documents(data, draft, lookups=Mock(get=Mock(return_value=COMPONENTS)))
    assert len(data["al_court_bundle"]) == 2
    assert all("elements" not in item for item in data["al_court_bundle"])


@pytest.mark.parametrize(
    "problem", ["orphan", "cycle", "foreign", "wrong_code", "two_leads", "services", "copies", "missing_lead"]
)
def test_invalid_group_blocked(grouped, monkeypatch, problem):
    draft, lead, child = grouped
    if problem == "orphan":
        child.attached_to = None
    elif problem == "cycle":
        lead.attached_to = child
        lead.save()
    elif problem == "foreign":
        other = FilingDraft.objects.create(user=draft.user, jurisdiction="illinois")
        child.attached_to = FilingDocument.objects.create(draft=other, role="lead")
    elif problem == "wrong_code":
        child.filing_type_code = "exhibit"
    elif problem == "two_leads":
        child.filing_component_code = "332"
    elif problem == "services":
        child.requested_optional_services = [{"code": "extra"}]
    elif problem == "copies":
        child.courtesy_copy_email = "test@example.org"
    elif problem == "missing_lead":
        lead.filing_component_code = "331"
        lead.save()
    child.save()
    with pytest.raises(PayloadValidationError):
        group_filing_documents(payload(draft, monkeypatch), draft, lookups=Mock(get=Mock(return_value=COMPONENTS)))


@pytest.mark.parametrize("components", [None, [], [dict(c, allowmultiple=False) for c in COMPONENTS]])
def test_missing_rules_and_nonrepeatable_components_block(grouped, monkeypatch, components):
    draft, lead, child = grouped
    FilingDocument.objects.create(
        draft=draft,
        role="supporting",
        sort_order=1,
        name="B.pdf",
        s3_key="owned/b.pdf",
        filing_type_code="petition",
        filing_component_code="331",
        document_type_code="public",
        attached_to=lead,
    )
    with pytest.raises(PayloadValidationError):
        group_filing_documents(payload(draft, monkeypatch), draft, lookups=Mock(get=Mock(return_value=components)))


def test_parent_change_invalidates_quote(grouped):
    draft, lead, child = grouped
    before = fee_fingerprint(draft)
    child.attached_to = None
    child.save()
    assert fee_fingerprint(draft) != before


def test_organize_saves_and_review_shows_parent(client, grouped):
    draft, lead, child = grouped
    details = [
        {
            "id": doc.pk,
            "name": doc.name,
            "filing_type": "petition",
            "document_type": "public",
            "filing_component": doc.filing_component_code,
            "attached_to": doc.attached_to_id,
        }
        for doc in (lead, child)
    ]
    response = client.post(
        reverse("organize_documents", args=["illinois"]),
        {"documents": details, "main_document_id": lead.pk, "return_to": "review"},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    child.refresh_from_db()
    assert child.attached_to_id == lead.pk
    draft.selected_payment_account_id = "test-payment"
    draft.save()
    page = client.get(reverse("case_review", args=["illinois"]))
    assert b"Attachment to: petition.pdf" in page.content


def test_fee_and_submission_use_same_grouped_payload(grouped, monkeypatch, settings):
    draft, lead, child = grouped
    settings.EFSP_TEST_DOCUMENT_URL = ""
    draft.court_code = "winnebago"
    draft.save()
    monkeypatch.setattr("efile.services.existing_cases.reconcile_case_parties", lambda *a, **kw: None)
    monkeypatch.setattr("efile.services.service_contacts.bind_service", lambda *a, **kw: None)
    monkeypatch.setattr(
        "efile.services.efsp_payload._EfspLookups.get",
        lambda self, url: COMPONENTS if url.endswith("filing_components") else [],
    )
    original = payload(draft, monkeypatch)
    fee = prepare_efile_payload(deepcopy(original), "illinois", "winnebago", draft=draft)
    submission = prepare_efile_payload(deepcopy(original), "illinois", "winnebago", draft=draft)
    assert fee == submission
    assert len(submission["al_court_bundle"]) == 1
    assert len(submission["al_court_bundle"][0]["elements"]) == 2


def test_correction_copy_remaps_attachment_parent(grouped):
    from efile.services.handoff import create_correction

    draft, lead, child = grouped
    draft.mark_submitted({"filing_id": "old"})
    revision = create_correction(draft, {"status": "returned"})
    copied_lead = revision.documents.get(role="lead")
    copied_child = revision.documents.get(role="supporting")
    assert copied_child.attached_to_id == copied_lead.pk
    assert copied_child.attached_to_id != lead.pk


def test_legacy_upload_save_preserves_parent_links(grouped):
    from efile.services.drafts import read_upload_data, write_upload_data

    draft, lead, child = grouped
    data = read_upload_data(draft)
    write_upload_data(draft, data)
    rebuilt = draft.documents.get(role="supporting")
    assert rebuilt.attached_to_id == lead.pk


def test_deleting_parent_does_not_promote_attachment_silently(grouped, monkeypatch):
    draft, lead, child = grouped
    lead.delete()
    child.refresh_from_db()
    assert child.attached_to_id is None
    with pytest.raises(PayloadValidationError, match="is marked as an attachment but has no lead document"):
        group_filing_documents(payload(draft, monkeypatch), draft, lookups=Mock(get=Mock(return_value=COMPONENTS)))


def test_invalid_filing_code_is_actionable(grouped, monkeypatch):
    draft, lead, child = grouped
    draft.documents.update(filing_type_code="../../invalid")
    with pytest.raises(PayloadValidationError, match="valid filing code"):
        group_filing_documents(payload(draft, monkeypatch), draft, lookups=Mock())


@pytest.mark.parametrize("parent", [True, "1", -1, 99999])
def test_organize_rejects_invalid_parent_identifiers(client, grouped, parent):
    draft, lead, child = grouped
    details = [
        {
            "id": doc.pk,
            "name": doc.name,
            "filing_type": "petition",
            "document_type": "public",
            "filing_component": doc.filing_component_code,
            "attached_to": parent if doc == child else None,
        }
        for doc in (lead, child)
    ]
    response = client.post(
        reverse("organize_documents", args=["illinois"]),
        {"documents": details, "main_document_id": lead.pk},
        content_type="application/json",
    )
    assert response.status_code == 400
    child.refresh_from_db()
    assert child.attached_to_id == lead.pk


def test_legacy_rebuild_remaps_a_supporting_parent(grouped):
    from efile.services.drafts import read_upload_data, write_upload_data

    draft, lead, child = grouped
    parent = FilingDocument.objects.create(
        draft=draft,
        role="supporting",
        sort_order=1,
        name="Other lead.pdf",
        s3_key="owned/other.pdf",
        filing_type_code="petition",
        filing_component_code="332",
        document_type_code="public",
    )
    child.attached_to = parent
    child.save()
    write_upload_data(draft, read_upload_data(draft))
    rebuilt_child = draft.documents.get(s3_key="owned/exhibit.pdf")
    rebuilt_parent = draft.documents.get(s3_key="owned/other.pdf")
    assert rebuilt_parent.pk != parent.pk
    assert rebuilt_child.attached_to_id == rebuilt_parent.pk


def test_orphan_attachment_names_pdf_and_links_to_correction(grouped, monkeypatch):
    from efile.services.efsp_errors import error_actions

    draft, lead, child = grouped
    child.attached_to = None
    child.save()
    data = payload(draft, monkeypatch)
    with pytest.raises(PayloadValidationError) as caught:
        group_filing_documents(data, draft, lookups=Mock(get=Mock(return_value=COMPONENTS)))
    message = str(caught.value)
    assert child.name in message
    assert '"Attachment to: [document name]"' in message
    assert '"Lead document"' in message
    assert isinstance(caught.value, PayloadValidationError)
    actions = error_actions(draft, data, caught.value.problems)
    assert actions[0]["label"] == "Fix document roles in Organize documents"
    assert f"draft={draft.pk}&return_to=review" in actions[0]["url"]

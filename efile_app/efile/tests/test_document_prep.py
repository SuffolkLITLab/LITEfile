from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from efile.models import FilingDocument, FilingDraft
from efile.services.current_drafts import CURRENT_DRAFT_SESSION_KEY
from efile.services.drafts import read_upload_data
from efile.tests.helpers import reviewed_document
from efile.workflow import ExistingCase, WorkflowStepKey


def authorize(client, draft):
    session = client.session
    session[CURRENT_DRAFT_SESSION_KEY] = draft.pk
    session["auth_tokens"] = {"TYLER-TOKEN-ILLINOIS": "token"}
    session["jurisdiction"] = "illinois"
    session.save()


@pytest.fixture
def document_draft(client, django_user_model):
    user = django_user_model.objects.create_user(username="document-user", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(
        user=user,
        jurisdiction="illinois",
        workflow_version=2,
        existing_case=ExistingCase.NEW,
        court_code="cook:cd",
        case_category_code="100",
        case_type_code="200",
        current_step=WorkflowStepKey.DOCUMENT_CHECKLIST,
    )
    reviewed_document(
        draft=draft,
        role=FilingDocument.Role.LEAD,
        sort_order=0,
        name="petition.pdf",
    )
    client.force_login(user)
    authorize(client, draft)
    return draft


@pytest.mark.django_db
def test_document_checklist_continues_without_any_confirmation(client, document_draft):
    # A guide, not a gate: nothing has to be ticked or confirmed to continue.
    response = client.post(reverse("document_checklist", kwargs={"jurisdiction": "illinois"}), {})

    document_draft.refresh_from_db()
    assert response.status_code == 302
    assert response.url.partition("?")[0] == reverse("organize_documents", kwargs={"jurisdiction": "illinois"})
    assert document_draft.current_step == WorkflowStepKey.ORGANIZE_DOCUMENTS


@pytest.mark.django_db
def test_document_checklist_does_not_ask_the_filer_to_confirm_it_is_complete(client, document_draft):
    page = client.get(reverse("document_checklist", kwargs={"jurisdiction": "illinois"})).content.decode()

    assert 'name="documents_complete"' not in page
    assert "I have added all the documents" not in page


@pytest.mark.django_db
def test_document_checklist_adds_missing_documents_inline(client, document_draft):
    upload = SimpleUploadedFile("exhibit.pdf", b"%PDF exhibit", content_type="application/pdf")

    with patch("efile.views.document_checklist.upload_files") as upload_files:
        response = client.post(
            reverse("document_checklist", kwargs={"jurisdiction": "illinois"}),
            {"action": "upload", "documents": [upload]},
        )

    assert response.status_code == 200
    assert response.json()["success"] is True
    upload_files.assert_called_once()
    assert upload_files.call_args.kwargs["current_step"] == WorkflowStepKey.DOCUMENT_CHECKLIST


@pytest.fixture
def planned_draft(document_draft):
    """A draft whose case is one the Illinois configuration has guidance for."""

    document_draft.court_code = "cook:cd1"
    document_draft.court_name = "Cook County Circuit Court - County Division"
    document_draft.case_category_name = "Miscellaneous"
    document_draft.case_type_name = "Name Change"
    document_draft.save()
    lead = document_draft.documents.get(role=FilingDocument.Role.LEAD)
    lead.filing_type_name = "Petition for Name Change"
    lead.save(update_fields=["filing_type_name", "updated_at"])
    return document_draft


@pytest.mark.django_db
def test_document_checklist_shows_configured_guidance(client, planned_draft):
    response = client.get(reverse("document_checklist", kwargs={"jurisdiction": "illinois"}))

    planned_draft.refresh_from_db()
    page = response.content.decode()
    assert response.status_code == 200
    assert "Your document plan" in page
    assert "This list is a guide" in page
    assert "Commonly included" in page
    assert "Always needed" not in page
    assert "Your case may need more or fewer." in page
    assert "Request for name change" in page
    assert "County Division information sheet" in page
    assert planned_draft.plan.checklist["petition"]["requirement"] == "always"


@pytest.mark.django_db
def test_document_checklist_hides_its_disclaimer_without_configured_guidance(client, document_draft):
    response = client.get(reverse("document_checklist", kwargs={"jurisdiction": "illinois"}))

    document_draft.refresh_from_db()
    page = response.content.decode()
    assert "Your document plan" not in page
    assert "This list is a guide" not in page
    assert "cannot tell you which legal forms your case needs" not in page
    assert document_draft.plan_id is None


@pytest.mark.django_db
def test_document_checklist_saves_gathered_documents(client, planned_draft):
    response = client.post(
        reverse("document_checklist", kwargs={"jurisdiction": "illinois"}),
        {"action": "save_progress", "status_petition": "have", "status_proposed_order": "filed"},
    )

    planned_draft.refresh_from_db()
    checklist = planned_draft.plan.checklist
    assert response.status_code == 302
    assert response.url.partition("?")[0] == reverse("document_checklist", kwargs={"jurisdiction": "illinois"})
    assert checklist["petition"]["status"] == "have"
    assert checklist["proposed_order"]["status"] == "filed"
    assert checklist["publication_notice"]["status"] == ""


@pytest.mark.django_db
def test_document_checklist_saves_gathered_documents_when_continuing(client, planned_draft):
    response = client.post(
        reverse("document_checklist", kwargs={"jurisdiction": "illinois"}),
        {"status_petition": "have"},
    )

    planned_draft.refresh_from_db()
    assert response.status_code == 302
    assert response.url.partition("?")[0] == reverse("organize_documents", kwargs={"jurisdiction": "illinois"})
    assert planned_draft.plan.checklist["petition"]["status"] == "have"


@pytest.mark.django_db
def test_document_checklist_continues_with_every_item_unticked(client, planned_draft):
    response = client.post(reverse("document_checklist", kwargs={"jurisdiction": "illinois"}), {})

    planned_draft.refresh_from_db()
    assert response.status_code == 302
    assert response.url.partition("?")[0] == reverse("organize_documents", kwargs={"jurisdiction": "illinois"})
    # Nothing the filer left alone is treated as missing or an error.
    assert planned_draft.plan.checklist["publication_notice"]["status"] == ""


@pytest.mark.django_db
def test_organize_does_not_require_the_checklist(client, document_draft):
    response = client.get(reverse("organize_documents", kwargs={"jurisdiction": "illinois"}))

    assert response.status_code == 200


@pytest.mark.django_db
def test_organize_redirects_when_court_is_missing(client, document_draft):
    document_draft.court_code = ""
    document_draft.document_checklist_acknowledged = True
    document_draft.save(update_fields=["court_code", "document_checklist_acknowledged", "updated_at"])

    response = client.get(reverse("organize_documents", kwargs={"jurisdiction": "illinois"}))

    assert response.status_code == 302
    assert response.url.partition("?")[0] == reverse("extraction_review", kwargs={"jurisdiction": "illinois"})


@pytest.mark.django_db
def test_organize_returns_to_review_when_edited_from_there(client, document_draft):
    document_draft.document_checklist_acknowledged = True
    document_draft.save(update_fields=["document_checklist_acknowledged", "updated_at"])
    lead = document_draft.documents.get(role=FilingDocument.Role.LEAD)
    details = [
        {
            "id": lead.pk,
            "name": "Petition",
            "filing_type": "petition",
            "filing_type_name": "Petition",
            "document_type": "public",
            "document_type_name": "No (Public)",
            "filing_component": "lead",
            "filing_component_name": "Lead Document",
        },
    ]

    response = client.post(
        reverse("organize_documents", kwargs={"jurisdiction": "illinois"}),
        {"documents": details, "main_document_id": lead.pk, "return_to": "review"},
        content_type="application/json",
    )

    document_draft.refresh_from_db()
    assert response.status_code == 200
    assert response.json()["redirect_url"].partition("?")[0] == reverse(
        "case_review", kwargs={"jurisdiction": "illinois"}
    )
    assert document_draft.current_step == WorkflowStepKey.REVIEW


@pytest.mark.django_db
def test_organize_shows_no_radio_choice_for_a_single_document(client, document_draft):
    document_draft.document_checklist_acknowledged = True
    document_draft.save(update_fields=["document_checklist_acknowledged", "updated_at"])
    lead = document_draft.documents.get(role=FilingDocument.Role.LEAD)

    response = client.get(reverse("organize_documents", kwargs={"jurisdiction": "illinois"}))

    assert response.status_code == 200
    content = response.content.decode()
    assert "main-document-choice" not in content
    assert 'type="radio"' not in content
    assert f'name="main_document" value="{lead.pk}"' in content


@pytest.mark.django_db
def test_organize_shows_radio_choice_for_multiple_documents(client, document_draft):
    document_draft.document_checklist_acknowledged = True
    document_draft.save(update_fields=["document_checklist_acknowledged", "updated_at"])
    reviewed_document(
        draft=document_draft,
        role=FilingDocument.Role.SUPPORTING,
        sort_order=0,
        name="exhibit.pdf",
    )

    response = client.get(reverse("organize_documents", kwargs={"jurisdiction": "illinois"}))

    assert response.status_code == 200
    content = response.content.decode()
    assert "only document in this filing" not in content
    assert content.count('type="radio"') == 2


@pytest.mark.django_db
def test_organize_saves_details_and_supporting_order(client, document_draft):
    first = reviewed_document(
        draft=document_draft,
        role=FilingDocument.Role.SUPPORTING,
        sort_order=0,
        name="first.pdf",
    )
    second = reviewed_document(
        draft=document_draft,
        role=FilingDocument.Role.SUPPORTING,
        sort_order=1,
        name="second.pdf",
    )
    lead = document_draft.documents.get(role=FilingDocument.Role.LEAD)
    document_draft.document_checklist_acknowledged = True
    document_draft.save(update_fields=["document_checklist_acknowledged", "updated_at"])
    details = [
        {
            "id": lead.pk,
            "name": "Petition for name change",
            "filing_type": "petition",
            "filing_type_name": "Petition",
            "document_type": "public",
            "document_type_name": "No (Public)",
            "filing_component": "lead",
            "filing_component_name": "Lead Document",
            "courtesy_copy_email": "filer@example.com",
            "requested_optional_services": ["certified"],
            "requires_amount_in_controversy": True,
        },
        {
            "id": second.pk,
            "name": "Exhibit B",
            "filing_type": "exhibit",
            "filing_type_name": "Exhibit",
            "document_type": "sealed",
            "document_type_name": "Yes (Confidential)",
            "filing_component": "attachment",
            "filing_component_name": "Attachments",
            "courtesy_copy_email": "",
        },
        {
            "id": first.pk,
            "name": "Exhibit A",
            "filing_type": "exhibit",
            "filing_type_name": "Exhibit",
            "document_type": "public",
            "document_type_name": "No (Public)",
            "filing_component": "attachment",
            "filing_component_name": "Attachments",
            "courtesy_copy_email": "",
        },
    ]

    response = client.post(
        reverse("organize_documents", kwargs={"jurisdiction": "illinois"}),
        {"documents": details, "main_document_id": second.pk},
        content_type="application/json",
    )

    document_draft.refresh_from_db()
    lead.refresh_from_db()
    assert response.status_code == 200
    assert response.json()["redirect_url"].partition("?")[0] == reverse(
        "your_information", kwargs={"jurisdiction": "illinois"}
    )
    assert document_draft.current_step == WorkflowStepKey.YOUR_INFORMATION
    assert lead.role == FilingDocument.Role.SUPPORTING
    assert lead.filing_type_code == "petition"
    assert lead.courtesy_copy_email == "filer@example.com"
    assert lead.requested_optional_services == ["certified"]
    assert lead.filing_requires_amount_in_controversy is True
    second.refresh_from_db()
    assert second.role == FilingDocument.Role.LEAD
    assert (document_draft.filing_type_code, document_draft.filing_type_name) == (
        second.filing_type_code,
        second.filing_type_name,
    )
    assert list(
        document_draft.documents.filter(role=FilingDocument.Role.SUPPORTING)
        .order_by("sort_order")
        .values_list("pk", flat=True)
    ) == [lead.pk, first.pk]
    saved = read_upload_data(document_draft)
    assert saved["lead_filing_component"] == "attachment"
    assert saved["supporting_documents"][0]["filing_type"] == "petition"


def _lead_details(document):
    return {
        "id": document.pk,
        "name": "Petition for name change",
        "filing_type": "petition",
        "filing_type_name": "Petition",
        "document_type": "public",
        "document_type_name": "No (Public)",
    }


@pytest.mark.django_db
@patch("efile.views.organize_documents._court_document_types", return_value=[])
def test_organize_accepts_no_confidentiality_when_the_court_offers_no_choices(
    court_document_types, client, document_draft
):
    document_draft.document_checklist_acknowledged = True
    document_draft.save(update_fields=["document_checklist_acknowledged", "updated_at"])
    lead = document_draft.documents.get(role=FilingDocument.Role.LEAD)
    details = _lead_details(lead)
    details["document_type"] = ""
    details["document_type_name"] = ""

    response = client.post(
        reverse("organize_documents", kwargs={"jurisdiction": "illinois"}),
        {"documents": [details], "main_document_id": lead.pk},
        content_type="application/json",
    )

    lead.refresh_from_db()
    assert response.status_code == 200
    assert lead.document_type_code == ""
    assert lead.document_type_name == ""
    court_document_types.assert_called_once_with(document_draft, "petition")


@pytest.mark.django_db
@patch(
    "efile.views.organize_documents._court_document_types",
    return_value=[{"code": "PUBLIC", "name": "Non-confidential"}],
)
def test_organize_rejects_no_confidentiality_when_the_court_offers_choices(
    court_document_types, client, document_draft
):
    document_draft.document_checklist_acknowledged = True
    document_draft.save(update_fields=["document_checklist_acknowledged", "updated_at"])
    lead = document_draft.documents.get(role=FilingDocument.Role.LEAD)
    details = _lead_details(lead)
    details["document_type"] = ""
    details["document_type_name"] = ""

    response = client.post(
        reverse("organize_documents", kwargs={"jurisdiction": "illinois"}),
        {"documents": [details], "main_document_id": lead.pk},
        content_type="application/json",
    )

    lead.refresh_from_db()
    assert response.status_code == 400
    assert response.json() == {
        "success": False,
        "error": "Choose a confidentiality setting for petition.pdf.",
    }
    assert lead.document_type_code == ""
    court_document_types.assert_called_once_with(document_draft, "petition")


@pytest.mark.django_db
def test_organize_rejects_a_post_with_no_document_list(client, document_draft):
    """A missing list is a bad request, not a crash.

    ``documents`` is iterated the moment it reaches the save, so letting a
    non-list through turns a stale or truncated POST into a 500 with no
    message the filer can act on.
    """

    document_draft.document_checklist_acknowledged = True
    document_draft.save(update_fields=["document_checklist_acknowledged", "updated_at"])
    lead = document_draft.documents.get(role=FilingDocument.Role.LEAD)

    response = client.post(
        reverse("organize_documents", kwargs={"jurisdiction": "illinois"}),
        {"main_document_id": lead.pk},
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.json() == {"success": False, "error": "Document details are missing."}


@pytest.mark.django_db
@pytest.mark.parametrize("unset", [None, "", "NaN", "null", "undefined", float("nan"), 0, []])
def test_organize_falls_back_to_the_lead_document_for_any_unset_main_document(client, document_draft, unset):
    """Every shape of "the client did not name one" takes the same path.

    The browser has more than one way to render an empty numeric field, so the
    fallback is keyed on the value being unreadable rather than on a list of
    the sentinels seen so far -- the next unlisted one would otherwise strand
    the filer on an error they cannot clear.
    """

    document_draft.document_checklist_acknowledged = True
    document_draft.save(update_fields=["document_checklist_acknowledged", "updated_at"])
    lead = document_draft.documents.get(role=FilingDocument.Role.LEAD)

    response = client.post(
        reverse("organize_documents", kwargs={"jurisdiction": "illinois"}),
        {"documents": [_lead_details(lead)], "main_document_id": unset},
        content_type="application/json",
    )

    lead.refresh_from_db()
    assert response.status_code == 200
    assert lead.role == FilingDocument.Role.LEAD

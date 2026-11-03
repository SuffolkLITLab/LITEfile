"""Tests for the parts of a plan that ask the filer to do something.

A checklist that only records "I have this" leaves the filer holding a document
the court never sees. These tests describe the path from saying you have it, to
putting it in the envelope, to being told before you file if it is still not
there -- and the home the plan lives at between filings.
"""

import json
from unittest.mock import Mock, patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from efile.models import FilingDocument, FilingDraft, FilingPlan
from efile.services.current_drafts import CURRENT_DRAFT_SESSION_KEY
from efile.services.document_previews import preview_fingerprint
from efile.services.drafts import read_upload_data, write_upload_data
from efile.services.filing_plans import (
    documents_missing_from_envelope,
    ensure_plan_for_draft,
    set_checklist_answers,
    set_checklist_progress,
)
from efile.tests.helpers import reviewed_document
from efile.workflow import ExistingCase, WorkflowStepKey

pytestmark = pytest.mark.usefixtures("enabled_document_checklists")

CHECKLIST_URL = reverse("document_checklist", kwargs={"jurisdiction": "illinois"})
PLANS_URL = reverse("filing_plans", kwargs={"jurisdiction": "illinois"})


def sign_in(client, user, draft=None):
    client.force_login(user)
    session = client.session
    if draft is not None:
        session[CURRENT_DRAFT_SESSION_KEY] = draft.pk
    session["jurisdiction"] = "illinois"
    session["auth_tokens"] = {"TYLER-TOKEN-ILLINOIS": "token"}
    session.save()


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="plan-action-user", tyler_jurisdiction="illinois")


@pytest.fixture
def draft(user):
    """A name change filing, which the shipped Illinois checklist covers."""

    draft = FilingDraft.objects.create(
        user=user,
        jurisdiction="illinois",
        workflow_version=2,
        existing_case=ExistingCase.NEW,
        current_step=WorkflowStepKey.DOCUMENT_CHECKLIST,
        court_code="cook:cd1",
        court_name="Cook County Circuit Court - County Division",
        case_category_code="78332",
        case_category_name="Miscellaneous",
        case_type_code="78346",
        case_type_name="Name Change",
    )
    reviewed_document(
        draft=draft,
        role=FilingDocument.Role.LEAD,
        sort_order=0,
        name="petition.pdf",
        filing_type_code="78690",
        filing_type_name="Petition for Name Change",
        document_type_code="public",
    )
    return draft


@pytest.fixture
def signed_in(client, user, draft):
    sign_in(client, user, draft)
    return draft


def a_supporting_document(draft, name="fee-waiver.pdf"):
    return reviewed_document(
        draft=draft,
        role=FilingDocument.Role.SUPPORTING,
        sort_order=FilingDocument.objects.filter(draft=draft, role=FilingDocument.Role.SUPPORTING).count(),
        name=name,
        filing_type_code="78690",
        document_type_code="public",
    )


def configured_plan(draft):
    plan = ensure_plan_for_draft(draft)
    assert plan is not None
    return plan


# --- Saying you have something, and then actually filing it -----------------


@pytest.mark.django_db
def test_the_main_document_answers_the_lead_item_by_itself(client, signed_in):
    """Nobody should be asked to attach the document they started with."""

    client.get(CHECKLIST_URL)

    lead = FilingDocument.objects.get(draft=signed_in, role=FilingDocument.Role.LEAD)
    signed_in.refresh_from_db()
    assert lead.checklist_item_id == "petition"
    assert signed_in.plan.checklist["petition"]["status"] == "have"


@pytest.mark.django_db
def test_a_document_you_say_you_have_is_not_in_the_filing_yet(client, signed_in):
    client.post(
        CHECKLIST_URL,
        {"action": "save_progress", "status_petition": "have", "status_fee_waiver": "have"},
    )

    signed_in.refresh_from_db()
    missing = documents_missing_from_envelope(signed_in.plan, signed_in)

    assert [item["id"] for item in missing if item["reason"] == "have"] == ["fee_waiver"]
    page = client.get(CHECKLIST_URL).content.decode()
    assert "not in this filing yet" in page
    assert "the clerk may wait for them" in page


@pytest.mark.django_db
def test_a_file_already_added_can_answer_a_checklist_item(client, signed_in):
    document = a_supporting_document(signed_in)

    response = client.post(
        CHECKLIST_URL,
        {"action": "attach_item", "item_id": "fee_waiver", "document_id": document.pk},
    )

    assert response.status_code == 302
    document.refresh_from_db()
    signed_in.refresh_from_db()
    assert document.checklist_item_id == "fee_waiver"
    assert signed_in.plan.checklist["fee_waiver"]["status"] == "have"
    assert "fee_waiver" not in {item["id"] for item in documents_missing_from_envelope(signed_in.plan, signed_in)}


@pytest.mark.django_db
def test_one_document_answers_one_item(client, signed_in):
    """Re-answering an item with a different file releases the first one."""

    first = a_supporting_document(signed_in, "draft-waiver.pdf")
    second = a_supporting_document(signed_in, "signed-waiver.pdf")

    client.post(CHECKLIST_URL, {"action": "attach_item", "item_id": "fee_waiver", "document_id": first.pk})
    client.post(CHECKLIST_URL, {"action": "attach_item", "item_id": "fee_waiver", "document_id": second.pk})

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.checklist_item_id == ""
    assert second.checklist_item_id == "fee_waiver"


@pytest.mark.django_db
def test_a_file_from_another_filing_cannot_be_attached(client, signed_in, user):
    someone_elses_draft = FilingDraft.objects.create(user=user, jurisdiction="illinois", workflow_version=2)
    document = a_supporting_document(someone_elses_draft)

    client.post(CHECKLIST_URL, {"action": "attach_item", "item_id": "fee_waiver", "document_id": document.pk})

    document.refresh_from_db()
    assert document.checklist_item_id == ""


@pytest.mark.django_db
def test_detaching_puts_the_item_back_on_the_list(client, signed_in):
    document = a_supporting_document(signed_in)
    client.post(CHECKLIST_URL, {"action": "attach_item", "item_id": "fee_waiver", "document_id": document.pk})

    client.post(CHECKLIST_URL, {"action": "detach_item", "item_id": "fee_waiver"})

    document.refresh_from_db()
    signed_in.refresh_from_db()
    assert document.checklist_item_id == ""
    missing = documents_missing_from_envelope(signed_in.plan, signed_in)
    assert [item["id"] for item in missing if item["reason"] == "have"] == ["fee_waiver"]


@pytest.mark.django_db
def test_the_main_document_guess_stays_rejected(client, signed_in):
    """ "Not this file" about the main document has to survive a page load."""

    client.get(CHECKLIST_URL)

    client.post(CHECKLIST_URL, {"action": "detach_item", "item_id": "petition"})
    client.get(CHECKLIST_URL)

    lead = FilingDocument.objects.get(draft=signed_in, role=FilingDocument.Role.LEAD)
    assert lead.checklist_item_id == ""


@pytest.mark.django_db
def test_adding_another_file_does_not_forget_what_is_already_attached(client, signed_in):
    """Uploads rebuild the supporting document rows; the answers survive it."""

    document = a_supporting_document(signed_in)
    document.s3_key = "uploads/fee-waiver.pdf"
    document.public_url = "https://example.test/fee-waiver.pdf"
    document.save()
    client.post(CHECKLIST_URL, {"action": "attach_item", "item_id": "fee_waiver", "document_id": document.pk})

    upload_data = read_upload_data(signed_in)
    upload_data["files"]["supporting"].append({"name": "exhibit.pdf", "s3_key": "uploads/exhibit.pdf"})
    write_upload_data(signed_in, upload_data)

    assert FilingDocument.objects.get(draft=signed_in, s3_key="uploads/fee-waiver.pdf").checklist_item_id == (
        "fee_waiver"
    )
    assert FilingDocument.objects.get(draft=signed_in, s3_key="uploads/exhibit.pdf").checklist_item_id == ""


@pytest.mark.django_db
def test_a_document_in_the_envelope_cannot_be_un_had(client, signed_in):
    """Unticking a box does not make an attached document disappear."""

    document = a_supporting_document(signed_in)
    client.post(CHECKLIST_URL, {"action": "attach_item", "item_id": "fee_waiver", "document_id": document.pk})

    client.post(CHECKLIST_URL, {"action": "save_progress", "status_fee_waiver": ""})

    signed_in.refresh_from_db()
    assert signed_in.plan.checklist["fee_waiver"]["status"] == "have"


@pytest.mark.django_db
def test_always_needed_documents_are_missed_even_unticked(signed_in):
    plan = configured_plan(signed_in)

    always_needed = {item_id for item_id, item in plan.checklist.items() if item.get("requirement") == "always"} - {
        "petition"
    }
    missing = {item["id"]: item["reason"] for item in documents_missing_from_envelope(plan, signed_in)}

    assert always_needed
    assert {item_id: "always" for item_id in always_needed}.items() <= missing.items()


@pytest.mark.django_db
def test_adding_a_document_from_the_checklist_fills_in_its_filing_type(client, signed_in):
    document = a_supporting_document(signed_in)
    document.filing_type_code = ""
    document.filing_type_name = ""
    document.save()

    with patch(
        "efile.services.filing_plans.requests.get",
        return_value=Mock(
            raise_for_status=Mock(),
            json=Mock(return_value=[{"code": "6529", "name": "Application for Waiver of Court Fees"}]),
        ),
    ):
        client.post(
            CHECKLIST_URL,
            {"action": "attach_item", "item_id": "fee_waiver", "document_id": document.pk},
        )

    document.refresh_from_db()
    assert document.filing_type_code == "6529"
    assert document.filing_type_name == "Application for Waiver of Court Fees"


@pytest.mark.django_db
def test_a_filing_type_the_filer_chose_is_never_overwritten(client, signed_in):
    document = a_supporting_document(signed_in)
    document.filing_type_code = "6714"
    document.filing_type_name = "Motion"
    document.save()

    with patch(
        "efile.services.filing_plans.requests.get",
        return_value=Mock(
            raise_for_status=Mock(),
            json=Mock(return_value=[{"code": "6529", "name": "Application for Waiver of Court Fees"}]),
        ),
    ):
        client.post(
            CHECKLIST_URL,
            {"action": "attach_item", "item_id": "fee_waiver", "document_id": document.pk},
        )

    document.refresh_from_db()
    assert document.filing_type_name == "Motion"


@pytest.mark.django_db
def test_a_court_lookup_that_fails_still_attaches_the_document(client, signed_in):
    document = a_supporting_document(signed_in)
    document.filing_type_code = ""
    document.filing_type_name = ""
    document.save()

    with patch("efile.services.filing_plans.requests.get", side_effect=OSError("boom")):
        client.post(
            CHECKLIST_URL,
            {"action": "attach_item", "item_id": "fee_waiver", "document_id": document.pk},
        )

    document.refresh_from_db()
    assert document.checklist_item_id == "fee_waiver"
    assert document.filing_type_code == ""


@pytest.mark.django_db
def test_the_checklist_offers_every_answer(client, signed_in):
    page = client.get(CHECKLIST_URL).content.decode()

    assert "I have it now" in page
    assert "I already filed this" in page
    assert "I will file it later" in page
    assert 'name="due_publication_notice"' in page


@pytest.mark.django_db
def test_answering_on_the_checklist_is_saved(client, signed_in):
    client.post(
        CHECKLIST_URL,
        {
            "action": "save_progress",
            "status_proposed_order": "filed",
            "status_publication_notice": "later",
            "due_publication_notice": "2026-09-30",
        },
    )

    signed_in.refresh_from_db()
    checklist = signed_in.plan.checklist
    assert checklist["proposed_order"]["status"] == "filed"
    assert checklist["publication_notice"]["status"] == "later"
    assert checklist["publication_notice"]["due_date"] == "2026-09-30"


@pytest.mark.django_db
def test_the_checklist_step_explains_the_list_too(client, signed_in):
    page = client.get(CHECKLIST_URL).content.decode()

    assert "About this list" in page
    assert "a guide, not legal advice" in page


# --- The warning before filing ----------------------------------------------


@pytest.mark.django_db
def test_review_names_what_the_plan_expects_and_the_envelope_lacks(client, signed_in):
    plan = configured_plan(signed_in)
    set_checklist_progress(plan, ["fee_waiver"])
    signed_in.selected_payment_account_id = "pay-1"
    signed_in.current_step = WorkflowStepKey.REVIEW
    signed_in.save()

    page = client.get(reverse("case_review", kwargs={"jurisdiction": "illinois"})).content.decode()

    assert "Other filers like you often include these documents" in page
    assert "not advice from the court about what you must file" in page
    assert "continue with this filing" in page
    assert "Request to waive court fees" in page
    assert f"{CHECKLIST_URL}?return_to=review" in page


@pytest.mark.django_db
def test_a_document_already_filed_is_not_a_gap(signed_in):
    plan = configured_plan(signed_in)
    set_checklist_answers(plan, {"fee_waiver": {"status": "filed"}})

    missing = {entry["id"] for entry in documents_missing_from_envelope(plan, signed_in)}
    assert "fee_waiver" not in missing


@pytest.mark.django_db
def test_a_document_left_until_later_is_not_a_gap(signed_in):
    plan = configured_plan(signed_in)
    set_checklist_answers(plan, {"fee_waiver": {"status": "later", "due_date": "2026-10-01"}})

    missing = {entry["id"] for entry in documents_missing_from_envelope(plan, signed_in)}
    assert "fee_waiver" not in missing


@pytest.mark.django_db
def test_a_document_in_hand_but_not_attached_is_still_a_gap(signed_in):
    plan = configured_plan(signed_in)
    set_checklist_answers(plan, {"fee_waiver": {"status": "have"}})

    missing = {entry["id"]: entry["reason"] for entry in documents_missing_from_envelope(plan, signed_in)}
    assert missing["fee_waiver"] == "have"


@pytest.mark.django_db
def test_adding_a_document_on_the_way_back_from_review_goes_through_organizing(client, signed_in):
    """A file with no filing type cannot go to the court, so it is organized first."""

    a_supporting_document(signed_in).__class__.objects.filter(draft=signed_in, role="supporting").update(
        filing_type_code=""
    )

    response = client.post(
        f"{CHECKLIST_URL}?return_to=review",
        {"documents_complete": "yes", "return_to": "review", "status_petition": "have"},
    )

    assert (
        response.url
        == reverse("organize_documents", kwargs={"jurisdiction": "illinois"})
        + f"?return_to=review&draft={signed_in.pk}"
    )


@pytest.mark.django_db
def test_a_complete_filing_returns_straight_to_review(client, signed_in):
    response = client.post(
        f"{CHECKLIST_URL}?return_to=review",
        {"documents_complete": "yes", "return_to": "review", "status_petition": "have"},
    )

    assert response.url.partition("?")[0] == reverse("case_review", kwargs={"jurisdiction": "illinois"})


@pytest.mark.django_db
def test_checklist_upload_with_an_automatic_filing_type_requires_confidentiality(client, signed_in):
    def upload(draft, *_args, **_kwargs):
        FilingDocument.objects.create(
            draft=draft,
            role="supporting",
            name="waiver.pdf",
            preparation="unchanged",
        )

    with (
        patch("efile.views.document_checklist.upload_files", side_effect=upload),
        patch("efile.views.document_checklist.filing_type_for_item", return_value=("6529", "Fee waiver")),
    ):
        attached = client.post(
            f"{CHECKLIST_URL}?return_to=review",
            {
                "action": "attach_item",
                "item_id": "fee_waiver",
                "document": SimpleUploadedFile("waiver.pdf", b"uploaded copy"),
            },
        )
    assert "#document-checks" in attached.url
    document = signed_in.documents.get(role="supporting")
    assert document.filing_type_code == "6529"
    assert document.document_type_code == ""
    assert not document.document_type_confirmed
    checked = client.post(
        reverse("document_checks", kwargs={"jurisdiction": "illinois"}) + f"?draft={signed_in.pk}",
        {"action": "confirm", "document_id": document.pk, "preview_fingerprint": preview_fingerprint([document])},
    )
    assert checked.status_code == 200
    continued = client.post(f"{CHECKLIST_URL}?return_to=review", {"return_to": "review"})
    assert continued.url.partition("?")[0] == reverse("organize_documents", kwargs={"jurisdiction": "illinois"})
    assert "return_to=review" in continued.url

    lead = signed_in.documents.get(role="lead")
    details = {
        "main_document_id": lead.pk,
        "return_to": "review",
        "documents": [
            {"id": lead.pk, "filing_type": lead.filing_type_code, "document_type": "public"},
            {"id": document.pk, "filing_type": document.filing_type_code, "document_type": ""},
        ],
    }
    organize = reverse("organize_documents", kwargs={"jurisdiction": "illinois"}) + f"?draft={signed_in.pk}"
    with patch("efile.views.organize_documents._court_document_types", return_value=[{"code": "sealed"}]):
        refused = client.post(organize, json.dumps(details), content_type="application/json")
    assert refused.status_code == 400
    assert "Choose a confidentiality setting" in refused.json()["error"]
    document.refresh_from_db()
    assert not document.document_type_confirmed

    details["documents"][1].update(document_type="sealed", filing_component="supporting")
    with patch("efile.views.organize_documents.draft_unavailable_message", return_value=""):
        organized = client.post(organize, json.dumps(details), content_type="application/json")
    assert organized.status_code == 200
    assert organized.json()["redirect_url"].partition("?")[0] == reverse(
        "case_review", kwargs={"jurisdiction": "illinois"}
    )
    document.refresh_from_db()
    assert document.document_type_code == "sealed"
    assert document.filing_component_code == "supporting"
    assert document.document_type_confirmed


# --- The plan's own home -----------------------------------------------------


@pytest.mark.django_db
def test_my_plans_lists_the_matters_i_am_working_on(client, signed_in):
    plan = configured_plan(signed_in)

    page = client.get(PLANS_URL).content.decode()

    assert plan.title in page
    assert "Request to waive court fees" in page


@pytest.mark.django_db
def test_the_plans_page_explains_the_list_and_links_onward(client, signed_in):
    plan = configured_plan(signed_in)

    page = client.get(PLANS_URL).content.decode()

    assert "About this list" in page
    assert plan.guidance["learn_more_url"] in page
    assert "a guide, not legal advice" in page


@pytest.mark.django_db
def test_i_can_work_on_my_plan_between_filings(client, signed_in):
    plan = configured_plan(signed_in)

    client.post(
        PLANS_URL,
        {
            "action": "save_progress",
            "plan_id": plan.pk,
            "status_publication_notice": "later",
            "due_publication_notice": "2026-09-30",
        },
    )

    plan.refresh_from_db()
    assert plan.checklist["publication_notice"]["status"] == "later"
    assert plan.checklist["publication_notice"]["due_date"] == "2026-09-30"


@pytest.mark.django_db
def test_i_can_rename_a_plan_to_something_i_recognize(client, signed_in):
    plan = configured_plan(signed_in)

    client.post(PLANS_URL, {"action": "rename", "plan_id": plan.pk, "title": "My child's name change"})

    plan.refresh_from_db()
    assert plan.title == "My child's name change"


@pytest.mark.django_db
def test_i_can_delete_a_plan_i_am_done_with(client, signed_in):
    """The list is the filer's own, so clearing something off it is theirs to do."""

    plan = configured_plan(signed_in)
    draft = signed_in
    draft.plan = plan
    draft.save(update_fields=["plan"])

    page = client.get(PLANS_URL).content.decode()
    assert "Delete this plan" in page

    client.post(PLANS_URL, {"action": "delete", "plan_id": plan.pk})

    assert not FilingPlan.objects.filter(pk=plan.pk).exists()
    # The filing made under it is not collateral damage.
    draft.refresh_from_db()
    assert draft.plan_id is None
    assert FilingDraft.objects.filter(pk=draft.pk).exists()


@pytest.mark.django_db
def test_i_cannot_touch_someone_elses_plan(client, signed_in, django_user_model):
    plan = configured_plan(signed_in)
    intruder = django_user_model.objects.create_user(username="intruder", tyler_jurisdiction="illinois")
    sign_in(client, intruder)

    response = client.post(PLANS_URL, {"action": "rename", "plan_id": plan.pk, "title": "Mine now"})

    assert response.status_code == 404
    plan.refresh_from_db()
    assert plan.title == "Name Change"


@pytest.mark.django_db
def test_the_options_page_offers_a_plan_i_already_started(client, signed_in):
    plan = configured_plan(signed_in)

    page = client.get(reverse("efile_options", kwargs={"jurisdiction": "illinois"})).content.decode()

    assert "My filing plans" in page
    assert plan.title in page
    assert reverse("start_filing_from_plan", kwargs={"jurisdiction": "illinois", "plan_id": plan.pk}) in page
    assert '<details class="option-card option-disclosure">' in page
    assert '<span class="option-count" aria-hidden="true">1</span>' in page
    assert '<details class="option-card option-disclosure" open>' not in page


# --- Linking a plan to a court case -----------------------------------------


@pytest.mark.django_db
def test_linking_a_plan_to_one_of_my_court_cases(client, signed_in, monkeypatch):
    plan = configured_plan(signed_in)

    monkeypatch.setattr(
        "efile.views.filing_plans.accepted_case_for_user",
        lambda *_args, **_kwargs: {
            "filing_status": "accepted",
            "case_tracking_id": "tracking-123",
            "case_number": "2025MR000123",
            "case_title": "In re Ada Lovelace",
            "court_code": "cook:cd1",
        },
    )
    client.post(
        PLANS_URL,
        {
            "action": "link_case",
            "plan_id": plan.pk,
            "case_tracking_id": "tracking-123",
            "docket_number": "not-trusted",
            "case_title": "Not trusted",
            "court_code": "not-trusted",
            "court_name": "Cook County Circuit Court - County Division",
        },
    )

    plan.refresh_from_db()
    assert plan.is_linked_to_a_case
    assert plan.docket_number == "2025MR000123"
    assert plan.case_title == "In re Ada Lovelace"
    assert plan.court_code == "cook:cd1"


@pytest.mark.django_db
def test_a_plan_cannot_link_a_case_the_account_did_not_file_into(client, signed_in, monkeypatch):
    plan = configured_plan(signed_in)
    monkeypatch.setattr("efile.views.filing_plans.accepted_case_for_user", lambda *_args, **_kwargs: None)

    client.post(
        PLANS_URL,
        {
            "action": "link_case",
            "plan_id": plan.pk,
            "case_tracking_id": "someone-elses-case",
            "docket_number": "2025MR999999",
            "court_code": "cook:cd1",
        },
    )

    plan.refresh_from_db()
    assert not plan.is_linked_to_a_case


@pytest.mark.django_db
def test_a_plan_without_a_case_stays_unlinked(client, signed_in):
    plan = configured_plan(signed_in)

    client.post(PLANS_URL, {"action": "link_case", "plan_id": plan.pk, "case_tracking_id": "tracking-123"})

    plan.refresh_from_db()
    assert not plan.is_linked_to_a_case


@pytest.mark.django_db
def test_a_filing_started_from_a_linked_plan_goes_into_that_case(client, signed_in, monkeypatch):
    plan = configured_plan(signed_in)
    plan.case_tracking_id = "tracking-123"
    plan.docket_number = "2025MR000123"
    plan.case_title = "In re Ada Lovelace"
    plan.save()
    monkeypatch.setattr(
        "efile.services.filing_plans.resolve_plan_case_codes",
        lambda plan: dict.fromkeys(("case_category_code", "case_type_code", "lead_filing_type_code"), ""),
    )

    client.post(reverse("start_filing_from_plan", kwargs={"jurisdiction": "illinois", "plan_id": plan.pk}))

    started = FilingDraft.objects.filter(plan=plan).exclude(pk=signed_in.pk).get()
    assert started.existing_case == ExistingCase.EXISTING
    assert started.previous_case_id == "tracking-123"
    assert started.docket_number == "2025MR000123"


@pytest.mark.django_db
def test_a_case_the_plan_knows_is_not_searched_for_again(client, signed_in):
    signed_in.existing_case = ExistingCase.EXISTING
    signed_in.previous_case_id = "tracking-123"
    signed_in.docket_number = "2025MR000123"
    signed_in.save()

    response = client.get(reverse("case_lookup", kwargs={"jurisdiction": "illinois"}))

    assert response.status_code == 302
    assert response.url.partition("?")[0] == reverse("case_confirmation", kwargs={"jurisdiction": "illinois"})


@pytest.mark.django_db
def test_confirming_a_case_teaches_the_plan_where_to_file_next(client, signed_in, monkeypatch):
    plan = configured_plan(signed_in)
    signed_in.existing_case = ExistingCase.EXISTING
    signed_in.previous_case_id = "tracking-123"
    signed_in.docket_number = "2025MR000123"
    signed_in.case_title = "In re Ada Lovelace"
    signed_in.save()

    from efile.tests.helpers import loaded_case_snapshot

    monkeypatch.setattr(
        "efile.services.existing_cases.fetch_case", lambda draft, token: loaded_case_snapshot(draft, confirmed=False)
    )

    client.post(reverse("case_confirmation", kwargs={"jurisdiction": "illinois"}), {"confirmed": "yes"})

    plan.refresh_from_db()
    assert plan.case_tracking_id == "tracking-123"
    assert plan.docket_number == "2025MR000123"


@pytest.mark.django_db
def test_saying_a_case_is_not_mine_unlinks_the_plan(client, signed_in, monkeypatch):
    plan = configured_plan(signed_in)
    plan.case_tracking_id = "tracking-123"
    plan.docket_number = "2025MR000123"
    plan.save()
    signed_in.existing_case = ExistingCase.EXISTING
    signed_in.previous_case_id = "tracking-123"
    signed_in.docket_number = "2025MR000123"
    signed_in.save()

    client.post(reverse("case_confirmation", kwargs={"jurisdiction": "illinois"}), {"confirmed": "no"})

    plan.refresh_from_db()
    assert not plan.is_linked_to_a_case
    assert FilingPlan.objects.filter(pk=plan.pk).exists()

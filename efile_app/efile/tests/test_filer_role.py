"""Tests for asking which side of the case the filer is on.

In a two-sided case one case type means two different jobs, so the documents
follow the side rather than the case. These tests cover where the question is
asked, what it changes, and what happens when the answer changes.
"""

import pytest
from django.urls import reverse

from efile.models import FilingDocument, FilingDraft
from efile.services.current_drafts import CURRENT_DRAFT_SESSION_KEY
from efile.services.filing_plans import create_draft_from_plan, ensure_plan_for_draft, set_checklist_progress
from efile.services.people import guess_filer_party_type
from efile.tests.helpers import reviewed_document
from efile.workflow import ExistingCase, WorkflowStepKey

CHECKLIST_URL = reverse("document_checklist", kwargs={"jurisdiction": "illinois"})
EXTRACTION_URL = reverse("extraction_review", kwargs={"jurisdiction": "illinois"})
ROLES_URL = reverse("api:filer_roles")

# The Cook County Municipal Civil Division case type the shipped Illinois
# configuration gives two sides.
EVICTION_CASE = {
    "court_code": "cook:cvd1",
    "court_name": "Cook County - Municipal Civil - District 1",
    "case_category_code": "80001",
    "case_category_name": "Eviction",
    "case_type_code": "80002",
    "case_type_name": "Eviction - Possession - Residential Complaint Filed - Non-Jury",
}


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="side-user", tyler_jurisdiction="illinois")


@pytest.fixture
def draft(user):
    draft = FilingDraft.objects.create(
        user=user,
        jurisdiction="illinois",
        workflow_version=2,
        existing_case=ExistingCase.NEW,
        current_step=WorkflowStepKey.DOCUMENT_CHECKLIST,
        **EVICTION_CASE,
    )
    reviewed_document(
        draft=draft,
        role=FilingDocument.Role.LEAD,
        sort_order=0,
        name="complaint.pdf",
        filing_type_code="90001",
        filing_type_name="Complaint / Petition - Eviction - Residential - Possession Only - Fee",
        document_type_code="public",
    )
    return draft


@pytest.fixture
def signed_in(client, user, draft):
    client.force_login(user)
    session = client.session
    session[CURRENT_DRAFT_SESSION_KEY] = draft.pk
    session["jurisdiction"] = "illinois"
    session["auth_tokens"] = {"TYLER-TOKEN-ILLINOIS": "token"}
    session.save()
    return draft


# --- Asking on the screen that settles what the case is ----------------------


@pytest.mark.django_db
def test_the_sides_are_offered_while_the_case_type_is_being_chosen(client, signed_in):
    """The confirm-filing screen asks, so it must be able to ask before saving."""

    response = client.get(
        ROLES_URL,
        {
            "jurisdiction": "illinois",
            "court": EVICTION_CASE["court_code"],
            "case_category_name": EVICTION_CASE["case_category_name"],
            "case_type_name": EVICTION_CASE["case_type_name"],
            "filing_type_name": "Appearance Filed - Eviction - Possession Only",
        },
    )

    roles = response.json()["data"]
    assert [role["id"] for role in roles] == ["landlord", "tenant"]
    assert [role["id"] for role in roles if role["suggested"]] == ["tenant"]


@pytest.mark.django_db
def test_a_case_without_sides_offers_nothing_to_ask(client, signed_in):
    response = client.get(
        ROLES_URL,
        {"jurisdiction": "illinois", "court": "cook:cd1", "case_type_name": "Name Change"},
    )

    assert response.json()["data"] == []


def confirm_filing(client, **overrides):
    data = {
        "existing_case": ExistingCase.NEW,
        "court_code": EVICTION_CASE["court_code"],
        "court_name": EVICTION_CASE["court_name"],
        "case_category_code": EVICTION_CASE["case_category_code"],
        "case_category_name": EVICTION_CASE["case_category_name"],
        "case_type_code": EVICTION_CASE["case_type_code"],
        "case_type_name": EVICTION_CASE["case_type_name"],
        "filing_type_code": "90001",
        "filing_type_name": "Complaint / Petition - Eviction - Residential - Possession Only - Fee",
    }
    data.update(overrides)
    return client.post(EXTRACTION_URL, data)


def configured_plan(draft):
    plan = ensure_plan_for_draft(draft)
    assert plan is not None
    return plan


def guessed_party_type(draft, party_types):
    party_type = guess_filer_party_type(draft, party_types)
    assert party_type is not None
    return party_type


@pytest.mark.django_db
def test_confirming_the_filing_records_which_side_you_are_on(client, signed_in):
    response = confirm_filing(client, filer_role="landlord")

    signed_in.refresh_from_db()
    assert response.status_code == 302
    assert signed_in.filer_role == "landlord"


@pytest.mark.django_db
def test_a_two_sided_case_cannot_be_confirmed_without_a_side(client, signed_in):
    response = confirm_filing(client)

    signed_in.refresh_from_db()
    assert response.status_code == 200
    assert signed_in.filer_role == ""
    assert b"which side of this case you are on" in response.content.lower()


@pytest.mark.django_db
def test_a_side_from_a_different_case_is_not_accepted(client, signed_in):
    response = confirm_filing(client, filer_role="squatter")

    signed_in.refresh_from_db()
    assert response.status_code == 200
    assert signed_in.filer_role == ""


@pytest.mark.django_db
def test_a_case_without_sides_is_confirmed_without_being_asked(client, signed_in):
    response = confirm_filing(
        client,
        court_code="cook:cd1",
        case_category_name="Miscellaneous",
        case_type_name="Name Change",
        filing_type_name="Petition for Name Change",
    )

    signed_in.refresh_from_db()
    assert response.status_code == 302
    assert signed_in.filer_role == ""


# --- What the answer changes -------------------------------------------------


@pytest.mark.django_db
def test_each_side_gets_its_own_list_in_its_own_words(client, signed_in):
    signed_in.filer_role = "landlord"
    signed_in.save()
    landlord_page = client.get(CHECKLIST_URL).content.decode()

    # The same filer, now telling us they are on the other side of the case.
    FilingDraft.objects.filter(pk=signed_in.pk).update(filer_role="tenant")
    tenant_page = client.get(CHECKLIST_URL).content.decode()

    assert "Eviction complaint" in landlord_page
    assert "The notice you gave the tenant" in landlord_page
    assert "Answer to the complaint" not in landlord_page

    assert "Answer to the complaint" in tenant_page
    assert "The written notice your landlord gave you" in tenant_page
    assert "Eviction complaint" not in tenant_page


@pytest.mark.django_db
def test_the_checklist_says_whose_list_it_is(client, signed_in):
    signed_in.filer_role = "tenant"
    signed_in.save()

    page = client.get(CHECKLIST_URL).content.decode()

    assert "This list is for" in page
    assert "The tenant" in page
    assert EXTRACTION_URL in page


@pytest.mark.django_db
def test_a_side_is_still_askable_when_the_case_was_found_by_lookup(client, signed_in):
    """An existing case has no case type until after the lookup, so the
    checklist screen has to be able to ask too."""

    page = client.get(CHECKLIST_URL).content.decode()
    assert "Which side of this case are you on?" in page

    answered = client.post(CHECKLIST_URL, {"action": "set_filer_role", "filer_role": "tenant"}, follow=True)

    signed_in.refresh_from_db()
    assert signed_in.filer_role == "tenant"
    assert signed_in.plan is not None
    assert "Answer to the complaint" in answered.content.decode()


@pytest.mark.django_db
def test_no_plan_is_made_before_the_side_is_known(client, signed_in):
    """A half list is worse than none: the rest belongs to the other party."""

    client.get(CHECKLIST_URL)

    signed_in.refresh_from_db()
    assert signed_in.plan is None


# --- The plan follows the side -----------------------------------------------


@pytest.mark.django_db
def test_the_plan_remembers_which_side_the_matter_is_filed_from(signed_in):
    signed_in.filer_role = "tenant"
    signed_in.save()

    plan = configured_plan(signed_in)

    assert plan.filer_role == "tenant"
    assert "answer" in plan.checklist


@pytest.mark.django_db
def test_another_filing_in_the_matter_is_not_asked_again(signed_in):
    signed_in.filer_role = "tenant"
    signed_in.save()
    plan = configured_plan(signed_in)

    second = create_draft_from_plan(signed_in.user, plan)

    assert second.filer_role == "tenant"


@pytest.mark.django_db
def test_correcting_your_side_rebuilds_the_list_and_keeps_your_progress(client, signed_in):
    signed_in.filer_role = "tenant"
    signed_in.save()
    plan = configured_plan(signed_in)
    set_checklist_progress(plan, ["answer", "fee_waiver"])
    # A matter with a second filing behind it, so the plan is settled.
    create_draft_from_plan(signed_in.user, plan)

    signed_in.filer_role = "landlord"
    signed_in.save()
    corrected = configured_plan(signed_in)

    assert corrected.pk == plan.pk
    assert corrected.filer_role == "landlord"
    assert "answer" not in corrected.checklist
    assert "complaint" in corrected.checklist
    # Progress on an item both sides file is the filer's, and stays.
    assert corrected.checklist["fee_waiver"]["status"] == "have"


# --- The one other place the answer is worth reusing -------------------------


@pytest.mark.django_db
def test_your_side_suggests_your_party_type(signed_in):
    party_types = [
        {"code": "PL", "name": "Plaintiff/Petitioner", "required": True},
        {"code": "DE", "name": "Defendant/Respondent", "required": True},
    ]

    signed_in.filer_role = "tenant"
    assert guessed_party_type(signed_in, party_types)["code"] == "DE"

    signed_in.filer_role = "landlord"
    assert guessed_party_type(signed_in, party_types)["code"] == "PL"


@pytest.mark.django_db
def test_without_a_side_the_party_type_is_still_guessed_from_posture(signed_in):
    party_types = [
        {"code": "PL", "name": "Plaintiff/Petitioner", "required": True},
        {"code": "DE", "name": "Defendant/Respondent", "required": True},
    ]

    assert guessed_party_type(signed_in, party_types)["code"] == "PL"


@pytest.mark.django_db
def test_no_detected_side_defers_to_the_document_checklist(client, signed_in):
    response = confirm_filing(client, filing_type_name="Other filing")

    signed_in.refresh_from_db()
    assert response.status_code == 302
    assert response.url.partition("?")[0] == CHECKLIST_URL
    assert signed_in.filer_role == ""
    page = client.get(response.url)
    assert page.context["choosing_filer_role"] is True


@pytest.mark.django_db
def test_deferred_side_cannot_accept_an_unoffered_role(client, signed_in):
    response = confirm_filing(client, filing_type_name="Other filing", filer_role="invalid")
    assert response.status_code == 200
    signed_in.refresh_from_db()
    assert signed_in.filer_role == ""


@pytest.mark.django_db
def test_a_saved_side_still_on_offer_must_be_confirmed(client, signed_in):
    signed_in.filer_role = "landlord"
    signed_in.save()

    response = confirm_filing(client, filing_type_name="Other filing")

    assert response.status_code == 200
    assert "Choose which side of this case you are on" in response.content.decode()
    signed_in.refresh_from_db()
    assert signed_in.filer_role == "landlord"


@pytest.mark.django_db
def test_a_side_the_new_case_type_does_not_offer_is_cleared(client, signed_in):
    signed_in.filer_role = "petitioner"
    signed_in.save()

    response = confirm_filing(client, filing_type_name="Other filing")

    assert response.status_code == 302
    signed_in.refresh_from_db()
    assert signed_in.filer_role == ""
    assert client.get(response.url).context["choosing_filer_role"] is True


@pytest.mark.django_db
def test_a_review_detour_with_an_unanswered_side_goes_through_the_checklist(client, signed_in):
    response = confirm_filing(client, filing_type_name="Other filing", return_to="review")

    assert response.status_code == 302
    path, _, query = response.url.partition("?")
    assert path == CHECKLIST_URL
    assert "return_to=review" in query
    signed_in.refresh_from_db()
    assert signed_in.current_step == WorkflowStepKey.DOCUMENT_CHECKLIST

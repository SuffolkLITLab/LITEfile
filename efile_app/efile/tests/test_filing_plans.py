from datetime import date
from unittest.mock import Mock, patch

import pytest
from django.urls import reverse

from efile.models import FilingDocument, FilingDraft, FilingPlan
from efile.services.filing_plans import (
    checklist_items,
    create_draft_from_plan,
    ensure_plan_for_draft,
    filing_type_for_item,
    grouped_checklist,
    plan_progress,
    resolve_plan_case_codes,
    set_checklist_answers,
    set_checklist_progress,
)
from efile.tests.helpers import reviewed_document
from efile.workflow import ExistingCase, WorkflowStepKey

pytestmark = pytest.mark.usefixtures("enabled_document_checklists")


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="plan-user", tyler_jurisdiction="illinois")


def make_draft(user, **overrides):
    fields = {
        "jurisdiction": "illinois",
        "workflow_version": 2,
        "existing_case": ExistingCase.NEW,
        "court_code": "cook:cd1",
        "court_name": "Cook County Circuit Court - County Division",
        "case_category_code": "78332",
        "case_category_name": "Miscellaneous",
        "case_type_code": "78346",
        "case_type_name": "Name Change",
        "current_step": WorkflowStepKey.DOCUMENT_CHECKLIST,
    }
    fields.update(overrides)
    draft = FilingDraft.objects.create(user=user, **fields)
    reviewed_document(
        draft=draft,
        role=FilingDocument.Role.LEAD,
        sort_order=0,
        name="petition.pdf",
        filing_type_code="78690",
        filing_type_name="Petition for Name Change",
    )
    return draft


def make_plan(draft) -> FilingPlan:
    """Plan a draft the Illinois configuration covers, so the plan is never None."""

    plan = ensure_plan_for_draft(draft)
    assert plan is not None
    return plan


@pytest.mark.django_db
def test_plan_snapshots_the_configured_checklist(user):
    draft = make_draft(user)

    plan = make_plan(draft)

    draft.refresh_from_db()
    assert draft.plan == plan
    assert plan.title == "Name Change"
    assert plan.case_type_name == "Name Change"
    assert plan.lead_filing_type_name == "Petition for Name Change"
    assert plan.checklist["petition"]["requirement"] == "always"
    assert plan.checklist["petition"]["status"] == ""


@pytest.mark.django_db
def test_plan_keeps_no_court_codes(user):
    """A plan that stored a code would file against a stale one months later."""

    plan = make_plan(make_draft(user))

    saved = {field.name for field in FilingPlan._meta.get_fields()}
    assert not {name for name in saved if name.endswith("_code")} - {"court_code"}
    for item in plan.checklist.values():
        assert "code" not in item


@pytest.mark.django_db
def test_no_plan_for_a_case_type_no_partner_configured(user):
    draft = make_draft(user, case_category_name="Tax", case_type_name="Action in Debt")

    assert ensure_plan_for_draft(draft) is None
    draft.refresh_from_db()
    assert draft.plan is None
    assert FilingPlan.objects.count() == 0


@pytest.mark.django_db
def test_plan_is_created_once_per_draft(user):
    draft = make_draft(user)

    first = make_plan(draft)
    second = make_plan(draft)

    assert first.pk == second.pk
    assert FilingPlan.objects.count() == 1


@pytest.mark.django_db
def test_plan_follows_the_draft_when_the_filer_picks_a_different_case(user):
    draft = make_draft(user)
    plan = make_plan(draft)
    set_checklist_progress(plan, ["petition", "fee_waiver"])

    draft.court_code = "cook:dr1"
    draft.court_name = "Cook County Circuit Court - Domestic Relations Division"
    draft.case_category_name = "Domestic Relations - General Proceedings"
    draft.case_type_name = "Petition for Dissolution of Marriage - No Children"
    draft.save()
    refreshed = make_plan(draft)

    assert refreshed.pk == plan.pk
    assert refreshed.title == "Petition for Dissolution of Marriage - No Children"
    assert "publication_notice" not in refreshed.checklist
    assert refreshed.checklist["domestic_relations_cover_sheet"]["requirement"] == "always"
    # Progress on an item the new case still asks for is the filer's, and stays.
    assert refreshed.checklist["fee_waiver"]["status"] == "have"


@pytest.mark.django_db
def test_a_draft_that_becomes_an_unconfigured_case_loses_the_guidance(user):
    draft = make_draft(user)
    make_plan(draft)

    draft.case_category_name = "Tax"
    draft.case_type_name = "Action in Debt"
    draft.save()

    assert ensure_plan_for_draft(draft) is None
    draft.refresh_from_db()
    assert draft.plan is None


@pytest.mark.django_db
def test_a_matter_with_other_filings_keeps_its_own_case(user):
    first = make_draft(user)
    plan = make_plan(first)
    second = make_draft(user, case_type_name="Change of Name")
    second.plan = plan
    second.save(update_fields=["plan", "updated_at"])

    own_plan = make_plan(second)

    plan.refresh_from_db()
    assert own_plan.pk != plan.pk
    assert plan.case_type_name == "Name Change"
    assert own_plan.case_type_name == "Change of Name"


@pytest.mark.django_db
def test_saved_checklist_does_not_change_when_partner_config_changes(user):
    draft = make_draft(user)
    plan = make_plan(draft)
    set_checklist_progress(plan, ["petition"])

    with patch(
        "efile.services.filing_plans.resolve_document_checklist",
        return_value={"something_new": {"label": "New", "requirement": "always"}},
    ):
        ensure_plan_for_draft(draft)

    plan.refresh_from_db()
    assert "something_new" not in plan.checklist
    assert plan.checklist["petition"]["status"] == "have"


@pytest.mark.django_db
def test_progress_only_records_items_the_plan_knows(user):
    plan = make_plan(make_draft(user))

    set_checklist_progress(plan, ["petition", "not_a_real_item"])

    plan.refresh_from_db()
    assert plan.checklist["petition"]["status"] == "have"
    assert plan.checklist["proposed_order"]["status"] == ""
    assert "not_a_real_item" not in plan.checklist


@pytest.mark.django_db
def test_progress_survives_the_filing_it_started_with(user):
    draft = make_draft(user)
    plan = make_plan(draft)
    set_checklist_progress(plan, ["petition"])

    draft.mark_submitted({"confirmation_number": "abc"})
    draft.delete()

    plan.refresh_from_db()
    assert plan.checklist["petition"]["status"] == "have"


@pytest.mark.django_db
def test_one_filer_can_keep_several_plans_of_the_same_case_type(user):
    mine = make_plan(make_draft(user))
    my_childs = make_plan(make_draft(user))
    my_childs.title = "My child's name change"
    my_childs.save(update_fields=["title", "updated_at"])

    assert mine.pk != my_childs.pk
    assert FilingPlan.objects.filter(user=user).count() == 2


@pytest.mark.django_db
def test_grouped_checklist_orders_levels_and_keeps_progress(user):
    plan = make_plan(make_draft(user))
    set_checklist_progress(plan, ["petition"])

    groups = grouped_checklist(plan)

    assert [group["requirement"] for group in groups] == ["always", "usually", "sometimes"]
    assert groups[0]["label"] == "Commonly included"
    assert groups[0]["items"][0]["status"] == "have"
    assert grouped_checklist(None) == []


def fake_codes(*, categories, case_types, filing_types):
    """Stand in for the court's live code lists, which renumber over time."""

    def get(url, params=None, timeout=None):
        if url.endswith("/categories"):
            payload = categories
        elif url.endswith("/case_types/"):
            payload = case_types
        else:
            payload = filing_types
        return Mock(raise_for_status=Mock(), json=Mock(return_value=payload))

    return get


@pytest.mark.django_db
def test_plan_names_resolve_to_todays_codes(user):
    plan = make_plan(make_draft(user))

    with patch(
        "efile.services.filing_plans.requests.get",
        side_effect=fake_codes(
            categories=[{"code": "999001", "name": "Miscellaneous"}],
            case_types=[{"code": "999002", "name": "Name Change"}],
            filing_types=[{"code": "999003", "name": "Petition for Name Change"}],
        ),
    ):
        codes = resolve_plan_case_codes(plan)

    assert codes == {
        "case_category_code": "999001",
        "case_type_code": "999002",
        "lead_filing_type_code": "999003",
    }


@pytest.mark.django_db
def test_a_name_the_court_dropped_resolves_to_nothing(user):
    plan = make_plan(make_draft(user))

    with patch(
        "efile.services.filing_plans.requests.get",
        side_effect=fake_codes(
            categories=[{"code": "999001", "name": "Miscellaneous"}],
            case_types=[{"code": "999002", "name": "Change of Name"}],
            filing_types=[],
        ),
    ):
        codes = resolve_plan_case_codes(plan)

    assert codes["case_category_code"] == "999001"
    assert codes["case_type_code"] == ""
    assert codes["lead_filing_type_code"] == ""


@pytest.mark.django_db
def test_starting_another_filing_needs_your_own_plan(client, user, django_user_model):
    plan = make_plan(make_draft(user))
    someone_else = django_user_model.objects.create_user(username="other-user", tyler_jurisdiction="illinois")
    client.force_login(someone_else)
    session = client.session
    session["auth_tokens"] = {"TYLER-TOKEN-ILLINOIS": "token"}
    session["jurisdiction"] = "illinois"
    session.save()

    response = client.post(reverse("start_filing_from_plan", kwargs={"jurisdiction": "illinois", "plan_id": plan.pk}))

    assert response.status_code == 404
    assert FilingDraft.objects.filter(user=someone_else).count() == 0


@pytest.mark.django_db
def test_another_filing_from_a_plan_uses_todays_codes(user):
    first = make_draft(user)
    plan = make_plan(first)

    with patch(
        "efile.services.filing_plans.requests.get",
        side_effect=fake_codes(
            categories=[{"code": "999001", "name": "Miscellaneous"}],
            case_types=[{"code": "999002", "name": "Name Change"}],
            filing_types=[],
        ),
    ):
        second = create_draft_from_plan(user, plan)

    assert second.pk != first.pk
    assert second.plan == plan
    assert second.case_type_name == "Name Change"
    # Yesterday's code was 78346; the draft files under whatever it is today.
    assert second.case_type_code == "999002"
    assert second.case_category_code == "999001"
    assert second.current_step == WorkflowStepKey.UPLOAD_DOCUMENTS
    assert list(FilingDraft.objects.filter(plan=plan).order_by("pk")) == [first, second]


# --- Statuses, due dates, and guidance ---------------------------------------


def plan_item(plan, item_id, draft=None):
    return next(entry for entry in checklist_items(plan, draft) if entry["id"] == item_id)


@pytest.mark.django_db
def test_a_document_can_be_already_filed(user):
    plan = make_plan(make_draft(user))

    set_checklist_answers(plan, {"publication_notice": {"status": "filed"}})

    assert plan_item(plan, "publication_notice")["status"] == "filed"
    assert plan_item(plan, "publication_notice")["settled"] is True


@pytest.mark.django_db
def test_a_document_can_be_left_until_later_with_a_date(user):
    plan = make_plan(make_draft(user))

    set_checklist_answers(plan, {"publication_notice": {"status": "later", "due_date": "2026-09-30"}})

    answered = plan_item(plan, "publication_notice")
    assert answered["status"] == "later"
    assert answered["due_date"] == date(2026, 9, 30)
    assert answered["settled"] is False


@pytest.mark.django_db
def test_a_date_is_optional_and_a_date_we_cannot_read_is_not_kept(user):
    plan = make_plan(make_draft(user))

    set_checklist_answers(plan, {"publication_notice": {"status": "later", "due_date": "next Tuesday"}})

    assert plan_item(plan, "publication_notice")["status"] == "later"
    assert plan_item(plan, "publication_notice")["due_date"] is None
    assert "due_date" not in plan.checklist["publication_notice"]


@pytest.mark.django_db
def test_a_date_is_dropped_when_the_answer_stops_being_later(user):
    plan = make_plan(make_draft(user))
    set_checklist_answers(plan, {"publication_notice": {"status": "later", "due_date": "2026-09-30"}})

    set_checklist_answers(plan, {"publication_notice": {"status": "have"}})

    assert plan_item(plan, "publication_notice")["due_date"] is None


@pytest.mark.django_db
def test_an_answer_the_plan_never_offered_is_not_recorded(user):
    plan = make_plan(make_draft(user))

    set_checklist_answers(plan, {"publication_notice": {"status": "burned it"}})

    assert plan_item(plan, "publication_notice")["status"] == ""


@pytest.mark.django_db
def test_plans_written_before_there_were_statuses_still_read(user):
    plan = make_plan(make_draft(user))
    FilingPlan.objects.filter(pk=plan.pk).update(
        checklist={"publication_notice": {"label": "Notice", "requirement": "usually", "complete": True}}
    )
    plan.refresh_from_db()

    assert plan_item(plan, "publication_notice")["status"] == "have"


@pytest.mark.django_db
def test_progress_counts_filed_as_done_and_later_separately(user):
    plan = make_plan(make_draft(user))

    set_checklist_answers(
        plan,
        {
            "petition": {"status": "have"},
            "proposed_order": {"status": "filed"},
            "publication_notice": {"status": "later"},
        },
    )

    progress = plan_progress(plan)
    assert progress["complete"] == 2
    assert progress["later"] == 1
    assert progress["outstanding"] == progress["total"] - 3


@pytest.mark.django_db
def test_a_plan_keeps_what_its_partner_says_about_this_kind_of_filing(user):
    plan = make_plan(make_draft(user))

    assert "name change" in plan.guidance["summary"].lower()
    assert plan.guidance["learn_more_url"].startswith("https://")
    assert plan.guidance["learn_more_label"]


# --- Resolving filing types for checklist items ------------------------------

KANE_FILING_TYPES = [
    {"code": "6529", "name": "Waiver"},
    {"code": "6714", "name": "Motion"},
    {"code": "25946", "name": "Other Document Not Listed"},
    {"code": "25955", "name": "Petition"},
]


def court_publishes(filing_types):
    return patch(
        "efile.services.filing_plans.requests.get",
        return_value=Mock(raise_for_status=Mock(), json=Mock(return_value=filing_types)),
    )


@pytest.mark.django_db
def test_the_plan_knows_what_the_court_calls_a_proposed_order(user):
    draft = make_draft(
        user, court_code="kane", court_name="Kane County", case_type_code="10589", case_type_name="Change of Name"
    )
    ensure_plan_for_draft(draft)

    with court_publishes(KANE_FILING_TYPES):
        code, name = filing_type_for_item(draft, "proposed_order")

    assert (code, name) == ("25946", "Other Document Not Listed")


@pytest.mark.django_db
def test_the_most_preferred_name_the_court_offers_wins(user):
    draft = make_draft(
        user, court_code="kane", court_name="Kane County", case_type_code="10589", case_type_name="Change of Name"
    )
    ensure_plan_for_draft(draft)

    with court_publishes([{"code": "40001", "name": "Proposed Order"}, *KANE_FILING_TYPES]):
        code, name = filing_type_for_item(draft, "proposed_order")

    assert (code, name) == ("40001", "Proposed Order")


@pytest.mark.django_db
def test_a_court_that_names_it_nothing_leaves_the_choice_alone(user):
    draft = make_draft(user)
    ensure_plan_for_draft(draft)

    with court_publishes([{"code": "78690", "name": "Petition for Name Change"}]):
        assert filing_type_for_item(draft, "proposed_order") == ("", "")


@pytest.mark.django_db
def test_an_item_with_no_configured_names_asks_the_court_nothing(user):
    draft = make_draft(user)
    ensure_plan_for_draft(draft)

    with court_publishes(KANE_FILING_TYPES) as request:
        assert filing_type_for_item(draft, "county_division_cover_sheet") == ("", "")
        assert request.call_count == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    "names,expected",
    [
        (
            ["Application", "Waiver", "Application Waiver of Court Fees (Civil)"],
            "Application Waiver of Court Fees (Civil)",
        ),
        (["Application", "Waiver"], "Waiver"),
        (["Application"], "Application"),
    ],
)
def test_fee_waiver_checklist_uses_current_hierarchy_even_for_saved_plans(user, names, expected):
    draft = make_draft(user)
    ensure_plan_for_draft(draft)
    # Simulate a plan persisted before statewide generic fallback support.
    plan = draft.plan
    plan.checklist["fee_waiver"]["filing_type_names"] = ["Waiver"]
    plan.save()
    catalog = [{"code": str(index + 1), "name": name} for index, name in enumerate(names)]
    with court_publishes(catalog):
        assert filing_type_for_item(draft, "fee_waiver")[1] == expected

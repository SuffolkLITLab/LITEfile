"""The new-or-existing answer: kept, followed, and correctable (#232).

Vermont testing: a filer chose "Start a new case" from the menu, uploaded a
Small Claims Answer, was asked new-or-existing again on Confirm case, and
found "What are you trying to do?" -- a screen they never saw -- behind Back.
"""

import re

import pytest
from django.urls import reverse

from efile.models import DocumentExtraction, FilingDocument, FilingDraft, FilingPlan
from efile.services.current_drafts import CURRENT_DRAFT_SESSION_KEY
from efile.services.fee_quotes import FeeQuoteState, fee_quote_state, record_fee_quote
from efile.services.filing_path import (
    FILING_PATH_SOURCE_KEY,
    FilingPathSource,
    change_filing_path,
    filing_path_conflict,
)
from efile.tests.helpers import reviewed_document
from efile.workflow import ExistingCase, WorkflowStepKey, get_resume_step_url, get_visible_workflow

J = {"jurisdiction": "illinois"}
OPTIONS_URL = reverse("efile_options", kwargs=J)
START_URL = reverse("start_filing", kwargs=J)
FILING_PATH_URL = reverse("filing_path", kwargs=J)
UPLOAD_URL = reverse("upload_documents", kwargs=J)
CONFIRM_URL = reverse("extraction_review", kwargs=J)
CASE_LOOKUP_URL = reverse("case_lookup", kwargs=J)
REVIEW_URL = reverse("case_review", kwargs=J)


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="path-user", tyler_jurisdiction="illinois")


@pytest.fixture
def signed_in(client, user):
    client.force_login(user)
    session = client.session
    session["jurisdiction"] = "illinois"
    session["auth_tokens"] = {"TYLER-TOKEN-ILLINOIS": "token"}
    session.save()
    return client


def point_at(client, draft):
    session = client.session
    session[CURRENT_DRAFT_SESSION_KEY] = draft.pk
    session.save()


def start_from_menu(client, path):
    response = client.post(START_URL, {"existing_case": path})
    assert response.url.partition("?")[0] == UPLOAD_URL
    return FilingDraft.objects.latest("pk")


def back_link(content):
    """The workflow Back button's target, without its ?draft= scoping."""

    match = re.search(
        r'<a class="btn btn-outline-secondary"\s+href="([^"]+)"><i class="fa-solid fa-arrow-left"', content
    )
    assert match, "no Back button"
    return match.group(1).split("?")[0]


def lead_with_evidence(draft, *, phase, title="Answer", filing_type="answer-code"):
    lead = reviewed_document(
        draft=draft,
        role=FilingDocument.Role.LEAD,
        name="answer.pdf",
        s3_key="uploads/answer.pdf",
        filing_type_code=filing_type,
        filing_type_name="Answer" if filing_type else "",
        requested_optional_services=["certified-copy"],
    )
    DocumentExtraction.objects.create(
        document=lead,
        status=DocumentExtraction.Status.COMPLETE,
        evidence={"filing phase": phase, "document title": title},
    )
    FilingDraft.objects.filter(pk=draft.pk).update(extracted_guesses={"document title": title})
    draft.refresh_from_db()
    return lead


def small_claims(draft, **values):
    fields = {
        "court_code": "vt:washington",
        "court_name": "Washington Unit",
        "case_category_code": "small",
        "case_category_name": "Small Claims",
        "case_type_code": "sc",
        "case_type_name": "Small Claims",
        **values,
    }
    FilingDraft.objects.filter(pk=draft.pk).update(**fields)
    draft.refresh_from_db()


def confirm(client, **overrides):
    data = {
        "reviewed_extraction": "yes",
        "court_code": "vt:washington",
        "court_name": "Washington Unit",
        "case_category_code": "small",
        "case_category_name": "Small Claims",
        "case_type_code": "sc",
        "case_type_name": "Small Claims",
        **overrides,
    }
    return client.post(CONFIRM_URL, data)


# --- Back and the progress steps follow the way in ----------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("path", [ExistingCase.NEW, ExistingCase.EXISTING])
def test_a_menu_start_goes_back_to_where_it_started_not_to_the_question(signed_in, path):
    draft = start_from_menu(signed_in, path)

    assert draft.supplemental_fields[FILING_PATH_SOURCE_KEY] == FilingPathSource.START_MENU
    content = signed_in.get(UPLOAD_URL).content.decode()
    assert back_link(content) == OPTIONS_URL
    assert WorkflowStepKey.FILING_PATH not in [step.key for step in get_visible_workflow(draft)]
    # The progress bar still starts at Start.
    labels = re.findall(r'<span class="workflow-progress__label">([^<]+)</span>', content)
    assert labels[:2] == ["Start", "Upload"]


@pytest.mark.django_db
def test_a_start_through_the_question_goes_back_to_it(signed_in):
    signed_in.post(START_URL, {})
    draft = FilingDraft.objects.latest("pk")
    response = signed_in.post(FILING_PATH_URL, {"existing_case": ExistingCase.NEW})
    assert response.url.partition("?")[0] == UPLOAD_URL

    draft.refresh_from_db()
    assert draft.supplemental_fields[FILING_PATH_SOURCE_KEY] == FilingPathSource.QUESTION
    assert back_link(signed_in.get(UPLOAD_URL).content.decode()) == FILING_PATH_URL


@pytest.mark.django_db
def test_unsure_still_goes_through_the_question_and_is_asked_again_on_confirm(signed_in):
    signed_in.post(START_URL, {})
    signed_in.post(FILING_PATH_URL, {"existing_case": ExistingCase.UNSURE})
    draft = FilingDraft.objects.latest("pk")
    lead_with_evidence(draft, phase="unknown")

    assert back_link(signed_in.get(UPLOAD_URL).content.decode()) == FILING_PATH_URL
    content = signed_in.get(CONFIRM_URL).content.decode()
    assert 'id="path-summary"' not in content
    assert re.search(r'<fieldset class="path-confirmation"\s+id="path-question"\s*>', content)


@pytest.mark.django_db
def test_an_older_draft_that_does_not_say_how_it_started_keeps_the_question(signed_in, user):
    draft = FilingDraft.objects.create(
        user=user,
        jurisdiction="illinois",
        workflow_version=2,
        existing_case=ExistingCase.NEW,
        current_step=WorkflowStepKey.UPLOAD_DOCUMENTS,
    )
    point_at(signed_in, draft)

    assert back_link(signed_in.get(UPLOAD_URL).content.decode()) == FILING_PATH_URL


@pytest.mark.django_db
def test_a_plan_start_goes_back_to_the_start_page(signed_in, user, monkeypatch):
    plan = FilingPlan.objects.create(user=user, jurisdiction="illinois", title="Small claims")
    created = FilingDraft.objects.create(
        user=user, jurisdiction="illinois", workflow_version=2, plan=plan, existing_case=ExistingCase.EXISTING
    )
    monkeypatch.setattr("efile.views.draft_views.create_draft_from_plan", lambda user, plan: created)

    signed_in.post(reverse("start_filing_from_plan", kwargs={**J, "plan_id": plan.pk}))

    created.refresh_from_db()
    assert created.supplemental_fields[FILING_PATH_SOURCE_KEY] == FilingPathSource.PLAN
    assert back_link(signed_in.get(UPLOAD_URL).content.decode()) == OPTIONS_URL


@pytest.mark.django_db
def test_a_direct_link_to_the_question_still_works_and_changes_the_same_draft(signed_in):
    draft = start_from_menu(signed_in, ExistingCase.NEW)
    lead = lead_with_evidence(draft, phase="subsequent")

    page = signed_in.get(FILING_PATH_URL)
    assert page.status_code == 200
    response = signed_in.post(FILING_PATH_URL, {"existing_case": ExistingCase.EXISTING})

    assert response.url.partition("?")[0] == UPLOAD_URL
    draft.refresh_from_db()
    assert draft.existing_case == ExistingCase.EXISTING
    assert FilingDraft.objects.filter(user=draft.user).count() == 1
    assert FilingDocument.objects.get(pk=lead.pk).s3_key == "uploads/answer.pdf"


@pytest.mark.django_db
def test_resuming_a_menu_started_draft_lands_on_its_step_with_the_same_back(signed_in):
    draft = start_from_menu(signed_in, ExistingCase.NEW)

    url = get_resume_step_url(draft.current_step, "illinois", draft.pk)
    assert url is not None
    assert url.partition("?")[0] == UPLOAD_URL
    assert back_link(signed_in.get(url).content.decode()) == OPTIONS_URL


@pytest.mark.django_db
def test_upload_shows_the_saved_answer_with_a_change_link(signed_in):
    start_from_menu(signed_in, ExistingCase.NEW)

    content = signed_in.get(UPLOAD_URL).content.decode()

    assert "Starting a new case" in content
    assert f'href="{FILING_PATH_URL}' in content
    assert "Change whether this is a new or existing case" in content


# --- Confirm case shows the answer instead of asking again --------------------


@pytest.mark.django_db
def test_confirm_case_shows_the_saved_answer_with_change_and_keeps_the_question_folded(signed_in):
    draft = start_from_menu(signed_in, ExistingCase.NEW)
    lead_with_evidence(draft, phase="initial", title="Complaint")

    content = signed_in.get(CONFIRM_URL).content.decode()

    assert 'id="path-summary"' in content
    assert 'id="change-filing-path"' in content
    assert re.search(r'id="path-question"\s+hidden', content)
    # The answer still rides on the form.
    assert re.search(r'name="existing_case"\s+value="new"\s+checked', content)
    assert 'id="path-conflict"' not in content


# --- The mistaken new case: a Small Claims Answer -----------------------------


@pytest.mark.django_db
def test_an_answer_filed_as_a_new_case_is_pointed_out_without_changing_anything(signed_in):
    draft = start_from_menu(signed_in, ExistingCase.NEW)
    lead_with_evidence(draft, phase="subsequent", title="Small Claims Answer")

    content = signed_in.get(CONFIRM_URL).content.decode()

    assert 'id="path-conflict"' in content
    assert "Small Claims Answer" in content
    assert "reads like a filing in a case that is already open" in content
    assert "Check your paperwork." in content
    assert "If the case is already open, change your answer." in content
    assert 'data-value="existing"' in content
    draft.refresh_from_db()
    assert draft.existing_case == ExistingCase.NEW


@pytest.mark.django_db
def test_correcting_the_mistaken_new_case_keeps_the_draft_and_goes_to_case_lookup(signed_in):
    draft = start_from_menu(signed_in, ExistingCase.NEW)
    lead = lead_with_evidence(draft, phase="subsequent", title="Small Claims Answer")
    small_claims(draft)
    record_fee_quote(draft, "95.00", [{"label": "Filing fee", "amount": "95.00"}])
    assert fee_quote_state(draft) == FeeQuoteState.CURRENT

    # Even from Review: an existing case has to be found before Review.
    response = confirm(signed_in, existing_case=ExistingCase.EXISTING, docket_number="24-SC-0012", return_to="review")

    assert response.status_code == 302
    assert response.url.partition("?")[0] == CASE_LOOKUP_URL
    draft.refresh_from_db()
    lead.refresh_from_db()
    assert FilingDraft.objects.filter(user=draft.user).count() == 1
    assert draft.existing_case == ExistingCase.EXISTING
    assert draft.docket_number == "24-SC-0012"
    assert lead.s3_key == "uploads/answer.pdf"
    # Filing types chosen from the new-case list, and the quote that priced them, go.
    assert (lead.filing_type_code, lead.requested_optional_services) == ("", [])
    assert draft.filing_type_code == ""
    assert (draft.quoted_fee_total, draft.quoted_fee_breakdown, draft.quoted_fee_fingerprint) == ("", [], "")
    assert fee_quote_state(draft) == FeeQuoteState.MISSING

    messages = [str(message) for message in signed_in.get(CASE_LOOKUP_URL).context["messages"]]
    assert any("Your uploaded documents are kept" in message and "filing type again" in message for message in messages)


@pytest.mark.django_db
def test_a_premature_existing_case_filing_type_is_not_applied(signed_in):
    draft = start_from_menu(signed_in, ExistingCase.NEW)
    lead = lead_with_evidence(draft, phase="subsequent")

    confirm(
        signed_in,
        existing_case=ExistingCase.EXISTING,
        filing_type_code="answer-existing",
        filing_type_name="Answer",
    )

    lead.refresh_from_db()
    assert lead.filing_type_code == ""


@pytest.mark.django_db
def test_changing_back_to_a_new_case_drops_the_existing_case_it_was_filed_into(signed_in):
    draft = start_from_menu(signed_in, ExistingCase.EXISTING)
    lead = lead_with_evidence(draft, phase="initial", title="Complaint")
    small_claims(draft, previous_case_id="case-tracking-9", docket_number="24-SC-0012", case_title="Kris v. Pat")

    response = confirm(signed_in, existing_case=ExistingCase.NEW)

    assert response.url.partition("?")[0] == reverse("document_checklist", kwargs=J)
    draft.refresh_from_db()
    lead.refresh_from_db()
    assert (draft.previous_case_id, draft.docket_number, draft.case_title) == ("", "", "")
    assert lead.filing_type_code == ""
    assert not draft.is_filing_into_found_case


@pytest.mark.django_db
def test_an_unchanged_answer_from_review_still_returns_to_review(signed_in):
    draft = start_from_menu(signed_in, ExistingCase.NEW)
    lead = lead_with_evidence(draft, phase="initial", title="Complaint")
    # This detour starts after the filer organized the filing for Review.
    lead.document_type_code = "public"
    lead.save()

    response = confirm(signed_in, existing_case=ExistingCase.NEW, return_to="review")

    assert response.url.partition("?")[0] == REVIEW_URL
    lead.refresh_from_db()
    assert lead.filing_type_code == "answer-code"


@pytest.mark.django_db
def test_a_different_answer_sent_back_with_an_error_keeps_the_question_open(signed_in):
    draft = start_from_menu(signed_in, ExistingCase.NEW)
    lead_with_evidence(draft, phase="subsequent")

    response = signed_in.post(CONFIRM_URL, {"existing_case": ExistingCase.EXISTING})  # no acknowledgement

    content = response.content.decode()
    assert response.status_code == 200
    assert re.search(r'<fieldset class="path-confirmation"\s+id="path-question"\s*>', content)
    assert re.search(r'name="existing_case"\s+value="existing"\s+checked', content)
    # Existing is what the document suggests, so there is nothing to point out;
    # the note must not describe the saved answer the filer just replaced.
    assert 'id="path-conflict"' not in content
    assert "You chose to start a new case" not in content
    draft.refresh_from_db()
    assert draft.existing_case == ExistingCase.NEW


@pytest.mark.django_db
def test_the_conflict_note_on_a_resent_form_describes_the_answer_submitted(signed_in):
    draft = start_from_menu(signed_in, ExistingCase.EXISTING)
    lead_with_evidence(draft, phase="subsequent")
    assert 'id="path-conflict"' not in signed_in.get(CONFIRM_URL).content.decode()

    content = signed_in.post(CONFIRM_URL, {"existing_case": ExistingCase.NEW}).content.decode()

    assert 'id="path-conflict"' in content
    assert "You chose to start a new case" in content
    assert 'data-value="existing"' in content


# --- The service on its own ---------------------------------------------------


@pytest.mark.django_db
def test_change_filing_path_leaves_a_first_answer_and_a_same_answer_alone(user):
    draft = FilingDraft.objects.create(user=user, jurisdiction="illinois", quoted_fee_total="10.00")
    reviewed_document(draft=draft, role=FilingDocument.Role.LEAD, filing_type_code="x")

    first = change_filing_path(draft, ExistingCase.NEW)
    assert first.changed and not first.switched
    assert first.cleared == ["fees"]
    same = change_filing_path(draft, ExistingCase.NEW)
    assert not same.changed
    assert FilingDocument.objects.get(draft=draft).filing_type_code == "x"


@pytest.mark.django_db
def test_unsure_to_new_keeps_filing_types_since_the_court_lists_are_the_same(user):
    draft = FilingDraft.objects.create(user=user, jurisdiction="illinois", existing_case=ExistingCase.UNSURE)
    reviewed_document(draft=draft, role=FilingDocument.Role.LEAD, filing_type_code="complaint")

    change = change_filing_path(draft, ExistingCase.NEW)

    assert change.switched
    assert "filing_types" not in change.cleared
    assert FilingDocument.objects.get(draft=draft).filing_type_code == "complaint"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("chosen", "phase", "suggested"),
    [
        (ExistingCase.NEW, "subsequent", "existing"),
        (ExistingCase.EXISTING, "initial", "new"),
        (ExistingCase.NEW, "initial", None),
        (ExistingCase.EXISTING, "subsequent", None),
        (ExistingCase.UNSURE, "subsequent", None),
        (ExistingCase.NEW, "unknown", None),
        ("", "subsequent", None),
    ],
)
def test_filing_path_conflict_only_when_the_document_reads_as_the_other_kind(user, chosen, phase, suggested):
    draft = FilingDraft.objects.create(user=user, jurisdiction="illinois", existing_case=chosen)

    conflict = filing_path_conflict(draft, {"filing phase": phase}, "Answer")

    assert (conflict or {}).get("suggested") == suggested


@pytest.mark.django_db
def test_new_or_existing_comes_before_the_case_fields_with_the_case_number_beside_it(signed_in):
    """It decides which filing types are offered and whether there is a case number."""

    draft = start_from_menu(signed_in, ExistingCase.EXISTING)
    lead_with_evidence(draft, phase="subsequent")

    content = signed_in.get(CONFIRM_URL).content.decode()

    section = content.index('id="path-section"')
    question = content.index('id="path-question"')
    case_number = content.index('id="docket-number-field"')
    fields = content.index('class="review-grid"')
    category = content.index('id="case_category_code"')
    assert section < question < case_number < fields < category


@pytest.mark.django_db
def test_generic_start_keeps_back_at_home_and_branch_unset(signed_in):
    response = signed_in.post(START_URL, {})
    draft = FilingDraft.objects.latest("pk")
    assert response.url.partition("?")[0] == UPLOAD_URL
    assert draft.existing_case == ""
    assert back_link(signed_in.get(response.url).content.decode()) == OPTIONS_URL
    assert WorkflowStepKey.FILING_PATH not in [step.key for step in get_visible_workflow(draft)]
    reviewed_document(draft=draft, role=FilingDocument.Role.LEAD, name="document.pdf")
    confirm_page = signed_in.get(CONFIRM_URL)
    assert confirm_page.context["path_question_open"]
    assert confirm_page.context["chosen_existing_case"] == ""

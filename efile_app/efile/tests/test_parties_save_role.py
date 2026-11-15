"""Saving the filer's role is not the same as finishing the People step.

Vermont testing (#233): a filer chose Defendant, pressed the role button, and
was moved on before reading the party list beneath it -- the list that says
who the court will be told this case is about. Save now stays on People; only
the Continue button after the list moves on.
"""

import json
import re
from unittest.mock import patch

import pytest
from django.urls import reverse

from efile.models import FilingDocument, FilingDraft, FilingParty
from efile.services.current_drafts import CURRENT_DRAFT_SESSION_KEY
from efile.services.people import NOT_A_PARTY
from efile.tests.helpers import reviewed_document
from efile.workflow import ExistingCase, WorkflowStepKey

PARTIES_URL = reverse("parties", kwargs={"jurisdiction": "illinois"})
SERVICE_URL = reverse("service", kwargs={"jurisdiction": "illinois"})
REVIEW_URL = reverse("case_review", kwargs={"jurisdiction": "illinois"})
PARTY_DETAILS_URL = reverse("party_details", kwargs={"jurisdiction": "illinois"})

PARTY_TYPES = [
    {"code": "plaintiff", "name": "Plaintiff", "required": True},
    {"code": "defendant", "name": "Defendant", "required": True},
]

ADDRESS = {
    "address_line_1": "1 Main Street",
    "city": "Chicago",
    "state": "IL",
    "zip_code": "60601",
}


@pytest.fixture
def draft(client, django_user_model):
    user = django_user_model.objects.create_user(username="save-role", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(
        user=user,
        jurisdiction="illinois",
        workflow_version=2,
        existing_case=ExistingCase.NEW,
        court_code="cook:cvd1",
        case_type_code="NC",
        case_type_name="Name Change",
        current_step=WorkflowStepKey.PARTIES,
        document_checklist_acknowledged=True,
    )
    reviewed_document(
        draft=draft,
        role=FilingDocument.Role.LEAD,
        sort_order=0,
        name="answer.pdf",
        filing_type_code="90001",
        filing_type_name="Answer",
        document_type_code="public",
    )
    client.force_login(user)
    session = client.session
    session[CURRENT_DRAFT_SESSION_KEY] = draft.pk
    session["jurisdiction"] = "illinois"
    session["auth_tokens"] = {"TYLER-TOKEN-ILLINOIS": "token"}
    session.save()
    return draft


def make_filer(draft, **overrides):
    values = {"first_name": "Kris", "last_name": "Tester", "email": "kris@example.com", **ADDRESS}
    values.update(overrides)
    return FilingParty.objects.create(draft=draft, role="filer", sort_order=0, **values)


def make_party(draft, sort_order, **overrides):
    values = {
        "party_type": "plaintiff",
        "party_type_name": "Plaintiff",
        "first_name": "Pat",
        "last_name": "Landlord",
        **ADDRESS,
    }
    values.update(overrides)
    return FilingParty.objects.create(draft=draft, role="other", sort_order=sort_order, **values)


def found(pattern, text, flags=0):
    match = re.search(pattern, text, flags)
    assert match is not None, f"no match for {pattern!r}"
    return match


def post(client, **data):
    with patch("efile.views.parties.get_party_types", return_value=PARTY_TYPES):
        return client.post(PARTIES_URL, data)


def get(client, url=PARTIES_URL):
    with patch("efile.views.parties.get_party_types", return_value=PARTY_TYPES):
        return client.get(url)


def assert_stays_on_people(response, draft):
    assert response.status_code == 302
    assert response.url.partition("?")[0] == PARTIES_URL
    assert response.url.endswith("#party-list")
    draft.refresh_from_db()
    assert draft.current_step == WorkflowStepKey.PARTIES


# --- Save stays, Continue moves on ------------------------------------------


@pytest.mark.django_db
def test_saving_a_role_with_a_complete_roster_stays_on_people(client, draft):
    filer = make_filer(draft)
    make_party(draft, 0)

    response = post(client, action="save_role", filer_party_type="defendant")

    assert_stays_on_people(response, draft)
    filer.refresh_from_db()
    assert filer.party_type == "defendant"
    assert filer.is_filing_party

    page = get(client).content.decode()
    assert "Your role is saved. Check the party list, then continue." in page
    # The roster the filer is being asked to check shows what they just saved.
    assert re.search(r"Defendant\s*·\s*You", page)
    assert "Pat Landlord" in page


@pytest.mark.django_db
def test_continue_alone_advances_a_complete_roster(client, draft):
    make_filer(draft)
    make_party(draft, 0)

    post(client, action="save_role", filer_party_type="defendant")
    response = post(client, action="continue", filer_party_type="defendant")

    assert response.status_code == 302
    assert response.url.partition("?")[0] == SERVICE_URL
    draft.refresh_from_db()
    assert draft.current_step == WorkflowStepKey.SERVICE


@pytest.mark.django_db
def test_continue_carries_an_unsaved_role_choice(client, draft):
    """Choosing a role and going straight to Continue is not a Save loop."""

    filer = make_filer(draft)
    make_party(draft, 0)

    response = post(client, action="continue", filer_party_type="defendant")

    assert response.url.partition("?")[0] == SERVICE_URL
    filer.refresh_from_db()
    assert filer.party_type == "defendant"


@pytest.mark.django_db
def test_saving_a_role_with_required_parties_missing_says_so_and_stays(client, draft):
    make_filer(draft)

    response = post(client, action="save_role", filer_party_type="defendant")

    assert_stays_on_people(response, draft)
    # Nothing blank is invented on Save; the GET that follows would only clear it.
    assert not FilingParty.objects.filter(draft=draft, role="other").exists()
    page = get(client).content.decode()
    assert "The court also needs a Plaintiff in this case." in page
    assert "Continue to missing party details" in page


@pytest.mark.django_db
def test_continue_with_required_parties_missing_collects_them(client, draft):
    make_filer(draft, party_type="defendant", party_type_name="Defendant")

    response = post(client, action="continue", filer_party_type="defendant")

    plaintiff = FilingParty.objects.get(draft=draft, role="other")
    assert plaintiff.party_type == "plaintiff"
    assert response.status_code == 302
    assert response.url.partition("?")[0] == PARTY_DETAILS_URL
    assert f"party={plaintiff.pk}" in response.url
    draft.refresh_from_db()
    assert draft.current_step == WorkflowStepKey.PARTY_DETAILS


@pytest.mark.django_db
def test_continue_with_an_incomplete_named_party_asks_for_their_details(client, draft):
    make_filer(draft)
    landlord = make_party(draft, 0, last_name="")

    page = get(client).content.decode()
    assert "Needs details" in page
    assert "Continue to missing party details" in page

    response = post(client, action="continue", filer_party_type="defendant")

    assert response.url.partition("?")[0] == PARTY_DETAILS_URL
    assert f"party={landlord.pk}" in response.url


@pytest.mark.django_db
def test_neither_button_moves_on_without_a_role(client, draft):
    make_filer(draft)
    make_party(draft, 0)

    for action in ("save_role", "continue"):
        response = post(client, action=action)
        assert response.status_code == 200
        assert b"Choose your role in this case" in response.content
        draft.refresh_from_db()
        assert draft.current_step == WorkflowStepKey.PARTIES


@pytest.mark.django_db
def test_repeated_saves_do_not_duplicate_the_filer(client, draft):
    make_filer(draft)
    make_party(draft, 0)

    post(client, action="save_role", filer_party_type="defendant")
    post(client, action="save_role", filer_party_type="plaintiff")
    post(client, action="save_role", filer_party_type="defendant")

    assert FilingParty.objects.filter(draft=draft, role="filer").count() == 1
    assert FilingParty.objects.filter(draft=draft).count() == 2
    assert FilingParty.objects.get(draft=draft, role="filer").party_type == "defendant"


@pytest.mark.django_db
def test_saved_role_is_what_the_screen_comes_back_on(client, draft):
    """Back from the next step, or a resumed draft, finds the answer given."""

    make_filer(draft)
    make_party(draft, 0)
    post(client, action="save_role", filer_party_type="defendant")

    page = get(client).content.decode()
    defendant = re.search(r'<input[^>]*name="filer_party_type"[^>]*value="defendant"[^>]*>', page)
    assert defendant is not None and "checked" in defendant.group()


# --- Filing for someone else --------------------------------------------------


@pytest.mark.django_db
def test_saving_a_filing_for_someone_else_stays_and_shows_who(client, draft):
    filer = make_filer(draft)
    tenant = make_party(draft, 0, party_type="defendant", party_type_name="Defendant", first_name="Real")
    make_party(draft, 1)

    response = post(
        client,
        action="save_role",
        filer_party_type=NOT_A_PARTY,
        filing_for=tenant.pk,
        notice_email="kris@example.com",
    )

    assert_stays_on_people(response, draft)
    filer.refresh_from_db()
    tenant.refresh_from_db()
    assert filer.party_type == ""
    assert tenant.is_filing_party
    page = get(client).content.decode()
    assert "you are filing for them" in page
    assert "You — filing this, but not a party in the case" in page

    response = post(
        client,
        action="continue",
        filer_party_type=NOT_A_PARTY,
        filing_for=tenant.pk,
        notice_email="kris@example.com",
    )
    assert response.url.partition("?")[0] == SERVICE_URL


# --- Account and document names that differ -----------------------------------


@pytest.mark.django_db
def test_claiming_a_differently_named_party_still_asks_then_stays_for_review(client, draft):
    filer = make_filer(draft)
    named = make_party(
        draft, 0, party_type="defendant", party_type_name="Defendant", first_name="Kristen", last_name="Doe"
    )
    make_party(draft, 1)

    refused = post(client, action="claim_party", party_id=named.pk)
    assert refused.url.partition("?")[0] == PARTIES_URL
    assert FilingParty.objects.filter(pk=named.pk).exists()

    claimed = post(client, action="claim_party", party_id=named.pk, name_choice="theirs")
    assert claimed.url.partition("?")[0] == PARTIES_URL
    draft.refresh_from_db()
    assert draft.current_step == WorkflowStepKey.PARTIES
    filer.refresh_from_db()
    assert (filer.first_name, filer.last_name) == ("Kristen", "Doe")
    assert filer.party_type == "defendant"

    # The result is on the list to be read before anyone presses Continue.
    page = get(client).content.decode()
    assert re.search(r"Kristen Doe\s*</strong>\s*<small>\s*Defendant\s*·\s*You", page)


# --- Return to Review ---------------------------------------------------------


@pytest.mark.django_db
def test_save_from_review_stays_and_continue_returns_to_review(client, draft):
    make_filer(draft)
    make_party(draft, 0)

    saved = post(client, action="save_role", filer_party_type="defendant", return_to="review")
    assert "return_to=review" in saved.url
    assert saved.url.partition("?")[0] == PARTIES_URL

    page = get(client, f"{PARTIES_URL}?return_to=review").content.decode()
    assert "Continue to review" in page

    response = post(client, action="continue", filer_party_type="defendant", return_to="review")
    assert response.url.partition("?")[0] == REVIEW_URL


# --- Keyboard and structure ---------------------------------------------------


@pytest.mark.django_db
def test_enter_in_the_role_form_saves_and_continue_follows_the_roster(client, draft):
    """Implicit submission uses a form's first submit button, so Enter saves."""

    make_filer(draft)
    make_party(draft, 0)

    page = get(client).content.decode()
    role_form = found(r'<form[^>]*id="your-role"[^>]*>(.*?)</form>', page, re.S).group(1)
    first_submit = found(r'<button[^>]*type="submit"[^>]*>', role_form).group()
    assert 'value="save_role"' in first_submit
    assert 'value="continue"' not in role_form

    continue_button = re.search(r'<button[^>]*form="your-role"[^>]*value="continue"[^>]*>', page, re.S)
    assert continue_button is not None
    assert page.index('id="party-list"') < continue_button.start()


# --- Continue says what it will do for the role being submitted ---------------


def continue_previews(page):
    return json.loads(
        found(r'<script id="continue-previews" type="application/json">(.*?)</script>', page, re.S).group(1)
    )


def continue_label(page):
    return found(r'<span id="continue-from-parties-label">([^<]*)</span>', page).group(1).strip()


@pytest.mark.django_db
def test_with_no_role_chosen_continue_promises_nothing(client, draft):
    make_filer(draft)
    make_party(draft, 0)

    page = get(client).content.decode()

    assert continue_label(page) == "Continue"
    assert re.search(r'<p class="party-roster__hint"\s+id="party-list-next"\s+hidden>', page)


@pytest.mark.django_db
def test_continue_is_labelled_for_the_role_selected_not_the_one_saved(client, draft):
    """A Plaintiff is on the list: Defendant goes straight on, Plaintiff needs a Defendant."""

    make_filer(draft, party_type="defendant", party_type_name="Defendant")
    make_party(draft, 0)

    previews = continue_previews(get(client).content.decode())

    assert previews["defendant"] == {"label": "Continue", "hint": ""}
    assert previews["plaintiff"]["label"] == "Continue to missing party details"
    assert "The court also needs a Defendant in this case." in previews["plaintiff"]["hint"]
    assert previews[NOT_A_PARTY]["label"] == "Continue to missing party details"


@pytest.mark.django_db
def test_the_saved_role_selects_its_own_label_on_the_page(client, draft):
    make_filer(draft, party_type="plaintiff", party_type_name="Plaintiff")
    make_party(draft, 0)

    page = get(client).content.decode()

    assert continue_label(page) == "Continue to missing party details"
    assert "The court also needs a Defendant in this case." in page


@pytest.mark.django_db
@pytest.mark.parametrize("saved", ["", "defendant", "plaintiff"])
@pytest.mark.parametrize("chosen", ["defendant", "plaintiff"])
def test_the_label_for_each_role_matches_where_continue_goes(client, draft, saved, chosen):
    """The reviewer's two cases, and the rest: Continue straight from a changed choice."""

    make_filer(draft, party_type=saved, party_type_name=saved.title())
    make_party(draft, 0)
    label = continue_previews(get(client).content.decode())[chosen]["label"]

    response = post(client, action="continue", filer_party_type=chosen)

    goes_to_details = response.url.partition("?")[0] == PARTY_DETAILS_URL
    assert goes_to_details == (label == "Continue to missing party details")
    if not goes_to_details:
        assert response.url.partition("?")[0] == SERVICE_URL

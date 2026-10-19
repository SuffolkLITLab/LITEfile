import re
from unittest.mock import patch

import pytest
from django.urls import reverse

from efile.models import DocumentExtraction, FilingDocument, FilingDraft, FilingParty
from efile.services.current_drafts import CURRENT_DRAFT_SESSION_KEY
from efile.services.drafts import read_case_data
from efile.services.party_requirements import AddressRequirement
from efile.services.people import guess_filer_party_type
from efile.tests.helpers import reviewed_document
from efile.workflow import ExistingCase, WorkflowStepKey

PARTY_TYPES = [
    {"code": "plaintiff", "name": "Plaintiff", "required": True},
    {"code": "defendant", "name": "Defendant", "required": True},
]


def authorize(client, draft):
    session = client.session
    session[CURRENT_DRAFT_SESSION_KEY] = draft.pk
    session["auth_tokens"] = {"TYLER-TOKEN-ILLINOIS": "token"}
    session["jurisdiction"] = "illinois"
    session.save()


@pytest.fixture
def people_draft(client, django_user_model):
    user = django_user_model.objects.create_user(username="people-user", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(
        user=user,
        jurisdiction="illinois",
        workflow_version=2,
        existing_case=ExistingCase.NEW,
        court_code="cook:cvd1",
        case_type_code="NC",
        case_type_name="Name Change",
        current_step=WorkflowStepKey.YOUR_INFORMATION,
    )
    client.force_login(user)
    authorize(client, draft)
    return draft


@pytest.mark.django_db
def test_your_information_persists_filer_contact(client, people_draft):
    response = client.post(
        reverse("your_information", kwargs={"jurisdiction": "illinois"}),
        {
            "first_name": "Jamie",
            "last_name": "Rivera",
            "address_line_1": "100 State Street",
            "city": "Chicago",
            "state": "IL",
            "zip_code": "60601",
            "email": "jamie@example.com",
            "phone": "312-555-0100",
        },
    )

    people_draft.refresh_from_db()
    filer = people_draft.parties.get(role="filer")
    assert response.status_code == 302
    assert response.url.partition("?")[0] == reverse("parties", kwargs={"jurisdiction": "illinois"})
    assert filer.first_name == "Jamie"
    assert filer.address_line_1 == "100 State Street"
    assert people_draft.current_step == WorkflowStepKey.PARTIES


@pytest.mark.django_db
def test_your_information_suffix_is_a_dropdown_fed_by_the_court(client, people_draft):
    response = client.get(reverse("your_information", kwargs={"jurisdiction": "illinois"}))

    assert response.status_code == 200
    content = response.content.decode()
    suffix_select = re.search(r"<select[^>]*id=\"suffix\"[^>]*>", content)
    assert suffix_select is not None
    assert 'data-court="cook:cvd1"' in content


@pytest.mark.django_db
def test_your_information_returns_to_review_when_edited_from_there(client, people_draft):
    response = client.post(
        reverse("your_information", kwargs={"jurisdiction": "illinois"}),
        {
            "first_name": "Jamie",
            "last_name": "Rivera",
            "address_line_1": "100 State Street",
            "city": "Chicago",
            "state": "IL",
            "zip_code": "60601",
            "email": "jamie@example.com",
            "return_to": "review",
        },
    )

    people_draft.refresh_from_db()
    assert response.status_code == 302
    assert response.url.partition("?")[0] == reverse("case_review", kwargs={"jurisdiction": "illinois"})
    assert people_draft.current_step == WorkflowStepKey.REVIEW


@pytest.mark.django_db
def test_guess_filer_party_type_suggests_the_initiator_for_a_new_case(people_draft):
    guess = guess_filer_party_type(people_draft, PARTY_TYPES)

    assert guess == {"code": "plaintiff", "name": "Plaintiff", "required": True}


@pytest.mark.django_db
def test_guess_filer_party_type_suggests_the_respondent_for_an_answer(people_draft):
    people_draft.existing_case = ExistingCase.EXISTING
    people_draft.save(update_fields=["existing_case", "updated_at"])
    reviewed_document(
        draft=people_draft,
        role=FilingDocument.Role.LEAD,
        name="answer.pdf",
        filing_type_name="Answer to Complaint",
    )

    guess = guess_filer_party_type(people_draft, PARTY_TYPES)

    assert guess == {"code": "defendant", "name": "Defendant", "required": True}


@pytest.mark.django_db
def test_guess_filer_party_type_is_none_without_a_clear_signal(people_draft):
    people_draft.existing_case = ExistingCase.EXISTING
    people_draft.save(update_fields=["existing_case", "updated_at"])

    assert guess_filer_party_type(people_draft, PARTY_TYPES) is None


@pytest.mark.django_db
def test_parties_page_offers_the_guess_as_a_button_not_a_prefill(client, people_draft):
    FilingParty.objects.create(draft=people_draft, role="filer", sort_order=0)

    with patch("efile.views.parties.get_party_types", return_value=PARTY_TYPES):
        response = client.get(reverse("parties", kwargs={"jurisdiction": "illinois"}))

    assert response.status_code == 200
    content = response.content.decode()
    assert 'id="apply-party-type-guess"' in content
    assert 'data-value="plaintiff"' in content
    plaintiff_radio = re.search(r'<input[^>]*name="filer_party_type"[^>]*value="plaintiff"[^>]*>', content)
    assert plaintiff_radio is not None
    assert "checked" not in plaintiff_radio.group()


@pytest.mark.django_db
def test_parties_page_hides_the_guess_once_a_role_is_chosen(client, people_draft):
    FilingParty.objects.create(draft=people_draft, role="filer", sort_order=0, party_type="defendant")

    with patch("efile.views.parties.get_party_types", return_value=PARTY_TYPES):
        response = client.get(reverse("parties", kwargs={"jurisdiction": "illinois"}))

    assert response.status_code == 200
    assert b'id="apply-party-type-guess"' not in response.content


@pytest.mark.django_db
def test_parties_creates_missing_required_party_and_repeats_details(client, people_draft):
    filer = FilingParty.objects.create(
        draft=people_draft,
        role="filer",
        sort_order=0,
        first_name="Jamie",
        last_name="Rivera",
        address_line_1="100 State Street",
        city="Chicago",
        state="IL",
        zip_code="60601",
    )

    with patch("efile.views.parties.get_party_types", return_value=PARTY_TYPES):
        response = client.post(
            reverse("parties", kwargs={"jurisdiction": "illinois"}),
            {"filer_party_type": "plaintiff"},
        )

    filer.refresh_from_db()
    other = people_draft.parties.get(role="other")
    assert response.status_code == 302
    assert response.url.endswith(f"?party={other.pk}&draft={people_draft.pk}")
    assert filer.party_type == "plaintiff"
    assert other.party_type == "defendant"
    assert other.party_type_name == "Defendant"


@pytest.mark.django_db
def test_parties_returns_to_review_when_edited_from_there(client, people_draft):
    FilingParty.objects.create(
        draft=people_draft,
        role="filer",
        sort_order=0,
        party_type="plaintiff",
        first_name="Jamie",
        last_name="Rivera",
        address_line_1="100 State Street",
        city="Chicago",
        state="IL",
        zip_code="60601",
    )
    FilingParty.objects.create(
        draft=people_draft,
        role="other",
        sort_order=0,
        party_type="defendant",
        party_type_name="Defendant",
        first_name="Morgan",
        last_name="Lee",
        address_line_1="200 Court Avenue",
        city="Chicago",
        state="IL",
        zip_code="60602",
    )

    with patch("efile.views.parties.get_party_types", return_value=PARTY_TYPES):
        response = client.post(
            reverse("parties", kwargs={"jurisdiction": "illinois"}),
            {"filer_party_type": "plaintiff", "return_to": "review"},
        )

    people_draft.refresh_from_db()
    assert response.status_code == 302
    assert response.url.partition("?")[0] == reverse("case_review", kwargs={"jurisdiction": "illinois"})
    assert people_draft.current_step == WorkflowStepKey.REVIEW


@pytest.mark.django_db
def test_party_details_suffix_is_a_dropdown_fed_by_the_court(client, people_draft):
    party = FilingParty.objects.create(draft=people_draft, role="other", sort_order=0)

    with patch("efile.views.party_details.get_party_types", return_value=PARTY_TYPES):
        response = client.get(f"{reverse('party_details', kwargs={'jurisdiction': 'illinois'})}?party={party.pk}")

    assert response.status_code == 200
    content = response.content.decode()
    suffix_select = re.search(r"<select[^>]*id=\"suffix\"[^>]*>", content)
    assert suffix_select is not None
    assert 'data-court="cook:cvd1"' in content


@pytest.mark.django_db
def test_party_details_saves_party_and_advances_to_payment_when_no_questions(client, people_draft):
    FilingParty.objects.create(
        draft=people_draft,
        role="filer",
        sort_order=0,
        party_type="plaintiff",
        first_name="Jamie",
        last_name="Rivera",
        address_line_1="100 State Street",
        city="Chicago",
        state="IL",
        zip_code="60601",
    )
    party = FilingParty.objects.create(
        draft=people_draft,
        role="other",
        sort_order=0,
        party_type="defendant",
        party_type_name="Defendant",
    )

    with patch("efile.views.party_details.get_party_types", return_value=PARTY_TYPES):
        response = client.post(
            f"{reverse('party_details', kwargs={'jurisdiction': 'illinois'})}?party={party.pk}",
            {
                "party_kind": "person",
                "party_type": "defendant",
                "first_name": "Morgan",
                "last_name": "Lee",
                "address_line_1": "200 Court Avenue",
                "city": "Chicago",
                "state": "IL",
                "zip_code": "60602",
            },
        )

    people_draft.refresh_from_db()
    party.refresh_from_db()
    assert response.status_code == 302
    assert response.url.partition("?")[0] == reverse("payment", kwargs={"jurisdiction": "illinois"})
    assert party.first_name == "Morgan"
    assert people_draft.current_step == WorkflowStepKey.PAYMENT


@pytest.mark.django_db
def test_party_details_saves_an_other_party_without_an_optional_address(client, people_draft):
    FilingParty.objects.create(
        draft=people_draft,
        role="filer",
        sort_order=0,
        party_type="plaintiff",
        first_name="Jamie",
        last_name="Rivera",
        address_line_1="100 State Street",
        city="Chicago",
        state="IL",
        zip_code="60601",
    )
    party = FilingParty.objects.create(
        draft=people_draft,
        role="other",
        sort_order=0,
        party_type="defendant",
        party_type_name="Defendant",
    )

    with patch("efile.views.party_details.get_party_types", return_value=PARTY_TYPES):
        response = client.post(
            f"{reverse('party_details', kwargs={'jurisdiction': 'illinois'})}?party={party.pk}",
            {
                "party_kind": "person",
                "party_type": "defendant",
                "first_name": "Morgan",
                "last_name": "Lee",
            },
        )

    party.refresh_from_db()
    assert response.status_code == 302
    assert response.url.partition("?")[0] == reverse("payment", kwargs={"jurisdiction": "illinois"})
    assert party.first_name == "Morgan"
    assert party.address_line_1 == ""


@pytest.mark.django_db
def test_party_details_does_not_discard_a_saved_optional_address(client, people_draft):
    party = FilingParty.objects.create(
        draft=people_draft,
        role="other",
        sort_order=0,
        party_type="defendant",
        party_type_name="Defendant",
        first_name="Morgan",
        last_name="Lee",
        address_line_1="200 Court Avenue",
        city="Chicago",
        state="IL",
        zip_code="60602",
    )

    with patch("efile.views.party_details.get_party_types", return_value=PARTY_TYPES):
        response = client.get(f"{reverse('party_details', kwargs={'jurisdiction': 'illinois'})}?party={party.pk}")

    content = response.content.decode()
    assert 'value="200 Court Avenue"' in content
    assert 'value="60602"' in content
    address_toggle = re.search(r'<input[^>]*id="add-party-address"[^>]*>', content)
    assert address_toggle is not None
    assert "checked" in address_toggle.group()
    address_fields = re.search(r'<div[^>]*id="party-address-fields"[^>]*>', content)
    assert address_fields is not None
    assert "hidden" not in address_fields.group()


@pytest.mark.django_db
def test_optional_address_fields_start_hidden_with_checkbox_and_help(client, people_draft):
    party = FilingParty.objects.create(
        draft=people_draft,
        role="other",
        sort_order=0,
        party_type="defendant",
        party_type_name="Defendant",
        first_name="Morgan",
        last_name="Lee",
    )

    with patch("efile.views.party_details.get_party_types", return_value=PARTY_TYPES):
        response = client.get(f"{reverse('party_details', kwargs={'jurisdiction': 'illinois'})}?party={party.pk}")

    content = response.content.decode()
    address_toggle = re.search(r'<input[^>]*id="add-party-address"[^>]*>', content)
    assert address_toggle is not None
    assert "checked" not in address_toggle.group()
    address_fields = re.search(r'<div[^>]*id="party-address-fields"[^>]*>', content)
    assert address_fields is not None
    assert "hidden" in address_fields.group()
    assert "Do I need to list an address?" in content
    assert "Many filings do not require you to list an address for the opposing party" in content


@pytest.mark.django_db
def test_party_details_rejects_a_partial_optional_address(client, people_draft):
    party = FilingParty.objects.create(draft=people_draft, role="other", sort_order=0)

    with patch("efile.views.party_details.get_party_types", return_value=PARTY_TYPES):
        response = client.post(
            f"{reverse('party_details', kwargs={'jurisdiction': 'illinois'})}?party={party.pk}",
            {
                "party_kind": "person",
                "party_type": "defendant",
                "first_name": "Morgan",
                "last_name": "Lee",
                "state": "IL",
            },
        )

    assert response.status_code == 200
    assert b"Complete the optional mailing address" in response.content


@pytest.mark.django_db
def test_party_details_explains_a_configured_required_address(client, people_draft):
    party = FilingParty.objects.create(draft=people_draft, role="other", sort_order=0)
    requirement = AddressRequirement(True, "The court needs this address for service.")

    with (
        patch("efile.views.party_details.get_party_types", return_value=PARTY_TYPES),
        patch("efile.views.party_details.party_address_requirement", return_value=requirement),
    ):
        response = client.get(f"{reverse('party_details', kwargs={'jurisdiction': 'illinois'})}?party={party.pk}")

    content = response.content.decode()
    assert "The court needs this address for service." in content
    street = re.search(r'<input[^>]*name="address_line_1"[^>]*>', content)
    assert street is not None
    assert "required" in street.group()
    assert 'id="add-party-address"' not in content
    address_fields = re.search(r'<div[^>]*id="party-address-fields"[^>]*>', content)
    assert address_fields is not None
    assert "hidden" not in address_fields.group()


@pytest.mark.django_db
def test_party_details_returns_to_review_when_edited_from_there(client, people_draft):
    FilingParty.objects.create(
        draft=people_draft,
        role="filer",
        sort_order=0,
        party_type="plaintiff",
        first_name="Jamie",
        last_name="Rivera",
        address_line_1="100 State Street",
        city="Chicago",
        state="IL",
        zip_code="60601",
    )
    party = FilingParty.objects.create(
        draft=people_draft,
        role="other",
        sort_order=0,
        party_type="defendant",
        party_type_name="Defendant",
    )

    with patch("efile.views.party_details.get_party_types", return_value=PARTY_TYPES):
        response = client.post(
            f"{reverse('party_details', kwargs={'jurisdiction': 'illinois'})}?party={party.pk}",
            {
                "party_kind": "person",
                "party_type": "defendant",
                "first_name": "Morgan",
                "last_name": "Lee",
                "address_line_1": "200 Court Avenue",
                "city": "Chicago",
                "state": "IL",
                "zip_code": "60602",
                "return_to": "review",
            },
        )

    people_draft.refresh_from_db()
    assert response.status_code == 302
    assert response.url.partition("?")[0] == reverse("case_review", kwargs={"jurisdiction": "illinois"})
    assert people_draft.current_step == WorkflowStepKey.REVIEW


@pytest.mark.django_db
def test_case_questions_are_configured_and_saved(client, people_draft):
    people_draft.case_type_name = "Dissolution (with children)"
    people_draft.current_step = WorkflowStepKey.CASE_QUESTIONS
    people_draft.save(update_fields=["case_type_name", "current_step", "updated_at"])

    response = client.post(
        reverse("case_questions", kwargs={"jurisdiction": "illinois"}),
        {"has_children": "true", "child_count": "2"},
    )

    people_draft.refresh_from_db()
    assert response.status_code == 302
    assert response.url.partition("?")[0] == reverse("payment", kwargs={"jurisdiction": "illinois"})
    assert people_draft.supplemental_fields["has_children"] is True
    assert people_draft.supplemental_fields["child_count"] == 2
    assert people_draft.supplemental_fields["_case_questions_required"] is True


@pytest.mark.django_db
def test_case_questions_returns_to_review_when_edited_from_there(client, people_draft):
    people_draft.case_type_name = "Dissolution (with children)"
    people_draft.current_step = WorkflowStepKey.CASE_QUESTIONS
    people_draft.save(update_fields=["case_type_name", "current_step", "updated_at"])

    response = client.post(
        reverse("case_questions", kwargs={"jurisdiction": "illinois"}),
        {"has_children": "true", "child_count": "2", "return_to": "review"},
    )

    people_draft.refresh_from_db()
    assert response.status_code == 302
    assert response.url.partition("?")[0] == reverse("case_review", kwargs={"jurisdiction": "illinois"})
    assert people_draft.current_step == WorkflowStepKey.REVIEW


@pytest.mark.django_db
def test_case_questions_asks_for_amount_in_controversy_with_no_other_questions(client, people_draft):
    """The early "nothing to ask, skip ahead" exit used to fire even when a
    document's filing type required an amount in controversy, since it only
    checked the config-driven questions list."""
    reviewed_document(
        draft=people_draft,
        role=FilingDocument.Role.LEAD,
        name="petition.pdf",
        filing_requires_amount_in_controversy=True,
    )
    people_draft.current_step = WorkflowStepKey.CASE_QUESTIONS
    people_draft.save(update_fields=["current_step", "updated_at"])

    response = client.get(reverse("case_questions", kwargs={"jurisdiction": "illinois"}))

    assert response.status_code == 200
    assert b"Amount in controversy" in response.content


@pytest.mark.django_db
@pytest.mark.parametrize("saved_amount", ["", "500.00"])
def test_extracted_amount_is_only_a_suggestion_until_confirmed(client, people_draft, saved_amount):
    document = reviewed_document(
        draft=people_draft,
        role=FilingDocument.Role.LEAD,
        name="petition.pdf",
        filing_requires_amount_in_controversy=True,
    )
    DocumentExtraction.objects.create(
        document=document,
        status=DocumentExtraction.Status.COMPLETE,
        evidence={"monetary amounts": [{"label": "Amount in controversy", "amount": "1275.00"}]},
    )
    people_draft.amount_in_controversy = saved_amount
    people_draft.save(update_fields=["amount_in_controversy"])
    url = reverse("case_questions", kwargs={"jurisdiction": "illinois"})
    response = client.get(url)
    assert response.context["amount_in_controversy"] == (saved_amount or "1275.00")
    people_draft.refresh_from_db()
    assert people_draft.amount_in_controversy == saved_amount

    # A rejected submission must retain what was typed, not restore the guess.
    response = client.post(url, {"amount_in_controversy": "invalid amount"})
    assert response.context["amount_in_controversy"] == "invalid amount"
    people_draft.refresh_from_db()
    assert people_draft.amount_in_controversy == saved_amount

    response = client.post(url, {"amount_in_controversy": "1200.00"})
    assert response.status_code == 302
    people_draft.refresh_from_db()
    assert people_draft.amount_in_controversy == "1200.00"


@pytest.mark.django_db
def test_case_questions_saves_a_valid_amount_in_controversy(client, people_draft):
    reviewed_document(
        draft=people_draft,
        role=FilingDocument.Role.LEAD,
        name="petition.pdf",
        filing_requires_amount_in_controversy=True,
    )
    people_draft.current_step = WorkflowStepKey.CASE_QUESTIONS
    people_draft.save(update_fields=["current_step", "updated_at"])

    response = client.post(
        reverse("case_questions", kwargs={"jurisdiction": "illinois"}),
        {"amount_in_controversy": "$12,500.00"},
    )

    people_draft.refresh_from_db()
    assert response.status_code == 302
    assert response.url.partition("?")[0] == reverse("payment", kwargs={"jurisdiction": "illinois"})
    assert people_draft.amount_in_controversy == "12500.00"


@pytest.mark.django_db
def test_case_questions_rejects_a_missing_or_invalid_amount(client, people_draft):
    reviewed_document(
        draft=people_draft,
        role=FilingDocument.Role.LEAD,
        name="petition.pdf",
        filing_requires_amount_in_controversy=True,
    )
    people_draft.current_step = WorkflowStepKey.CASE_QUESTIONS
    people_draft.save(update_fields=["current_step", "updated_at"])

    response = client.post(
        reverse("case_questions", kwargs={"jurisdiction": "illinois"}),
        {"amount_in_controversy": "not a number"},
    )

    people_draft.refresh_from_db()
    assert response.status_code == 200
    assert people_draft.amount_in_controversy == ""
    assert people_draft.current_step == WorkflowStepKey.CASE_QUESTIONS


@pytest.mark.django_db
def test_parties_routes_to_case_questions_when_amount_in_controversy_is_needed(client, people_draft):
    """Even with no config-driven case-type questions, a document that needs
    an amount in controversy must still route through case_questions instead
    of straight to payment."""
    FilingParty.objects.create(
        draft=people_draft,
        role="filer",
        sort_order=0,
        party_type="plaintiff",
        first_name="Jamie",
        last_name="Rivera",
        address_line_1="100 State Street",
        city="Chicago",
        state="IL",
        zip_code="60601",
    )
    FilingParty.objects.create(
        draft=people_draft,
        role="other",
        sort_order=0,
        party_type="defendant",
        party_type_name="Defendant",
        first_name="Morgan",
        last_name="Lee",
        address_line_1="200 Court Avenue",
        city="Chicago",
        state="IL",
        zip_code="60602",
    )
    reviewed_document(
        draft=people_draft,
        role=FilingDocument.Role.LEAD,
        name="petition.pdf",
        filing_requires_amount_in_controversy=True,
    )

    with patch("efile.views.parties.get_party_types", return_value=PARTY_TYPES):
        response = client.post(
            reverse("parties", kwargs={"jurisdiction": "illinois"}),
            {"filer_party_type": "plaintiff"},
        )

    people_draft.refresh_from_db()
    assert response.status_code == 302
    assert response.url.partition("?")[0] == reverse("case_questions", kwargs={"jurisdiction": "illinois"})
    assert people_draft.current_step == WorkflowStepKey.CASE_QUESTIONS


@pytest.mark.django_db
def test_new_parties_are_available_to_legacy_payload_bridge(people_draft):
    FilingParty.objects.create(
        draft=people_draft,
        role="filer",
        sort_order=0,
        party_type="plaintiff",
        party_type_name="Plaintiff",
        first_name="Jamie",
        last_name="Rivera",
        address_line_1="100 State Street",
        city="Chicago",
        state="IL",
        zip_code="60601",
    )

    data = read_case_data(people_draft)

    assert data["party_type"] == "plaintiff"
    assert data["petitioner_first_name"] == "Jamie"
    assert data["filing_parties"][0]["party_type_name"] == "Plaintiff"


ACCOUNT_PROFILE = {
    "first_name": "Robin",
    "last_name": "Ellis",
    "address": "500 Main Street",
    "address_line2": "",
    "city": "Burlington",
    "state": "VT",
    "zip": "05401",
    "phone": "802-555-0100",
}


@pytest.mark.django_db
def test_your_information_fills_the_account_details_before_the_page_renders(client, people_draft):
    """The filer should not watch their own name arrive a second after the form
    does, and wonder why the system they just signed in to does not know them."""

    with patch("efile.views.your_information.cached_account_profile", return_value=ACCOUNT_PROFILE):
        response = client.get(reverse("your_information", kwargs={"jurisdiction": "illinois"}))

    content = response.content.decode()
    assert response.status_code == 200
    assert 'value="Robin"' in content
    assert 'value="500 Main Street"' in content
    # Having rendered them, the page has no reason to fetch them again.
    assert 'data-profile-prefilled="1"' in content


@pytest.mark.django_db
def test_your_information_keeps_draft_values_over_account_values(client, people_draft):
    FilingParty.objects.create(
        draft=people_draft,
        role="filer",
        sort_order=0,
        first_name="Sam",
        city="Chicago",
    )

    with patch("efile.views.your_information.cached_account_profile", return_value=ACCOUNT_PROFILE):
        response = client.get(reverse("your_information", kwargs={"jurisdiction": "illinois"}))

    content = response.content.decode()
    assert 'value="Sam"' in content
    assert 'value="Chicago"' in content
    assert 'value="Robin"' not in content
    # Fields the filer has not answered yet still come from the account.
    assert 'value="500 Main Street"' in content


@pytest.mark.django_db
def test_your_information_leaves_the_form_usable_when_the_account_cannot_be_read(client, people_draft):
    """A court that does not answer must not block the page; the filer types it
    in, and the script is left to try the fetch itself."""

    with patch("efile.views.your_information.cached_account_profile", return_value=None):
        response = client.get(reverse("your_information", kwargs={"jurisdiction": "illinois"}))

    assert response.status_code == 200
    assert 'data-profile-prefilled="0"' in response.content.decode()


@pytest.mark.django_db
@pytest.mark.parametrize(("jurisdiction", "state_code"), [("illinois", "IL"), ("vermont", "VT")])
def test_your_information_defaults_the_state_to_this_jurisdiction(client, django_user_model, jurisdiction, state_code):
    user = django_user_model.objects.create_user(username=f"{jurisdiction}-filer", tyler_jurisdiction=jurisdiction)
    draft = FilingDraft.objects.create(
        user=user,
        jurisdiction=jurisdiction,
        workflow_version=2,
        existing_case=ExistingCase.NEW,
        court_code="court",
        current_step=WorkflowStepKey.YOUR_INFORMATION,
    )
    client.force_login(user)
    session = client.session
    session[CURRENT_DRAFT_SESSION_KEY] = draft.pk
    session["auth_tokens"] = {f"TYLER-TOKEN-{jurisdiction.upper()}": "token"}
    session["jurisdiction"] = jurisdiction
    session.save()

    with patch("efile.views.your_information.cached_account_profile", return_value=None):
        response = client.get(reverse("your_information", kwargs={"jurisdiction": jurisdiction}))

    content = response.content.decode()
    assert re.search(r'<select[^>]*name="state"[^>]*>', content)
    assert re.search(rf'<option value="{state_code}"\s+selected', content)


@pytest.mark.django_db
def test_your_information_asks_to_check_contact_details(client, people_draft):
    with patch("efile.views.your_information.cached_account_profile", return_value=None):
        response = client.get(reverse("your_information", kwargs={"jurisdiction": "illinois"}))

    content = response.content.decode()
    assert "Check your name and contact information." in content
    assert "eFile account" not in content


@pytest.mark.django_db
def test_the_role_question_is_asked_the_way_other_primary_questions_are(client, people_draft):
    """It was small indented text above a large blue answer area, and testers
    read past it."""

    FilingParty.objects.create(draft=people_draft, role="filer", sort_order=0)

    with patch("efile.views.parties.get_party_types", return_value=PARTY_TYPES):
        response = client.get(reverse("parties", kwargs={"jurisdiction": "illinois"}))

    content = response.content.decode()
    assert 'class="form-field primary-question"' in content
    assert "What is your role in this case?" in content
    assert "Look for your role next to your name on the court documents." in content
    assert "I am not one of the parties — I am filing for someone else" in content


def _filer_and_other(draft, *, filer_type="plaintiff", other_type=""):
    FilingParty.objects.create(
        draft=draft,
        role="filer",
        sort_order=0,
        party_type=filer_type,
        party_type_name="Plaintiff" if filer_type == "plaintiff" else "",
        first_name="Jamie",
        last_name="Rivera",
        address_line_1="100 State Street",
        city="Chicago",
        state="IL",
        zip_code="60601",
    )
    return FilingParty.objects.create(draft=draft, role="other", sort_order=1, party_type=other_type)


def _post_party(client, party, **fields):
    data = {"party_kind": "person", "first_name": "Morgan", "last_name": "Lee", **fields}
    with patch("efile.views.party_details.get_party_types", return_value=PARTY_TYPES):
        return client.post(f"{reverse('party_details', kwargs={'jurisdiction': 'illinois'})}?party={party.pk}", data)


@pytest.mark.django_db
def test_giving_another_party_the_filers_role_asks_whether_they_are_on_the_filers_side(client, people_draft):
    party = _filer_and_other(people_draft)

    response = _post_party(client, party, party_type="plaintiff", email="morgan@example.com")

    party.refresh_from_db()
    assert response.status_code == 200
    assert party.party_type == ""
    content = response.content.decode()
    confirm = re.search(r'<div class="same-role-confirm[^"]*"\s+id="same-role-confirm"[^>]*>', content)
    assert confirm is not None
    assert "hidden" not in confirm.group()
    assert re.search(r'<p class="field-error"\s+id="same-role-error"\s*>', content)
    assert re.search(r'id="same_role_confirmed"[^>]*aria-invalid="true"', content, re.S)
    # Nothing they typed is lost.
    assert 'value="Morgan"' in content
    assert 'value="morgan@example.com"' in content
    assert re.search(r'value="plaintiff"\s+checked', content)


@pytest.mark.django_db
def test_a_confirmed_co_party_can_share_the_filers_role(client, people_draft):
    party = _filer_and_other(people_draft)

    response = _post_party(client, party, party_type="plaintiff", same_role_confirmed="yes")

    party.refresh_from_db()
    assert response.status_code == 302
    assert party.party_type == "plaintiff"


@pytest.mark.django_db
def test_a_different_role_needs_no_confirmation(client, people_draft):
    party = _filer_and_other(people_draft)

    response = _post_party(client, party, party_type="defendant")

    party.refresh_from_db()
    assert response.status_code == 302
    assert party.party_type == "defendant"


@pytest.mark.django_db
def test_a_filer_who_is_not_a_party_is_never_asked(client, people_draft):
    party = _filer_and_other(people_draft, filer_type="")

    with patch("efile.views.party_details.get_party_types", return_value=PARTY_TYPES):
        page = client.get(f"{reverse('party_details', kwargs={'jurisdiction': 'illinois'})}?party={party.pk}")
    assert 'id="same-role-confirm"' not in page.content.decode()

    response = _post_party(client, party, party_type="plaintiff")
    assert response.status_code == 302


@pytest.mark.django_db
def test_a_party_already_saved_with_the_filers_role_starts_confirmed(client, people_draft):
    party = _filer_and_other(people_draft, other_type="plaintiff")

    with patch("efile.views.party_details.get_party_types", return_value=PARTY_TYPES):
        page = client.get(f"{reverse('party_details', kwargs={'jurisdiction': 'illinois'})}?party={party.pk}")

    content = page.content.decode()
    confirm = re.search(r'<div class="same-role-confirm[^"]*"\s+id="same-role-confirm"[^>]*>', content)
    assert confirm is not None and "hidden" not in confirm.group()
    assert re.search(r'id="same_role_confirmed"[^>]*checked', content, re.S)


@pytest.mark.django_db
def test_party_details_removes_the_party_and_keeps_the_review_detour(client, people_draft):
    FilingParty.objects.create(
        draft=people_draft,
        role="filer",
        sort_order=0,
        party_type="plaintiff",
        first_name="Jamie",
        last_name="Rivera",
        address_line_1="100 State Street",
        city="Chicago",
        state="IL",
        zip_code="60601",
    )
    party = FilingParty.objects.create(draft=people_draft, role="other", sort_order=0, first_name="Duplicate")
    url = reverse("party_details", kwargs={"jurisdiction": "illinois"})
    parties_url = reverse("parties", kwargs={"jurisdiction": "illinois"})

    with patch("efile.views.party_details.get_party_types", return_value=PARTY_TYPES):
        page = client.get(f"{url}?party={party.pk}&return_to=review").content.decode()
    remove_form = re.search(rf'<form method="post" action="{parties_url}">(.*?)</form>', page, re.S)
    assert remove_form is not None
    assert 'name="action" value="remove"' in remove_form.group(1)
    assert f'name="party_id" value="{party.pk}"' in remove_form.group(1)
    assert 'name="return_to" value="review"' in remove_form.group(1)

    with patch("efile.views.parties.get_party_types", return_value=PARTY_TYPES):
        response = client.post(parties_url, {"action": "remove", "party_id": party.pk, "return_to": "review"})

    assert response.status_code == 302
    path, _, query = response.url.partition("?")
    assert path == parties_url
    assert "return_to=review" in query
    assert not FilingParty.objects.filter(pk=party.pk).exists()

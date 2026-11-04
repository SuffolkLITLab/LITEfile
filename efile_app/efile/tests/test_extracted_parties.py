"""Names read off a document become parties on the right side of the case."""

import re
from unittest.mock import patch

import pytest
from django.urls import reverse

from efile.models import FilingDocument, FilingDraft, FilingParty
from efile.party_sides import PartySide
from efile.services.current_drafts import CURRENT_DRAFT_SESSION_KEY
from efile.services.extracted_parties import (
    extracted_party_suggestions,
    looks_like_organization,
    save_reviewed_parties,
    split_person_name,
)
from efile.services.people import (
    absorb_filer_duplicates,
    apply_party_sides,
    filer_name_match,
    guess_filer_party_type,
    match_party_type,
)
from efile.tests.helpers import reviewed_document
from efile.workflow import ExistingCase, WorkflowStepKey

PARTY_TYPES = [
    {"code": "cross", "name": "Cross-Defendant", "required": False},
    {"code": "plaintiff", "name": "Plaintiff/Petitioner", "required": True},
    {"code": "defendant", "name": "Defendant", "required": True},
    {"code": "gal", "name": "Guardian Ad Litem", "required": False},
]

GUESSES = {
    "document title": "Complaint",
    "plaintiff or petitioner names": "Alex Rivera; Riverbend Properties LLC",
    "defendant or respondent names": "Morgan Lee",
    "other party names": "Pat Lee (Guardian ad Litem)",
}


def authorize(client, draft):
    client.force_login(draft.user)
    session = client.session
    session[CURRENT_DRAFT_SESSION_KEY] = draft.pk
    session["auth_tokens"] = {f"TYLER-TOKEN-{draft.jurisdiction.upper()}": "token"}
    session["jurisdiction"] = draft.jurisdiction
    session.save()


@pytest.fixture
def review_draft(db, django_user_model):
    user = django_user_model.objects.create_user(username="party-user", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(
        user=user,
        jurisdiction="illinois",
        workflow_version=2,
        current_step=WorkflowStepKey.EXTRACTION_REVIEW,
        extracted_guesses=dict(GUESSES),
    )
    reviewed_document(draft=draft, role=FilingDocument.Role.LEAD, name="complaint.pdf")
    return draft


def test_suggestions_carry_a_side_without_knowing_the_case_type():
    suggestions = extracted_party_suggestions(GUESSES)

    assert suggestions == [
        {"name": "Alex Rivera", "role_hint": "", "side": PartySide.INITIATING},
        {"name": "Riverbend Properties LLC", "role_hint": "", "side": PartySide.INITIATING},
        {"name": "Morgan Lee", "role_hint": "", "side": PartySide.RESPONDING},
        {"name": "Pat Lee", "role_hint": "Guardian ad Litem", "side": PartySide.OTHER},
    ]


def test_suggestions_split_the_ways_documents_actually_join_names():
    suggestions = extracted_party_suggestions({"defendant or respondent names": "Morgan Lee and Sam Lee & Casey Lee"})

    assert [item["name"] for item in suggestions] == ["Morgan Lee", "Sam Lee", "Casey Lee"]


def test_a_repeated_name_on_one_side_is_listed_once():
    suggestions = extracted_party_suggestions({"plaintiff or petitioner names": "Alex Rivera; alex  rivera"})

    assert [item["name"] for item in suggestions] == ["Alex Rivera"]


@pytest.mark.parametrize(
    ("name", "is_organization"),
    [
        ("Riverbend Properties LLC", True),
        ("City of Chicago", True),
        ("Alex Rivera", False),
        ("Ruth Bank", True),
    ],
)
def test_organizations_are_told_apart_from_people(name, is_organization):
    assert looks_like_organization(name) is is_organization


def test_a_suffix_stays_with_the_last_name_rather_than_being_dropped():
    """The suffix field takes one of the court's own codes, so a suffix read
    off a document has nowhere valid to go -- but losing it silently is worse
    than a split the filer can correct."""

    assert split_person_name("Alex T. Rivera Jr.") == {
        "first_name": "Alex",
        "middle_name": "T.",
        "last_name": "Rivera Jr.",
    }


def test_match_prefers_the_plain_party_type_over_a_compound_one():
    match = match_party_type(PARTY_TYPES, ["defendant"])

    assert match is not None
    assert match["code"] == "defendant"


@pytest.mark.django_db
def test_review_screen_only_preselects_captioned_case_parties(client, review_draft):
    authorize(client, review_draft)

    content = client.get(reverse("extraction_review", kwargs={"jurisdiction": "illinois"})).content.decode()

    listing = re.search(r'id="review-parties-list">(.*?)</ol>', content, re.S)
    assert listing is not None
    listed = listing.group(1)
    rows = re.findall(r'name="party_name"\s+value="([^"]*)"', listed)
    assert rows == ["Alex Rivera", "Riverbend Properties LLC", "Morgan Lee"]
    chosen = re.findall(r'<option value="([a-z]+)"\s*selected>', listed)
    assert chosen == ["initiating", "initiating", "responding"]
    assert "Guardian ad Litem" not in listed
    assert "Pat Lee (Guardian ad Litem)" in content


@pytest.mark.django_db
def test_review_screen_saves_the_names_as_sides_not_court_codes(client, review_draft):
    authorize(client, review_draft)

    response = client.post(
        reverse("extraction_review", kwargs={"jurisdiction": "illinois"}),
        {
            "existing_case": ExistingCase.NEW,
            "court_code": "cook:cvd1",
            "case_category_code": "civil",
            "case_type_code": "NC",
            "party_id": ["", ""],
            "party_name": ["Alexis Rivera", "Morgan Lee"],
            "party_side": [PartySide.INITIATING, PartySide.RESPONDING],
            "party_role_hint": ["", ""],
        },
    )

    parties = list(FilingParty.objects.filter(draft=review_draft, role="other").order_by("sort_order"))
    assert response.status_code == 302
    assert [(party.first_name, party.last_name, party.party_side) for party in parties] == [
        ("Alexis", "Rivera", PartySide.INITIATING),
        ("Morgan", "Lee", PartySide.RESPONDING),
    ]
    # The court's own codes are not knowable from a document, and are left for
    # the party screen to resolve once the case type has been fetched.
    assert {party.party_type for party in parties} == {""}


@pytest.mark.django_db
def test_review_screen_keeps_an_address_while_a_name_is_corrected(client, review_draft):
    authorize(client, review_draft)
    party = FilingParty.objects.create(
        draft=review_draft,
        role="other",
        sort_order=0,
        first_name="Morgan",
        last_name="Lee",
        party_side=PartySide.RESPONDING,
        address_line_1="200 Court Avenue",
        city="Chicago",
        state="IL",
        zip_code="60602",
    )

    client.post(
        reverse("extraction_review", kwargs={"jurisdiction": "illinois"}),
        {
            "existing_case": ExistingCase.NEW,
            "court_code": "cook:cvd1",
            "case_category_code": "civil",
            "case_type_code": "NC",
            "party_id": [str(party.pk)],
            "party_name": ["Morgan Leigh"],
            "party_side": [PartySide.RESPONDING],
            "party_role_hint": [""],
        },
    )

    party.refresh_from_db()
    assert party.last_name == "Leigh"
    assert party.address_line_1 == "200 Court Avenue"


@pytest.mark.django_db
def test_review_screen_deletes_a_party_the_filer_removed(client, review_draft):
    authorize(client, review_draft)
    kept = FilingParty.objects.create(
        draft=review_draft, role="other", sort_order=0, first_name="Morgan", last_name="Lee"
    )
    removed = FilingParty.objects.create(
        draft=review_draft, role="other", sort_order=1, first_name="Sam", last_name="Lee"
    )

    client.post(
        reverse("extraction_review", kwargs={"jurisdiction": "illinois"}),
        {
            "existing_case": ExistingCase.NEW,
            "court_code": "cook:cvd1",
            "case_category_code": "civil",
            "case_type_code": "NC",
            "party_id": [str(kept.pk)],
            "party_name": ["Morgan Lee"],
            "party_side": [PartySide.RESPONDING],
            "party_role_hint": [""],
        },
    )

    assert FilingParty.objects.filter(pk=removed.pk).count() == 0
    assert FilingParty.objects.filter(pk=kept.pk).count() == 1


@pytest.mark.django_db
def test_review_screen_keeps_typed_names_when_the_case_answer_is_missing(client, review_draft):
    """A validation error must not cost the filer the names they just fixed."""

    authorize(client, review_draft)

    response = client.post(
        reverse("extraction_review", kwargs={"jurisdiction": "illinois"}),
        {
            "party_id": [""],
            "party_name": ["Alexis Rivera"],
            "party_side": [PartySide.INITIATING],
            "party_role_hint": [""],
        },
    )

    assert response.status_code == 200
    assert 'value="Alexis Rivera"' in response.content.decode()
    assert FilingParty.objects.filter(draft=review_draft).count() == 0


@pytest.mark.django_db
def test_sides_become_this_courts_party_types(review_draft):
    initiating = FilingParty.objects.create(
        draft=review_draft,
        role="other",
        sort_order=0,
        first_name="Alex",
        last_name="Rivera",
        party_side=PartySide.INITIATING,
    )
    responding = FilingParty.objects.create(
        draft=review_draft,
        role="other",
        sort_order=1,
        first_name="Morgan",
        last_name="Lee",
        party_side=PartySide.RESPONDING,
    )
    guardian = FilingParty.objects.create(
        draft=review_draft,
        role="other",
        sort_order=2,
        first_name="Pat",
        last_name="Lee",
        party_side=PartySide.OTHER,
        party_role_hint="Guardian ad Litem",
    )

    apply_party_sides(review_draft, PARTY_TYPES)

    for party in (initiating, responding, guardian):
        party.refresh_from_db()
    assert initiating.party_type == "plaintiff"
    assert initiating.party_type_name == "Plaintiff/Petitioner"
    assert responding.party_type == "defendant"
    assert guardian.party_type == "gal"


@pytest.mark.django_db
def test_a_party_type_the_filer_already_chose_is_never_overwritten(review_draft):
    party = FilingParty.objects.create(
        draft=review_draft,
        role="other",
        sort_order=0,
        first_name="Alex",
        last_name="Rivera",
        party_side=PartySide.INITIATING,
        party_type="cross",
        party_type_name="Cross-Defendant",
    )

    apply_party_sides(review_draft, PARTY_TYPES)

    party.refresh_from_db()
    assert party.party_type == "cross"


@pytest.mark.django_db
def test_the_filer_is_not_also_filed_as_a_second_person_of_the_same_name(review_draft):
    filer = FilingParty.objects.create(
        draft=review_draft,
        role="filer",
        sort_order=0,
        first_name="Alex",
        last_name="Rivera",
        party_type="plaintiff",
        party_type_name="Plaintiff/Petitioner",
    )
    FilingParty.objects.create(
        draft=review_draft,
        role="other",
        sort_order=0,
        first_name="Alex",
        last_name="Rivera",
        party_side=PartySide.INITIATING,
    )
    FilingParty.objects.create(
        draft=review_draft,
        role="other",
        sort_order=1,
        first_name="Morgan",
        last_name="Lee",
        party_side=PartySide.RESPONDING,
    )

    side = absorb_filer_duplicates(review_draft)

    filer.refresh_from_db()
    assert side == PartySide.INITIATING
    assert filer.party_side == PartySide.INITIATING
    assert [party.last_name for party in FilingParty.objects.filter(draft=review_draft, role="other")] == ["Lee"]


@pytest.mark.django_db
def test_a_caption_name_matching_a_filer_who_is_not_a_party_is_kept(review_draft):
    """Two people share a name, and someone filing for a relative they are
    named after is the whole reason that answer exists. Deleting the party on
    a name alone would take a real party out of the case."""

    FilingParty.objects.create(draft=review_draft, role="filer", sort_order=0, first_name="Alex", last_name="Rivera")
    named = FilingParty.objects.create(
        draft=review_draft,
        role="other",
        sort_order=0,
        first_name="Alex",
        last_name="Rivera",
        party_side=PartySide.INITIATING,
    )

    side = absorb_filer_duplicates(review_draft)

    assert side == PartySide.INITIATING
    assert FilingParty.objects.filter(pk=named.pk).exists()
    assert filer_name_match(review_draft) == named


@pytest.mark.django_db
def test_the_documents_own_answer_beats_the_case_posture_guess(review_draft):
    """A new case is usually opened by the plaintiff, but not this one: the
    document named the filer among the defendants."""

    review_draft.existing_case = ExistingCase.NEW
    review_draft.save(update_fields=["existing_case", "updated_at"])
    FilingParty.objects.create(draft=review_draft, role="filer", sort_order=0, first_name="Morgan", last_name="Lee")
    FilingParty.objects.create(
        draft=review_draft,
        role="other",
        sort_order=0,
        first_name="Morgan",
        last_name="Lee",
        party_side=PartySide.RESPONDING,
    )

    guess = guess_filer_party_type(review_draft, PARTY_TYPES)

    assert guess is not None
    assert guess["code"] == "defendant"


@pytest.mark.django_db
def test_the_party_screen_maps_sides_and_asks_only_for_what_is_missing(client, review_draft):
    review_draft.court_code = "cook:cvd1"
    review_draft.case_type_code = "NC"
    review_draft.save(update_fields=["court_code", "case_type_code", "updated_at"])
    authorize(client, review_draft)
    FilingParty.objects.create(
        draft=review_draft,
        role="filer",
        sort_order=0,
        first_name="Alex",
        last_name="Rivera",
        address_line_1="100 State Street",
        city="Chicago",
        state="IL",
        zip_code="60601",
    )
    responding = FilingParty.objects.create(
        draft=review_draft,
        role="other",
        sort_order=0,
        first_name="Morgan",
        last_name="Lee",
        party_side=PartySide.RESPONDING,
    )

    with patch("efile.views.parties.get_party_types", return_value=PARTY_TYPES):
        response = client.post(
            reverse("parties", kwargs={"jurisdiction": "illinois"}),
            {"filer_party_type": "plaintiff"},
        )

    responding.refresh_from_db()
    # The extracted defendant already covers the required defendant party type,
    # so no blank placeholder is created alongside them. Their name and mapped
    # role are enough when this filing has no rule requiring an address.
    assert responding.party_type == "defendant"
    assert FilingParty.objects.filter(draft=review_draft, role="other").count() == 1
    assert response.status_code == 302
    assert response.url.partition("?")[0] == reverse("payment", kwargs={"jurisdiction": "illinois"})


# --- Answers that are not names ----------------------------------------------


@pytest.mark.parametrize(
    "answer",
    ["Unknown", "unknown party", "N/A", "None", "Not listed", "TBD", "et al.", "See attached", "Defendants"],
)
def test_a_model_saying_it_found_nobody_does_not_become_a_party(answer):
    """The names field is where a model puts "Unknown" when the caption had
    nobody in it, and the filer should not have to work out that Unknown is
    not a person."""

    assert extracted_party_suggestions({"defendant or respondent names": answer}) == []


@pytest.mark.parametrize(
    "name",
    ["All Unknown Occupants", "Unknown Occupants of 12 Main Street", "Jane None", "Party City Inc"],
)
def test_a_real_name_containing_one_of_those_words_is_kept(name):
    """ "All Unknown Occupants" is a real defendant in a real eviction. The
    guard compares whole names for exactly this reason."""

    assert [item["name"] for item in extracted_party_suggestions({"defendant or respondent names": name})] == [name]


def test_a_placeholder_among_real_names_is_the_only_one_dropped():
    suggestions = extracted_party_suggestions({"defendant or respondent names": "Morgan Lee; Unknown; Sam Lee"})

    assert [item["name"] for item in suggestions] == ["Morgan Lee", "Sam Lee"]


def test_the_same_person_read_onto_two_sides_is_listed_once():
    """A name in both the caption and the "other parties" answer is one person
    misread, not two people, and adding them twice adds a party to the case."""

    suggestions = extracted_party_suggestions(
        {
            "plaintiff or petitioner names": "Alex Rivera",
            "defendant or respondent names": "Morgan Lee",
            "other party names": "Morgan Lee (Tenant)",
        }
    )

    assert [item["name"] for item in suggestions] == ["Alex Rivera", "Morgan Lee"]
    assert [item["side"] for item in suggestions] == [PartySide.INITIATING, PartySide.RESPONDING]


@pytest.mark.django_db
def test_a_placeholder_typed_into_the_review_screen_adds_nobody(review_draft):
    save_reviewed_parties(
        review_draft,
        [
            {"id": None, "name": "Morgan Lee", "side": PartySide.RESPONDING, "role_hint": ""},
            {"id": None, "name": "Unknown", "side": PartySide.RESPONDING, "role_hint": ""},
        ],
    )

    names = [party.last_name for party in FilingParty.objects.filter(draft=review_draft, role="other")]
    assert names == ["Lee"]


# --- "This is me", said on the screen that reads the document ----------------


@pytest.mark.django_db
def test_the_review_screen_records_which_person_is_the_filer(client, review_draft):
    authorize(client, review_draft)

    client.post(
        reverse("extraction_review", kwargs={"jurisdiction": "illinois"}),
        {
            "existing_case": ExistingCase.NEW,
            "court_code": "cook:cvd1",
            "case_category_code": "civil",
            "case_type_code": "NC",
            "party_id": ["", ""],
            "party_name": ["Alex Rivera", "Morgan Lee"],
            "party_side": [PartySide.INITIATING, PartySide.RESPONDING],
            "party_role_hint": ["", ""],
            "party_is_self": ["false", "true"],
        },
    )

    parties = list(FilingParty.objects.filter(draft=review_draft, role="other").order_by("sort_order"))
    assert [(party.last_name, party.is_self) for party in parties] == [("Rivera", False), ("Lee", True)]


@pytest.mark.django_db
def test_only_one_person_can_be_the_filer(client, review_draft):
    """However many rows say so -- there is one person signed in."""

    authorize(client, review_draft)

    client.post(
        reverse("extraction_review", kwargs={"jurisdiction": "illinois"}),
        {
            "existing_case": ExistingCase.NEW,
            "court_code": "cook:cvd1",
            "case_category_code": "civil",
            "case_type_code": "NC",
            "party_id": ["", ""],
            "party_name": ["Alex Rivera", "Morgan Lee"],
            "party_side": [PartySide.INITIATING, PartySide.RESPONDING],
            "party_role_hint": ["", ""],
            "party_is_self": ["true", "true"],
        },
    )

    marked = FilingParty.objects.filter(draft=review_draft, role="other", is_self=True)
    assert [party.last_name for party in marked] == ["Rivera"]


@pytest.mark.django_db
def test_the_review_screen_offers_the_tick_on_every_person_it_found(client, review_draft):
    authorize(client, review_draft)

    content = client.get(reverse("extraction_review", kwargs={"jurisdiction": "illinois"})).content.decode()

    listing = re.search(r'id="review-parties-list">(.*?)</ol>', content, re.S)
    assert listing is not None
    assert listing.group(1).count('name="party_is_self"') == 3
    assert "This is me" in content
    assert "If you are listed, choose “This is me”." in content


@pytest.mark.django_db
def test_a_company_is_not_offered_as_the_person_filing(client, review_draft):
    """The account is registered to an individual, so no company is them --
    and the row still posts its value, or the lists stop lining up."""

    authorize(client, review_draft)

    content = client.get(reverse("extraction_review", kwargs={"jurisdiction": "illinois"})).content.decode()

    listing = re.search(r'id="review-parties-list">(.*?)</ol>', content, re.S)
    assert listing is not None
    rows = listing.group(1)
    # Three captioned parties, one of them Riverbend Properties LLC.
    assert rows.count('name="party_is_self"') == 3
    assert rows.count("review-party__is-me-toggle") == 2


@pytest.mark.django_db
def test_a_company_ticked_as_you_is_not_recorded_as_you(client, review_draft):
    """Storing it would be storing an answer nothing can act on: a company is
    never the person signed in, so the parties screen would ignore it and the
    filer would be left wondering where their answer went."""

    authorize(client, review_draft)

    client.post(
        reverse("extraction_review", kwargs={"jurisdiction": "illinois"}),
        {
            "existing_case": ExistingCase.NEW,
            "court_code": "cook:cvd1",
            "case_category_code": "civil",
            "case_type_code": "NC",
            "party_id": [""],
            "party_name": ["Riverbend Properties LLC"],
            "party_side": [PartySide.INITIATING],
            "party_role_hint": [""],
            "party_is_self": ["true"],
        },
    )

    party = FilingParty.objects.get(draft=review_draft, role="other")
    assert party.organization_name == "Riverbend Properties LLC"
    assert party.is_self is False


@pytest.mark.django_db
def test_a_company_does_not_use_up_the_one_slot_for_you(client, review_draft):
    """Otherwise the real person on the next row silently loses the tick."""

    authorize(client, review_draft)

    client.post(
        reverse("extraction_review", kwargs={"jurisdiction": "illinois"}),
        {
            "existing_case": ExistingCase.NEW,
            "court_code": "cook:cvd1",
            "case_category_code": "civil",
            "case_type_code": "NC",
            "party_id": ["", ""],
            "party_name": ["Riverbend Properties LLC", "Morgan Lee"],
            "party_side": [PartySide.INITIATING, PartySide.RESPONDING],
            "party_role_hint": ["", ""],
            "party_is_self": ["true", "true"],
        },
    )

    marked = FilingParty.objects.filter(draft=review_draft, role="other", is_self=True)
    assert [party.last_name for party in marked] == ["Lee"]

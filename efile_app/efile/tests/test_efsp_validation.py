"""Use actual EFSP data to exercise prevention, outages and precise edit links."""

import json
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from django.core.cache import cache
from django.forms import ChoiceField
from django.urls import reverse

from efile.models import FilingDocument, FilingDraft, FilingParty
from efile.services.current_drafts import CURRENT_DRAFT_SESSION_KEY
from efile.services.efsp_errors import describe_efsp_error, efsp_error_problems, error_actions
from efile.services.efsp_payload import PayloadValidationError, validate_party_formats
from efile.services.efsp_validation import party_validation, portable_regex, state_choices, validate_party
from efile.services.postal_codes import USPS_STATES

SAMPLES = json.loads((Path(__file__).parent / "fixtures/efsp_validation_2026-10-02.json").read_text())
RESPONSES = {row["path"]: row["body"] for row in SAMPLES["responses"]}


@pytest.fixture(autouse=True)
def efsp_validation_metadata(monkeypatch, settings):
    """Override the suite's fallback fixture with recorded live responses."""
    settings.EFSP_URL = "https://efile-test.suffolklitlab.org"
    cache.clear()

    def get(url, **kwargs):
        path = url.split("/jurisdictions/", 1)[-1].replace("%3A", ":")
        return Mock(status_code=200 if path in RESPONSES else 404, json=lambda: RESPONSES.get(path))

    monkeypatch.setattr("efile.services.efsp_validation.requests.get", get)
    yield
    cache.clear()


def test_usps_fallback_includes_all_62_postal_locations():
    choices, source = state_choices("unknown", "unknown")
    assert source == "usps"
    assert len(choices) == 62
    assert set(dict(choices)) == set(USPS_STATES)
    assert {"DC", "AA", "AE", "AP", "AS", "GU", "MP", "PR", "VI", "FM", "MH", "PW"} <= set(dict(choices))


@pytest.mark.parametrize(
    ("jurisdiction", "court", "count"),
    [
        ("illinois", "cook:cvd1", 54),
        ("massachusetts", "0705:LA", 51),
        ("vermont", "6000", 51),
    ],
)
def test_recorded_state_choices_match_the_efsp_exactly(jurisdiction, court, count):
    choices, source = state_choices(jurisdiction, court)
    assert source == "efsp"
    assert len(choices) == count
    assert set(dict(choices)) == set(RESPONSES[f"{jurisdiction}/codes/courts/{court}/countries/US/states"])
    assert not {"AA", "AE", "AP"}.intersection(dict(choices))


def test_empty_list_means_no_supported_locations_rather_than_fallback(monkeypatch):
    monkeypatch.setattr(
        "efile.services.efsp_validation.requests.get", lambda *a, **k: Mock(status_code=200, json=lambda: [])
    )
    assert state_choices("vermont", "6000") == ([], "efsp")


def test_failed_refresh_uses_last_good_then_usps_when_no_cache(monkeypatch):
    choices = state_choices("massachusetts", "0705:LA")
    for key in [
        "efsp-rule:https://efile-test.suffolklitlab.org/jurisdictions/massachusetts/codes/courts/0705%3ALA/countries/US/states"
    ]:
        cache.delete(key)
    get = Mock(side_effect=requests.Timeout)
    monkeypatch.setattr("efile.services.efsp_validation.requests.get", get)
    assert state_choices("massachusetts", "0705:LA") == choices
    assert state_choices("illinois", "new-court")[1] == "usps"
    calls = get.call_count
    assert state_choices("illinois", "new-court")[1] == "usps"
    assert get.call_count == calls


def test_malformed_states_fall_back_and_are_not_kept_as_last_good(monkeypatch):
    monkeypatch.setattr(
        "efile.services.efsp_validation.requests.get", lambda *a, **k: Mock(status_code=200, json=lambda: [{}])
    )
    assert state_choices("illinois", "cook:cvd1")[1] == "usps"
    assert not cache.get(
        "efsp-rule:https://efile-test.suffolklitlab.org/jurisdictions/illinois/codes/courts/cook%3Acvd1/countries/US/states:last-good"
    )


def test_rules_cache_does_not_cross_courts_or_efsp_environments(settings, monkeypatch):
    assert party_validation("massachusetts", "0705:LA")["validation_rules"]["first_name"]["max_length"] == 50
    assert party_validation("illinois", "cook:cvd1")["validation_rules"]["first_name"]["max_length"] is None
    settings.EFSP_URL = "https://another-efsp.example"
    monkeypatch.setattr("efile.services.efsp_validation.requests.get", Mock(side_effect=requests.Timeout))
    assert state_choices("massachusetts", "0705:LA")[1] == "usps"


@pytest.mark.parametrize("phone", ["1234567890", "(123) 456-7890", "+1 1234567890", "+1(123)456-7890", ""])
def test_massachusetts_phone_validation_matches_proxy_normalization(phone):
    errors = validate_party({"phone": phone}, party_validation("massachusetts", "0705:LA"))
    assert "phone" not in errors


def test_live_messages_examples_and_length_limits():
    metadata = party_validation("massachusetts", "0705:LA")
    errors = validate_party({"first_name": "a" * 51, "email": "invalid", "phone": "123"}, metadata)
    assert errors["first_name"] == "First name must be 50 characters or fewer."
    assert errors["email"] == "Please enter a valid email address: Example: someone@domain.com"
    assert errors["phone"] == "Do not use hyphens or other characters--just numbers"
    assert metadata["validation_rules"]["phone"]["help"] == "Ex: 1234567890"


def test_organization_rules_use_business_name_instead_of_person_name():
    errors = validate_party(
        {"organization_name": "a" * 100, "last_name": "b" * 51},
        party_validation("massachusetts", "0705:LA"),
        organization=True,
    )
    assert not errors


@pytest.mark.parametrize(
    "pattern", [r"(?i)abc", r"\p{L}+", r"\Qabc\E", r"[a-z&&[^x]]", r"\Aabc\z", r"\d++", r"[\s]", "["]
)
def test_java_only_or_invalid_regexes_are_deferred_to_efsp(pattern):
    assert portable_regex(pattern) is None


def test_unanchored_regex_uses_search_like_the_proxy():
    metadata = {
        "validation_rules": {"email": {"regex": "foo", "message": "bad"}},
        "state_choices": [],
        "state_source": "unavailable",
    }
    assert not validate_party({"email": "afoo@b"}, metadata)


def test_payload_validation_catches_extracted_values_before_fees_or_submission():
    payload = {"other_parties": [{"name": {"first": "a" * 51, "last": "Lee"}, "address": {"state": "AE"}}]}
    with pytest.raises(PayloadValidationError) as exc:
        validate_party_formats(payload, "massachusetts", "0705:LA")
    assert isinstance(exc.value, PayloadValidationError)
    assert {p["name"] for p in exc.value.problems} == {"other_parties[0].name.first", "other_parties[0].address.state"}


@pytest.fixture
def draft(client, django_user_model):
    user = django_user_model.objects.create_user(username="validation-user", tyler_jurisdiction="massachusetts")
    draft = FilingDraft.objects.create(
        user=user, jurisdiction="massachusetts", court_code="0705:LA", workflow_version=2
    )
    client.force_login(user)
    session = client.session
    session[CURRENT_DRAFT_SESSION_KEY] = draft.pk
    session["jurisdiction"] = "massachusetts"
    session["auth_tokens"] = {"TYLER-TOKEN-MASSACHUSETTS": "token"}
    session.save()
    return draft


@pytest.mark.django_db
def test_filer_form_preserves_invalid_values_and_returns_field_errors(client, draft):
    with patch("efile.views.your_information.cached_account_profile", return_value=None):
        response = client.post(
            reverse("your_information", kwargs={"jurisdiction": draft.jurisdiction}),
            {
                "first_name": "a" * 51,
                "last_name": "Lee",
                "address_line_1": "1 Main Street",
                "city": "APO",
                "state": "AE",
                "zip_code": "09001",
                "email": "lee@example.com",
                "phone": "123",
            },
        )
    assert response.status_code == 200
    assert set(response.context["field_errors"]) == {"first_name", "phone", "state"}
    assert response.context["filer"].first_name == "a" * 51
    assert not draft.parties.get(role="filer").first_name
    content = response.content.decode()
    assert 'value="AE" selected disabled' in content
    assert "Do not choose a different state." in content
    assert 'aria-invalid="true"' in content


@pytest.mark.django_db
def test_other_party_form_validates_names_and_keeps_attempted_contact_values(client, draft):
    party = FilingParty.objects.create(
        draft=draft, role="other", first_name="Morgan", last_name="Lee", party_type="DEF"
    )
    with patch("efile.views.party_details.get_party_types", return_value=[{"code": "DEF", "name": "Defendant"}]):
        response = client.post(
            reverse("party_details", kwargs={"jurisdiction": draft.jurisdiction}) + f"?party={party.pk}",
            {
                "party_type": "DEF",
                "first_name": "a" * 51,
                "last_name": "Lee",
                "email": "new@example.com",
                "phone": "123",
            },
        )
    assert response.status_code == 200
    assert set(response.context["field_errors"]) == {"first_name", "phone"}
    assert response.context["party"].email == "new@example.com"
    party.refresh_from_db()
    assert party.first_name == "Morgan"


@pytest.mark.django_db
def test_edit_links_follow_payload_parties_and_preserve_draft_identity(draft):
    FilingParty.objects.create(draft=draft, role="filer", first_name="Sam", last_name="Jones")
    target = FilingParty.objects.create(
        draft=draft, role="other", first_name="Lee", last_name="Smith", party_type="DEF", sort_order=9
    )
    payload = {"users": [{"name": {"first": "Lee", "last": "Smith"}, "party_type": "DEF"}]}
    actions = error_actions(draft, payload, [{"name": "users[0].address.state", "message": "Bad state"}])
    assert len(actions) == 1
    query = parse_qs(urlsplit(actions[0]["url"]).query)
    assert query == {"party": [str(target.pk)], "draft": [str(draft.pk)], "return_to": ["review"], "focus": ["state"]}
    assert "Lee Smith" in actions[0]["label"]


@pytest.mark.django_db
def test_ambiguous_parties_and_unknown_field_paths_do_not_get_guessed_links(draft):
    for index in range(2):
        FilingParty.objects.create(
            draft=draft, role="other", sort_order=index, first_name="Lee", last_name="Smith", party_type="DEF"
        )
    payload = {"other_parties": [{"name": {"first": "Lee", "last": "Smith"}, "party_type": "DEF"}]}
    assert error_actions(draft, payload, [{"name": "other_parties[0].address.state"}]) == []
    assert error_actions(draft, payload, [{"name": "other_parties[999].address.state"}, {"name": "unknown"}]) == []


@pytest.mark.django_db
@pytest.mark.parametrize("described", [False, True])
def test_legacy_state_message_locates_unique_rejected_value(draft, described):
    FilingParty.objects.create(draft=draft, role="other", first_name="Lee", last_name="Smith", party_type="DEF")
    payload = {
        "other_parties": [{"name": {"first": "Lee", "last": "Smith"}, "party_type": "DEF", "address": {"state": "BAD"}}]
    }
    message = "Opposing party dosesn't support a state named BAD"
    if described:
        # The views pass the described message, which appends a hint sentence.
        response = Mock(status_code=400, text=message)
        response.json.return_value = {"error": message}
        message = describe_efsp_error(response)
        assert message != "Opposing party dosesn't support a state named BAD"
    actions = error_actions(draft, payload, [], message=message)
    assert len(actions) == 1
    assert "focus=state" in actions[0]["url"]


def test_nested_proxy_error_and_name_length_regex_are_parsed():
    response = Mock(status_code=400)
    response.json.return_value = {
        "error": {"name": "users[0].name.last", "currentVal": "a" * 51, "description": ": must match regex: ^.{0,50}$"}
    }
    assert "50 characters or fewer" in describe_efsp_error(response)
    assert efsp_error_problems(response)[0]["name"] == "users[0].name.last"


@pytest.mark.django_db
@pytest.mark.parametrize("prevalidate", [True, False])
def test_fee_errors_return_precise_actions_for_both_local_and_upstream_rejections(
    client, draft, prevalidate, monkeypatch
):
    FilingDocument.objects.create(
        draft=draft, role="lead", s3_key="owned/petition.pdf", filing_type_code="petition", filing_component_code="lead"
    )
    monkeypatch.setattr(
        "efile.services.efsp_payload.S3UploadHandler.get_public_url",
        lambda *args, **kwargs: "https://storage.invalid/petition.pdf",
    )
    monkeypatch.setattr("efile.services.efsp_payload._EfspLookups.get", lambda *args: [])
    party = FilingParty.objects.create(draft=draft, role="other", first_name="Lee", last_name="Smith", party_type="DEF")
    payload = {
        "other_parties": [
            {
                "name": {"first": "Lee", "last": "Smith"},
                "party_type": "DEF",
                "address": {"state": "AE" if prevalidate else "MA"},
            }
        ],
        "al_court_bundle": [{}],
    }
    response = Mock(status_code=400, text="Rejected")
    response.json.return_value = {"wrong_vars": [{"name": "other_parties[0].name.last", "currentVal": "Smith"}]}
    with patch("efile.api.filing_views.requests.post", return_value=response) as post:
        result = client.post(
            reverse("api:payment_fees"), json.dumps({"efile_data": payload}), content_type="application/json"
        )
    assert result.status_code == 400
    actions = result.json()["error_actions"]
    assert len(actions) == 1
    params = parse_qs(urlsplit(actions[0]["url"]).query)
    assert params["party"] == [str(party.pk)]
    assert params["focus"] == ["state" if prevalidate else "last_name"]
    assert post.called is not prevalidate


def test_registration_uses_system_state_codes_and_explains_missing_addresses():
    from efile.forms import EFileRegistrationForm

    with patch("efile.forms.state_choices", return_value=([("DC", "District of Columbia")], "efsp")):
        form = EFileRegistrationForm(jurisdiction="massachusetts")
    field = form.fields["state"]
    assert isinstance(field, ChoiceField)
    assert list(field.choices) == [("", "Select a state"), ("DC", "District of Columbia")]
    assert "Do not choose a different state" in form.fields["state"].help_text


def test_foreign_addresses_never_get_usps_choices_during_an_outage():
    assert state_choices("massachusetts", "0705:LA", "CA") == ([], "unavailable")

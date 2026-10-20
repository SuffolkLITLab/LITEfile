from unittest.mock import Mock

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory
from django.urls import reverse

from efile.models import FilingDocument, FilingDraft
from efile.services.current_drafts import CURRENT_DRAFT_SESSION_KEY
from efile.services.filing_availability import (
    draft_unavailable_message,
    filing_unavailable_message,
    outgoing_unavailable_message,
)
from efile.tests.helpers import reviewed_document
from efile.views.session_api import forward_final_filing
from efile.views.submission import _claim_for_submission


@pytest.fixture
def submission_draft(client, django_user_model):
    user = django_user_model.objects.create_user(username="availability-user", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(
        user=user,
        jurisdiction="illinois",
        workflow_version=2,
        existing_case="new",
        court_code="cook:law1",
        court_name="Cook County",
        case_category_code="civil",
        case_category_name="Civil",
        case_type_code="Contract",
        case_type_name="Contract",
    )
    reviewed_document(
        draft=draft,
        role=FilingDocument.Role.LEAD,
        sort_order=0,
        name="Petition.pdf",
        filing_type_code="petition",
        filing_type_name="Petition",
        document_type_code="public",
    )
    client.force_login(user)
    session = client.session
    session[CURRENT_DRAFT_SESSION_KEY] = draft.pk
    session["jurisdiction"] = "illinois"
    session["auth_tokens"] = {"TYLER-TOKEN-ILLINOIS": "test-token"}
    session.save()
    return draft


@pytest.fixture
def configure(monkeypatch):
    def set_config(availability, court="cook:law1"):
        monkeypatch.setattr(
            "efile.services.filing_availability.config_loader.load_jurisdiction_config",
            lambda jurisdiction: {"court_specific_requirements": {court: {"filing_availability": availability}}}
            if jurisdiction == "illinois"
            else {},
        )

    return set_config


def test_default_enabled_and_jurisdiction_and_court_scope(configure):
    configure({"enabled": False})
    assert filing_unavailable_message("illinois", "cook:law1")
    assert not filing_unavailable_message("illinois", "cook:law2")
    assert not filing_unavailable_message("vermont", "cook:law1")


@pytest.mark.parametrize(
    "selector,arguments",
    [
        ("case_categories", {"case_category": "Human name"}),
        ("case_types", {"case_type": "Human name"}),
        ("filing_types", {"filing_types": ["unrestricted", "Human name"]}),
    ],
)
def test_each_selector_matches_exact_names(configure, selector, arguments):
    configure({"rules": [{selector: ["Human name"], "message": "Scheduling is unavailable."}]})
    assert filing_unavailable_message("illinois", "cook:law1", **arguments) == "Scheduling is unavailable."
    assert not filing_unavailable_message("illinois", "cook:law1")


def test_rules_combine_selectors_and_fall_back_to_court_message(configure):
    configure({"message": "Court notice", "rules": [{"case_types": ["Contract"], "filing_types": ["Motion"]}]})
    assert not filing_unavailable_message("illinois", "cook:law1", case_type="Contract")
    assert (
        filing_unavailable_message("illinois", "cook:law1", case_type="Contract", filing_types=["Motion"])
        == "Court notice"
    )


def test_county_prefix_is_bounded_and_specific_message_wins(monkeypatch):
    monkeypatch.setattr(
        "efile.services.filing_availability.config_loader.load_jurisdiction_config",
        lambda jurisdiction: {
            "court_specific_requirements": {
                "cook:*": {"filing_availability": {"enabled": False, "message": "County notice"}},
                "cook:law1": {
                    "filing_availability": {
                        "enabled": True,
                        "rules": [{"case_types": ["Contract"], "message": "Hearing scheduling is unavailable."}],
                    }
                },
            }
        },
    )
    assert (
        filing_unavailable_message("illinois", "cook:law1", case_type="Contract")
        == "Hearing scheduling is unavailable."
    )
    assert filing_unavailable_message("illinois", "cook:law1", case_type="other") == "County notice"
    assert filing_unavailable_message("illinois", "cook:cd1") == "County notice"
    assert not filing_unavailable_message("illinois", "cooksville:law1")


def test_empty_rules_do_not_disable_and_specific_overrides_generic(configure):
    configure({"rules": [{"message": "No selector"}, {"case_types": []}]})
    assert not filing_unavailable_message("illinois", "cook:law1")
    configure({"enabled": False, "message": "Generic", "rules": [{"case_types": ["Contract"], "message": "Specific"}]})
    assert filing_unavailable_message("illinois", "cook:law1", case_type="Contract") == "Specific"


@pytest.mark.django_db
def test_supporting_document_blocks_envelope_and_removal_restores_filing(configure, submission_draft):
    configure({"rules": [{"filing_types": ["Motion"]}]})
    assert not draft_unavailable_message(submission_draft)
    document = FilingDocument.objects.create(
        draft=submission_draft, role="supporting", sort_order=1, filing_type_code="123", filing_type_name="Motion"
    )
    assert draft_unavailable_message(submission_draft)
    document.delete()
    assert not draft_unavailable_message(submission_draft)


@pytest.mark.django_db
@pytest.mark.parametrize("view", ["document_checklist", "case_review"])
def test_blocked_page_preserves_draft_and_escapes_message(configure, client, submission_draft, view):
    configure({"rules": [{"case_types": ["Contract"], "message": "Scheduling <script>alert(1)</script>"}]})
    response = client.get(reverse(view, kwargs={"jurisdiction": "illinois"}))
    assert response.status_code == 403
    assert b"Scheduling &lt;script&gt;" in response.content
    assert b"Correct case details" in response.content
    submission_draft.refresh_from_db()
    assert submission_draft.status == FilingDraft.Status.DRAFT
    assert submission_draft.documents.exists()


@pytest.mark.django_db
def test_direct_submit_cannot_bypass_new_restriction(configure, client, submission_draft, monkeypatch):
    configure({"rules": [{"case_types": ["Contract"], "message": "Hearing scheduling is unavailable."}]})
    forward = Mock()
    monkeypatch.setattr("efile.views.submission.forward_final_filing", forward)
    response = client.post(reverse("submit_final_filing"), {}, content_type="application/json")
    assert response.status_code == 403
    assert response.json()["error_code"] == "submission_filing_unavailable"
    forward.assert_not_called()
    submission_draft.refresh_from_db()
    assert submission_draft.status == FilingDraft.Status.DRAFT


@pytest.mark.django_db
def test_outgoing_ids_are_resolved_to_names_before_checking(configure, client, submission_draft, monkeypatch):
    configure({"rules": [{"case_types": ["Contract"], "filing_types": ["Blocked filing"]}]})
    submission_draft.documents.update(s3_key="owned/lead.pdf", filing_type_code="allowed", filing_component_code="lead")
    FilingDocument.objects.create(
        draft=submission_draft,
        role="supporting",
        s3_key="owned/supporting.pdf",
        filing_type_code="blocked",
        filing_component_code="lead",
    )
    monkeypatch.setattr(
        "efile.services.efsp_payload.S3UploadHandler.get_public_url",
        lambda *args, **kwargs: "https://storage.invalid/document.pdf",
    )
    request = RequestFactory().post("/")
    request.user = submission_draft.user
    request.session = client.session
    external = Mock()
    monkeypatch.setattr("requests.post", external)
    monkeypatch.setattr(
        "efile.services.filing_availability._EfspLookups.get",
        lambda self, url: (
            [{"code": "Contract", "name": "Contract"}]
            if "case_types" in url
            else [{"code": "allowed", "name": "Allowed filing"}, {"code": "blocked", "name": "Blocked filing"}]
        ),
    )
    response = forward_final_filing(
        request,
        {
            "efile_data": {
                "al_court_bundle": [
                    {"filing_type": "allowed"},
                    {"filing_type": "blocked", "filing_description": "Allowed filing"},
                ]
            }
        },
    )
    assert response.status_code == 403
    external.assert_not_called()


@pytest.mark.django_db
def test_confirm_new_case_keeps_choices_editable_when_blocked(configure, client, submission_draft):
    configure({"rules": [{"case_types": ["Blocked type"], "message": "Scheduling is unavailable."}]})
    response = client.post(
        reverse("extraction_review", kwargs={"jurisdiction": "illinois"}),
        {
            "existing_case": "new",
            "court_code": "cook:law1",
            "court_name": "Cook County",
            "case_category_code": "civil",
            "case_type_code": "blocked",
            "case_type_name": "Blocked type",
        },
    )
    assert response.status_code == 200
    assert b"Scheduling is unavailable." in response.content
    assert response.context["extraction_context"]["case_type_code"] == "blocked"
    submission_draft.refresh_from_db()
    assert submission_draft.case_type_code == "Contract"


@pytest.mark.django_db
def test_existing_case_confirmation_is_blocked_but_search_again_works(configure, client, submission_draft):
    configure({"enabled": False})
    submission_draft.existing_case = "existing"
    submission_draft.previous_case_id = "case-123"
    submission_draft.docket_number = "2026-CV-123"
    submission_draft.save()
    url = reverse("case_confirmation", kwargs={"jurisdiction": "illinois"})
    response = client.get(url)
    assert response.context["availability_message"]
    assert b'value="yes"' in response.content
    assert client.post(url, {"confirmed": "yes"}).status_code == 403
    assert client.post(url, {"confirmed": "no"}).status_code == 302


@pytest.mark.django_db
def test_organize_blocks_supporting_type_but_allows_correction(configure, client, submission_draft):
    configure({"rules": [{"filing_types": ["Motion"], "message": "Scheduling is unavailable."}]})
    document = submission_draft.documents.first()
    url = reverse("organize_documents", kwargs={"jurisdiction": "illinois"})
    details = {"id": document.pk, "filing_type": "123", "filing_type_name": "Motion", "document_type": "public"}
    payload = {"main_document_id": document.pk, "documents": [details]}
    response = client.post(url, payload, content_type="application/json")
    assert response.status_code == 403
    assert response.json()["error"] == "Scheduling is unavailable."
    assert b"Scheduling is unavailable." in client.get(url).content
    details["filing_type"] = "petition"
    details["filing_type_name"] = "Petition"
    assert client.post(url, payload, content_type="application/json").status_code == 200


@pytest.mark.django_db
def test_claim_rechecks_availability(configure, submission_draft):
    configure({"enabled": False, "message": "Disabled since review"})
    with pytest.raises(ValueError, match="Disabled since review"):
        _claim_for_submission(submission_draft, {})
    submission_draft.refresh_from_db()
    assert submission_draft.status == FilingDraft.Status.DRAFT


@pytest.mark.django_db
def test_live_api_checks_partial_choices_and_multiple_filing_types(configure, client):
    configure({"enabled": False, "message": "Court disabled"})
    url = reverse("api:filing_availability")
    params = {"jurisdiction": "illinois", "court": "cook:law1"}
    response = client.get(url, params)
    assert response.json() == {"success": True, "available": False, "message": "Court disabled"}
    assert "no-store" in response.headers["Cache-Control"]
    configure({"rules": [{"filing_types": ["Motion"], "message": "Scheduling unavailable"}]})
    assert client.get(url, params).json()["available"]
    params["filing_type_name"] = ["petition", "Motion"]
    assert not client.get(url, params).json()["available"]


@pytest.mark.django_db
def test_live_api_rejects_unknown_jurisdiction(client):
    assert client.get(reverse("api:filing_availability"), {"jurisdiction": "unknown"}).status_code == 400


@pytest.mark.parametrize(
    "name,blocked", [("Contract", True), ("contract", False), ("Contract dispute", False), ("183541", False)]
)
def test_exact_name_matching_is_not_substring_or_id_matching(configure, name, blocked):
    configure({"rules": [{"case_types": ["Contract"]}]})
    assert bool(filing_unavailable_message("illinois", "cook:law1", case_type=name)) == blocked


@pytest.mark.parametrize(
    "selector,argument",
    [("case_categories", "case_category"), ("case_types", "case_type"), ("filing_types", "filing_types")],
)
@pytest.mark.parametrize(
    "name,expected", [("MOTION", True), ("Motion to dismiss", True), ("Notice of Motion", False), ("", False)]
)
def test_regex_fullmatches_names_with_explicit_flags(configure, selector, argument, name, expected):
    configure({"rules": [{selector: [{"regex": "(?i)motion(?: to .+)?"}]}]})
    value = [name] if argument == "filing_types" else name
    assert bool(filing_unavailable_message("illinois", "cook:law1", **{argument: value})) == expected


@pytest.mark.django_db
@pytest.mark.parametrize("code", ["183541", "999999"])
def test_saved_draft_rule_survives_numeric_id_changes(configure, submission_draft, code):
    configure({"rules": [{"case_types": ["Contract"]}]})
    submission_draft.case_type_code = code
    submission_draft.save()
    assert draft_unavailable_message(submission_draft)


@pytest.mark.parametrize("code", ["183541", "999999"])
def test_submit_resolves_current_ids_and_ignores_client_names(configure, monkeypatch, code):
    configure({"rules": [{"case_types": ["Contract"]}]})
    monkeypatch.setattr(
        "efile.services.filing_availability._EfspLookups.get", lambda self, url: [{"code": code, "name": "Contract"}]
    )
    assert outgoing_unavailable_message(
        "illinois",
        "cook:law1",
        {},
        {
            "efile_case_type": code,
            "case_type_name": "Unrestricted type",
            "al_court_bundle": [],
        },
    )


def test_submit_blocks_when_names_cannot_be_resolved(configure, monkeypatch):
    configure({"rules": [{"case_types": ["Contract"]}]})
    monkeypatch.setattr("efile.services.filing_availability._EfspLookups.get", lambda self, url: None)
    with pytest.raises(ValueError, match="could not confirm"):
        outgoing_unavailable_message("illinois", "cook:law1", {}, {"efile_case_type": "123"})


def test_literal_punctuation_and_invalid_regex(configure):
    configure({"rules": [{"filing_types": ["Motion (Other)"]}]})
    assert filing_unavailable_message("illinois", "cook:law1", filing_types=["Motion (Other)"])
    assert not filing_unavailable_message("illinois", "cook:law1", filing_types=["Motion Other"])
    configure({"rules": [{"case_types": [{"regex": "["}]}]})
    with pytest.raises(ImproperlyConfigured, match="Invalid filing availability regex"):
        filing_unavailable_message("illinois", "cook:law1", case_type="Contract")


@pytest.mark.parametrize("selector", [[{"regex": "["}], "Contract", [7]])
def test_malformed_rules_fail_even_with_blank_names(configure, selector):
    # Drafts can save blank names; the rule must still fail before the claim.
    configure({"rules": [{"case_types": selector}]})
    with pytest.raises(ImproperlyConfigured):
        filing_unavailable_message("illinois", "cook:law1")


def test_null_selector_matches_nothing(configure):
    configure({"rules": [{"case_types": None}]})
    assert not filing_unavailable_message("illinois", "cook:law1", case_type="Contract")


def test_empty_selectors_need_no_code_lookup(configure, monkeypatch):
    configure({"rules": [{"case_types": []}]})
    lookup = Mock()
    monkeypatch.setattr("efile.services.filing_availability._EfspLookups.get", lookup)
    assert not outgoing_unavailable_message("illinois", "cook:law1", {}, {})
    lookup.assert_not_called()


@pytest.mark.django_db
def test_draft_check_skips_document_query_without_filing_type_rules(
    configure, submission_draft, django_assert_num_queries
):
    configure({"rules": [{"case_types": ["Contract"]}]})
    with django_assert_num_queries(0):
        assert draft_unavailable_message(submission_draft)

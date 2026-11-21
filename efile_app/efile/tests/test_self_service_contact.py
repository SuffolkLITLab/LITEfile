from unittest.mock import Mock

import pytest
import requests
from django.urls import reverse

from efile.models import FilingDraft


@pytest.fixture
def setup(client, django_user_model, monkeypatch):
    user = django_user_model.objects.create_user(
        username="self-service",
        email="filer@example.com",
        first_name="Alex",
        last_name="Example",
        tyler_jurisdiction="illinois",
    )
    draft = FilingDraft.objects.create(
        user=user,
        jurisdiction="illinois",
        status="submitted",
        court_code="winnebago",
        submission_response={"caseId": "case-123", "envelopeId": "456"},
    )
    monkeypatch.setattr("efile.views.service_contacts.cached_account_profile", lambda *args: {})
    client.force_login(user)
    session = client.session
    session["auth_tokens"] = {"TYLER-TOKEN-ILLINOIS": "token"}
    session.save()
    proxy = Mock(side_effect=[[], "contact-123"])

    def response(*args, **kwargs):
        return Mock(status_code=200, json=lambda: proxy(*args, **kwargs))

    monkeypatch.setattr("efile.services.service_contacts.requests.get", response)
    monkeypatch.setattr("efile.services.service_contacts.requests.post", response)
    put = Mock(return_value=Mock(status_code=201))
    monkeypatch.setattr("efile.services.service_contacts.requests.put", put)
    return draft, proxy, put, reverse("self_service_contact", args=[draft.public_id])


DATA = {
    "first_name": "Alex",
    "last_name": "Example",
    "consent": "yes",
    "email": "injected@example.com",
    "caseId": "other",
}


@pytest.mark.django_db
def test_registers_account_contact_and_never_resubmits(client, setup):
    draft, proxy, put, url = setup
    response = client.post(url, DATA, follow=True)
    assert response.status_code == 200
    assert b"You are registered" in response.content
    assert proxy.call_args.kwargs["json"]["email"] == "filer@example.com"
    assert proxy.call_args.kwargs["json"]["isPublic"] is False
    assert put.call_args.kwargs["json"] == {"caseId": "case-123"}
    draft.refresh_from_db()
    assert draft.supplemental_fields == {}
    assert draft.status == "submitted"
    assert draft.submission_response == {"caseId": "case-123", "envelopeId": "456"}
    proxy.side_effect = [
        [{"serviceContactID": "contact-123", "firstName": "Alex", "lastName": "Example", "email": "filer@example.com"}]
    ]
    client.post(url, DATA)
    assert put.call_count == 2


@pytest.mark.django_db
def test_get_and_missing_consent_do_not_mutate_court(client, setup):
    draft, proxy, put, url = setup
    assert b"Register for future service" in client.get(url).content
    client.post(url, {**DATA, "consent": ""})
    proxy.assert_not_called()
    put.assert_not_called()


@pytest.mark.django_db
def test_owner_only(client, setup, django_user_model):
    draft, proxy, put, url = setup
    client.force_login(django_user_model.objects.create_user(username="someone-else", tyler_jurisdiction="illinois"))
    session = client.session
    session["auth_tokens"] = {"TYLER-TOKEN-ILLINOIS": "token"}
    session.save()
    assert client.post(url, DATA).status_code == 404
    client.logout()
    assert client.post(url, DATA).status_code == 302
    proxy.assert_not_called()


@pytest.mark.django_db
def test_existing_contact_reused_and_empty_success_response_supported(client, setup):
    draft, proxy, put, url = setup
    proxy.side_effect = [
        [{"serviceContactID": "contact-123", "firstName": "Alex", "lastName": "Example", "email": "filer@example.com"}]
    ]
    put.return_value.status_code = 204
    assert b"You are registered" in client.post(url, DATA, follow=True).content
    assert proxy.call_count == 1
    put.return_value.json.assert_not_called()


@pytest.mark.django_db
def test_attachment_failure_preserves_filing_and_allows_retry(client, setup):
    draft, proxy, put, url = setup
    put.side_effect = requests.Timeout()
    assert b"do not submit your filing again" in client.post(url, DATA).content
    draft.refresh_from_db()
    assert draft.supplemental_fields == {}
    assert draft.status == "submitted"
    proxy.side_effect = [
        [{"serviceContactID": "contact-123", "firstName": "Alex", "lastName": "Example", "email": "filer@example.com"}]
    ]
    put.side_effect = None
    assert b"You are registered" in client.post(url, DATA, follow=True).content


@pytest.mark.django_db
@pytest.mark.parametrize("previous", ["", "existing-case"])
def test_missing_new_case_id_or_existing_case_fallback(client, setup, previous):
    draft, proxy, put, url = setup
    draft.submission_response = {}
    draft.previous_case_id = previous
    draft.save()
    response = client.post(url, DATA)
    assert b"no case ID available" in response.content
    proxy.assert_not_called()


@pytest.mark.django_db
def test_drafts_cannot_register(client, setup):
    draft, proxy, put, url = setup
    draft.status = "draft"
    draft.save()
    assert client.post(url, DATA).status_code == 404
    proxy.assert_not_called()


@pytest.mark.django_db
def test_missing_token_redirects_without_court_mutation(client, setup):
    draft, proxy, put, url = setup
    session = client.session
    session["auth_tokens"] = {}
    session.save()
    assert client.post(url, DATA).status_code == 302
    proxy.assert_not_called()
    put.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize("changes", [{"first_name": ""}, {"last_name": "x" * 101}])
def test_invalid_names_do_not_create_contact(client, setup, changes):
    draft, proxy, put, url = setup
    assert b"first and last name" in client.post(url, {**DATA, **changes}).content
    proxy.assert_not_called()


@pytest.mark.django_db
def test_missing_account_email_does_not_use_posted_address(client, setup):
    draft, proxy, put, url = setup
    draft.user.email = ""
    draft.user.save()
    assert b"valid email address" in client.post(url, DATA).content
    proxy.assert_not_called()


@pytest.mark.django_db
def test_csrf_required(setup):
    from django.test import Client

    draft, proxy, put, url = setup
    client = Client(enforce_csrf_checks=True)
    client.force_login(draft.user)
    assert client.post(url, DATA).status_code == 403
    proxy.assert_not_called()


@pytest.mark.django_db
def test_signed_out_ownerless_submission_redirects_without_exposing_details(client, setup):
    draft, proxy, put, url = setup
    draft.user = None
    draft.save()
    client.logout()
    response = client.post(url, DATA)
    assert response.status_code == 302
    proxy.assert_not_called()
    put.assert_not_called()


@pytest.mark.django_db
def test_expired_token_at_attachment_offers_sign_in(client, setup):
    draft, proxy, put, url = setup
    put.return_value.status_code = 401
    response = client.post(url, DATA)
    assert response.status_code == 302
    assert "/login/" in response.url


@pytest.mark.django_db
def test_account_profile_and_jurisdiction_context(client, setup, monkeypatch):
    draft, proxy, put, url = setup
    monkeypatch.setattr(
        "efile.views.service_contacts.cached_account_profile",
        lambda *args: {"first_name": "Account", "last_name": "Holder"},
    )
    response = client.get(url)
    assert response.context["first_name"] == "Account"
    assert response.context["last_name"] == "Holder"
    assert response.context["config"]
    assert b'aria-label="Filing progress"' not in response.content
    proxy.assert_not_called()


@pytest.mark.django_db
def test_middle_name_and_spacing_reuse_existing_contact(client, setup):
    draft, proxy, put, url = setup
    proxy.side_effect = [
        [
            {
                "serviceContactID": "existing",
                "firstName": "Mary Ann",
                "middleName": "J",
                "lastName": "Example",
                "email": "filer@example.com",
            }
        ]
    ]
    response = client.post(url, {**DATA, "first_name": "Mary  Ann"}, follow=True)
    assert b"You are registered" in response.content
    assert proxy.call_count == 1
    assert "/existing/cases" in put.call_args.args[0]
    assert put.call_args.kwargs["allow_redirects"] is False
    assert put.call_args.kwargs["headers"]["tyler-token-illinois"] == "token"

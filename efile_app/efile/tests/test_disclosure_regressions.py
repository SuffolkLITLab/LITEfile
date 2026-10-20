"""Confidentiality regressions use synthetic identities and no external services."""

import json
from unittest.mock import Mock, patch

import pytest
from django.test import Client, RequestFactory

from efile.models import FilingDocument, FilingDraft
from efile.services.court_payments import bank_payment_policy
from efile.services.efsp_payload import PayloadValidationError, prepare_efile_payload
from efile.services.existing_cases import CaseImportError, fetch_case
from efile.services.filings import fetch_filing_detail
from efile.services.payment_accounts import payment_accounts
from efile.views.session_api import forward_final_filing

pytestmark = pytest.mark.django_db


@pytest.fixture
def signed_in(django_user_model):
    client = Client(enforce_csrf_checks=True)
    user = django_user_model.objects.create_user(username="filer", tyler_jurisdiction="illinois")
    client.force_login(user)
    draft = FilingDraft.objects.create(user=user, jurisdiction="illinois", court_code="cook:law1")
    session = client.session
    session.update(
        {
            "jurisdiction": "illinois",
            "filing_draft_id": draft.pk,
            "auth_tokens": {"TYLER-TOKEN-ILLINOIS": "dummy-user-token"},
        }
    )
    session.save()
    client.get("/jurisdiction/illinois/login/")
    return client, draft


@pytest.fixture(autouse=True)
def no_external_requests(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Unexpected external request")

    monkeypatch.setattr("requests.sessions.Session.send", blocked)


@pytest.mark.parametrize("authenticated", [False, True])
def test_token_response_never_contains_service_key(signed_in, settings, authenticated):
    settings.SUFFOLK_EFILE_API_KEY = "private-service-key-marker"
    client = signed_in[0] if authenticated else Client(enforce_csrf_checks=True)
    response = client.get("/api/auth/tyler-token/?jurisdiction=illinois")
    assert response.status_code == (200 if authenticated else 401)
    assert settings.SUFFOLK_EFILE_API_KEY.encode() not in response.content
    assert "no-store" in response.headers["Cache-Control"]
    if authenticated:
        assert response.json()["data"] == {"tyler_token": "dummy-user-token", "state": "illinois"}


@pytest.mark.parametrize("state", ["vermont", "massachusetts"])
def test_token_requires_active_identity_for_requested_state(signed_in, state):
    client, _ = signed_in
    session = client.session
    session["auth_tokens"][f"TYLER-TOKEN-{state.upper()}"] = "old-other-state-token"
    session.save()
    assert client.get("/api/auth/tyler-token/", {"jurisdiction": state}).status_code == 401


def test_token_requires_unexpired_court_login(signed_in):
    client, _ = signed_in
    session = client.session
    session["auth_tokens"] = {}
    session.save()
    assert client.get("/api/auth/tyler-token/").status_code == 401


@pytest.mark.parametrize(
    "path",
    [
        "/api/auth/login/",
        "/api/auth/logout/",
        "/api/save-case-data/",
        "/api/save-party-type/",
        "/api/fetch-party-type/",
        "/api/clear-session/",
        "/api/payment-fees/",
        "/api/submit-final-filing/",
        "/api/suffolk/lookup-case/",
    ],
)
def test_cross_origin_browser_mutations_require_csrf(signed_in, path):
    client, draft = signed_in
    original_session = dict(client.session)
    body = (
        '{"username":"attacker@example.invalid","password":"attacker-password","jurisdiction":"illinois","pad":"="}\r\n'
    )
    with patch("efile.api.auth_views.authenticate") as authenticate:
        response = client.post(
            path, body, content_type="text/plain", HTTP_ORIGIN="https://attacker.invalid", secure=True
        )
    assert response.status_code == 403
    authenticate.assert_not_called()
    assert dict(client.session) == original_session
    draft.refresh_from_db()
    assert draft.case_title == ""


def test_anonymous_login_csrf_cannot_create_attacker_session():
    client = Client(enforce_csrf_checks=True)
    with patch("efile.api.auth_views.authenticate") as authenticate:
        response = client.post(
            "/api/auth/login/",
            '{"username":"attacker","password":"test","jurisdiction":"illinois","pad":"="}\r\n',
            content_type="text/plain",
            HTTP_ORIGIN="https://attacker.invalid",
            secure=True,
        )
    assert response.status_code == 403
    authenticate.assert_not_called()
    assert "_auth_user_id" not in client.session


def test_same_origin_json_login_still_works():
    client = Client(enforce_csrf_checks=True)
    client.get("/jurisdiction/illinois/login/")
    tokens = {"TYLER-TOKEN-ILLINOIS": "dummy-token", "TYLER-ID-ILLINOIS": "dummy-id"}
    with patch("efile.authentication.auth_with_tyler_api", return_value={"tokens": tokens}):
        response = client.post(
            "/api/auth/login/",
            {"username": "filer@example.invalid", "password": "dummy-password", "jurisdiction": "illinois"},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
        )
    assert response.status_code == 200
    assert response.json()["success"]
    assert client.session["auth_tokens"] == tokens


@pytest.mark.parametrize(
    "bad", ["../../api_user_settings/serverid?", "%2e%2e", "illinois/../vermont", "illinois#", "bogus"]
)
def test_proxy_jurisdiction_injection_is_rejected_before_network(signed_in, bad):
    client, _ = signed_in
    for endpoint in ["payment-account-types", "payment-accounts", "auth/profile", "auth/tyler-token", "filings"]:
        assert client.get(f"/api/{endpoint}/", {"jurisdiction": bad}).status_code == 400


@pytest.mark.parametrize(
    "court", ["../../../api_user_settings/serverid?", "..", ".", "%2e%2e", "court?x=1", "court#", "court\\x"]
)
def test_case_lookup_rejects_path_injection(signed_in, court):
    client, _ = signed_in
    response = client.get(
        "/api/suffolk/lookup-case/", {"jurisdiction": "illinois", "court": court, "caseNumber": "123"}
    )
    assert response.status_code == 400


def test_payment_types_still_work_without_following_redirects(signed_in):
    client, _ = signed_in
    with patch(
        "efile.api.auth_views.requests.get", return_value=Mock(status_code=200, json=lambda: [{"code": "CC"}])
    ) as get:
        response = client.get("/api/payment-account-types/")
    assert response.status_code == 200
    assert get.call_args.kwargs["allow_redirects"] is False
    assert "/jurisdictions/illinois/payments/types" in get.call_args.args[0]


def malicious_payload():
    return {
        "users": [],
        "al_court_bundle": [
            {
                "proxy_enabled": True,
                "filing_type": "forged",
                "document_type": "public",
                "data_url": "http://127.0.0.1/internal",
                "page_count": 1,
                "elements": [{"proxy_enabled": True, "data_url": "http://169.254.169.254/latest/meta-data"}],
            }
        ],
    }


@pytest.mark.parametrize("boundary", ["fees", "submission"])
def test_both_forwarding_boundaries_use_only_owned_document_bytes(signed_in, boundary, monkeypatch):
    client, draft = signed_in
    document = FilingDocument.objects.create(
        draft=draft,
        role="lead",
        name="private.pdf",
        s3_key="owned/private.pdf",
        filing_type_code="petition",
        document_type_code="confidential",
        filing_component_code="lead",
    )
    monkeypatch.setattr("efile.services.efsp_payload._EfspLookups.get", lambda *args: [])
    payload = malicious_payload()
    with patch("efile.services.efsp_payload.S3UploadHandler") as storage, patch("requests.post") as post:
        storage.return_value.get_public_url.return_value = "https://storage.invalid/owned.pdf?signature=dummy"
        post.return_value = Mock(status_code=200, json=lambda: {"feesCalculationAmount": {"value": "0"}})
        if boundary == "fees":
            response = client.post(
                "/api/payment-fees/",
                {"efile_data": payload},
                content_type="application/json",
                HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
            )
        else:
            request = RequestFactory().post("/")
            request.user, request.session = draft.user, client.session
            response = forward_final_filing(request, {"efile_data": payload})
        assert response.status_code == 200, response.content
        storage.return_value.get_public_url.assert_called_once_with(document.s3_key, expiration=3600)
        forwarded = post.call_args.kwargs["json"]
        bundle = forwarded["al_court_bundle"][0]
        assert bundle["data_url"] == "https://storage.invalid/owned.pdf?signature=dummy"
        assert bundle["document_type"] == "confidential"
        assert bundle["filing_type"] == "petition"
        assert "elements" not in bundle and "page_count" not in bundle
        assert "127.0.0.1" not in json.dumps(forwarded)
        assert "169.254.169.254" not in json.dumps(forwarded)
        assert post.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize(
    "problem",
    [
        "no-draft",
        "empty-draft",
        "foreign-document",
        "missing-key",
        "wrong-jurisdiction",
        "wrong-court",
        "invalid-court",
    ],
)
def test_document_binding_fails_closed(signed_in, problem):
    _, draft = signed_in
    payload = malicious_payload()
    if problem not in {"no-draft", "empty-draft"}:
        FilingDocument.objects.create(
            draft=draft, role="lead", name="private.pdf", s3_key="" if problem == "missing-key" else "owned/private.pdf"
        )
    if problem == "foreign-document":
        foreign = FilingDraft.objects.create(jurisdiction="illinois")
        document = FilingDocument.objects.create(draft=foreign, role="lead", s3_key="other/private.pdf")
        payload["al_court_bundle"][0]["document_id"] = document.pk
    with patch("efile.services.efsp_payload.S3UploadHandler") as storage:
        with pytest.raises(PayloadValidationError):
            prepare_efile_payload(
                payload,
                "vermont" if problem == "wrong-jurisdiction" else "illinois",
                ".." if problem == "invalid-court" else "other" if problem == "wrong-court" else "cook:law1",
                draft=None if problem == "no-draft" else draft,
            )
        storage.return_value.get_public_url.assert_not_called()


@pytest.mark.parametrize("endpoint", ["payment-fees", "submit-final-filing"])
def test_forwarding_authenticates_session_jurisdiction_not_query(signed_in, endpoint):
    client, _ = signed_in
    session = client.session
    session["jurisdiction"] = "vermont"
    session.save()
    response = client.post(
        f"/api/{endpoint}/?jurisdiction=illinois",
        {"efile_data": malicious_payload()},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
    )
    assert response.status_code == 401


@pytest.mark.parametrize("endpoint", ["payment-fees", "submit-final-filing"])
def test_forwarding_requires_court_token(signed_in, endpoint):
    client, _ = signed_in
    session = client.session
    session["auth_tokens"] = {}
    session.save()
    response = client.post(
        f"/api/{endpoint}/",
        {"efile_data": malicious_payload()},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
    )
    assert response.status_code == 401


def test_empty_draft_cannot_forward_arbitrary_document_url(signed_in):
    client, _ = signed_in
    with patch("requests.post") as post:
        response = client.post(
            "/api/payment-fees/",
            {"efile_data": malicious_payload()},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
        )
    assert response.status_code == 400
    post.assert_not_called()


@pytest.mark.parametrize(
    "content_type,body,status",
    [
        ("text/plain", '{"password":"private-marker"}', 415),
        ("application/json", '{"password":"private-marker",', 400),
        ("application/json", '["private-marker"]', 400),
    ],
)
def test_login_rejects_malformed_inputs_without_echoing_secrets(signed_in, content_type, body, status):
    client, _ = signed_in
    response = client.post(
        "/api/auth/login/", body, content_type=content_type, HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value
    )
    assert response.status_code == status
    assert b"private-marker" not in response.content


def test_case_lookup_accepts_literal_court_codes_and_uses_query_params(signed_in):
    client, _ = signed_in
    upstream = Mock(status_code=200)
    upstream.json.return_value = [{"caseTrackingID": "dummy-case", "caseTitleText": "A v B"}]
    with patch("efile.api.suffolk_api_views.requests.get", return_value=upstream) as get:
        response = client.get(
            "/api/suffolk/lookup-case/", {"jurisdiction": "illinois", "court": "cook:law1", "caseNumber": "A&B/123"}
        )
    assert response.status_code == 200
    assert response.json()["caseInfo"]["caseTrackingID"] == "dummy-case"
    assert get.call_args.args[0].endswith("/jurisdictions/illinois/cases/courts/cook:law1/cases")
    assert get.call_args.kwargs["params"] == {"docket_number": "A&B/123"}
    assert get.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize("value", ["..", ".", "%2e%2e", "court/../other", "court?query", "court#fragment"])
def test_credentialed_service_paths_reject_nonliteral_codes(signed_in, value):
    client, draft = signed_in
    request = RequestFactory().get("/")
    request.user, request.session = draft.user, client.session
    with patch("requests.get") as get:
        with pytest.raises(ValueError):
            fetch_filing_detail(request, "illinois", value, "filing-id")
        with pytest.raises(ValueError):
            fetch_filing_detail(request, "illinois", "cook:law1", value)
        draft.previous_case_id = "case-id"
        draft.court_code = value
        with pytest.raises(CaseImportError):
            fetch_case(draft, "dummy-token")
        draft.court_code = "cook:law1"
        draft.previous_case_id = value
        with pytest.raises(CaseImportError):
            fetch_case(draft, "dummy-token")
        assert bank_payment_policy("illinois", value) == "unknown"
        get.assert_not_called()


def test_payment_service_validates_jurisdiction_before_attaching_key():
    with patch("requests.get") as get:
        with pytest.raises(ValueError):
            payment_accounts("../../api_user_settings/serverid?", "dummy-token")
        get.assert_not_called()

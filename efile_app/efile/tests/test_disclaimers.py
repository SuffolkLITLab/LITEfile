from typing import cast
from unittest.mock import Mock

import pytest
import requests
from django.core.cache import cache
from django.http import JsonResponse
from django.urls import reverse

from efile.services import disclaimers
from efile.tests import test_review_submit_flow

submission_draft = test_review_submit_flow.submission_draft


@pytest.fixture
def court_requirements():
    """Use the real court lookup here instead of the suite-wide stand-in, with nothing cached."""
    cache.clear()


def _assert_no_court_lookup():
    calls = cast(Mock, disclaimers.requests.get).call_args_list
    assert not any("/disclaimer_requirements" in call.args[0] for call in calls)


@pytest.fixture
def requirements(monkeypatch):
    rows = [{"code": "privacy", "name": "Privacy", "listorder": 1, "requirementText": "Redact <private> data."}]
    response = Mock()
    response.json.return_value = rows
    monkeypatch.setattr(disclaimers.requests, "get", Mock(return_value=response))
    return rows


@pytest.mark.django_db
def test_review_sanitizes_court_text(client, submission_draft, requirements):
    submission_draft.selected_payment_account_id = "pay"
    submission_draft.save()
    content = client.get(reverse("case_review", kwargs={"jurisdiction": "illinois"})).content.decode()
    assert "Before you submit your filing" in content
    assert "Redact  data." in content
    assert "I have read and accept the court requirements above." in content


@pytest.mark.django_db
def test_acceptance_is_bound_to_text_and_court(submission_draft, requirements):
    token = disclaimers.disclaimer_context(submission_draft)["disclaimer_token"]
    payload = {"confirm_submission": True, "disclaimer_token": token}
    assert disclaimers.validate_acceptance(submission_draft, payload)["requirements"][0]["code"] == "privacy"
    requirements[0]["requirementText"] = "Updated requirement"
    with pytest.raises(ValueError, match="Review and accept"):
        disclaimers.validate_acceptance(submission_draft, payload)
    requirements[0]["requirementText"] = "Redact <private> data."
    submission_draft.court_code = "different"
    with pytest.raises(ValueError, match="Review and accept"):
        disclaimers.validate_acceptance(submission_draft, payload)


@pytest.mark.django_db
@pytest.mark.parametrize("rows", [None, {}, [{"code": "a"}], [{"code": "a", "requirementText": ""}]])
def test_malformed_response_blocks_acceptance(submission_draft, requirements, rows):
    cast(Mock, disclaimers.requests.get).return_value.json.return_value = rows
    assert disclaimers.disclaimer_context(submission_draft)["disclaimer_token"] == ""
    with pytest.raises(disclaimers.DisclaimerUnavailable):
        disclaimers.validate_acceptance(submission_draft, {"confirm_submission": True})


@pytest.mark.django_db
def test_outage_disables_review(client, submission_draft, requirements):
    submission_draft.selected_payment_account_id = "pay"
    submission_draft.save()
    cast(Mock, disclaimers.requests.get).side_effect = requests.Timeout()
    content = client.get(reverse("case_review", kwargs={"jurisdiction": "illinois"})).content.decode()
    assert 'id="confirm-filing" disabled' in " ".join(content.split())
    assert "We could not load" in content


@pytest.mark.django_db
def test_submission_requires_and_records_acceptance(client, submission_draft, requirements, monkeypatch):
    monkeypatch.setattr("efile.views.submission.fee_quote_is_usable", lambda draft: True)
    upstream = Mock(return_value=JsonResponse({"success": True, "api_response": {}}))
    monkeypatch.setattr("efile.views.submission.forward_final_filing", upstream)
    url = "/api/submit-final-filing/"
    response = client.post(url, {"confirm_submission": True}, content_type="application/json")
    assert response.status_code == 400
    upstream.assert_not_called()
    token = disclaimers.disclaimer_context(submission_draft)["disclaimer_token"]
    response = client.post(
        url, {"confirm_submission": True, "disclaimer_token": token}, content_type="application/json"
    )
    assert response.status_code == 200
    submission_draft.refresh_from_db()
    acceptance = submission_draft.disclaimer_acceptance
    assert acceptance["requirements"][0]["text"] == "Redact <private> data."
    assert acceptance["accepted_at"]


@pytest.mark.django_db
def test_empty_requirements_still_accepts_review(submission_draft, requirements):
    requirements.clear()
    context = disclaimers.disclaimer_context(submission_draft)
    assert context["court_disclaimers"] == []
    assert (
        disclaimers.validate_acceptance(
            submission_draft, {"confirm_submission": True, "disclaimer_token": context["disclaimer_token"]}
        )["requirements"]
        == []
    )


@pytest.mark.django_db
@pytest.mark.parametrize("court", ["", "cook:law1"])
def test_upload_uses_state_notices_without_court_lookup(client, submission_draft, requirements, monkeypatch, court):
    submission_draft.court_code = court
    submission_draft.save()
    monkeypatch.setattr(
        "efile.views.upload_documents.config_loader.get_upload_disclaimers",
        lambda state: [{"name": "State notice", "text": "Protect <private> information."}],
    )
    content = client.get(reverse("upload_documents", kwargs={"jurisdiction": "illinois"})).content.decode()
    assert "Before you upload your documents" in content
    assert "Protect  information." in content
    assert "State notice" in content
    _assert_no_court_lookup()


@pytest.mark.django_db
def test_upload_hides_unconfigured_notices(client, submission_draft, requirements, monkeypatch):
    monkeypatch.setattr("efile.views.upload_documents.config_loader.get_upload_disclaimers", lambda state: [])
    content = client.get(reverse("upload_documents", kwargs={"jurisdiction": "illinois"})).content.decode()
    assert "Before you upload your documents" not in content
    assert "Choose a court" not in content
    _assert_no_court_lookup()


def test_state_notices_are_configured_and_independent():
    from efile.utils.config_loader import config_loader

    ma = config_loader.get_upload_disclaimers("massachusetts")
    vt = config_loader.get_upload_disclaimers("vermont")
    assert len(ma) == len(vt) == 2
    assert "Rule 1:24" in ma[0]["text"]
    assert "V.R.E.F." in vt[1]["text"]
    ma.clear()
    assert len(config_loader.get_upload_disclaimers("massachusetts")) == 2
    assert "Social Security Numbers" in config_loader.get_upload_disclaimers("illinois")[0]["text"]


@pytest.mark.django_db
def test_illinois_upload_displays_standard_notices_before_court_selection(client, submission_draft, requirements):
    submission_draft.court_code = ""
    submission_draft.save()
    content = client.get(reverse("upload_documents", kwargs={"jurisdiction": "illinois"})).content.decode()
    assert "Before you upload your documents" in content
    assert "Supreme Court Rule 15" in content
    assert "Supreme Court Rule 138" in content
    assert "Social Security Numbers" in content
    assert "&lt;p&gt;" not in content
    _assert_no_court_lookup()


def test_disclaimer_html_preserves_formatting_and_removes_unsafe_markup():
    from efile.templatetags.disclaimer_text import disclaimer_html

    rendered = disclaimer_html(
        '<p onclick="bad()"><strong>Notice</strong><span style="color:red"> text</span></p>'
        '<a href="https://example.com/rule" target="_blank">Rule</a>'
        '<a href="javascript:alert(1)">Unsafe link</a><img src="x" onerror="bad()">'
    )
    assert "<p><strong>Notice</strong> text</p>" in rendered
    assert '<a href="https://example.com/rule">Rule</a>' in rendered
    assert "<a>Unsafe link</a>" in rendered
    for unsafe in ["onclick", "style=", "target=", "javascript:", "<img", "onerror"]:
        assert unsafe not in rendered


@pytest.mark.django_db
def test_proxy_escapes_are_removed_before_display_and_acceptance(submission_draft, requirements):
    requirements[0]["requirementText"] = r"<p>Read <a href=\"https://example.com\">the rule</a>.</p>\nNext"
    text = disclaimers.court_disclaimers(submission_draft)[0]["text"]
    assert text == '<p>Read <a href="https://example.com">the rule</a>.</p>\nNext'


def test_disclaimer_html_handles_plain_text():
    from efile.templatetags.disclaimer_text import disclaimer_html

    assert disclaimer_html('<p>Read <a href="https://example.com">the rule</a>.</p>\nNext') == (
        '<p>Read <a href="https://example.com">the rule</a>.</p>Next'
    )
    assert disclaimer_html("First line\nSecond line") == "First line<br>Second line"


@pytest.mark.django_db
def test_review_reuses_the_lookup_but_submit_rechecks_the_court(submission_draft, requirements):
    get = cast(Mock, disclaimers.requests.get)
    token = disclaimers.disclaimer_context(submission_draft)["disclaimer_token"]
    disclaimers.disclaimer_context(submission_draft)
    assert get.call_count == 1
    requirements[0]["requirementText"] = "Updated requirement"
    with pytest.raises(ValueError, match="Review and accept"):
        disclaimers.validate_acceptance(submission_draft, {"confirm_submission": True, "disclaimer_token": token})
    assert get.call_count == 2
    # The recheck refreshed the cache, so reloading Review shows the new text.
    assert disclaimers.court_disclaimers(submission_draft)[0]["text"] == "Updated requirement"


def test_disclaimer_spacing_is_not_controlled_by_empty_court_paragraphs():
    from efile.templatetags.disclaimer_text import disclaimer_html

    assert disclaimer_html("<p>&nbsp;</p>\n<p>&nbsp;</p>\n<p>Notice</p>\n<p>&nbsp;</p>\n<p>Next</p>") == (
        "<p>Notice</p><p>Next</p>"
    )
    assert disclaimer_html("First\n\n\nSecond") == "First<br>Second"


@pytest.mark.django_db
def test_submission_without_draft_does_not_reach_tyler(client, monkeypatch, django_user_model):
    user = django_user_model.objects.create_user(username="no-draft", tyler_jurisdiction="illinois")
    client.force_login(user)
    session = client.session
    session.update({"jurisdiction": "illinois", "auth_tokens": {"TYLER-TOKEN-ILLINOIS": "test-token"}})
    session.save()
    upstream = Mock()
    monkeypatch.setattr("efile.views.submission.forward_final_filing", upstream)
    response = client.post("/api/submit-final-filing/", {"confirm_submission": True}, content_type="application/json")
    assert response.status_code == 400
    assert "Open your filing" in response.json()["error"]
    upstream.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize("body", ["", "{broken", "[]", "null"])
def test_malformed_submission_has_friendly_error(client, submission_draft, monkeypatch, body):
    upstream = Mock()
    monkeypatch.setattr("efile.views.submission.forward_final_filing", upstream)
    response = client.post("/api/submit-final-filing/", body, content_type="application/json")
    assert response.status_code == 400
    assert response.json()["error"] == "We could not read your submission. Reload the review page and try again."
    upstream.assert_not_called()

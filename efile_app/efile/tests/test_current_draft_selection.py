"""Tests for which filing a screen is working on.

The rule these protect: reading is not choosing. Looking at a page -- or an API
call that page fires -- must never decide that the filer is working on some
older filing, because that decision can land after they have started a new one
and silently put them back in the old filing's documents.
"""

import pytest
from django.urls import reverse

from efile.models import FilingDocument, FilingDraft
from efile.services.current_drafts import CURRENT_DRAFT_SESSION_KEY
from efile.services.filing_plans import ensure_plan_for_draft
from efile.tests.helpers import reviewed_document
from efile.workflow import ExistingCase, WorkflowStepKey

OPTIONS_URL = reverse("efile_options", kwargs={"jurisdiction": "illinois"})
UPLOAD_URL = reverse("upload_documents", kwargs={"jurisdiction": "illinois"})
EXTRACTION_STATUS_URL = reverse("document_extraction_status", kwargs={"jurisdiction": "illinois"})
START_FILING_URL = reverse("start_filing", kwargs={"jurisdiction": "illinois"})


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="draft-user", tyler_jurisdiction="illinois")


@pytest.fixture
def last_months_filing(user):
    """A finished-with but still active draft, with a document and a plan."""

    draft = FilingDraft.objects.create(
        user=user,
        jurisdiction="illinois",
        workflow_version=2,
        existing_case=ExistingCase.NEW,
        current_step=WorkflowStepKey.DOCUMENT_CHECKLIST,
        court_code="cook:cd1",
        court_name="Cook County Circuit Court - County Division",
        case_category_name="Miscellaneous",
        case_type_name="Name Change",
    )
    reviewed_document(
        draft=draft,
        role=FilingDocument.Role.LEAD,
        sort_order=0,
        name="last-months-petition.pdf",
        filing_type_name="Petition for Name Change",
    )
    ensure_plan_for_draft(draft)
    return draft


@pytest.fixture
def signed_in(client, user):
    """Signed in, with nothing marked as the filing being worked on."""

    client.force_login(user)
    session = client.session
    session["jurisdiction"] = "illinois"
    session["auth_tokens"] = {"TYLER-TOKEN-ILLINOIS": "token"}
    session.save()
    return client


def current_draft_id(client):
    return client.session.get(CURRENT_DRAFT_SESSION_KEY)


# --- Reading is not choosing -------------------------------------------------


@pytest.mark.django_db
def test_looking_at_the_options_page_does_not_pick_up_an_old_filing(signed_in, last_months_filing):
    signed_in.get(OPTIONS_URL)

    assert current_draft_id(signed_in) is None


@pytest.mark.django_db
def test_the_case_data_api_does_not_pick_up_an_old_filing(signed_in, last_months_filing):
    """The options page fires this on load, alongside the new-filing request."""

    response = signed_in.get(reverse("get_case_data_api"))

    assert response.status_code == 200
    assert current_draft_id(signed_in) is None


@pytest.mark.django_db
def test_a_late_read_cannot_undo_a_new_filing(signed_in, last_months_filing):
    """The bug, in the order it happened: start a filing, then a page load that
    was already in flight comes back and answers "which filing?" as well."""

    signed_in.post(START_FILING_URL, {"existing_case": "new"})
    new_draft_id = current_draft_id(signed_in)

    signed_in.get(reverse("get_case_data_api"))
    signed_in.get(OPTIONS_URL)

    assert current_draft_id(signed_in) == new_draft_id
    page = signed_in.get(UPLOAD_URL)
    assert page.context["filing_draft"]["id"] == new_draft_id
    assert list(page.context["documents"]) == []


# --- Starting a filing gives a blank slate -----------------------------------


@pytest.mark.django_db
def test_a_new_filing_starts_with_no_documents(signed_in, last_months_filing):
    signed_in.get(OPTIONS_URL)
    signed_in.post(START_FILING_URL, {"existing_case": "new"})

    page = signed_in.get(UPLOAD_URL)

    assert list(page.context["documents"]) == []
    assert page.context["filing_draft"]["id"] != last_months_filing.pk


@pytest.mark.django_db
def test_a_screen_with_no_filing_of_its_own_starts_one(signed_in, last_months_filing):
    """Reaching a step without a filing gets an empty one, not the newest one."""

    page = signed_in.get(UPLOAD_URL)

    assert page.context["filing_draft"]["id"] != last_months_filing.pk
    assert list(page.context["documents"]) == []
    last_months_filing.refresh_from_db()
    assert last_months_filing.documents.count() == 1


# --- Resuming still works, because the filer asks for it ---------------------


@pytest.mark.django_db
def test_the_options_page_offers_to_resume_the_filing_it_found(signed_in, last_months_filing):
    response = signed_in.get(OPTIONS_URL)

    assert response.context["resume_url"] == (
        reverse("document_checklist", kwargs={"jurisdiction": "illinois"}) + f"?draft={last_months_filing.pk}"
    )


@pytest.mark.django_db
def test_resuming_a_filing_by_name_picks_it_back_up(signed_in, last_months_filing):
    page = signed_in.get(f"{UPLOAD_URL}?draft={last_months_filing.pk}")

    assert page.context["filing_draft"]["id"] == last_months_filing.pk
    assert [document.name for document in page.context["documents"]] == ["last-months-petition.pdf"]
    # Resuming names this page's draft without replacing another tab's pointer.
    assert current_draft_id(signed_in) is None


@pytest.mark.django_db
def test_resuming_switches_away_from_the_filing_you_were_in(signed_in, last_months_filing):
    """Naming a filing is the filer saying "this one", whatever they were last in."""

    signed_in.post(START_FILING_URL, {"existing_case": "new"})
    new_draft_id = current_draft_id(signed_in)
    assert current_draft_id(signed_in) == new_draft_id

    page = signed_in.get(f"{UPLOAD_URL}?draft={last_months_filing.pk}")

    assert page.context["filing_draft"]["id"] == last_months_filing.pk
    assert current_draft_id(signed_in) == new_draft_id


@pytest.mark.django_db
def test_you_cannot_resume_someone_elses_filing(signed_in, last_months_filing, django_user_model):
    intruder = django_user_model.objects.create_user(username="intruder", tyler_jurisdiction="illinois")
    signed_in.force_login(intruder)
    session = signed_in.session
    session["jurisdiction"] = "illinois"
    session["auth_tokens"] = {"TYLER-TOKEN-ILLINOIS": "token"}
    session.save()

    page = signed_in.get(f"{UPLOAD_URL}?draft={last_months_filing.pk}")

    assert page.status_code == 409
    assert not FilingDraft.objects.filter(user=intruder).exists()


@pytest.mark.django_db
def test_a_filing_from_another_jurisdiction_is_not_resumed(signed_in, last_months_filing):
    FilingDraft.objects.filter(pk=last_months_filing.pk).update(jurisdiction="massachusetts")

    page = signed_in.get(f"{UPLOAD_URL}?draft={last_months_filing.pk}")

    assert page.status_code == 409


@pytest.mark.django_db
def test_unavailable_draft_returns_json_to_upload_and_status_requests(signed_in, last_months_filing):
    last_months_filing.status = FilingDraft.Status.SUBMITTED
    last_months_filing.save(update_fields=["status"])
    draft_query = f"?draft={last_months_filing.pk}"
    responses = (
        signed_in.post(UPLOAD_URL + draft_query, {"action": "ai_preference"}, HTTP_ACCEPT="application/json"),
        signed_in.get(EXTRACTION_STATUS_URL + draft_query, HTTP_ACCEPT="application/json"),
    )

    for response in responses:
        assert response.status_code == 409
        assert response.json()["error_code"] == "draft_unavailable"
        assert "My drafts" in response.json()["error"]
    page = signed_in.get(UPLOAD_URL + draft_query)
    assert page.status_code == 409
    assert page.headers["Content-Type"].startswith("text/html")


@pytest.mark.django_db
def test_expired_session_returns_json_to_upload_and_status_requests(client, last_months_filing):
    draft_query = f"?draft={last_months_filing.pk}"
    responses = (
        client.post(UPLOAD_URL + draft_query, {"action": "ai_preference"}, HTTP_ACCEPT="application/json"),
        client.get(EXTRACTION_STATUS_URL + draft_query, HTTP_ACCEPT="application/json"),
    )

    for response in responses:
        assert response.status_code == 401
        assert response.json()["error"] == "Sign in again to continue."


@pytest.mark.django_db
def test_missing_court_session_returns_json_to_upload_request(signed_in, last_months_filing):
    session = signed_in.session
    session.pop("auth_tokens", None)
    session.save()
    url = f"{UPLOAD_URL}?draft={last_months_filing.pk}"

    response = signed_in.post(url, {"action": "ai_preference"}, HTTP_ACCEPT="application/json")
    assert response.status_code == 401
    assert response.json()["error"] == "Sign in again to continue."
    assert signed_in.get(url).status_code == 302

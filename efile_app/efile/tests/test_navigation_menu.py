"""Getting somewhere else from wherever the filer is.

The complaint behind this was simple: part way through a filing there was no way
back to a home screen, to another filing, or to a fresh start without hunting for
a URL. Every screen now carries the same menu, and the two things people came to
do -- start a case, file into one they already have -- are one click from
anywhere.
"""

import pytest
from django.urls import reverse

from efile.models import FilingDraft
from efile.services.current_drafts import CURRENT_DRAFT_SESSION_KEY
from efile.workflow import ExistingCase, WorkflowStepKey

OPTIONS_URL = reverse("efile_options", kwargs={"jurisdiction": "illinois"})
START_URL = reverse("start_filing", kwargs={"jurisdiction": "illinois"})
UPLOAD_URL = reverse("upload_documents", kwargs={"jurisdiction": "illinois"})
FILING_PATH_URL = reverse("filing_path", kwargs={"jurisdiction": "illinois"})


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="nav-user", tyler_jurisdiction="illinois")


def sign_in(client, user):
    client.force_login(user)
    session = client.session
    session["jurisdiction"] = "illinois"
    session["auth_tokens"] = {"TYLER-TOKEN-ILLINOIS": "token"}
    session.save()


@pytest.mark.django_db
def test_every_signed_in_screen_carries_the_filing_menu(client, user):
    sign_in(client, user)

    content = client.get(OPTIONS_URL).content.decode()

    assert "Start filing" in content
    assert "File into an existing case" not in content
    assert "My draft e-filings" in content
    assert "My filings" in content
    assert "My filing plans" in content
    assert reverse("my_drafts", kwargs={"jurisdiction": "illinois"}) in content
    assert reverse("filing_statuses", kwargs={"jurisdiction": "illinois"}) in content


@pytest.mark.django_db
def test_the_menu_is_there_part_way_through_a_filing_too(client, user):
    """The whole complaint: mid-filing there was no way anywhere else."""

    sign_in(client, user)

    content = client.get(FILING_PATH_URL).content.decode()

    assert "Start filing" in content
    assert "My draft e-filings" in content
    assert START_URL in content


@pytest.mark.django_db
def test_the_menu_is_not_offered_to_someone_who_is_not_signed_in(client):
    content = client.get(OPTIONS_URL).content.decode()

    assert "My draft e-filings" not in content
    assert "Sign in" in content


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("chosen", "expected"),
    [("new", ExistingCase.NEW), ("existing", ExistingCase.EXISTING)],
)
def test_starting_a_filing_from_the_menu_knows_which_kind_it_is(client, user, chosen, expected):
    sign_in(client, user)

    response = client.post(START_URL, {"existing_case": chosen})

    draft = FilingDraft.objects.get(user=user)
    assert response.status_code == 302
    assert response.url == f"{UPLOAD_URL}?draft={draft.pk}"
    assert draft.existing_case == expected
    assert draft.current_step == WorkflowStepKey.UPLOAD_DOCUMENTS
    # The filing the filer just asked for is the one they are now in.
    assert client.session[CURRENT_DRAFT_SESSION_KEY] == draft.pk


@pytest.mark.django_db
def test_generic_start_defers_the_branch_until_confirm(client, user):
    sign_in(client, user)

    response = client.post(START_URL, {})

    assert response.url == f"{UPLOAD_URL}?draft={FilingDraft.objects.get(user=user).pk}"
    assert FilingDraft.objects.get(user=user).existing_case == ""


@pytest.mark.django_db
def test_starting_a_filing_never_reopens_the_last_one(client, user):
    """The way out of a draft is to start another one, so it must be another one."""

    sign_in(client, user)
    old = FilingDraft.objects.create(
        user=user,
        jurisdiction="illinois",
        existing_case=ExistingCase.EXISTING,
        docket_number="2024-EV-000123",
        current_step=WorkflowStepKey.REVIEW,
    )
    session = client.session
    session[CURRENT_DRAFT_SESSION_KEY] = old.pk
    session.save()

    client.post(START_URL, {})

    current_id = client.session[CURRENT_DRAFT_SESSION_KEY]
    assert current_id != old.pk
    assert FilingDraft.objects.get(pk=current_id).docket_number == ""
    old.refresh_from_db()
    assert old.status == FilingDraft.Status.DRAFT  # the old one is left alone, not thrown away


@pytest.mark.django_db
def test_starting_a_filing_needs_a_signed_in_filer(client):
    response = client.post(START_URL, {"existing_case": "new"})

    assert response.status_code == 302
    assert "/login/" in response.url
    assert not FilingDraft.objects.exists()


@pytest.mark.django_db
def test_starting_a_filing_needs_the_jurisdiction_signed_in_too(client, user):
    """Django login is not Tyler login: without the court's token there is nothing to file with."""

    client.force_login(user)

    response = client.post(START_URL, {"existing_case": "new"})

    assert response.status_code == 302
    assert "/login/" in response.url
    assert not FilingDraft.objects.exists()


@pytest.mark.django_db
def test_the_menu_does_not_start_filings_on_a_get(client, user):
    sign_in(client, user)

    response = client.get(START_URL)

    assert response.status_code == 405
    assert not FilingDraft.objects.exists()

"""A filing whose submit response was lost must not be reported as failed (#445)."""

import json
from datetime import timedelta

import pytest
from django.core.management import CommandError, call_command
from django.urls import reverse
from django.utils import timezone

from efile.models import FilingDraft, StaffRoleGrant
from efile.services.submission_outcomes import unconfirmed_submissions
from efile.tests.test_staff_tools import verified_client
from efile.workflow import WorkflowStepKey

pytestmark = pytest.mark.django_db


@pytest.fixture
def filer(django_user_model):
    return django_user_model.objects.create_user(username="outcome-filer", tyler_jurisdiction="illinois")


def _draft(user, status, *, jurisdiction="illinois", age=timedelta(0)):
    draft = FilingDraft.objects.create(
        user=user, jurisdiction=jurisdiction, current_step=WorkflowStepKey.REVIEW, court_code="sangamon"
    )
    FilingDraft.objects.filter(pk=draft.pk).update(status=status, updated_at=timezone.now() - age)
    draft.refresh_from_db()
    return draft


def _status(client, draft):
    return client.get(reverse("submission_status"), {"draft": draft.pk})


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (FilingDraft.Status.DRAFT, "not_sent"),
        (FilingDraft.Status.SUBMITTING, "pending"),
        (FilingDraft.Status.ERROR, "unknown"),
    ],
)
def test_status_reports_only_a_released_draft_as_safe_to_resubmit(client, filer, status, outcome):
    draft = _draft(filer, status)
    client.force_login(filer)

    response = _status(client, draft)

    assert response.status_code == 200
    assert response.json() == {"success": True, "outcome": outcome}


def test_status_sends_a_filing_that_reached_the_court_to_its_confirmation(client, filer):
    # The submit finished on the server, but its response never reached the browser.
    draft = _draft(filer, FilingDraft.Status.SUBMITTED)
    client.force_login(filer)

    response = _status(client, draft)

    payload = response.json()
    assert payload["outcome"] == "submitted"
    assert payload["redirect_url"] == f"/jurisdiction/illinois/filing-confirmation/?draft={draft.pk}"
    assert client.get(payload["redirect_url"]).status_code in (200, 302)


def test_status_does_not_reveal_another_filers_draft(client, filer, django_user_model):
    other = django_user_model.objects.create_user(username="other-filer", tyler_jurisdiction="illinois")
    draft = _draft(other, FilingDraft.Status.SUBMITTED)
    client.force_login(filer)

    response = _status(client, draft)

    assert response.status_code == 409
    assert "outcome" not in response.json()


def test_status_requires_a_named_draft_and_a_signed_in_filer(client, filer):
    draft = _draft(filer, FilingDraft.Status.SUBMITTED)

    assert "outcome" not in _status(client, draft).json()
    client.force_login(filer)
    assert client.get(reverse("submission_status")).status_code == 400


def test_status_is_read_only_and_never_reclaims_a_draft(client, filer):
    draft = _draft(filer, FilingDraft.Status.SUBMITTING)
    client.force_login(filer)

    for _ in range(3):
        _status(client, draft)

    draft.refresh_from_db()
    assert draft.status == FilingDraft.Status.SUBMITTING
    assert client.post(reverse("submission_status"), {"draft": draft.pk}).status_code == 405


def test_unconfirmed_submissions_are_errors_and_stale_claims_only(filer):
    stale = _draft(filer, FilingDraft.Status.SUBMITTING, age=timedelta(minutes=30))
    errored = _draft(filer, FilingDraft.Status.ERROR)
    _draft(filer, FilingDraft.Status.SUBMITTING, age=timedelta(minutes=1))  # still in flight
    _draft(filer, FilingDraft.Status.SUBMITTED, age=timedelta(days=1))
    _draft(filer, FilingDraft.Status.DRAFT, age=timedelta(days=1))
    elsewhere = _draft(filer, FilingDraft.Status.ERROR, jurisdiction="vermont")

    # Oldest first: the longest-waiting filer is checked first.
    assert list(unconfirmed_submissions()) == [stale, errored, elsewhere]
    assert list(unconfirmed_submissions(["illinois"])) == [stale, errored]


def test_command_lists_unconfirmed_submissions_without_filer_details(filer, capsys):
    stale = _draft(filer, FilingDraft.Status.SUBMITTING, age=timedelta(hours=2))

    call_command("unconfirmed_submissions")
    report = json.loads(capsys.readouterr().out)

    assert [row["draft_id"] for row in report["unconfirmed"]] == [stale.pk]
    row = report["unconfirmed"][0]
    assert row["status"] == "submitting" and row["court"] == "sangamon" and row["minutes"] >= 119
    assert set(row) == {"draft_id", "jurisdiction", "court", "status", "since", "minutes", "submission_operation"}
    with pytest.raises(CommandError):
        call_command("unconfirmed_submissions", "--fail-if-any")
    call_command("unconfirmed_submissions", "--fail-if-any", "--jurisdiction", "vermont")


def test_account_managers_see_how_many_filings_need_checking(client, filer, django_user_model):
    _draft(filer, FilingDraft.Status.SUBMITTING, age=timedelta(hours=1))
    _draft(filer, FilingDraft.Status.ERROR, jurisdiction="vermont")
    manager = django_user_model.objects.create_user(
        username="manager", password="a-strong-local-password", is_staff=True
    )
    StaffRoleGrant.objects.create(user=manager, jurisdiction="illinois", role="accounts")
    analyst = django_user_model.objects.create_user(
        username="analyst", password="a-strong-local-password", is_staff=True
    )
    StaffRoleGrant.objects.create(user=analyst, jurisdiction="illinois", role="analytics")

    page = verified_client(client, manager).get(reverse("litefile_staff:index"))
    assert page.status_code == 200
    assert b"Filings with an unknown outcome" in page.content
    assert b"1 filing may or may not have reached the court" in page.content

    client.logout()
    page = verified_client(client, analyst).get(reverse("litefile_staff:index"))
    assert b"Filings with an unknown outcome" not in page.content

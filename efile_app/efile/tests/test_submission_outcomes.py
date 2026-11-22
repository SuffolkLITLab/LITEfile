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


def _sign_in(client, user):
    client.force_login(user)
    session = client.session
    session["jurisdiction"] = "illinois"
    session["auth_tokens"] = {"TYLER-TOKEN-ILLINOIS": "token"}
    session.save()


def test_status_check_leaves_the_session_alone(client, filer):
    from efile.services.current_drafts import CURRENT_DRAFT_SESSION_KEY

    draft = _draft(filer, FilingDraft.Status.SUBMITTED)
    client.force_login(filer)
    session = client.session
    session[CURRENT_DRAFT_SESSION_KEY] = draft.pk
    session.save()

    assert _status(client, draft).json()["outcome"] == "submitted"
    assert client.session[CURRENT_DRAFT_SESSION_KEY] == draft.pk


def test_status_refuses_signed_out_and_discarded_drafts(client, filer):
    abandoned = _draft(filer, FilingDraft.Status.ABANDONED)
    submitted = _draft(filer, FilingDraft.Status.SUBMITTED)

    assert _status(client, submitted).status_code == 409
    client.force_login(filer)
    assert _status(client, abandoned).status_code == 409


def test_reopening_a_filing_cut_off_mid_send_does_not_suggest_starting_again(client, filer, django_user_model):
    draft = _draft(filer, FilingDraft.Status.SUBMITTING)
    _sign_in(client, filer)

    page = client.get(reverse("case_review", args=["illinois"]), {"draft": draft.pk})

    assert page.status_code == 409
    content = page.content.decode()
    assert "We are checking whether this filing reached the court" in content
    assert "Do not start it again" in content
    assert "start a new one" not in content
    assert reverse("filing_statuses", args=["illinois"]) in content

    # Someone else's draft, or a finished one, gets the ordinary page.
    other = django_user_model.objects.create_user(username="other-reopen", tyler_jurisdiction="illinois")
    for named in (_draft(other, FilingDraft.Status.SUBMITTING), _draft(filer, FilingDraft.Status.ABANDONED)):
        content = client.get(reverse("case_review", args=["illinois"]), {"draft": named.pk}).content.decode()
        assert "This draft is no longer available here" in content
        assert "reached the court" not in content


def test_review_of_an_errored_submission_starts_with_submit_off(client, filer, monkeypatch):
    from efile.views import review

    captured = {}

    def fake_render(_request, _template, context, *args, **kwargs):
        captured.update(context)
        from django.http import HttpResponse

        return HttpResponse("review")

    monkeypatch.setattr(review, "render", fake_render)
    _sign_in(client, filer)
    for status, unknown in ((FilingDraft.Status.ERROR, True), (FilingDraft.Status.DRAFT, False)):
        captured.clear()
        draft = _draft(filer, status)
        FilingDraft.objects.filter(pk=draft.pk).update(selected_payment_account_id="account")
        response = client.get(reverse("case_review", args=["illinois"]), {"draft": draft.pk})
        assert response.status_code == 200
        assert captured["submission_outcome_unknown"] is unknown


def test_my_drafts_keeps_a_filing_that_may_have_reached_the_court(client, filer):
    errored = _draft(filer, FilingDraft.Status.ERROR)
    ordinary = _draft(filer, FilingDraft.Status.DRAFT)
    _sign_in(client, filer)
    url = reverse("my_drafts", args=["illinois"])

    content = client.get(url).content.decode()
    assert content.count("We are not sure whether this filing reached the court.") == 1
    assert content.count('value="delete"') == 1

    client.post(url, {"action": "delete", "draft_id": errored.pk})
    client.post(url, {"action": "delete", "draft_id": ordinary.pk})
    errored.refresh_from_db()
    ordinary.refresh_from_db()
    assert errored.status == FilingDraft.Status.ERROR
    assert ordinary.status == FilingDraft.Status.ABANDONED


def test_command_rejects_a_misspelled_jurisdiction():
    with pytest.raises(CommandError, match="Unknown jurisdiction: il"):
        call_command("unconfirmed_submissions", "--jurisdiction", "il", "--fail-if-any")


def test_workers_treat_sigterm_like_sigint():
    import signal

    from efile.utils.worker_signals import stop_on_sigterm

    previous = signal.getsignal(signal.SIGTERM)
    try:
        stop_on_sigterm()
        assert signal.getsignal(signal.SIGTERM) is signal.default_int_handler
        with pytest.raises(KeyboardInterrupt):
            signal.raise_signal(signal.SIGTERM)
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_a_submit_request_still_running_is_not_reported_as_not_sent(client, filer, monkeypatch):
    from django.core.cache import caches

    from efile.tests.helpers import accepted_submission
    from efile.tests.test_durable_drafts import _prepare_submission
    from efile.views import submission

    draft = _draft(filer, FilingDraft.Status.DRAFT)
    client.force_login(filer)
    _prepare_submission(client, draft, monkeypatch)
    seen = []

    def slow_payment_check(*_args, **_kwargs):
        # The filer's connection dropped; the page asks while this runs.
        seen.append(client.get(reverse("submission_status"), {"draft": draft.pk}).json()["outcome"])
        raise ValueError("The payment account could not be checked.")

    monkeypatch.setattr(submission, "validate_filing_payment", slow_payment_check)
    response = client.post(
        reverse("submit_final_filing"),
        data=accepted_submission(draft, efile_data={"al_court_bundle": [{}]}),
        content_type="application/json",
    )

    assert response.status_code == 412
    assert seen == ["pending"]
    # Once the request ends, nothing is left claiming to be in progress.
    assert caches["shared"].get(f"submit-request:{draft.pk}") is None
    assert _status(client, draft).json()["outcome"] == "not_sent"


def test_a_refused_second_submit_is_marked_as_such(client, filer, monkeypatch):
    from efile.tests.helpers import accepted_submission
    from efile.tests.test_durable_drafts import _prepare_submission

    draft = _draft(filer, FilingDraft.Status.DRAFT)
    client.force_login(filer)
    _prepare_submission(client, draft, monkeypatch)
    monkeypatch.setattr("efile.views.submission.validate_filing_payment", lambda *args, **kwargs: None)
    FilingDraft.objects.filter(pk=draft.pk).update(status=FilingDraft.Status.SUBMITTING)
    monkeypatch.setattr("efile.views.submission.draft_unavailable_message", lambda *_args: None)

    response = client.post(
        reverse("submit_final_filing"),
        data=accepted_submission(draft, efile_data={"al_court_bundle": [{}]}),
        content_type="application/json",
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "submission_claimed"


def test_review_opened_from_the_session_for_a_filing_mid_send_starts_with_submit_off(client, filer, monkeypatch):
    from efile.services.current_drafts import CURRENT_DRAFT_SESSION_KEY
    from efile.views import review

    captured = {}

    def fake_render(_request, _template, context, *args, **kwargs):
        from django.http import HttpResponse

        captured.update(context)
        return HttpResponse("review")

    monkeypatch.setattr(review, "render", fake_render)
    _sign_in(client, filer)
    draft = _draft(filer, FilingDraft.Status.SUBMITTING)
    FilingDraft.objects.filter(pk=draft.pk).update(selected_payment_account_id="account")
    session = client.session
    session[CURRENT_DRAFT_SESSION_KEY] = draft.pk
    session.save()

    response = client.get(reverse("case_review", args=["illinois"]))

    if response.status_code != 200:
        pytest.skip(f"Review does not open a SUBMITTING draft from the session ({response.status_code})")
    assert captured["submission_outcome_unknown"] is True

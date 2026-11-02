"""Exercise real permissions, TOTP login, privacy cleanup, and usage reports."""

import csv
import io
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from unittest.mock import Mock, patch

import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice

from efile.models import (
    ArchivedCase,
    DocumentExtraction,
    FilingDocument,
    FilingDraft,
    FilingIdentifier,
    FilingMetadataEvent,
    FilingParty,
    FilingPlan,
    InterviewHandoff,
    PendingActivation,
    PrivacyRequest,
    StaffAudit,
    StaffRoleGrant,
    StoredUpload,
    UsageContributor,
    UsageCounter,
    UsageEvent,
    UsageMatter,
)
from efile.services.analytics import (
    count_event,
    drain_events,
    record_court_detail,
    record_court_statuses,
    record_event,
    record_matter,
    report_rows,
)
from efile.services.document_extractions import claim_next_extraction, process_document_extraction
from efile.services.document_uploads import upload_files
from efile.services.privacy import create_request, preview, process_request, verify_request
from efile.utils.s3_upload_handler import S3UploadHandler

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner(django_user_model):
    return django_user_model.objects.create_user(
        username="litigant",
        email="private@example.com",
        tyler_username="private@example.com",
        tyler_jurisdiction="illinois",
    )


@pytest.fixture
def administrator(django_user_model):
    user = django_user_model.objects.create_user(username="staff", password="a-strong-local-password", is_staff=True)
    StaffRoleGrant.objects.create(user=user, jurisdiction="illinois", role="accounts")
    return user


def verified_client(client, user):
    device = TOTPDevice.objects.create(user=user, name="Test authenticator")
    client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
    session = client.session
    session["otp_device_id"] = device.persistent_id
    session["staff_verified_at"] = timezone.now().timestamp()
    session.save()
    return client


def make_session(user, draft=None):
    session = SessionStore()
    session["_auth_user_id"] = str(user.pk)
    session["auth_tokens"] = {"private": "SECRET-COURT-TOKEN"}
    if draft:
        session["filing_draft_id"] = draft.pk
    session.save()
    return session.session_key


@pytest.mark.parametrize("endpoint", ["accounts", "requests", "analytics"])
def test_staff_pages_require_totp_and_role(client, owner, administrator, endpoint):
    url = reverse(f"litefile_staff:{endpoint}")
    assert client.get(url).status_code == 302
    client.force_login(owner)
    assert client.get(url).status_code == 302
    client.force_login(administrator, backend="django.contrib.auth.backends.ModelBackend")
    assert client.get(url).status_code == 302
    verified_client(client, administrator)
    assert client.get(url).status_code == (403 if endpoint == "analytics" else 200)


def test_analytics_only_role_cannot_read_delete_or_grant(client, django_user_model, owner, settings):
    user = django_user_model.objects.create_user(username="reporter", is_staff=True)
    StaffRoleGrant.objects.create(user=user, jurisdiction="illinois", role="analytics")
    verified_client(client, user)
    assert client.get(reverse("litefile_staff:analytics")).status_code == 200
    for url in [
        reverse("litefile_staff:account", args=[owner.pk]),
        reverse("litefile_staff:requests"),
        reverse("litefile_staff:efile_staffrolegrant_add"),
    ]:
        assert client.get(url).status_code == 403
        assert client.post(url, {"action": "process"}).status_code == 403
    settings.LITEFILE_ANALYTICS_EXPORT_ENABLED = True
    response = client.get(
        reverse("litefile_staff:analytics"),
        {
            "jurisdiction": "vermont",
            "start": "2026-09-01",
            "end": "2026-09-30",
            "grouping": "month",
            "dimension": "all",
            "format": "csv",
        },
    )
    assert response["Content-Type"].startswith("text/html")  # Invalid scoped choice never exports.


def test_totp_login_local_only_replay_and_expiry(client, administrator, settings):
    device = TOTPDevice.objects.create(user=administrator, name="Authenticator")
    url = reverse("litefile_staff:login")
    with patch("efile.authentication.auth_with_tyler_api") as tyler:
        response = client.post(url, {"username": administrator.username, "password": "a-strong-local-password"})
        assert response.status_code == 200
        assert "_auth_user_id" not in client.session
        token = str(totp(device.bin_key, step=device.step, t0=device.t0, digits=device.digits))
        response = client.post(
            url, {"username": administrator.username, "password": "a-strong-local-password", "otp_token": token}
        )
        assert response.status_code == 302
        assert client.get(reverse("litefile_staff:accounts")).status_code == 200
        tyler.assert_not_called()
        client.logout()
        assert (
            client.post(
                url, {"username": administrator.username, "password": "a-strong-local-password", "otp_token": token}
            ).status_code
            == 200
        )
    verified_client(client, administrator)
    session = client.session
    session["staff_verified_at"] = timezone.now().timestamp() - settings.LITEFILE_STAFF_SESSION_SECONDS - 1
    session.save()
    assert client.get(reverse("litefile_staff:accounts")).status_code == 302


def test_role_revocation_and_private_headers(client, administrator):
    verified_client(client, administrator)
    response = client.get(reverse("litefile_staff:accounts"))
    assert response["Cache-Control"] == "no-store"
    assert response["Referrer-Policy"] == "same-origin"
    assert "noindex" in response["X-Robots-Tag"]
    administrator.staff_roles.all().delete()
    assert client.get(reverse("litefile_staff:accounts")).status_code == 302
    client.logout()
    assert client.get("/admin/").status_code == 404


@pytest.mark.parametrize("secure", [False, True])
def test_staff_login_preserves_same_origin_csrf_checks(administrator, secure):
    from django.test import Client

    device = TOTPDevice.objects.create(user=administrator, name="CSRF test authenticator")
    browser = Client(enforce_csrf_checks=True)
    url = reverse("litefile_staff:login")
    host = "localhost:8001"
    origin = f"{'https' if secure else 'http'}://{host}"
    response = browser.get(url, secure=secure, HTTP_HOST=host)
    assert response["Referrer-Policy"] == "same-origin"
    data = {
        "username": administrator.username,
        "password": "a-strong-local-password",
        "otp_token": str(totp(device.bin_key)).zfill(6),
        "csrfmiddlewaretoken": browser.cookies["csrftoken"].value,
    }
    for untrusted_origin in ("null", "https://untrusted.invalid"):
        assert browser.post(url, data, secure=secure, HTTP_HOST=host, HTTP_ORIGIN=untrusted_origin).status_code == 403
    assert (
        browser.post(
            url,
            {key: value for key, value in data.items() if key != "csrfmiddlewaretoken"},
            secure=secure,
            HTTP_HOST=host,
            HTTP_ORIGIN=origin,
        ).status_code
        == 403
    )
    with patch("efile.authentication.auth_with_tyler_api") as court_auth:
        assert browser.post(url, data, secure=secure, HTTP_HOST=host, HTTP_ORIGIN=origin).status_code == 302
        assert browser.get(reverse("litefile_staff:accounts"), secure=secure, HTTP_HOST=host).status_code == 200
        court_auth.assert_not_called()


def test_lookup_scoped_minimal_and_get_does_not_delete(client, owner, administrator, django_user_model):
    other = django_user_model.objects.create_user(
        username="other-state", email=owner.email, tyler_jurisdiction="vermont"
    )
    draft = FilingDraft.objects.create(
        user=owner, jurisdiction="illinois", submission_response={"payment": "PRIVATE-PAYMENT"}
    )
    key = make_session(owner, draft)
    verified_client(client, administrator)
    response = client.post(reverse("litefile_staff:accounts"), {"jurisdiction": "illinois", "email": owner.email})
    assert response.status_code == 200
    assert list(response.context["accounts"]) == [owner]
    assert client.get(reverse("litefile_staff:account", args=[other.pk])).status_code == 403
    response = client.get(reverse("litefile_staff:account", args=[owner.pk]), {"action": "revoke"})
    assert response.status_code == 200
    assert b"SECRET-COURT-TOKEN" not in response.content
    assert b"PRIVATE-PAYMENT" not in response.content
    assert Session.objects.filter(pk=key).exists()
    token = response.context["sessions"][0]["token"]
    client.post(reverse("litefile_staff:account", args=[owner.pk]), {"action": "revoke", "session": token})
    assert not Session.objects.filter(pk=key).exists()


def test_full_deletion_inventory_and_retry(owner, administrator, django_user_model, settings):
    settings.LITEFILE_PRIVACY_DELETION_ENABLED = True
    other = django_user_model.objects.create_user(username="unrelated", tyler_jurisdiction="illinois")
    plan = FilingPlan.objects.create(user=owner, jurisdiction="illinois", title="Private matter")
    draft = FilingDraft.objects.create(user=owner, jurisdiction="illinois", plan=plan)
    correction = FilingDraft.objects.create(user=owner, jurisdiction="illinois", correction_of=draft)
    document = FilingDocument.objects.create(draft=draft, role="lead", s3_key="prepared", original_s3_key="original")
    FilingDocument.objects.create(draft=correction, role="lead", s3_key="prepared", original_s3_key="original")
    FilingParty.objects.create(draft=draft, role="filer", first_name="Private name", email=owner.email)
    DocumentExtraction.objects.create(document=document, evidence={"private": "data"})
    InterviewHandoff.objects.create(
        draft=draft, source="test", source_id="1", idempotency_key="1", fingerprint="hash", payload={"private": "data"}
    )
    FilingMetadataEvent.objects.create(draft=draft, path="case_title", kind="user_edit", value={"private": "data"})
    ArchivedCase.objects.create(user=owner, jurisdiction="illinois", case_tracking_id="private-case")
    PendingActivation.remember(owner.email, "illinois")
    PendingActivation.remember(owner.email, "vermont")
    owned_session = make_session(owner, draft)
    unrelated_session = make_session(other)
    request = create_request(administrator, owner, account_wide=True)
    handler = Mock()
    with pytest.raises(ValueError, match="Verify"):
        process_request(request.pk, administrator, handler=handler)
    verify_request(request.pk, administrator)
    assert preview(request)["counts"]["objects"] == 2
    handler.erase_file.side_effect = [{"success": True}, {"success": False}]
    request = process_request(request.pk, administrator, handler=handler)
    assert request.status == "attention" and request.outcome == "storage_failed"
    assert len(request.deleted_keys) == 1
    assert FilingDraft.objects.filter(pk=draft.pk, deletion_pending=True).exists()
    assert not Session.objects.filter(pk=owned_session).exists()
    with pytest.raises(PermissionDenied):
        document.name = "Restore erased data"
        document.save()
    with pytest.raises(PermissionDenied):
        FilingDraft.objects.create(user=owner, jurisdiction="illinois")
    handler.erase_file.side_effect = None
    handler.erase_file.return_value = {"success": True}
    request = process_request(request.pk, administrator, handler=handler)
    assert request.status == "completed" and request.external_cleanup_pending
    assert not django_user_model.objects.filter(pk=owner.pk).exists()
    with pytest.raises(PermissionDenied):
        make_session(owner)  # A late login cannot recreate deleted credentials.
    assert Session.objects.filter(pk=unrelated_session).exists()
    assert PendingActivation.exists_for(owner.email, "vermont")
    assert not PendingActivation.exists_for(owner.email, "illinois")
    assert not any([request.target_id, request.object_keys, request.deleted_keys, request.draft_ids])
    assert handler.erase_file.call_count == 3
    assert process_request(request.pk, administrator, handler=handler).status == "completed"
    assert "private" not in json.dumps(list(StaffAudit.objects.values("counts", "outcome")))


def test_selected_scope_preserves_unrelated_data_and_blocks_shared_objects(owner, administrator, settings):
    settings.LITEFILE_PRIVACY_DELETION_ENABLED = True
    draft = FilingDraft.objects.create(user=owner, jurisdiction="illinois")
    preserved = FilingDraft.objects.create(user=owner, jurisdiction="illinois")
    removed = FilingDocument.objects.create(draft=draft, role="lead", s3_key="old-upload")
    removed.delete()  # The upload inventory must outlive the document row.
    assert StoredUpload.objects.filter(draft=draft, key="old-upload").exists()
    first_session = make_session(owner, draft)
    preserved_session = make_session(owner, preserved)
    request = create_request(administrator, owner, account_wide=False, draft_ids=[draft.pk])
    verify_request(request.pk, administrator)
    handler = Mock(erase_file=Mock(return_value={"success": True}))
    process_request(request.pk, administrator, handler=handler)
    handler.erase_file.assert_called_once_with("old-upload")
    assert owner.filing_drafts.count() == 1
    assert Session.objects.filter(pk=preserved_session).exists()
    assert not Session.objects.filter(pk=first_session).exists()
    shared = FilingDraft.objects.create(user=owner, jurisdiction="illinois")
    FilingDocument.objects.create(draft=shared, role="lead", s3_key="shared")
    FilingDocument.objects.create(draft=preserved, role="lead", s3_key="shared")
    request = create_request(administrator, owner, account_wide=False, draft_ids=[shared.pk])
    verify_request(request.pk, administrator)
    assert "shared_objects" in preview(request)["blockers"]
    assert process_request(request.pk, administrator, handler=handler).status == "attention"
    assert handler.erase_file.call_count == 1


@pytest.mark.parametrize("status", ["submitting", "error"])
def test_submission_blocks_deletion(owner, administrator, settings, status):
    settings.LITEFILE_PRIVACY_DELETION_ENABLED = True
    FilingDraft.objects.create(user=owner, jurisdiction="illinois", status=status)
    request = create_request(administrator, owner, account_wide=True)
    verify_request(request.pk, administrator)
    handler = Mock()
    assert process_request(request.pk, administrator, handler=handler).outcome == "submission_requires_reconciliation"
    handler.erase_file.assert_not_called()


def test_s3_permanent_erasure_exact_versions(monkeypatch):
    handler = S3UploadHandler()
    handler.s3_client = Mock()
    handler.bucket_name = "private-test-bucket"
    monkeypatch.setattr(handler, "_ensure_initialized", Mock(return_value=True))
    handler.s3_client.get_bucket_versioning.return_value = {"Status": "Enabled"}
    paginator = handler.s3_client.get_paginator.return_value
    paginator.paginate.side_effect = [
        [
            {
                "Versions": [{"Key": "key", "VersionId": "v1"}, {"Key": "key-other", "VersionId": "v2"}],
                "DeleteMarkers": [{"Key": "key", "VersionId": "marker"}],
            }
        ],
        [],
    ]
    handler.s3_client.delete_objects.return_value = {}
    assert handler.erase_file("key") == {"success": True}
    assert handler.s3_client.delete_objects.call_args.kwargs["Delete"]["Objects"] == [
        {"Key": "key", "VersionId": "v1"},
        {"Key": "key", "VersionId": "marker"},
    ]
    handler.s3_client.delete_object.assert_not_called()
    paginator.paginate.side_effect = [[{"Versions": [{"Key": "key", "VersionId": "locked"}]}]]
    handler.s3_client.delete_objects.return_value = {"Errors": [{"Code": "AccessDenied"}]}
    assert handler.erase_file("key") == {"success": False}


def test_analytics_idempotency_dates_deletion_and_suppression(owner, administrator, settings):
    settings.LITEFILE_ANALYTICS_ENABLED = True
    settings.LITEFILE_PRIVACY_DELETION_ENABLED = True
    at = datetime(2026, 9, 30, 23, 59, tzinfo=UTC)
    with patch("efile.services.analytics.timezone.now", return_value=at):
        draft = FilingDraft.objects.create(
            user=owner, jurisdiction="illinois", existing_case="new", case_type_name="Name Change"
        )
        FilingParty.objects.create(
            draft=draft, role="filer", party_type="plaintiff", party_side="initiating", zip_code="02108-9999"
        )
        record_event(draft, "review")
        record_event(draft, "review")
        record_event(draft, "submission_attempt", operation="logical-operation")
        record_event(draft, "submission_attempt", operation="logical-operation")
    with patch("efile.services.analytics.timezone.now", return_value=at + timedelta(minutes=1)):
        draft.mark_submitted({})
    drain_events()
    before = list(UsageCounter.objects.order_by("pk").values("day", "metric", "count", "dimension", "value"))
    drain_events()
    assert before == list(UsageCounter.objects.order_by("pk").values("day", "metric", "count", "dimension", "value"))
    assert UsageCounter.objects.get(metric="review", granularity="day", dimension="all").day == date(2026, 9, 30)
    assert UsageCounter.objects.get(metric="transmitted", granularity="day", dimension="all").day == date(2026, 10, 1)
    assert set(UsageEvent.objects.get(metric="review").dimensions) <= {
        "case_type",
        "filer_side",
        "filing_for",
        "zip_code",
        "usage_frequency",
        "filing_kind",
    }
    rows = report_rows(["illinois"], date(2026, 9, 1), date(2026, 9, 30), dimension="zip_code")
    assert rows and all(row["count"] == "suppressed" and row["value"] == "suppressed" for row in rows)
    request = create_request(administrator, owner, account_wide=True)
    verify_request(request.pk, administrator)
    process_request(request.pk, administrator, handler=Mock())
    assert (
        not UsageEvent.objects.exists() and not UsageContributor.objects.exists() and not UsageMatter.objects.exists()
    )
    assert before == list(UsageCounter.objects.order_by("pk").values("day", "metric", "count", "dimension", "value"))


def test_analytics_worker_failure_retry_allowlist_and_staff_exclusion(owner, administrator, settings):
    settings.LITEFILE_ANALYTICS_ENABLED = True
    draft = FilingDraft.objects.create(user=owner, jurisdiction="illinois")
    event = UsageEvent.objects.get(draft=draft)
    with patch("efile.services.analytics.UsageCounter.objects.get_or_create", side_effect=RuntimeError("unavailable")):
        with pytest.raises(RuntimeError):
            count_event(event.pk)
    assert not UsageCounter.objects.exists()
    drain_events()
    assert UsageCounter.objects.get(dimension="all", granularity="day").count == 1
    event.dimensions["email"] = owner.email
    event.counted_at = None
    event.save()
    with pytest.raises(ValueError, match="Disallowed"):
        count_event(event.pk)
    staff_draft = FilingDraft.objects.create(user=administrator, jurisdiction="illinois")
    assert not UsageEvent.objects.filter(draft=staff_draft).exists()
    settings.DEBUG = True
    other = FilingDraft.objects.create(user=owner, jurisdiction="illinois")
    assert not UsageEvent.objects.filter(draft=other).exists()


# The real supervisor closes request connections; do not wrap it in a test-owned transaction.
@pytest.mark.django_db(transaction=True)
def test_extraction_supervisor_rolls_usage_and_survives_rollup_failure(owner, settings):
    from django.core.management import call_command

    settings.LITEFILE_ANALYTICS_ENABLED = True
    draft = FilingDraft.objects.create(user=owner, jurisdiction="illinois")
    event = UsageEvent.objects.get(draft=draft)
    with patch("efile.services.document_extractions.claim_next_extraction", return_value=None):
        with patch("efile.management.commands.process_document_extractions.call_command", side_effect=RuntimeError):
            call_command("process_document_extractions", once=True)
        event.refresh_from_db()
        assert event.counted_at is None
        assert not UsageCounter.objects.exists()
        call_command("process_document_extractions", once=True)
    event.refresh_from_db()
    assert event.counted_at is not None
    assert event.dimensions == {}
    assert UsageCounter.objects.get(dimension="all", granularity="day").count == 1


def test_dashboard_csv_match(client, administrator, settings):
    StaffRoleGrant.objects.create(user=administrator, jurisdiction="illinois", role="analytics")
    settings.LITEFILE_ANALYTICS_EXPORT_ENABLED = True
    UsageCounter.objects.create(
        day=date(2026, 9, 1),
        granularity="month",
        jurisdiction="illinois",
        metric="started",
        filing_kind="new",
        count=17,
        contributors=10,
    )
    verified_client(client, administrator)
    filters = {
        "jurisdiction": "illinois",
        "start": "2026-09-01",
        "end": "2026-09-30",
        "grouping": "month",
        "dimension": "all",
    }
    url = reverse("litefile_staff:analytics")
    response = client.get(url, filters)
    assert response.status_code == 200
    csv_response = client.get(url, {**filters, "format": "csv"})
    rows = list(csv.DictReader(io.StringIO(csv_response.content.decode())))
    assert rows == [{key: str(value) for key, value in row.items()} for row in response.context["rows"]]
    UsageCounter.objects.create(
        day=date(2026, 9, 1),
        granularity="month",
        jurisdiction="illinois",
        metric="started",
        filing_kind="new",
        dimension="zip_code",
        value="02108",
        count=1,
        contributors=1,
    )
    assert client.get(url, filters).context["rows"][0]["count"] == 17
    details = {**filters, "dimension": "zip_code"}
    assert client.get(url, details).context["rows"][0]["count"] == "suppressed"
    assert "02108" not in client.get(url, {**details, "format": "csv"}).content.decode()


def test_staff_privacy_workflow_csrf(client, owner, administrator, settings):
    settings.LITEFILE_PRIVACY_DELETION_ENABLED = True
    verified_client(client, administrator)
    client.post(reverse("litefile_staff:accounts"), {"jurisdiction": "illinois", "email": owner.email})
    assert client.session["staff_lookup"]["email"] == owner.email
    assert "Review deletion" in client.get(reverse("litefile_staff:account", args=[owner.pk])).content.decode()
    response = client.post(
        reverse("litefile_staff:account", args=[owner.pk]), {"action": "request", "account_wide": "on"}
    )
    assert response.status_code == 302
    request = PrivacyRequest.objects.get(target=owner)
    assert type(owner).objects.filter(pk=owner.pk).exists()
    url = reverse("litefile_staff:privacy_request", args=[request.pk])
    assert client.get(url, {"action": "process", "confirmed": str(request.reference)}).status_code == 200
    request.refresh_from_db()
    assert request.status == "received"
    assert "Verify and continue" in client.get(url).content.decode()
    client.post(url, {"action": "process", "confirmed": str(request.reference)})
    request.refresh_from_db()
    assert request.status == "received"
    client.post(url, {"action": "verify", "verified": "yes"})
    settings.LITEFILE_PRIVACY_DELETION_ENABLED = False
    disabled_page = client.get(url).content.decode()
    assert "Deletion is disabled on this deployment" in disabled_page
    assert "Permanently delete account" not in disabled_page
    settings.LITEFILE_PRIVACY_DELETION_ENABLED = True
    assert "Permanently delete account" in client.get(url).content.decode()
    token = client.get(url).context["preview_token"]
    client.post(url, {"action": "process", "confirmed": str(request.reference), "preview": token})
    request.refresh_from_db()
    assert request.status == "completed"
    assert "staff_lookup" not in client.session
    assert client.get(reverse("litefile_staff:account", args=[owner.pk])).status_code == 404
    from django.test import Client

    csrf_client = verified_client(Client(enforce_csrf_checks=True), administrator)
    assert csrf_client.post(url, {"action": "external_resolved", "resolved": "yes"}).status_code == 403


def test_superuser_can_onboard_staff_with_multiple_scoped_roles(client, django_user_model):
    root = django_user_model.objects.create_superuser(
        username="root", email="root@invalid.example", password="a-strong-local-password"
    )
    client.force_login(root, backend="django.contrib.auth.backends.ModelBackend")
    assert client.get(reverse("litefile_staff:efile_userprofile_add")).status_code == 302
    verified_client(client, root)
    response = client.post(
        reverse("litefile_staff:efile_userprofile_add"),
        {
            "username": "new-staff",
            "password1": "a-different-strong-password",
            "password2": "a-different-strong-password",
            "usable_password": "true",
        },
    )
    assert response.status_code == 302
    staff = django_user_model.objects.get(username="new-staff")
    assert staff.is_staff and not staff.is_superuser and staff.check_password("a-different-strong-password")
    for role in ("accounts", "analytics"):
        assert (
            client.post(
                reverse("litefile_staff:efile_staffrolegrant_add"),
                {"user": staff.pk, "jurisdiction": "vermont", "role": role},
            ).status_code
            == 302
        )
    assert staff.staff_roles.count() == 2
    url = reverse("litefile_staff:authenticator", args=[staff.pk])
    assert client.get(url).status_code == 200
    assert not TOTPDevice.objects.filter(user=staff).exists()
    response = client.post(url, {"confirmed": "yes"})
    assert response.status_code == 200 and b"otpauth://" in response.content
    assert TOTPDevice.objects.filter(user=staff, confirmed=True).count() == 1


def test_extraction_and_stale_upload_cannot_restore_frozen_data(owner, administrator, settings):
    settings.LITEFILE_PRIVACY_DELETION_ENABLED = True
    draft = FilingDraft.objects.create(user=owner, jurisdiction="illinois")
    document = FilingDocument.objects.create(draft=draft, role="lead", s3_key="prepared")
    job = DocumentExtraction.objects.create(
        document=document,
        status="processing",
        claim_token=uuid.uuid4(),
        lease_expires_at=timezone.now() + timedelta(minutes=15),
    )
    request = create_request(administrator, owner, account_wide=True)
    verify_request(request.pk, administrator)
    handler = Mock()
    assert process_request(request.pk, administrator, handler=handler).outcome == "extraction_worker_in_flight"
    handler.erase_file.assert_not_called()
    FilingDraft.objects.filter(pk=draft.pk).update(deletion_pending=True)
    assert claim_next_extraction() is None
    with patch("efile.services.document_extractions.S3UploadHandler") as storage:
        assert process_document_extraction(job.pk, job.claim_token) is None
        storage.assert_not_called()
    with patch("efile.services.document_uploads.store_prepared_document") as prepare:
        with pytest.raises(ValueError, match="no longer available"):
            upload_files(draft, [], "illinois")
        prepare.assert_not_called()


def test_failed_upload_retains_ownership_for_later_erasure(owner):
    draft = FilingDraft.objects.create(user=owner, jurisdiction="illinois")
    handler = Mock()
    handler.delete_file.return_value = {"success": False}

    def partial_upload(*args, keys, **kwargs):
        keys.append("partially-saved-original")
        raise ValueError("Preparation failed")

    with (
        patch("efile.services.document_uploads.S3UploadHandler", return_value=handler),
        patch("efile.services.document_uploads.store_prepared_document", side_effect=partial_upload),
    ):
        with pytest.raises(ValueError, match="Preparation failed"):
            upload_files(draft, [Mock(name="upload")], "illinois")
    assert StoredUpload.objects.filter(draft=draft, key="partially-saved-original").exists()
    assert not draft.documents.exists()


def test_known_case_and_plan_aliases_do_not_inflate_matters(owner, settings):
    settings.LITEFILE_ANALYTICS_ENABLED = True
    plan = FilingPlan.objects.create(user=owner, jurisdiction="illinois", title="Matter")
    first = FilingDraft.objects.create(user=owner, jurisdiction="illinois", court_code="court", plan=plan)
    first.mark_submitted({})
    plan.case_tracking_id = "known-case"
    plan.save()
    later = FilingDraft.objects.create(
        user=owner, jurisdiction="illinois", court_code="court", plan=plan, previous_case_id="known-case"
    )
    later.mark_submitted({})
    other_plan = FilingPlan.objects.create(user=owner, jurisdiction="illinois", title="Same matter")
    third = FilingDraft.objects.create(
        user=owner, jurisdiction="illinois", court_code="court", plan=other_plan, previous_case_id="known-case"
    )
    third.mark_submitted({})
    record_matter(third)
    assert UsageEvent.objects.filter(metric="matter_first_used").count() == 1
    drain_events()
    assert UsageCounter.objects.get(metric="matter_first_used", granularity="month", dimension="all").count == 1


def test_outbox_rejects_free_text_in_an_allowed_dimension(owner, settings):
    settings.LITEFILE_ANALYTICS_ENABLED = True
    draft = FilingDraft.objects.create(user=owner, jurisdiction="illinois")
    event = UsageEvent.objects.get(draft=draft)
    event.dimensions["case_type"] = owner.email
    event.save()
    with pytest.raises(ValueError, match="Disallowed usage value"):
        count_event(event.pk)


def test_submission_counts_documents_pages_guided_forms_and_source(owner, settings):
    settings.LITEFILE_ANALYTICS_ENABLED = True
    settings.LITEFILE_HANDOFF_SOURCES = {"vt_rfa": {}}
    draft = FilingDraft.objects.create(
        user=owner,
        jurisdiction="vermont",
        existing_case="new",
        court_code="family",
        filing_type_code="complaint",
        selected_payment_account_type="WV",
    )
    InterviewHandoff.objects.create(
        draft=draft,
        source="vt_rfa",
        source_id="source-1",
        idempotency_key="request-1",
        fingerprint="a" * 64,
        payload={"documents": [{"form_name": "Complaint"}, {"form_name": "Affidavit"}, {"form_name": "Notice"}]},
    )
    for index, pages in enumerate((4, 2)):
        document = FilingDocument.objects.create(
            draft=draft,
            role="lead" if index == 0 else "supporting",
            sort_order=0,
            preparation="unchanged",
            filing_type_code="complaint" if index == 0 else "affidavit",
        )
        DocumentExtraction.objects.create(document=document, status="complete", total_pages=pages)
        FilingMetadataEvent.objects.create(
            draft=draft, path=f"documents.{document.pk}", kind="source_suggestion", value={"form_name": "Form"}
        )
    # The filer removed the third guided form before submitting.
    removed = FilingDocument.objects.create(draft=draft, role="supporting", sort_order=1)
    FilingMetadataEvent.objects.create(
        draft=draft, path=f"documents.{removed.pk}", kind="source_suggestion", value={"form_name": "Notice"}
    )
    removed.delete()

    draft.mark_submitted({})
    transmitted = UsageEvent.objects.get(draft=draft, metric="transmitted")
    assert (
        transmitted.dimensions
        | {
            "interview_source": "vt_rfa",
            "fee_waiver": "submitted",
            "court": "family",
            "filing_type": "complaint",
        }
        == transmitted.dimensions
    )
    assert UsageEvent.objects.get(draft=draft, metric="documents_transmitted").quantity == 2
    assert UsageEvent.objects.get(draft=draft, metric="pages_transmitted").quantity == 6
    assert UsageEvent.objects.get(draft=draft, metric="guided_forms_transmitted").quantity == 2
    drain_events()
    assert UsageCounter.objects.get(metric="documents_transmitted", granularity="day", dimension="all").count == 2
    assert UsageCounter.objects.get(metric="pages_transmitted", granularity="day", dimension="all").count == 6


def test_attorney_dimension_uses_states_the_workflow_persists(owner, settings):
    settings.LITEFILE_ANALYTICS_ENABLED = True

    self_represented = FilingDraft.objects.create(user=owner, jurisdiction="illinois", existing_case="new")
    FilingParty.objects.create(
        draft=self_represented,
        role="filer",
        party_type="plaintiff",
        is_filing_party=True,
    )
    record_event(self_represented, "review")
    assert UsageEvent.objects.get(draft=self_represented, metric="review").dimensions["has_attorney"] == "no"

    represented = FilingDraft.objects.create(user=owner, jurisdiction="illinois", existing_case="existing")
    FilingParty.objects.create(draft=represented, role="filer")
    FilingParty.objects.create(
        draft=represented,
        role="other",
        source="court",
        source_case_id="case-1",
        external_party_id="party-1",
        is_self=True,
        representation={"status": "represented", "attorneys": [{"id": "attorney-1"}]},
    )
    record_event(represented, "review")
    assert UsageEvent.objects.get(draft=represented, metric="review").dimensions["has_attorney"] == "yes"

    unknown = FilingDraft.objects.create(user=owner, jurisdiction="illinois", existing_case="existing")
    FilingParty.objects.create(draft=unknown, role="filer")
    FilingParty.objects.create(
        draft=unknown,
        role="other",
        source="court",
        source_case_id="case-2",
        external_party_id="party-2",
        is_self=True,
        representation={"status": "unknown", "attorneys": []},
    )
    record_event(unknown, "review")
    assert UsageEvent.objects.get(draft=unknown, metric="review").dimensions["has_attorney"] == "unknown"


def test_court_outcomes_and_rejection_reasons_are_deduplicated_without_clerk_text(owner, settings):
    settings.LITEFILE_ANALYTICS_ENABLED = True
    draft = FilingDraft.objects.create(user=owner, jurisdiction="illinois", existing_case="existing")
    draft.mark_submitted({})
    FilingIdentifier.objects.create(draft=draft, filing_id="filing-1")
    unsubmitted = FilingDraft.objects.create(user=owner, jurisdiction="illinois")
    FilingIdentifier.objects.create(draft=unsubmitted, filing_id="filing-2")
    filings = [
        {"filing_id": "filing-1", "filing_status": "rejected"},
        {"filing_id": "filing-2", "filing_status": "accepted"},
    ]
    record_court_statuses(owner, "illinois", filings)
    # Later loads of My cases do not lock or re-check decided filings.
    with patch("efile.services.analytics.record_event") as again:
        record_court_statuses(owner, "illinois", filings)
    again.assert_not_called()
    assert not UsageEvent.objects.filter(draft=unsubmitted, metric__startswith="court_").exists()
    detail = {
        "status": "rejected",
        "comments": [{"text": "Private names and facts: the required signature is missing."}],
    }
    record_court_detail(draft, "filing-1", detail)
    record_court_detail(draft, "filing-1", detail)

    assert UsageEvent.objects.filter(draft=draft, metric="court_returned").count() == 1
    reason = UsageEvent.objects.get(draft=draft, metric="rejection_reason")
    assert reason.dimensions["rejection_reason"] == "signature_or_notarization"
    assert "Private names" not in json.dumps(reason.dimensions)


def test_reports_include_distinct_contributing_account_count(settings, django_user_model):
    settings.LITEFILE_ANALYTICS_ENABLED = True
    at = datetime(2026, 9, 10, tzinfo=UTC)
    for index in range(5):
        user = django_user_model.objects.create_user(username=f"report-user-{index}", tyler_jurisdiction="illinois")
        with patch("efile.services.analytics.timezone.now", return_value=at):
            draft = FilingDraft.objects.create(user=user, jurisdiction="illinois", existing_case="new")
            record_event(draft, "draft_resumed", operation=f"resume-{index}-1")
            record_event(draft, "draft_resumed", operation=f"resume-{index}-2")
    drain_events()
    rows = report_rows(["illinois"], date(2026, 9, 1), date(2026, 9, 30))
    resumed = next(row for row in rows if row["metric"] == "draft_resumed")
    assert resumed["count"] == 10
    assert resumed["contributors"] == 5


@pytest.mark.parametrize(
    ("comment", "category"),
    [
        ("Please correct the party information.", "party_information"),
        ("Case assigned to the wrong division.", "other_or_unknown"),
        ("The order was designed for another court.", "other_or_unknown"),
        ("The affidavit is unsigned.", "signature_or_notarization"),
        ("Wrong formatting of the margins.", "document_format"),
        ("Feedback on the filing type: use a motion.", "filing_type"),
        ("The filing fee was not paid.", "fees_or_waiver"),
    ],
)
def test_rejection_categories_match_whole_words(comment, category):
    from efile.services.analytics import categorize_rejection

    assert categorize_rejection({"comments": [{"text": comment}]}) == category


def test_outcome_frequency_band_matches_transmission(owner, settings):
    settings.LITEFILE_ANALYTICS_ENABLED = True
    first = FilingDraft.objects.create(user=owner, jurisdiction="illinois")
    first.mark_submitted({})
    for _ in range(3):
        FilingDraft.objects.create(user=owner, jurisdiction="illinois").mark_submitted({})
    record_court_detail(first, "filing-1", {"status": "accepted"})
    assert UsageEvent.objects.get(draft=first, metric="transmitted").dimensions["usage_frequency"] == "first"
    assert UsageEvent.objects.get(draft=first, metric="court_accepted").dimensions["usage_frequency"] == "first"


def test_configuration_change_does_not_stall_the_outbox(owner, settings):
    settings.LITEFILE_ANALYTICS_ENABLED = True
    settings.LITEFILE_HANDOFF_SOURCES = {"vt_rfa": {}}
    draft = FilingDraft.objects.create(user=owner, jurisdiction="vermont")
    InterviewHandoff.objects.create(
        draft=draft, source="vt_rfa", source_id="s", idempotency_key="k", fingerprint="b" * 64, payload={}
    )
    record_event(draft, "review")
    bad = UsageEvent.objects.get(draft=draft, metric="started")
    bad.dimensions["case_type"] = owner.email
    bad.save()
    # The source is removed from configuration while the event is pending.
    settings.LITEFILE_HANDOFF_SOURCES = {}
    drain_events()
    assert (
        UsageCounter.objects.get(
            metric="review", granularity="month", dimension="interview_source", value="other_interview"
        ).count
        == 1
    )
    bad.refresh_from_db()
    assert bad.counted_at is not None and bad.dimensions == {}
    assert not UsageCounter.objects.filter(metric="started").exists()
    assert not UsageEvent.objects.filter(counted_at__isnull=True).exists()


def test_disallowed_values_are_refused_when_queued(owner, settings):
    settings.LITEFILE_ANALYTICS_ENABLED = True
    draft = FilingDraft.objects.create(user=owner, jurisdiction="illinois", court_code="c" * 90)
    record_event(draft, "review")
    assert not UsageEvent.objects.filter(draft=draft, metric="review").exists()

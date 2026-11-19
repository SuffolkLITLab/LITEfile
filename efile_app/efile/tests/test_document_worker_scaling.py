import uuid
from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from efile.management.commands import coordinate_document_workers as coordinator_module
from efile.management.commands.coordinate_document_workers import Coordinator, FlyControlError
from efile.models import DocumentPreparationJob, DocumentWorkerControl, DocumentWorkerHeartbeat, FilingDraft
from efile.services import document_worker_scaling as scaling
from efile.services.current_drafts import CURRENT_DRAFT_SESSION_KEY
from efile.tests.pdf_helpers import pdf_bytes

PRIMARY = {scaling.PREPARATION: "preparation1", scaling.EXTRACTION: "extraction1"}
SPARE = {scaling.PREPARATION: "preparation2", scaling.EXTRACTION: "extraction2"}

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def control(settings):
    settings.DOCUMENT_WORKER_SCALING_CONTROL = True
    settings.DOCUMENT_PREPARATION_ASYNC = True
    return DocumentWorkerControl.objects.create(
        pk=scaling.CONTROL_ID,
        enabled=True,
        coordinator_seen_at=timezone.now(),
        active_machine_ids=list(PRIMARY.values()),
    )


@pytest.fixture
def draft(control, client, django_user_model):
    user = django_user_model.objects.create_user(username="queue-check", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(user=user, jurisdiction="illinois", workflow_version=2)
    client.force_login(user)
    session = client.session
    session[CURRENT_DRAFT_SESSION_KEY] = draft.pk
    session["auth_tokens"] = {"TYLER-TOKEN-ILLINOIS": "token"}
    session["jurisdiction"] = "illinois"
    session.save()
    return draft


def submit(client, token):
    return client.post(
        reverse("upload_documents", kwargs={"jurisdiction": "illinois"}),
        {
            "upload_request_token": str(token),
            "documents": [SimpleUploadedFile("test.pdf", pdf_bytes(), content_type="application/pdf")],
        },
    )


def publish_worker(group, *, state=DocumentWorkerHeartbeat.State.READY):
    DocumentWorkerHeartbeat.objects.update_or_create(
        machine_id=PRIMARY[group],
        defaults={"group": group, "boot_id": uuid.uuid4(), "state": state, "seen_at": timezone.now()},
    )


def test_missing_wake_rejects_before_an_upload_is_stored(client, draft, control, monkeypatch):
    control.coordinator_seen_at = timezone.now() - timedelta(minutes=1)
    control.save()
    storage = Mock()
    monkeypatch.setattr("efile.services.document_preparation_queue.S3UploadHandler", storage)
    response = submit(client, uuid.uuid4())
    assert response.status_code == 503
    assert "selected files" in response.json()["error"]
    assert response["Retry-After"] == "3"
    assert not draft.preparation_jobs.exists()
    storage.assert_not_called()


def test_cold_upload_waits_for_real_heartbeats_and_accepted_replay_skips_admission(
    client, draft, control, fly_workers, monkeypatch
):
    fly, machines = fly_workers
    coordinator = Coordinator(fly)
    control.active_machine_ids = []
    control.save()

    def start(machine_id, action):
        assert action == "start"
        machines[machine_id]["state"] = "started"
        group = machines[machine_id]["config"]["metadata"]["fly_process_group"]
        publish_worker(group)

    fly.act.side_effect = start

    def wake_workers(_delay):
        assert not draft.preparation_jobs.exists()
        coordinator.reconcile()

    monkeypatch.setattr(scaling.time, "sleep", wake_workers)
    storage = Mock()
    storage.upload_file.side_effect = lambda file, *, file_type, object_key: {"success": True, "key": object_key}
    monkeypatch.setattr("efile.services.document_preparation_queue.S3UploadHandler", lambda: storage)
    token = uuid.uuid4()
    response = submit(client, token)
    assert response.status_code == 202
    assert fly.act.call_count == 2
    upload = draft.preparation_jobs.get()
    assert upload.status == "pending" and upload.originals_ready
    control.refresh_from_db()
    assert control.wake_until > timezone.now()

    control.coordinator_seen_at = None
    control.save()
    response = submit(client, token)
    assert response.status_code == 202
    assert response.json()["preparation_job_id"] == upload.pk
    storage.upload_file.assert_called_once()


def test_accepted_job_reports_delay_even_if_coordinator_dies(client, draft, control):
    DocumentPreparationJob.objects.create(
        draft=draft, request_token=uuid.uuid4(), status="pending", originals_ready=True
    )
    control.coordinator_seen_at = timezone.now() - timedelta(minutes=1)
    control.save()

    status = client.get(reverse("document_extraction_status", kwargs={"jurisdiction": "illinois"}))
    assert status.json()["preparation"]["delay_message"] == scaling.PROCESSING_DELAYED
    health = client.get(reverse("document_worker_health"))
    assert health.status_code == 503
    assert "coordinator" in health.json()["errors"]


def test_watchdog_reports_unclaimed_work_when_coordinator_is_healthy(client, draft):
    DocumentPreparationJob.objects.create(
        draft=draft, request_token=uuid.uuid4(), status="pending", available_at=timezone.now() - timedelta(minutes=1)
    )
    health = client.get(reverse("document_worker_health"))
    assert health.status_code == 503
    assert health.json()["errors"] == [scaling.PREPARATION]


def test_idle_exit_rechecks_wake_reservations_and_marks_draining(control, monkeypatch):
    monkeypatch.setenv("FLY_MACHINE_ID", PRIMARY[scaling.EXTRACTION])
    worker = scaling.WorkerActivity(scaling.EXTRACTION)
    worker.idle_since -= scaling.IDLE_SECONDS + 1
    control.wake_until = timezone.now() + timedelta(seconds=10)
    control.save()
    assert not worker.can_sleep()

    control.wake_until = None
    control.save()
    assert worker.can_sleep()
    assert DocumentWorkerHeartbeat.objects.get(pk=PRIMARY[scaling.EXTRACTION]).state == "draining"


@pytest.fixture
def fly_workers(control, monkeypatch):
    fly = Mock()
    fly.targets = {group: [PRIMARY[group], SPARE[group]] for group in scaling.GROUPS}
    machines = {
        machine_id: {
            "id": machine_id,
            "state": "stopped",
            "config": {"metadata": {"fly_process_group": group}, "restart": {"policy": "on-failure"}},
        }
        for group in scaling.GROUPS
        for machine_id in fly.targets[group]
    }
    fly.machines.return_value = machines
    if connection.vendor != "postgresql":
        monkeypatch.setattr(coordinator_module, "try_advisory_lock", lambda key: True)
    return fly, machines


def test_due_work_wakes_both_groups_even_when_a_worker_just_drained(draft, fly_workers):
    fly, _machines = fly_workers
    publish_worker(scaling.EXTRACTION, state=DocumentWorkerHeartbeat.State.DRAINING)
    DocumentPreparationJob.objects.create(draft=draft, request_token=uuid.uuid4(), status="pending")
    Coordinator(fly).reconcile()
    assert [call.args for call in fly.act.call_args_list] == [
        (PRIMARY[scaling.PREPARATION], "start"),
        (PRIMARY[scaling.EXTRACTION], "start"),
    ]


def test_start_failure_falls_back_to_an_independently_startable_worker(control, fly_workers):
    fly, _machines = fly_workers
    control.wake_until = timezone.now() + timedelta(seconds=10)
    control.save()

    def start(machine_id, _action):
        if machine_id == PRIMARY[scaling.PREPARATION]:
            raise FlyControlError

    fly.act.side_effect = start
    Coordinator(fly).reconcile()
    assert [call.args for call in fly.act.call_args_list][:2] == [
        (PRIMARY[scaling.PREPARATION], "start"),
        (SPARE[scaling.PREPARATION], "start"),
    ]


def test_started_but_unresponsive_worker_restarts_only_after_live_claim_expires(draft, fly_workers):
    fly, machines = fly_workers
    for machine in machines.values():
        machine["state"] = "started"
    job = DocumentPreparationJob.objects.create(
        draft=draft,
        request_token=uuid.uuid4(),
        status="processing",
        lease_expires_at=timezone.now() + timedelta(minutes=1),
    )
    coordinator = Coordinator(fly)
    coordinator.unresponsive_since = {machine_id: 0 for machine_id in machines}
    coordinator.reconcile()
    assert all(
        machine_id not in fly.targets[scaling.PREPARATION]
        for machine_id, _action in (c.args for c in fly.act.call_args_list)
    )

    job.lease_expires_at = timezone.now() - timedelta(seconds=1)
    job.save()
    coordinator.reconcile()
    fly.act.assert_any_call(PRIMARY[scaling.PREPARATION], "restart")


def test_unavailable_fly_api_cannot_refresh_coordinator_health(control, fly_workers):
    fly, _machines = fly_workers
    fly.machines.side_effect = FlyControlError
    original = control.coordinator_seen_at
    with pytest.raises(FlyControlError):
        Coordinator(fly).reconcile()
    control.refresh_from_db()
    assert control.coordinator_seen_at == original


def test_disabling_scaling_starts_warm_workers_even_before_policy_changes(control, fly_workers):
    fly, _machines = fly_workers
    control.enabled = False
    control.save()
    Coordinator(fly).reconcile()
    assert [call.args for call in fly.act.call_args_list] == [
        (PRIMARY[scaling.PREPARATION], "start"),
        (PRIMARY[scaling.EXTRACTION], "start"),
    ]


def test_repeated_wake_without_worker_heartbeat_is_degraded(control, fly_workers):
    fly, _machines = fly_workers
    control.wake_until = timezone.now() + timedelta(minutes=1)
    control.save()
    coordinator = Coordinator(fly)
    for machine_id in fly.targets[scaling.PREPARATION]:
        coordinator.attempts[machine_id] = (2, 0)
    coordinator.reconcile()
    control.refresh_from_db()
    assert control.unavailable_groups == [scaling.PREPARATION]
    assert scaling.queue_health_errors() == [scaling.PREPARATION]

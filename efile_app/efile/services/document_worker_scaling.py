"""Staging worker admission, heartbeat, and idle shutdown coordination."""

import os
import time
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q
from django.db.models.functions import Now

from efile.models import (
    DocumentExtraction,
    DocumentPreparationJob,
    DocumentWorkerControl,
    DocumentWorkerHeartbeat,
)
from efile.services.drafts import ACTIVE_DRAFT_STATUSES

PREPARATION = "preparation_worker"
EXTRACTION = "extraction_worker"
GROUPS = (PREPARATION, EXTRACTION)
CONTROL_ID = 1
FRESHNESS = timedelta(seconds=25)
WAKE_RESERVATION = timedelta(minutes=2)
CLAIM_DEADLINE = timedelta(seconds=45)
WAIT_SECONDS = 20
IDLE_SECONDS = 300
WORKER_TICK_SECONDS = 2
UPLOAD_UNAVAILABLE = (
    "We couldn't start document processing. Your selected files are still here. Try uploading again in a moment."
)
PROCESSING_DELAYED = "Document preparation is delayed. Your files are saved, and processing will retry automatically."
ANALYSIS_DELAYED = (
    "Document analysis is delayed. You can continue and enter the case details yourself when the wait ends."
)


class DocumentWorkersUnavailable(ValueError):
    """A new upload could not confirm that document workers are available."""


def control_state(*, lock=False):
    rows = DocumentWorkerControl.objects.annotate(db_now=Now())
    if lock:
        rows = rows.select_for_update()
    return rows.filter(pk=CONTROL_ID).first()


def controller_is_fresh(control):
    return bool(control.coordinator_seen_at and control.coordinator_seen_at >= control.db_now - FRESHNESS)


def _claimable_groups(since, active_machine_ids):
    return set(
        DocumentWorkerHeartbeat.objects.filter(
            seen_at__gte=since,
            machine_id__in=active_machine_ids,
            state__in=(DocumentWorkerHeartbeat.State.READY, DocumentWorkerHeartbeat.State.BUSY),
        ).values_list("group", flat=True)
    )


def reserve_workers(*, required=False, timeout=WAIT_SECONDS):
    """Request a wake-up; optionally wait for both supervisors before accepting files."""
    if not settings.DOCUMENT_WORKER_SCALING_CONTROL:
        return
    with transaction.atomic():
        control = control_state(lock=True)
        if control is None or not controller_is_fresh(control) or control.unavailable_groups:
            if required:
                raise DocumentWorkersUnavailable(UPLOAD_UNAVAILABLE)
            return
        requested_at = control.db_now
        # Coalesce speculative requests from all web processes into the same wake window.
        keep_awake = requested_at + WAKE_RESERVATION
        if required or control.wake_until is None or control.wake_until - requested_at < FRESHNESS:
            DocumentWorkerControl.objects.filter(pk=CONTROL_ID).update(wake_until=keep_awake)
    if not required:
        return
    deadline = time.monotonic() + timeout
    while True:
        control = control_state()
        if control is None or not controller_is_fresh(control) or control.unavailable_groups:
            raise DocumentWorkersUnavailable(UPLOAD_UNAVAILABLE)
        if set(GROUPS).issubset(
            _claimable_groups(max(requested_at, control.db_now - FRESHNESS), control.active_machine_ids)
        ):
            return
        if time.monotonic() >= deadline:
            raise DocumentWorkersUnavailable(UPLOAD_UNAVAILABLE)
        if connection.vendor == "postgresql" and not connection.in_atomic_block:
            connection.close()
        time.sleep(0.5)


def preparation_queue():
    return DocumentPreparationJob.objects.filter(
        draft__deletion_pending=False,
        draft__status__in=ACTIVE_DRAFT_STATUSES,
    )


def extraction_queue():
    return DocumentExtraction.objects.filter(document__draft__deletion_pending=False)


def queue_for(group):
    return preparation_queue() if group == PREPARATION else extraction_queue()


def group_has_work(group):
    states = ("pending", "processing", "uploading") if group == PREPARATION else ("pending", "processing")
    return queue_for(group).filter(status__in=states).exists()


def late_job_filter(now):
    return Q(status="pending", available_at__lt=now - CLAIM_DEADLINE) | Q(
        status="processing", lease_expires_at__lte=now
    )


def _busy_heartbeat_missing(group, now):
    return not DocumentWorkerHeartbeat.objects.filter(
        group=group, seen_at__gte=now - FRESHNESS, state=DocumentWorkerHeartbeat.State.BUSY
    ).exists()


def queue_health_errors():
    """Check from the web process, independently of the coordinator."""
    if not settings.DOCUMENT_WORKER_SCALING_CONTROL:
        return []
    control = control_state()
    if not control:
        return ["coordinator"]
    errors = []
    if not controller_is_fresh(control):
        errors.append("coordinator")
    errors.extend(control.unavailable_groups)
    for group in GROUPS:
        queue = queue_for(group)
        if queue.filter(late_job_filter(control.db_now)).exists() or (
            queue.filter(status="processing", started_at__lt=control.db_now - CLAIM_DEADLINE).exists()
            and _busy_heartbeat_missing(group, control.db_now)
        ):
            errors.append(group)
    return sorted(set(errors))


def job_is_delayed(job, group):
    if not settings.DOCUMENT_WORKER_SCALING_CONTROL or job.status not in ("pending", "processing"):
        return False
    control = control_state()
    if control is None or not controller_is_fresh(control) or group in control.unavailable_groups:
        return True
    if job.status == "pending":
        return job.available_at < control.db_now - CLAIM_DEADLINE
    return bool(
        (job.lease_expires_at and job.lease_expires_at <= control.db_now)
        or (
            job.started_at
            and job.started_at < control.db_now - CLAIM_DEADLINE
            and _busy_heartbeat_missing(group, control.db_now)
        )
    )


class WorkerActivity:
    """Report progress from the supervisor, including while its child runs."""

    def __init__(self, group):
        self.group = group
        self.enabled = settings.DOCUMENT_WORKER_SCALING_CONTROL
        self.machine_id = os.environ.get("FLY_MACHINE_ID", "")
        self.boot_id = uuid.uuid4()
        self.idle_since = time.monotonic() if self.enabled else 0.0
        self.last_seen = 0.0
        self.state = None
        if self.enabled and not self.machine_id:
            raise RuntimeError("Staging worker scaling requires FLY_MACHINE_ID")

    def report(self, state):
        if not self.enabled:
            return
        now = time.monotonic()
        if state == self.state and now - self.last_seen < WORKER_TICK_SECONDS:
            return
        DocumentWorkerHeartbeat.objects.update_or_create(
            machine_id=self.machine_id,
            defaults={"group": self.group, "boot_id": self.boot_id, "state": state, "seen_at": Now()},
        )
        self.last_seen, self.state = now, state
        if state == DocumentWorkerHeartbeat.State.BUSY:
            self.idle_since = now

    def can_sleep(self):
        if not self.enabled or time.monotonic() - self.idle_since < IDLE_SECONDS:
            return False
        # Lock against web admission reservations and check again before leaving the loop.
        with transaction.atomic():
            control = control_state(lock=True)
            if (
                not control
                or not control.enabled
                or not controller_is_fresh(control)
                or self.machine_id not in control.active_machine_ids
                or self.group in control.unavailable_groups
                or (control.wake_until and control.wake_until > control.db_now)
                or group_has_work(self.group)
            ):
                return False
            self.report(DocumentWorkerHeartbeat.State.DRAINING)
            return True


def try_advisory_lock(key):
    """Acquire a transaction-scoped lock on the session-pooler connection."""
    if connection.vendor != "postgresql":
        raise RuntimeError("Document worker scaling requires PostgreSQL")
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [key])
        return cursor.fetchone()[0]


def run_maintenance(group, work, *, coordinator=False):
    """Prevent overlapping maintenance during a coordinator/worker handoff."""
    if not settings.DOCUMENT_WORKER_SCALING_CONTROL:
        return work() if not coordinator else None
    with transaction.atomic():
        if not try_advisory_lock(509161 if group == PREPARATION else 509162):
            return None
        control = control_state()
        if coordinator == bool(control and control.enabled):
            return work()
    return None

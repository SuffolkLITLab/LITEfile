"""Keep staging's document queues recoverable when their workers are asleep."""

import io
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections, connection, connections, transaction
from django.db.models.functions import Now

from efile.models import DocumentWorkerControl, DocumentWorkerHeartbeat
from efile.services.document_preparation_queue import maintain_preparations
from efile.services.document_worker_scaling import (
    CONTROL_ID,
    EXTRACTION,
    FRESHNESS,
    GROUPS,
    PREPARATION,
    control_state,
    controller_is_fresh,
    group_has_work,
    queue_for,
    run_maintenance,
    try_advisory_lock,
)

logger = logging.getLogger(__name__)
POLL_SECONDS = 3
START_DEADLINE = 45
STARTABLE = frozenset({"created", "stopped", "suspended"})
TRANSITIONING = frozenset({"starting", "stopping", "pending", "replacing", "launching"})


class FlyControlError(RuntimeError):
    """A sanitized Fly Machines API failure."""


class FlyDocumentWorkers:
    def __init__(self, app, token, targets):
        if app != "litefile-staging" or not token:
            raise CommandError("Document worker coordination requires an app-scoped staging Fly token")
        if not isinstance(targets, dict) or set(targets) != set(GROUPS):
            raise CommandError("Configure ordered Fly Machine IDs for both document worker groups")
        if any(
            not isinstance(ids, list)
            or not ids
            or any(
                not isinstance(machine_id, str) or not re.fullmatch(r"[a-z0-9]{8,40}", machine_id) for machine_id in ids
            )
            for ids in targets.values()
        ):
            raise CommandError("Invalid document worker Machine IDs")
        ids = [machine_id for group_ids in targets.values() for machine_id in group_ids]
        if len(ids) != len(set(ids)):
            raise CommandError("Document worker Machine IDs must be unique")
        self.url = f"https://api.machines.dev/v1/apps/{app}/machines"
        self.targets = targets
        self.session = requests.Session()
        self.session.headers["Authorization"] = token if token.startswith(("FlyV1 ", "Bearer ")) else f"Bearer {token}"

    def machines(self):
        try:
            response = self.session.get(self.url, timeout=(0.8, 1.5))
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError
            return {machine["id"]: machine for machine in payload}
        except (requests.RequestException, ValueError, KeyError, TypeError):
            raise FlyControlError("Could not read Fly document worker state") from None

    def act(self, machine_id, action):
        try:
            response = self.session.post(f"{self.url}/{machine_id}/{action}", timeout=(0.8, 1.5))
            response.raise_for_status()
        except requests.RequestException:
            raise FlyControlError("Fly could not confirm a document worker action") from None


class Coordinator:
    def __init__(self, fly):
        self.fly = fly
        self.attempts = {}
        self.unresponsive_since = {}
        self.starting_until = {}

    def _candidates(self, machines, group, enabled):
        candidates = []
        policies = {"on-failure"} if enabled else {"always", "on-failure"}
        for machine_id in self.fly.targets[group]:
            machine = machines.get(machine_id)
            if not machine:
                continue
            config = machine.get("config") or {}
            metadata = config.get("metadata") or {}
            restart_policy = (config.get("restart") or {}).get("policy")
            if (
                metadata.get("fly_process_group") == group
                and restart_policy in policies
                and not config.get("standbys")
                and machine.get("host_status") in (None, "ok")
            ):
                candidates.append(machine)
        return candidates

    def _act(self, machine_id, action, now):
        number, _retry_at = self.attempts.get(machine_id, (0, 0))
        self.attempts[machine_id] = number + 1, now + min(60, 3 * (2 ** min(number, 5)))
        try:
            self.fly.act(machine_id, action)
        except FlyControlError:
            logger.warning("Document worker %s could not %s. A subsequent check will retry.", machine_id, action)
            return False
        self.starting_until[machine_id] = now + START_DEADLINE
        logger.info("Document worker %s accepted a Fly %s request", machine_id, action)
        return True

    def _group(self, control, machines, group, wake, heartbeats, now):
        candidates = self._candidates(machines, group, control.enabled)
        if not candidates:
            logger.error("Document worker group %s has no configured Fly Machine available", group)
            return False
        for machine in candidates:
            machine_id = machine["id"]
            heartbeat = heartbeats.get(machine_id)
            if (
                machine["state"] == "started"
                and heartbeat
                and heartbeat.group == group
                and heartbeat.state != DocumentWorkerHeartbeat.State.DRAINING
                and heartbeat.seen_at >= control.db_now - FRESHNESS
            ):
                for candidate in candidates:
                    candidate_id = candidate["id"]
                    self.attempts.pop(candidate_id, None)
                    self.unresponsive_since.pop(candidate_id, None)
                    self.starting_until.pop(candidate_id, None)
                return True
        if not wake:
            return True
        wake_actions = []
        for machine in candidates:
            machine_id, state = machine["id"], machine["state"]
            count, retry_at = self.attempts.get(machine_id, (0, 0))
            if now < self.starting_until.get(machine_id, 0):
                return count < 3
            waiting_since = self.unresponsive_since.setdefault(machine_id, now)
            if state in STARTABLE:
                wake_actions.append((count, retry_at, machine_id, "start"))
            elif state == "started" or state in TRANSITIONING:
                if now - waiting_since < START_DEADLINE:
                    return True
                elif state == "started":
                    # A worker proving no progress may be restarted after its claims expire.
                    wake_actions.append((count, retry_at, machine_id, "restart"))
        # Preserve the configured preference on a normal wake. Give a spare a
        # chance before retrying an unresponsive or repeatedly crashing primary.
        wake_actions.sort(key=lambda action: (action[3] == "restart", action[0]))
        for count, retry_at, machine_id, action in wake_actions:
            if now < retry_at:
                continue
            if not DocumentWorkerControl.objects.filter(pk=CONTROL_ID, enabled=control.enabled).exists():
                return False
            if (
                action == "restart"
                and queue_for(group).filter(status="processing", lease_expires_at__gt=Now()).exists()
            ):
                continue
            if self._act(machine_id, action, now):
                return count < 2
        return False

    def reconcile(self):
        with transaction.atomic():
            if not try_advisory_lock(509160):
                return
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL idle_in_transaction_session_timeout = '2800ms'")
            DocumentWorkerControl.objects.get_or_create(pk=CONTROL_ID)
            control = control_state()
            machines = self.fly.machines()
            heartbeats = DocumentWorkerHeartbeat.objects.in_bulk(
                machine_id for ids in self.fly.targets.values() for machine_id in ids
            )
            reservation = bool(control.wake_until and control.wake_until > control.db_now)
            preparation_work = group_has_work(PREPARATION)
            current_time = time.monotonic()
            unavailable = []
            for group in GROUPS:
                wake = not control.enabled or reservation or preparation_work or group_has_work(group)
                if not self._group(control, machines, group, wake, heartbeats, current_time):
                    unavailable.append(group)
            DocumentWorkerControl.objects.filter(pk=CONTROL_ID).update(
                coordinator_seen_at=Now(),
                unavailable_groups=unavailable,
                active_machine_ids=[
                    machine["id"]
                    for group in GROUPS
                    for machine in self._candidates(machines, group, control.enabled)
                    if machine["state"] == "started"
                ],
            )


def maintain_queues():
    try:
        run_maintenance(PREPARATION, maintain_preparations, coordinator=True)
        run_maintenance(
            EXTRACTION, lambda: call_command("process_usage_events", stdout=io.StringIO()), coordinator=True
        )
    except Exception as error:
        logger.error("Document queue maintenance failed (%s); it will retry", type(error).__name__)
    finally:
        connections.close_all()


class Command(BaseCommand):
    help = "Wake the staging document workers based on reserved uploads and durable queue state"

    def add_arguments(self, parser):
        parser.add_argument("--mode", choices=("warm", "autoscale"), help="Set staging's persistent worker mode")

    def handle(self, *args, **options):
        if not settings.DOCUMENT_WORKER_SCALING_CONTROL:
            raise CommandError("Document worker coordination is disabled by app settings")
        mode = options["mode"]
        if mode == "warm":
            DocumentWorkerControl.objects.update_or_create(
                pk=CONTROL_ID, defaults={"enabled": False, "coordinator_seen_at": None, "active_machine_ids": []}
            )
            self.stdout.write("Document worker mode set to warm")
            return
        try:
            targets = json.loads(os.environ.get("DOCUMENT_WORKER_MACHINES", "{}"))
        except ValueError:
            raise CommandError("Invalid DOCUMENT_WORKER_MACHINES JSON") from None
        fly = FlyDocumentWorkers(
            os.environ.get("FLY_APP_NAME", ""), os.environ.get("DOCUMENT_WORKER_FLY_TOKEN", ""), targets
        )
        coordinator = Coordinator(fly)
        if mode == "autoscale":
            control = control_state()
            if not control or not controller_is_fresh(control) or control.unavailable_groups:
                raise CommandError("Verify the live staging coordinator and worker health before enabling autoscale")
            machines = fly.machines()
            if any(len(coordinator._candidates(machines, group, True)) < 2 for group in GROUPS):
                raise CommandError("Set up two startable staging Machines with restart on-failure in each group first")
            DocumentWorkerControl.objects.update_or_create(
                pk=CONTROL_ID, defaults={"enabled": True, "coordinator_seen_at": None, "active_machine_ids": []}
            )
            self.stdout.write("Document worker mode set to autoscale")
            return
        next_maintenance = 0.0
        maintenance = None
        with ThreadPoolExecutor(max_workers=1) as maintenance_pool:
            while True:
                close_old_connections()
                try:
                    coordinator.reconcile()
                except FlyControlError:
                    logger.error("Staging document worker reconciliation could not read Fly state")
                if time.monotonic() >= next_maintenance and (maintenance is None or maintenance.done()):
                    next_maintenance = time.monotonic() + 60
                    maintenance = maintenance_pool.submit(maintain_queues)
                time.sleep(POLL_SECONDS)

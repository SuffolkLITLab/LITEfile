"""One bounded preparation child per supervisor; crashed claims remain durable."""

import logging
import multiprocessing
import os
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connections

from efile.utils.worker_signals import stop_on_sigterm

logger = logging.getLogger(__name__)


def _run(pk, token, memory_mb, timeout):
    import resource
    import signal

    import django

    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["OMP_NUM_THREADS"] = "1"
    resource.setrlimit(resource.RLIMIT_AS, (memory_mb * 1024 * 1024,) * 2)
    signal.alarm(timeout)
    django.setup()
    from efile.services.document_preparation_queue import fail_preparation, process_preparation

    try:
        process_preparation(pk, token)
    except Exception as error:
        logger.warning("Document preparation %s failed (%s)", pk, type(error).__name__)
        fail_preparation(pk, token, error)
    finally:
        connections.close_all()


class Command(BaseCommand):
    help = "Prepare durable uploads with one memory- and time-bounded child at a time"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--poll-interval", type=float, default=2)

    def handle(self, *args, **options):
        stop_on_sigterm()
        from efile.models import DocumentWorkerHeartbeat
        from efile.services.document_preparation import PreparationError
        from efile.services.document_preparation_queue import claim_preparation, fail_preparation, preparation_timeout
        from efile.services.document_worker_scaling import PREPARATION, WORKER_TICK_SECONDS, WorkerActivity

        os.environ.setdefault("MALLOC_ARENA_MAX", "2")
        activity = WorkerActivity(PREPARATION)
        while True:
            try:
                job = claim_preparation()
            finally:
                connections.close_all()
            if job is None:
                activity.report(DocumentWorkerHeartbeat.State.READY)
                if options["once"] or activity.can_sleep():
                    return
                time.sleep(max(0.1, options["poll_interval"]))
                continue
            timeout = preparation_timeout(job)
            timed_out = False
            child = multiprocessing.get_context("spawn").Process(
                target=_run,
                args=(job.pk, job.claim_token, settings.DOCUMENT_PREPARATION_MEMORY_MB, timeout),
            )
            try:
                child.start()
                if activity.enabled:
                    deadline = time.monotonic() + timeout
                    while child.is_alive() and time.monotonic() < deadline:
                        activity.report(DocumentWorkerHeartbeat.State.BUSY)
                        child.join(timeout=min(WORKER_TICK_SECONDS, max(0, deadline - time.monotonic())))
                else:
                    child.join(timeout=timeout)
                timed_out = child.is_alive() or child.exitcode == -14
            finally:
                if child.pid is not None:
                    if child.is_alive():
                        child.terminate()
                    child.join(timeout=5)
                    if child.is_alive():
                        child.kill()
                        child.join(timeout=5)
                    child.close()
                try:
                    error = (
                        PreparationError("Preparation took too long. Remove this upload and try a smaller batch.")
                        if timed_out
                        else RuntimeError("Worker exited")
                    )
                    fail_preparation(job.pk, job.claim_token, error)
                finally:
                    connections.close_all()
            if options["once"]:
                return

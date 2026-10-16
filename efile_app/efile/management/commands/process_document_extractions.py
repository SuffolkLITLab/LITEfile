import io
import logging
import multiprocessing
import os
import time

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import OperationalError, close_old_connections, connection

logger = logging.getLogger(__name__)


def _run_claim(job_id, claim_token, memory_mb, timeout_seconds):
    """Isolate PDF parsing and model clients from the queue supervisor."""
    import os
    import resource
    import signal

    import django

    # One job per child: numerical libraries must not reserve a thread pool
    # proportional to the host CPU count before PDF processing even starts.
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["OMP_NUM_THREADS"] = "1"
    # The deployed worker is Linux. Enforce bounds before loading the PDF;
    # the alarm also bounds an orphaned child if its supervisor is killed.
    try:
        memory_bytes = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    except ValueError as error:
        logger.error(f"Setting resource limit failed, but will continue: {error}")
    signal.alarm(timeout_seconds)
    django.setup()

    from efile.services.document_extractions import process_document_extraction, record_extraction_failure

    try:
        process_document_extraction(job_id, claim_token)
    except Exception as error:
        logger.error("Document extraction job %s failed (%s)", job_id, type(error).__name__)
        record_extraction_failure(job_id, claim_token, error)


class Command(BaseCommand):
    help = "Process queued lead-document extraction jobs"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process at most one job and exit")
        parser.add_argument("--poll-interval", type=float, default=2.0)

    def handle(self, *args, **options):
        from efile.services.document_extractions import (
            claim_next_extraction,
            record_extraction_failure,
            renew_extraction_lease,
        )

        next_rollup = 0.0
        # The child's memory cap is on address space, and glibc reserves a
        # malloc arena of up to 64 MB per thread, up to eight per CPU core. A
        # job that uses about 250 MB reached 800 MB of address space on an
        # eight-core machine. Two arenas keep each child's address space close
        # to what it uses, whatever the host's core count. Spawned children
        # read this at startup; an operator's own value is kept.
        os.environ.setdefault("MALLOC_ARENA_MAX", "2")

        def rollup_if_due():
            nonlocal next_rollup
            now = time.monotonic()
            if now < next_rollup:
                return
            next_rollup = now + 60
            try:
                call_command("process_usage_events", stdout=io.StringIO())
            except Exception:
                logger.error("Usage rollup failed; queued events retained for retry")

        while True:
            close_old_connections()
            rollup_if_due()
            try:
                job = claim_next_extraction()
            except OperationalError as error:
                # A large local code-index publication can outlast SQLite's
                # busy timeout. Keep the supervisor alive to claim work after
                # the writer finishes; other database errors still fail loudly.
                if (
                    options["once"]
                    or connection.vendor != "sqlite"
                    or str(error) not in {"database is locked", "database table is locked"}
                ):
                    raise
                logger.warning("Local database is busy; document extraction will retry")
                time.sleep(max(0.1, options["poll_interval"]))
                continue
            if job is None:
                if options["once"]:
                    return
                time.sleep(max(0.1, options["poll_interval"]))
                continue

            timeout = max(1, settings.DOCUMENT_EXTRACTION_TIMEOUT_SECONDS)
            child = multiprocessing.get_context("spawn").Process(
                target=_run_claim,
                args=(job.pk, job.claim_token, settings.DOCUMENT_EXTRACTION_MEMORY_MB, timeout),
            )
            deadline = time.monotonic() + timeout
            try:
                child.start()
                while child.is_alive():
                    child.join(timeout=min(30, max(0, deadline - time.monotonic())))
                    if not child.is_alive():
                        break
                    close_old_connections()
                    rollup_if_due()
                    if time.monotonic() >= deadline or not renew_extraction_lease(job.pk, job.claim_token):
                        child.terminate()
                        break
            finally:
                if child.pid is not None:
                    child.join(timeout=5)
                    if child.is_alive():
                        child.kill()
                        child.join(timeout=5)
                    child.close()
                # No-op for completed, failed, or superseded claims. Handles
                # OOM, a signal, and other exits without a Python exception.
                close_old_connections()
                record_extraction_failure(job.pk, job.claim_token, "Worker exited")

            if options["once"]:
                return

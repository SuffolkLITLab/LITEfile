"""Build complete code snapshots outside the request and release paths."""

import time
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, close_old_connections
from django.test.utils import override_settings
from django.utils import timezone

from efile.models import FilingCodeJob
from efile.services.filing_code_copy import rebuild, run_jobs
from efile.services.filing_code_labels import build_labels
from efile.services.filing_code_search import current_index, refresh_index, rules
from efile.utils.worker_signals import stop_on_sigterm

# How often the daily worker checks for staff-requested jobs between runs.
JOB_POLL_SECONDS = 30


def next_daily_run(now, at, zone):
    """The next moment at ``at`` (HH:MM) in ``zone`` after ``now``."""
    hour, minute = (int(part) for part in at.split(":"))
    local = now.astimezone(ZoneInfo(zone))
    run = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if run <= local:
        run = (local + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return run


class Command(BaseCommand):
    help = "Sync changed court code exports, preserving previous snapshots on failure."

    def add_arguments(self, parser):
        parser.add_argument("--jurisdiction", choices=rules()[0]["jurisdictions"])
        parser.add_argument("--interval", type=int, default=0, help="Repeat after this many seconds (0: run once).")
        parser.add_argument(
            "--daily",
            action="store_true",
            help="Run as the code index worker: sync once a day at FILING_CODE_SYNC_TIME "
            "(FILING_CODE_SYNC_TIMEZONE), and run resync/rebuild jobs staff queue meanwhile.",
        )
        parser.add_argument(
            "--rebuild",
            action="store_true",
            help="Rebuild the search index from the local copy of the EFSP codes; contacts nothing.",
        )
        parser.add_argument(
            "--build-labels",
            action="store_true",
            help="Save search labels for indexes saved before labels existed, from their paths; contacts nothing.",
        )
        parser.add_argument("--retry-interval", type=int, default=60, help="Seconds before retrying failed states.")
        parser.add_argument(
            "--legacy-crawl", action="store_true", help="Explicitly use the expensive full list-API crawler."
        )
        parser.add_argument(
            "--court",
            action="append",
            default=[],
            help="Re-download this court even if its revision is unchanged (repeatable; needs --jurisdiction).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Download and check changed courts and report path counts, without saving anything.",
        )
        parser.add_argument(
            "--cache-dir",
            default=str(Path.home() / ".cache" / "litefile" / "filing-code-cache"),
            help="Legacy crawler checkpoints, reused for six hours.",
        )

    def handle(self, *args, **options):
        stop_on_sigterm()
        interval = options["interval"]
        if interval < 0:
            raise CommandError("Interval must be nonnegative")
        retry_interval = options["retry_interval"]
        if retry_interval <= 0:
            raise CommandError("Retry interval must be positive")
        if options["court"] and not options["jurisdiction"]:
            raise CommandError("--court needs --jurisdiction")
        if (options["court"] or options["dry_run"]) and (interval or options["daily"]):
            raise CommandError("--court and --dry-run run once; drop --interval and --daily")
        if options["daily"] and interval:
            raise CommandError("Choose --daily or --interval")
        jurisdictions = [options["jurisdiction"]] if options["jurisdiction"] else rules()[0]["jurisdictions"]
        if options["rebuild"]:
            return self.rebuild(jurisdictions)
        if options["build_labels"]:
            if failures := self.build_missing_labels(jurisdictions):
                raise CommandError("Label build failed: " + ", ".join(failures))
            return
        if options["daily"]:
            return self.daily(jurisdictions, options)
        while True:
            failures = []
            # The worker sleeps for hours between passes; a connection the
            # database or proxy dropped meanwhile must not fail the next one.
            close_old_connections()
            for jurisdiction in jurisdictions:
                try:
                    if interval:
                        index = current_index(jurisdiction)
                        if index and (timezone.now() - index.refreshed_at).total_seconds() < interval:
                            continue
                    # This dedicated command doesn't serve requests. Rendering
                    # and retaining SQL debug strings for millions of rows is
                    # costly in local development; restore DEBUG after the build.
                    with override_settings(DEBUG=False):
                        count = refresh_index(
                            jurisdiction,
                            cache_dir=options["cache_dir"],
                            progress=lambda court, state=jurisdiction: self.stdout.write(f"{state}: {court}")
                            if options["verbosity"] > 1 or options["dry_run"]
                            else None,
                            force=options["court"],
                            dry_run=options["dry_run"],
                            **({"transport": "legacy"} if options["legacy_crawl"] else {}),
                        )
                    verb = "would synchronize" if options["dry_run"] else "synchronized"
                    self.stdout.write(self.style.SUCCESS(f"{jurisdiction}: {verb} {count} changed filing paths"))
                # DatabaseError covers a lost connection and an overlapping run
                # creating the same jurisdiction's row first (IntegrityError).
                except (requests.RequestException, ValueError, DatabaseError) as error:
                    failures.append(jurisdiction)
                    self.stderr.write(f"{jurisdiction}: refresh failed; previous index kept. {error}")
            if not interval:
                if failures:
                    raise CommandError("Refresh failed: " + ", ".join(failures))
                return
            delay = min(interval, retry_interval) if failures else interval
            if failures:
                self.stderr.write(f"Retrying failed states in {delay} seconds.")
            time.sleep(delay)

    def sync(self, jurisdictions, options):
        failures = []
        close_old_connections()
        for jurisdiction in jurisdictions:
            try:
                with override_settings(DEBUG=False):
                    count = refresh_index(
                        jurisdiction,
                        cache_dir=options["cache_dir"],
                        progress=(lambda court, state=jurisdiction: self.stdout.write(f"{state}: {court}"))
                        if options["verbosity"] > 1
                        else None,
                        **({"transport": "legacy"} if options["legacy_crawl"] else {}),
                    )
                self.stdout.write(self.style.SUCCESS(f"{jurisdiction}: synchronized {count} changed filing paths"))
            except (requests.RequestException, ValueError, DatabaseError, OSError) as error:
                # psycopg's errors for the EFSP database are DatabaseErrors too.
                failures.append(jurisdiction)
                self.stderr.write(f"{jurisdiction}: refresh failed; previous index kept. {error}")
        return failures

    def rebuild(self, jurisdictions):
        failures = []
        for jurisdiction in jurisdictions:
            try:
                with override_settings(DEBUG=False):
                    count = rebuild(
                        jurisdiction, progress=lambda line, state=jurisdiction: self.stdout.write(f"{state}: {line}")
                    )
                self.stdout.write(self.style.SUCCESS(f"{jurisdiction}: rebuilt {count} filing paths"))
            except (ValueError, DatabaseError) as error:
                failures.append(jurisdiction)
                self.stderr.write(f"{jurisdiction}: rebuild failed; previous index kept. {error}")
        if failures:
            raise CommandError("Rebuild failed: " + ", ".join(failures))

    def build_missing_labels(self, jurisdictions):
        failures = []
        for jurisdiction in jurisdictions:
            index = current_index(jurisdiction)
            if index is None or index.labels_built_at:
                continue
            try:
                with override_settings(DEBUG=False):
                    build_labels(index, progress=lambda line, state=jurisdiction: self.stdout.write(f"{state}: {line}"))
                self.stdout.write(self.style.SUCCESS(f"{jurisdiction}: search labels saved"))
            except DatabaseError as error:
                failures.append(jurisdiction)
                self.stderr.write(f"{jurisdiction}: label build failed; search keeps reading paths. {error}")
        return failures

    def daily(self, jurisdictions, options):
        at, zone = settings.FILING_CODE_SYNC_TIME, settings.FILING_CODE_SYNC_TIMEZONE
        # A job a dead worker left running would otherwise stay "running" forever.
        FilingCodeJob.objects.filter(status=FilingCodeJob.Status.RUNNING).update(
            status=FilingCodeJob.Status.FAILED, finished_at=timezone.now(), message="Interrupted by a worker restart."
        )
        # A fresh deployment, or one whose search rules changed, has nothing to
        # search until the first sync: don't wait for tonight's.
        pending = [j for j in jurisdictions if current_index(j) is None]
        # Indexes saved before labels existed search their paths, slowly, until this runs.
        self.build_missing_labels(jurisdictions)
        due = timezone.now() if pending else next_daily_run(timezone.now(), at, zone)
        while True:
            self.stdout.write(f"Next code sync at {due.astimezone(ZoneInfo(zone)):%Y-%m-%d %H:%M %Z}.")
            while timezone.now() < due:
                close_old_connections()
                run_jobs(jurisdictions, log=self.stdout.write)
                time.sleep(min(JOB_POLL_SECONDS, max(1, (due - timezone.now()).total_seconds())))
            # Failed states are retried alone, then the schedule returns to daily.
            pending = self.sync(pending or jurisdictions, options)
            if pending:
                self.stderr.write(f"Retrying failed states in {options['retry_interval']} seconds.")
                due = timezone.now() + timedelta(seconds=options["retry_interval"])
            else:
                due = next_daily_run(timezone.now(), at, zone)

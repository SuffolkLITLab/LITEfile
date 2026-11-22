"""List filings whose submission outcome is unknown, without filer details (#445)."""

import json

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from efile.services.submission_outcomes import unconfirmed_submissions


class Command(BaseCommand):
    help = (
        "Print drafts left SUBMITTING or ERROR, whose filing may or may not have reached the court, "
        "as JSON. Check each against the court's records; never resubmit one automatically."
    )

    def add_arguments(self, parser):
        parser.add_argument("--jurisdiction", action="append", help="Limit to a jurisdiction (repeatable)")
        parser.add_argument("--fail-if-any", action="store_true", help="Exit non-zero when any are found")

    def handle(self, *args, **options):
        now = timezone.now()
        drafts = unconfirmed_submissions(options["jurisdiction"], now=now)
        rows = [
            {
                "draft_id": draft.pk,
                "jurisdiction": draft.jurisdiction,
                "court": draft.court_code,
                "status": draft.status,
                "since": draft.updated_at.isoformat(),
                "minutes": int((now - draft.updated_at).total_seconds() // 60),
                "submission_operation": str(draft.submission_operation or ""),
            }
            for draft in drafts
        ]
        self.stdout.write(json.dumps({"observed_at": now.isoformat(), "unconfirmed": rows}, indent=2))
        if rows and options["fail_if_any"]:
            raise CommandError(f"{len(rows)} submission(s) have an unknown outcome")

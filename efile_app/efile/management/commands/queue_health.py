"""Read-only, aggregate queue health; safe to attach to private capacity reports."""

import json
import math
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import connection
from django.db.models import Count, Min
from django.utils import timezone

from efile.models import DocumentExtraction, DocumentPreparationJob


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def summarize(model, now):
    counts = {row["status"]: row["count"] for row in model.objects.values("status").annotate(count=Count("pk"))}
    pending = model.objects.filter(status="pending")
    oldest = pending.aggregate(oldest=Min("created_at"))["oldest"]
    recent = model.objects.filter(status="complete", completed_at__gte=now - timedelta(hours=24))
    rows = list(recent.order_by("-completed_at").values("started_at", "completed_at", "created_at", "attempts")[:1000])
    service = [(row["completed_at"] - row["started_at"]).total_seconds() for row in rows if row["started_at"]]
    elapsed = [(row["completed_at"] - row["created_at"]).total_seconds() for row in rows]
    return {
        "counts": counts,
        "pending_oldest_seconds": max(0, (now - oldest).total_seconds()) if oldest else None,
        "eligible_pending": pending.filter(available_at__lte=now).count(),
        "expired_processing": model.objects.filter(status="processing", lease_expires_at__lte=now).count(),
        "completed_last_24h": recent.count(),
        "timing_sample_size": len(rows),
        "last_attempt_seconds_p50": percentile(service, 0.5),
        "last_attempt_seconds_p95": percentile(service, 0.95),
        "accepted_to_complete_seconds_p95": percentile(elapsed, 0.95),
        "sample_jobs_with_retries": sum(row["attempts"] > 1 for row in rows),
    }


class Command(BaseCommand):
    help = "Print aggregate preparation/extraction queue health as JSON without document or user data"

    def handle(self, *args, **options):
        now = timezone.now()
        report = {
            "observed_at": now.isoformat(),
            "preparation": summarize(DocumentPreparationJob, now),
            "extraction": summarize(DocumentExtraction, now),
        }
        metadata = (
            DocumentExtraction.objects.filter(status="complete", completed_at__gte=now - timedelta(hours=24))
            .order_by("-completed_at")
            .values_list("analysis_metadata", flat=True)[:1000]
        )
        known, missing, estimates = 0, 0, []

        def amount(value):
            return (
                isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
            )

        for item in metadata:
            if not isinstance(item, dict):
                missing += 1
                continue
            diagnostics = item.get("evidence_diagnostics", {})
            if not isinstance(diagnostics, dict):
                diagnostics = {}
            value = diagnostics.get("estimated_usd")
            classifier = item.get("classification_usage")
            if not isinstance(classifier, list):
                classifier = None
            if classifier is None and item.get("classification_skipped") == "content_filter":
                classifier = []
            if item.get("analysis_mode") == "keyword":
                estimates.append(0.0)
                known += 1
            elif (
                not amount(value)
                or diagnostics.get("cost_incomplete", True)
                or classifier is None
                or any(not isinstance(call, dict) or not amount(call.get("estimated_usd")) for call in classifier)
            ):
                missing += 1
            else:
                known += 1
                estimates.append(float(value) + sum(float(call["estimated_usd"]) for call in classifier))
        report["completed_pipeline_cost_sample"] = {
            "known_jobs": known,
            "unknown_or_incomplete_jobs": missing,
            "estimated_usd": sum(estimates),
            "estimated_usd_per_known_job": sum(estimates) / known if known else None,
            "coverage": "Up to 1000 recent completions; excludes prior failed attempts, failed jobs, infrastructure and storage. Configured price estimates, not billing totals.",
        }
        report["database_connections"] = None
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT state, count(*) FROM pg_stat_activity WHERE datname=current_database() GROUP BY state"
                )
                report["database_connections"] = {state or "unknown": count for state, count in cursor.fetchall()}
        self.stdout.write(json.dumps(report, sort_keys=True))

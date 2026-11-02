# ruff: noqa: E402
"""Inspect durable outcomes in the disposable database, without correcting failures."""

import json
from pathlib import Path

import django

django.setup()
from django.db import connection
from django.db.models import Count
from efile.models import DocumentExtraction, FilingDocument, FilingDraft

assert connection.settings_dict["NAME"] == "capacity"
with connection.cursor() as cursor:
    cursor.execute("""select count(*) from efile_filingdraft
        where case_title ~ '^Synthetic-[0-9]+-[0-9]+$'
        and user_id <> split_part(case_title, '-', 2)::bigint""")
    title_owner_mismatches = cursor.fetchone()[0]
result = {
    "draft_count": FilingDraft.objects.count(),
    "document_count": FilingDocument.objects.count(),
    "submission_statuses": list(
        FilingDraft.objects.filter(court_code="capacity")
        .values("status")
        .annotate(count=Count("pk"))
    ),
    "extraction_statuses": list(
        DocumentExtraction.objects.values("status").annotate(count=Count("pk"))
    ),
    "saved_title_owner_mismatches": title_owner_mismatches,
    "in_flight_submissions": FilingDraft.objects.filter(status="submitting").count(),
}
output = Path("/bench/capacity/results/database-audit.json")
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(
    json.dumps(result, indent=2) + "\n"
)
print(json.dumps(result, indent=2))
assert not title_owner_mismatches
assert not result["in_flight_submissions"]

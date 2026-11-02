# ruff: noqa: E402
"""Prepare reviewed synthetic filings; timed test starts at the real submit boundary."""

import json
import os
from pathlib import Path

import django

django.setup()
from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.core import signing
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from efile.models import FilingDraft, FilingDocument
from efile.services.fee_quotes import record_fee_quote
from efile.utils.s3_upload_handler import S3UploadHandler

assert settings.DATABASES["default"]["HOST"] in {"db", "dbnet"}
handler = S3UploadHandler()
content = Path("/bench/capacity/document-256k.pdf").read_bytes()
stored = handler.upload_file(
    SimpleUploadedFile("Synthetic.pdf", content, content_type="application/pdf")
)
assert stored["success"]
rows = json.loads(Path("/bench/capacity/sessions.json").read_text())
result = []
for row in rows[: int(os.getenv("CAPACITY_SUBMISSIONS", "600"))]:
    draft = FilingDraft.objects.create(
        user_id=row["user_id"],
        jurisdiction="illinois",
        existing_case="new",
        current_step="review",
        court_code="capacity",
        court_name="Synthetic court",
        case_category_code="civil",
        case_type_code="contract",
        selected_payment_account_id="capacity-card",
        selected_payment_account_type="CC",
        document_checklist_acknowledged=True,
    )
    documents = []
    for i in range(3):
        documents.append(
            FilingDocument.objects.create(
                draft=draft,
                name=f"Synthetic-{i}.pdf",
                role="lead" if i == 0 else "supporting",
                sort_order=i,
                s3_key=stored["key"],
                public_url=stored["url"],
                content_type="application/pdf",
                size=len(content),
                preparation="unchanged",
                preparation_reviewed_at=timezone.now(),
                filing_type_code="petition",
                document_type_code="public",
                filing_component_code="lead",
                document_type_confirmed=True,
            )
        )
    record_fee_quote(draft, "0.00", [])
    session = SessionStore(session_key=row["sessionid"])
    session["filing_draft_id"] = draft.pk
    session.save()
    token = signing.dumps(
        {
            "draft": str(draft.pk),
            "jurisdiction": "illinois",
            "court": "capacity",
            "requirements": [],
        },
        salt="court-disclaimers",
    )
    result.append(
        {
            **row,
            "draft": draft.pk,
            "payload": {
                "confirm_submission": True,
                "disclaimer_token": token,
                "efile_data": {
                    "efile_case_category": "civil",
                    "efile_case_type": "contract",
                    "al_court_bundle": [{"document_id": d.pk} for d in documents],
                },
            },
        }
    )
Path("/bench/capacity/submissions.json").write_text(json.dumps(result))
print(f"Prepared {len(result)} reviewed drafts")

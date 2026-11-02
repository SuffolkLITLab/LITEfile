# ruff: noqa: E402
"""Seed only the disposable capacity database, including valid authenticated sessions."""

import io
import json
import os
from pathlib import Path

import django

django.setup()
from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore
from efile.models import FilingDraft, FilingDocument, UserProfile
from efile.utils.s3_upload_handler import S3UploadHandler
from pypdf import PdfWriter

assert settings.DATABASES["default"]["HOST"] in {"db", "dbnet"}
assert settings.DATABASES["default"]["NAME"] == "capacity"
users = int(os.getenv("CAPACITY_USERS", "1200"))
history = int(os.getenv("CAPACITY_HISTORY", "120000"))
if UserProfile.objects.filter(username__startswith="capacity-").exists():
    raise RuntimeError("Use a fresh disposable database")
UserProfile.objects.bulk_create(
    [
        UserProfile(username=f"capacity-{i}", tyler_jurisdiction="illinois")
        for i in range(users)
    ]
)
accounts = list(
    UserProfile.objects.filter(username__startswith="capacity-").order_by("pk")
)
for start in range(0, history, 2000):
    drafts = FilingDraft.objects.bulk_create(
        [
            FilingDraft(
                user=accounts[i % users],
                jurisdiction="illinois",
                status="submitted",
                case_title=f"Synthetic historical {i}",
            )
            for i in range(start, min(start + 2000, history))
        ]
    )
    FilingDocument.objects.bulk_create(
        [
            FilingDocument(
                draft=d,
                name=f"Synthetic-{j}.pdf",
                role="lead" if j == 0 else "supporting",
                sort_order=j,
            )
            for d in drafts
            for j in range(3)
        ]
    )
rows = []
for user in accounts:
    session = SessionStore()
    session[SESSION_KEY] = str(user.pk)
    session[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
    session[HASH_SESSION_KEY] = user.get_session_auth_hash()
    session["jurisdiction"] = "illinois"
    session["auth_tokens"] = {"TYLER-TOKEN-ILLINOIS": "synthetic-token"}
    session.save()
    rows.append({"sessionid": session.session_key, "user_id": user.pk})
Path("/bench/capacity/sessions.json").write_text(json.dumps(rows))
writer = PdfWriter()
for _ in range(5):
    writer.add_blank_page(width=612, height=792)
buf = io.BytesIO()
writer.write(buf)
# Valid PDF with a trailing PDF comment: controls transfer size without compression.
base = buf.getvalue()
for size in (256, 1024, 5120):
    Path(f"/bench/capacity/document-{size}k.pdf").write_bytes(
        base + b"\n%" + b"x" * (size * 1024 - len(base) - 2)
    )
handler = S3UploadHandler()
assert handler._ensure_initialized()
handler.s3_client.create_bucket(Bucket="capacity")
print(
    json.dumps(
        {
            "users": users,
            "historical_drafts": FilingDraft.objects.count(),
            "historical_documents": FilingDocument.objects.count(),
        }
    )
)

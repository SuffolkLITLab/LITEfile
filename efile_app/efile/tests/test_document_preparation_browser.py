"""Opt-in real Gotenberg + Chromium validation. Uses only synthetic documents.

Run with DOCUMENT_PREPARATION_BROWSER_TESTS=1 uv run pytest -s ...
S3 is an in-memory test double; conversion and PDF.js are real.
"""

import io
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from django.conf import settings
from django.db import close_old_connections, connections
from django.urls import reverse
from pypdf import PdfReader

from efile.models import FilingDraft, FilingParty
from efile.tests.pdf_helpers import docx_bytes, pdf_bytes
from efile.tests.test_document_extractions import authorize


@pytest.mark.integration
@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    not os.getenv("DOCUMENT_PREPARATION_BROWSER_TESTS"), reason="Opt-in: requires Gotenberg and Chromium"
)
@pytest.mark.parametrize("async_preparation", [False, True])
def test_real_conversion_and_browser_previews(
    live_server, client, django_user_model, tmp_path, monkeypatch, async_preparation
):
    monkeypatch.setattr(settings, "DOCUMENT_PREPARATION_ASYNC", async_preparation)
    user = django_user_model.objects.create_user(username="synthetic-preview", tyler_jurisdiction="vermont")
    draft = FilingDraft.objects.create(
        user=user,
        jurisdiction="vermont",
        workflow_version=2,
        ai_assistance_opted_out=True,
        existing_case="existing",
        previous_case_id="synthetic-case-id",
        docket_number="synthetic-number",
        case_category_code="synthetic-category",
        court_code="synthetic-court",
        case_type_code="synthetic-case",
    )
    from efile.tests.helpers import loaded_case_snapshot

    draft.existing_case_snapshot = loaded_case_snapshot(draft)
    draft.save()
    FilingParty.objects.create(
        draft=draft, role="filer", is_filing_party=True, first_name="Jordan", last_name="Example"
    )
    authorize(client, draft)
    objects = {}
    handler = MagicMock()
    handler.bucket_name = "synthetic"

    def store(file, **kwargs):
        key = f"{kwargs['file_type']}/{len(objects)}"
        objects[key] = file.read()
        return {"success": True, "key": key}

    handler.upload_file.side_effect = store
    handler.get_public_url.side_effect = lambda key: f"https://synthetic.invalid/{key}"
    handler.s3_client.get_object.side_effect = lambda **kw: {"Body": io.BytesIO(objects[kw["Key"]])}

    def download(key, destination):
        Path(destination).write_bytes(objects[key])
        return {"success": True}

    handler.download_file.side_effect = download
    handler.delete_file.side_effect = lambda key: {"success": objects.pop(key, None) is not None}
    paths = []
    for name, content in [
        (
            "multiline.pdf",
            pdf_bytes(form_value="First line\nSecond line\nThird line", missing_appearance=True, pages=2),
        ),
        ("word-filing.docx", docx_bytes("Synthetic court filing from Word")),
    ]:
        file = tmp_path / name
        file.write_bytes(content)
        paths.append(str(file))
    evidence = Path(os.getenv("DOCUMENT_PREPARATION_EVIDENCE_DIR", str(tmp_path / "evidence")))
    config = tmp_path / "browser.json"
    config.write_text(
        json.dumps(
            {
                "baseUrl": live_server.url,
                "asyncPreparation": async_preparation,
                "cookie": client.cookies[settings.SESSION_COOKIE_NAME].value,
                "files": paths,
                "evidence": str(evidence),
                **{
                    key: reverse(view, kwargs={"jurisdiction": "vermont"}) + f"?draft={draft.pk}"
                    for key, view in [
                        ("uploadUrl", "upload_documents"),
                        ("previewUrl", "preview_documents"),
                        ("organizeUrl", "organize_documents"),
                        ("paymentUrl", "payment"),
                    ]
                },
            }
        )
    )
    stop = threading.Event()
    worker_errors = []

    def drain():
        from efile.services.document_preparation_queue import claim_preparation, fail_preparation, process_preparation

        try:
            while not stop.is_set():
                close_old_connections()
                job = claim_preparation()
                if job:
                    try:
                        process_preparation(job.pk, job.claim_token)
                    except ValueError as error:
                        fail_preparation(job.pk, job.claim_token, error)
                stop.wait(0.1)
        except Exception as error:
            worker_errors.append(error)
        finally:
            connections.close_all()

    with (
        patch("efile.services.document_preparation_queue.S3UploadHandler", return_value=handler),
        patch("efile.utils.s3_upload_handler.S3UploadHandler", return_value=handler),
        patch("efile.services.document_uploads.S3UploadHandler", return_value=handler),
        patch("efile.views.document_previews.S3UploadHandler", return_value=handler),
        patch("efile.views.extraction_review.S3UploadHandler", return_value=handler),
        patch("efile.services.document_uploads.queue_document_extraction"),
        patch("efile.services.people.get_party_types", return_value=[]),
        patch("efile.views.payment.estimate_fees", return_value={}),
    ):
        worker = threading.Thread(target=drain) if async_preparation else None
        if worker:
            worker.start()
        try:
            result = subprocess.run(
                ["node", "tests/document-preparation-browser.js", str(config)],
                cwd=settings.BASE_DIR,
                capture_output=True,
                text=True,
                timeout=180,
            )
        finally:
            stop.set()
            if worker:
                worker.join(timeout=60)
                assert not worker.is_alive()
    assert not worker_errors, worker_errors
    print(result.stdout)
    assert result.returncode == 0, result.stdout + result.stderr
    assert draft.documents.count() == 2
    for doc in draft.documents.all():
        assert doc.original_s3_key
        assert doc.preparation_reviewed_at is not None
        reader = PdfReader(io.BytesIO(objects[doc.s3_key]))
        assert len(reader.pages) == 2
        assert not reader.get_fields()
        text = " ".join(page.extract_text() for page in reader.pages)
        if doc.preparation == "flattened":
            assert all(line in text for line in ["First line", "Second line", "Third line"])
        else:
            assert "Synthetic court filing from Word" in text
            assert cast(Any, reader.trailer["/Root"]).get("/StructTreeRoot")

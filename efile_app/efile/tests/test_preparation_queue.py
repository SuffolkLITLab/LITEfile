import uuid
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from efile.models import DocumentPreparationJob, FilingDraft, StoredUpload
from efile.services import document_preparation_queue as queue
from efile.services.document_preparation import PreparationError, PreparationUnavailable, prepare_document
from efile.services.document_previews import approve_document_review, preview_fingerprint, require_document_previews
from efile.services.document_uploads import upload_files
from efile.tests.pdf_helpers import pdf_bytes
from efile.tests.test_document_extractions import authorize

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def draft(django_user_model):
    user = django_user_model.objects.create_user(username="preparation-user", tyler_jurisdiction="illinois")
    return FilingDraft.objects.create(user=user, jurisdiction="illinois", workflow_version=2)


@pytest.fixture
def storage(monkeypatch):
    objects = {}
    handler = MagicMock()
    handler._ensure_initialized.return_value = True
    handler.bucket_name = "synthetic"

    def put(file, **kwargs):
        file.seek(0)
        key = f"synthetic/{uuid.uuid4()}"
        objects[key] = file.read()
        return {"success": True, "key": key}

    def download(key, path):
        from pathlib import Path

        if key not in objects:
            return {"success": False}
        Path(path).write_bytes(objects[key])
        return {"success": True}

    handler.upload_file.side_effect = put
    handler.download_file.side_effect = download
    handler.delete_file.side_effect = lambda key: {"success": objects.pop(key, None) is not None}
    handler.get_public_url.side_effect = lambda key: f"https://storage.invalid/{key}"
    for module in (queue, __import__("efile.services.document_uploads", fromlist=["S3UploadHandler"])):
        monkeypatch.setattr(module, "S3UploadHandler", lambda: handler)
    monkeypatch.setattr("efile.utils.s3_upload_handler.S3UploadHandler", lambda: handler)
    monkeypatch.setattr("efile.services.document_uploads.queue_document_extraction", lambda document: None)
    return handler, objects


def files(*contents):
    return [
        SimpleUploadedFile(f"document-{index}.pdf", content)
        for index, content in enumerate(contents or [pdf_bytes("Synthetic filing")])
    ]


def enqueue(draft, contents=None, **kwargs):
    return queue.enqueue_preparation(draft, contents or files(), uuid.uuid4(), **kwargs)


def test_acceptance_is_durable_idempotent_and_does_not_prepare(draft, storage, monkeypatch):
    prepare = MagicMock(side_effect=AssertionError("Preparation ran in request"))
    monkeypatch.setattr(queue, "prepare_document", prepare)
    token = uuid.uuid4()
    source = pdf_bytes("Synthetic original")
    job = queue.enqueue_preparation(draft, files(source), token)
    repeated = queue.enqueue_preparation(draft, files(source), token)
    assert job.pk == repeated.pk
    assert len(storage[1]) == 1
    assert StoredUpload.objects.filter(draft=draft).count() == 1
    assert not draft.documents.exists()
    prepare.assert_not_called()
    with pytest.raises(PreparationError, match="request changed"):
        queue.enqueue_preparation(draft, files(pdf_bytes("Different")), token)
    with pytest.raises(PreparationError, match="still being prepared"):
        enqueue(draft)
    with pytest.raises(ValueError, match="still being prepared"):
        require_document_previews(draft)


def test_worker_prepares_outside_transaction_and_publishes_once(draft, storage, monkeypatch):
    source = pdf_bytes("The same filing bytes")
    job = enqueue(draft, files(source, source))

    def checked_prepare(file, jurisdiction):
        assert not connection.in_atomic_block
        return prepare_document(file, jurisdiction)

    monkeypatch.setattr(queue, "prepare_document", checked_prepare)
    claimed = queue.claim_preparation()
    assert claimed.pk == job.pk
    assert queue.process_preparation(job.pk, claimed.claim_token)
    assert not queue.process_preparation(job.pk, claimed.claim_token)
    job.refresh_from_db()
    assert job.status == "complete"
    documents = list(draft.documents.order_by("pk"))
    assert len(documents) == 2
    assert [doc.role for doc in documents] == ["lead", "supporting"]
    assert all(storage[1][doc.s3_key] == source for doc in documents)
    assert all(doc.preparation == "unchanged" and doc.preparation_reviewed_at is None for doc in documents)
    assert len(storage[1]) == 2  # No redundant storage of unchanged bytes.
    with pytest.raises(ValueError, match="Review your PDFs"):
        require_document_previews(draft)
    approve_document_review(draft, preview_fingerprint(documents))
    require_document_previews(draft)


def test_invalid_batch_retains_originals_without_partial_documents(draft, storage):
    job = enqueue(draft, files(pdf_bytes(), b"not a PDF"))
    claimed = queue.claim_preparation()
    with pytest.raises(PreparationError) as error:
        queue.process_preparation(job.pk, claimed.claim_token)
    queue.fail_preparation(job.pk, claimed.claim_token, error.value)
    job.refresh_from_db()
    assert job.status == "failed"
    assert not draft.documents.exists()
    assert len(storage[1]) == 2
    with pytest.raises(ValueError):
        require_document_previews(draft)


def test_expired_worker_cannot_publish_or_overwrite_new_claim(draft, storage):
    job = enqueue(draft)
    old = queue.claim_preparation()
    DocumentPreparationJob.objects.filter(pk=job.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    current = queue.claim_preparation()
    assert current.claim_token != old.claim_token
    assert current.attempts == 2
    assert not queue.process_preparation(job.pk, old.claim_token)
    queue.fail_preparation(job.pk, old.claim_token, RuntimeError())
    assert queue.process_preparation(job.pk, current.claim_token)
    assert draft.documents.count() == 1


def test_crash_on_final_attempt_becomes_actionable_failure(draft, storage):
    job = enqueue(draft)
    DocumentPreparationJob.objects.filter(pk=job.pk).update(
        status="processing", attempts=3, lease_expires_at=timezone.now() - timedelta(seconds=1)
    )
    assert queue.claim_preparation() is None
    job.refresh_from_db()
    assert job.status == "failed"
    queue.resolve_preparation(draft, "retry_preparation")
    retry = queue.claim_preparation()
    assert retry.attempts == 1


def test_transient_failure_retries_with_backoff_then_stops(draft, storage):
    job = enqueue(draft)
    for attempt in range(1, 4):
        claimed = queue.claim_preparation()
        assert claimed.attempts == attempt
        queue.fail_preparation(job.pk, claimed.claim_token, PreparationUnavailable("Synthetic outage"))
        job.refresh_from_db()
        assert job.status == ("failed" if attempt == 3 else "pending")
        assert queue.claim_preparation() is None
        DocumentPreparationJob.objects.filter(pk=job.pk).update(available_at=timezone.now())
    assert not draft.documents.exists()
    assert len(storage[1]) == 1


@pytest.mark.parametrize("action", ["cancel", "privacy"])
def test_in_flight_work_cannot_resurrect_cancelled_or_frozen_upload(draft, storage, monkeypatch, action):
    job = enqueue(draft)
    claimed = queue.claim_preparation()

    def interrupt(file, jurisdiction):
        if action == "cancel":
            queue.resolve_preparation(draft, "cancel_preparation")
        else:
            FilingDraft.objects.filter(pk=draft.pk).update(deletion_pending=True)
        return prepare_document(file, jurisdiction)

    monkeypatch.setattr(queue, "prepare_document", interrupt)
    assert not queue.process_preparation(job.pk, claimed.claim_token)
    assert not draft.documents.exists()
    assert storage[0].upload_file.call_count == 1  # Only original acceptance.


def test_replacement_keeps_old_document_until_success(draft, storage):
    original = pdf_bytes("Old filing")
    upload_files(draft, files(original), "illinois")
    document = draft.documents.get()
    document.filing_type_code = "synthetic-code"
    document.save()
    old_key = document.s3_key
    replacement = pdf_bytes("Replacement filing")
    job = enqueue(draft, files(replacement), replacement_id=document.pk)
    document.refresh_from_db()
    assert document.s3_key == old_key
    with pytest.raises(ValueError):
        approve_document_review(draft, preview_fingerprint([document]))
    claimed = queue.claim_preparation()
    queue.process_preparation(job.pk, claimed.claim_token)
    document.refresh_from_db()
    assert draft.documents.count() == 1
    assert storage[1][document.s3_key] == replacement
    assert document.filing_type_code == "synthetic-code"
    assert document.preparation_reviewed_at is None


def test_changed_document_cannot_be_silently_overwritten(draft, storage):
    upload_files(draft, files(), "illinois")
    document = draft.documents.get()
    job = enqueue(draft, replacement_id=document.pk)
    claimed = queue.claim_preparation()
    document.name = "Changed while queued"
    document.save()
    with pytest.raises(PreparationError, match="filing changed"):
        queue.process_preparation(job.pk, claimed.claim_token)
    document.refresh_from_db()
    assert document.name == "Changed while queued"


def test_http_acceptance_resume_and_ownership(draft, storage, client, django_user_model, settings):
    settings.DOCUMENT_PREPARATION_ASYNC = True
    authorize(client, draft)
    url = reverse("upload_documents", kwargs={"jurisdiction": "illinois"})
    result = client.post(url, {"documents": files(), "upload_request_token": str(uuid.uuid4())})
    assert result.status_code == 202
    assert result.json()["preparation_pending"]
    page = client.get(url)
    assert b"Your uploads are saved" in page.content
    status_url = reverse("document_extraction_status", kwargs={"jurisdiction": "illinois"})
    assert client.get(status_url).json()["preparation"]["status"] == "pending"
    other = django_user_model.objects.create_user(username="other-preparation", tyler_jurisdiction="illinois")
    client.force_login(other)
    response = client.get(status_url, {"draft": draft.pk})
    assert response.status_code in {401, 404, 409}


def test_failed_publication_retains_cleanup_ownership(draft, storage, monkeypatch):
    from efile.services.document_preparation import PreparedDocument

    job = enqueue(draft, files(pdf_bytes(), pdf_bytes()))
    claimed = queue.claim_preparation()
    monkeypatch.setattr(
        queue,
        "prepare_document",
        lambda file, jurisdiction: PreparedDocument(pdf_bytes(), file.name, "flattened", True),
    )
    handler, objects = storage
    put = handler.upload_file.side_effect
    calls = 0

    def fail_second_filing(file, **kwargs):
        nonlocal calls
        calls += 1
        return put(file, **kwargs) if calls == 1 else {"success": False}

    handler.upload_file.side_effect = fail_second_filing
    handler.delete_file.side_effect = lambda key: {"success": False}
    with pytest.raises(ValueError, match="filing copy could not be saved"):
        queue.process_preparation(job.pk, claimed.claim_token)
    assert not draft.documents.exists()
    assert len(objects) == 3  # Two retained originals and one failed-cleanup filing copy.
    assert set(StoredUpload.objects.filter(draft=draft).values_list("key", flat=True)) == set(objects)
    job.refresh_from_db()
    assert job.status == "processing"


def test_postgres_locked_first_draft_does_not_idle_preparation_worker(draft, storage):
    import threading

    from django.db import connections, transaction

    if connection.vendor != "postgresql":
        pytest.skip("Requires real PostgreSQL row locks")
    first = enqueue(draft)
    other = FilingDraft.objects.create(user=draft.user, jurisdiction="illinois", workflow_version=2)
    second = enqueue(other)
    locked, release = threading.Event(), threading.Event()
    errors = []

    def hold():
        try:
            with transaction.atomic():
                FilingDraft.objects.select_for_update().get(pk=draft.pk)
                locked.set()
                assert release.wait(10)
        except Exception as error:
            errors.append(error)
        finally:
            connections.close_all()

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert locked.wait(5)
        claimed = queue.claim_preparation()
        assert claimed.pk == second.pk
        first.refresh_from_db()
        assert first.status == "pending"
    finally:
        release.set()
        thread.join(timeout=10)
    assert not errors


@pytest.mark.parametrize("status", ["pending", "processing", "failed"])
def test_pending_replacement_blocks_the_real_submission_endpoint(draft, storage, client, monkeypatch, status):
    upload_files(draft, files(), "illinois")
    document = draft.documents.get()
    approve_document_review(draft, preview_fingerprint([document]))
    job = enqueue(draft, replacement_id=document.pk)
    DocumentPreparationJob.objects.filter(pk=job.pk).update(status=status)
    authorize(client, draft)
    forward = MagicMock()
    monkeypatch.setattr("efile.views.submission.forward_final_filing", forward)
    response = client.post(reverse("submit_final_filing"), data="{}", content_type="application/json")
    assert response.status_code == 412
    assert "still being prepared" in response.json()["error"]
    forward.assert_not_called()
    document.refresh_from_db()
    assert document.preparation_reviewed_at is not None  # The old copy alone is insufficient.

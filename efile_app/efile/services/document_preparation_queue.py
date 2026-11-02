"""Durable, fenced preparation with no conversion inside a database transaction."""

import hashlib
import json
import tempfile
import uuid
from datetime import timedelta
from functools import partial
from pathlib import Path
from threading import BoundedSemaphore

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from efile.models import DocumentPreparationJob, FilingDraft, StoredUpload
from efile.services.document_preparation import (
    PreparationError,
    PreparationUnavailable,
    cleanup_unreferenced_uploads,
    prepare_document,
    store_prepared_document,
)
from efile.services.document_previews import preview_fingerprint
from efile.services.drafts import ACTIVE_DRAFT_STATUSES
from efile.utils.s3_upload_handler import S3UploadHandler

UNFINISHED = ("uploading", "pending", "processing", "failed")
MAX_ATTEMPTS = 3
UPLOAD_SLOTS = BoundedSemaphore(4)


def release_connection():
    # SQLite test connections may own an in-memory database.
    if connection.vendor == "postgresql" and not connection.in_atomic_block:
        connection.close()


def preparation_timeout(job):
    return max(300, 30 + 120 * len(job.originals))


def source_fingerprint(draft, replacement_id, version=2):
    if version == 1:
        return preview_fingerprint(draft.documents.all())
    if not replacement_id:
        return ""
    document = (
        draft.documents.filter(pk=replacement_id)
        .values("pk", "s3_key", "original_s3_key", "preparation", "size")
        .first()
    )
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()


def check_source(draft, job):
    if source_fingerprint(draft, job.replacement_id, job.fingerprint_version) != job.document_fingerprint:
        raise PreparationError("Your filing changed while preparing files. Remove this upload and upload again.")


def unfinished_preparation(draft):
    return DocumentPreparationJob.objects.filter(draft=draft, status__in=UNFINISHED).first()


def require_preparation_finished(draft):
    if unfinished_preparation(draft):
        raise PreparationError("Your files are still being prepared. Return to uploads to check their progress.")


def _editable(draft):
    if draft.deletion_pending or draft.status not in ACTIVE_DRAFT_STATUSES:
        raise PreparationError("This filing is no longer available to edit.")


def enqueue_preparation(draft, files, request_token, replacement_id=None):
    release_connection()
    with UPLOAD_SLOTS:
        return _enqueue(draft, files, request_token, replacement_id)


def _enqueue(draft, files, request_token, replacement_id):
    """Acknowledge only after every original and the job have been committed."""
    try:
        token = uuid.UUID(str(request_token))
        replacement_id = int(replacement_id) if replacement_id else None
    except (ValueError, TypeError) as error:
        raise PreparationError("Reload the upload page and try again.") from error
    if len(files) > 10:
        raise PreparationError("Choose up to 10 files at a time. You can add more after these are ready.")
    manifests = []
    total = 0
    for file in files:
        if Path(file.name).suffix.lower() not in settings.ALLOWED_FILE_TYPES:
            raise PreparationError("Choose a PDF or Word file.")
        file.seek(0)
        data = file.read(settings.MAX_FILE_SIZE + 1)
        file.seek(0)
        if not data or len(data) > settings.MAX_FILE_SIZE:
            raise PreparationError("Choose a file that is not empty and is up to 10 MB.")
        total += len(data)
        if total > settings.DOCUMENT_PREPARATION_BATCH_BYTES:
            raise PreparationError("Choose up to 50 MB at a time. You can add more files after these are ready.")
        manifests.append({"name": file.name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    if not manifests:
        raise PreparationError("Choose at least one PDF or Word document to upload.")
    fingerprint = hashlib.sha256(json.dumps([replacement_id, manifests], sort_keys=True).encode()).hexdigest()
    with transaction.atomic(durable=True):
        draft = FilingDraft.objects.select_for_update().get(pk=draft.pk)
        _editable(draft)
        previous = draft.preparation_jobs.filter(request_token=token).first()
        if previous:
            if previous.fingerprint != fingerprint:
                raise PreparationError("This upload request changed. Reload the page before uploading again.")
            if not previous.originals_ready:
                raise PreparationError("This upload did not finish. Reload the uploads page to check or remove it.")
            return previous
        require_preparation_finished(draft)
        if replacement_id and (len(files) != 1 or not draft.documents.filter(pk=replacement_id).exists()):
            raise PreparationError("Choose one file to replace a document in this draft.")
        for manifest in manifests:
            manifest["key"] = f"efile-documents/original/{uuid.uuid4().hex}{Path(manifest['name']).suffix.lower()}"
        job = DocumentPreparationJob.objects.create(
            draft=draft,
            request_token=token,
            fingerprint=fingerprint,
            originals=manifests,
            originals_ready=False,
            status="uploading",
            claim_token=uuid.uuid4(),
            lease_expires_at=timezone.now() + timedelta(minutes=30),
            replacement_id=replacement_id,
            fingerprint_version=2,
            document_fingerprint=source_fingerprint(draft, replacement_id),
        )
        StoredUpload.objects.bulk_create([StoredUpload(draft=draft, key=item["key"]) for item in manifests])
    handler = S3UploadHandler()
    try:
        for file, manifest in zip(files, manifests, strict=True):
            if not _upload_claim(job).exists():
                raise PreparationError("This upload was removed or interrupted. Reload the uploads page.")
            release_connection()
            result = handler.upload_file(file, file_type="original", object_key=manifest["key"])
            if not result.get("success") or result.get("key") != manifest["key"]:
                raise PreparationUnavailable(
                    "Your files could not be saved. Reload the uploads page to remove and retry."
                )
        with transaction.atomic():
            FilingDraft.objects.select_for_update().get(pk=draft.pk)
            current = _upload_claim(job).select_for_update().first()
            if current is None:
                raise PreparationError("This upload was removed or interrupted. Reload the uploads page.")
            current.status, current.originals_ready = "pending", True
            current.claim_token, current.lease_expires_at = None, None
            current.save()
            return current
    except Exception:
        _upload_claim(job).update(
            status="failed",
            claim_token=None,
            error="The upload did not finish. Remove this upload and choose the files again.",
            updated_at=timezone.now(),
        )
        raise


def _upload_claim(job):
    return DocumentPreparationJob.objects.filter(
        pk=job.pk,
        status="uploading",
        claim_token=job.claim_token,
        lease_expires_at__gt=timezone.now(),
        draft__deletion_pending=False,
        draft__status__in=ACTIVE_DRAFT_STATUSES,
    )


def maintain_preparations():
    now = timezone.now()
    DocumentPreparationJob.objects.filter(status="uploading", lease_expires_at__lte=now).update(
        status="failed",
        claim_token=None,
        error="The upload did not finish. Remove this upload and choose the files again.",
        updated_at=now,
    )
    # Ownership rows remain discoverable even if cleanup fails or this process dies.
    for job in (
        DocumentPreparationJob.objects.filter(status="cancelled")
        .filter(Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now))
        .exclude(originals=[], staged_keys=[])[:64]
    ):
        if cleanup_unreferenced_uploads([item["key"] for item in job.originals] + job.staged_keys):
            DocumentPreparationJob.objects.filter(pk=job.pk, status="cancelled").update(originals=[], staged_keys=[])
        # The ownership registry remains until privacy deletion.


def claim_preparation():
    maintain_preparations()
    now = timezone.now()
    expired = Q(status="processing", lease_expires_at__lte=now)
    DocumentPreparationJob.objects.filter(expired, attempts__gte=MAX_ATTEMPTS).update(
        status="failed",
        claim_token=None,
        lease_expires_at=None,
        completed_at=now,
        error="Preparation was interrupted. Retry or remove this upload on the uploads page.",
        updated_at=now,
    )
    eligible = DocumentPreparationJob.objects.filter(
        Q(status="pending", available_at__lte=now) | expired,
        attempts__lt=MAX_ATTEMPTS,
        originals_ready=True,
        draft__deletion_pending=False,
        draft__status__in=ACTIVE_DRAFT_STATUSES,
    )
    # Bounded scanning skips a locked draft instead of idling all workers behind it.
    candidates = list(eligible.order_by("created_at", "pk").values_list("pk", "draft_id")[:64])
    for pk, draft_id in candidates:
        with transaction.atomic():
            drafts = FilingDraft.objects.filter(pk=draft_id, deletion_pending=False, status__in=ACTIVE_DRAFT_STATUSES)
            drafts = (
                drafts.select_for_update(skip_locked=True)
                if connection.features.has_select_for_update_skip_locked
                else drafts.select_for_update()
            )
            if drafts.first() is None:
                continue
            jobs = eligible.filter(pk=pk)
            if connection.features.has_select_for_update_skip_locked:
                jobs = jobs.select_for_update(skip_locked=True)
            else:
                jobs = jobs.select_for_update()
            job = jobs.first()
            if job is None:
                continue
            job.status = "processing"
            job.attempts += 1
            job.claim_token = uuid.uuid4()
            job.started_at = now
            job.lease_expires_at = now + timedelta(seconds=preparation_timeout(job) + 60)
            job.error = ""
            job.save()
            return job
    return None


def _claim(pk, token):
    if token is None:
        return DocumentPreparationJob.objects.none()
    return DocumentPreparationJob.objects.filter(
        pk=pk,
        claim_token=token,
        status="processing",
        lease_expires_at__gt=timezone.now(),
        draft__deletion_pending=False,
        draft__status__in=ACTIVE_DRAFT_STATUSES,
    )


def fail_preparation(pk, token, error):
    job = _claim(pk, token).first()
    if job is None:
        return
    permanent = isinstance(error, PreparationError) and not isinstance(error, PreparationUnavailable)
    failed = permanent or job.attempts >= MAX_ATTEMPTS
    message = (
        str(error)
        if permanent
        else "Your files could not be prepared. Retry or remove this upload on the uploads page."
    )
    grace = job.lease_expires_at if job.staged_keys else None
    _claim(pk, token).update(
        status="failed" if failed else "pending",
        error=message if failed else "",
        claim_token=None,
        lease_expires_at=grace,
        completed_at=timezone.now() if failed else None,
        available_at=max(grace or timezone.now(), timezone.now() + timedelta(seconds=min(300, 10 * 2**job.attempts))),
        updated_at=timezone.now(),
    )


def process_preparation(pk, token):
    from efile.services.document_uploads import upload_files

    job = _claim(pk, token).select_related("draft").first()
    if job is None:
        return False
    handler = S3UploadHandler()
    files, prepared = [], []
    with tempfile.TemporaryDirectory(prefix="litefile-preparation-") as directory:
        for index, source in enumerate(job.originals):
            if not _claim(pk, token).exists():
                return False
            release_connection()
            path = Path(directory) / str(index)
            if not handler.download_file(source["key"], str(path)).get("success"):
                raise PreparationUnavailable("The saved original could not be read.")
            with path.open("rb") as stream:
                data = stream.read(settings.MAX_FILE_SIZE + 1)
            if len(data) != source["size"] or hashlib.sha256(data).hexdigest() != source["sha256"]:
                raise PreparationError("The saved original could not be verified. Remove this upload and try again.")
            file = SimpleUploadedFile(source["name"], data)
            files.append(file)
            prepared.append((prepare_document(file, job.draft.jurisdiction), source["key"]))
        # Commit a storage journal before any filing-copy PUT. A killed worker
        # cannot roll this ownership record back with the publication transaction.
        with transaction.atomic(durable=True):
            draft = FilingDraft.objects.select_for_update().get(pk=job.draft_id)
            current = _claim(pk, token).select_for_update().first()
            if current is None:
                return False
            check_source(draft, current)
            filing_keys = [
                f"efile-documents/document/{uuid.uuid4().hex}.pdf" if item.operation != "unchanged" else None
                for item, _ in prepared
            ]
            keys = [key for key in filing_keys if key]
            StoredUpload.objects.bulk_create([StoredUpload(draft=draft, key=key) for key in keys])
            current.staged_keys = [*current.staged_keys, *keys]
            current.save(update_fields=["staged_keys", "updated_at"])
        stored = []
        for file, (item, original_key), filing_key in zip(files, prepared, filing_keys, strict=True):
            if not _claim(pk, token).exists():
                return False
            release_connection()
            stored.append(
                store_prepared_document(
                    handler,
                    file,
                    draft.jurisdiction,
                    "document",
                    keys=[],
                    prepared=item,
                    original_key=original_key,
                    filing_key=filing_key,
                )
            )
        with transaction.atomic():
            draft = FilingDraft.objects.select_for_update().get(pk=job.draft_id)
            current = _claim(pk, token).select_for_update().first()
            if current is None:
                return False
            check_source(draft, current)
            upload_files(
                draft,
                files,
                draft.jurisdiction,
                replace_document_id=current.replacement_id,
                _stored_documents=stored,
                _preparation_job_id=current.pk,
            )
            current.status = "complete"
            current.claim_token = None
            current.lease_expires_at = None
            current.completed_at = timezone.now()
            current.save()

    return True


def resolve_preparation(draft, action, job_id=None):
    with transaction.atomic():
        draft = FilingDraft.objects.select_for_update().get(pk=draft.pk)
        _editable(draft)
        jobs = draft.preparation_jobs.select_for_update()
        job = jobs.filter(pk=job_id).first() if job_id else jobs.filter(status__in=UNFINISHED).first()
        if job is None:
            raise PreparationError("This upload is no longer available. Reload the uploads page.")
        if job.status == "complete":
            raise PreparationError("Preparation already finished. Reload the page to remove the completed documents.")
        if job.status == "cancelled":
            return
        if action == "retry_preparation":
            if job.status != "failed":
                raise PreparationError("These files are already waiting or being prepared.")
            if not job.originals_ready:
                raise PreparationError("The upload did not finish. Remove it and choose the files again.")
            if job.lease_expires_at and job.lease_expires_at > timezone.now():
                raise PreparationError("The previous storage operation is still stopping. Try again shortly.")
            check_source(draft, job)
            job.status, job.attempts, job.error = "pending", 0, ""
            job.available_at = timezone.now()
            job.completed_at = None
        elif action == "cancel_preparation":
            job.status = "cancelled"
            job.claim_token = None
            job.completed_at = timezone.now()
            if not job.lease_expires_at or job.lease_expires_at <= timezone.now():
                transaction.on_commit(
                    partial(
                        cleanup_unreferenced_uploads, [source["key"] for source in job.originals] + job.staged_keys
                    ),
                    robust=True,
                )
        else:
            raise PreparationError("Choose retry or remove this upload.")
        job.save()

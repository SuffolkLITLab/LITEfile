"""Durable, fenced preparation with no conversion inside a database transaction."""

import hashlib
import json
import tempfile
import uuid
from datetime import timedelta
from functools import partial
from pathlib import Path

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
    cleanup_uploads,
    prepare_document,
)
from efile.services.document_previews import preview_fingerprint
from efile.services.drafts import ACTIVE_DRAFT_STATUSES
from efile.utils.s3_upload_handler import S3UploadHandler

UNFINISHED = ("pending", "processing", "failed")
MAX_ATTEMPTS = 3
LEASE_SECONDS = 360


def unfinished_preparation(draft):
    return DocumentPreparationJob.objects.filter(draft=draft, status__in=UNFINISHED).first()


def require_preparation_finished(draft):
    if unfinished_preparation(draft):
        raise PreparationError("Your files are still being prepared. Return to uploads to check their progress.")


def _editable(draft):
    if draft.deletion_pending or draft.status not in ACTIVE_DRAFT_STATUSES:
        raise PreparationError("This filing is no longer available to edit.")


def enqueue_preparation(draft, files, request_token, replacement_id=None):
    """Acknowledge only after every original and the job have been committed."""
    try:
        token = uuid.UUID(str(request_token))
        replacement_id = int(replacement_id) if replacement_id else None
    except (ValueError, TypeError) as error:
        raise PreparationError("Reload the upload page and try again.") from error
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
    handler = S3UploadHandler()
    keys = []
    failure = None
    job = None
    with transaction.atomic():
        draft = FilingDraft.objects.select_for_update().get(pk=draft.pk)
        _editable(draft)
        previous = draft.preparation_jobs.filter(request_token=token).first()
        if previous:
            if previous.fingerprint != fingerprint:
                raise PreparationError("This upload request changed. Reload the page before uploading again.")
            return previous
        require_preparation_finished(draft)
        if replacement_id and (len(files) != 1 or not draft.documents.filter(pk=replacement_id).exists()):
            raise PreparationError("Choose one file to replace a document in this draft.")
        try:
            with transaction.atomic():
                for file, manifest in zip(files, manifests, strict=True):
                    result = handler.upload_file(file, file_type="original")
                    if not result.get("success"):
                        raise PreparationUnavailable("Your files could not be saved. Try uploading again.")
                    manifest["key"] = result["key"]
                    keys.append(result["key"])
                job = DocumentPreparationJob.objects.create(
                    draft=draft,
                    request_token=token,
                    fingerprint=fingerprint,
                    originals=manifests,
                    replacement_id=replacement_id,
                    document_fingerprint=preview_fingerprint(draft.documents.all()),
                )
        except Exception as error:
            cleanup_uploads(handler, keys)
            failure = error
        # Ownership survives a rolled-back job and failed object cleanup.
        StoredUpload.objects.bulk_create([StoredUpload(draft=draft, key=key) for key in keys], ignore_conflicts=True)
    if failure:
        raise failure
    return job


def claim_preparation():
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
            job = eligible.select_for_update().filter(pk=pk).first()
            if job is None:
                continue
            job.status = "processing"
            job.attempts += 1
            job.claim_token = uuid.uuid4()
            job.started_at = now
            job.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
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
    _claim(pk, token).update(
        status="failed" if failed else "pending",
        error=message if failed else "",
        claim_token=None,
        lease_expires_at=None,
        completed_at=timezone.now() if failed else None,
        available_at=timezone.now() + timedelta(seconds=min(300, 10 * 2**job.attempts)),
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
        # Parsing/conversion is over. Serialize storage publication against privacy,
        # cancellation, replacement and submission, and recheck the fenced claim.
        failure = None
        with transaction.atomic():
            draft = FilingDraft.objects.select_for_update().get(pk=job.draft_id)
            current = _claim(pk, token).select_for_update().first()
            if current is None:
                return False
            if preview_fingerprint(draft.documents.all()) != current.document_fingerprint:
                raise PreparationError(
                    "Your filing changed while preparing files. Remove this upload and upload again."
                )
            try:
                upload_files(
                    draft,
                    files,
                    draft.jurisdiction,
                    replace_document_id=current.replacement_id,
                    _prepared_documents=prepared,
                    _preparation_job_id=current.pk,
                )
            except Exception as error:
                # Commit ownership of any failed cleanup before propagating.
                failure = error
            else:
                current.status = "complete"
                current.claim_token = None
                current.lease_expires_at = None
                current.completed_at = timezone.now()
                current.save()
        if failure:
            raise failure
    return True


def resolve_preparation(draft, action):
    with transaction.atomic():
        draft = FilingDraft.objects.select_for_update().get(pk=draft.pk)
        _editable(draft)
        job = draft.preparation_jobs.select_for_update().filter(status__in=UNFINISHED).first()
        if job is None:
            return
        if action == "retry_preparation":
            if job.status != "failed":
                raise PreparationError("These files are already waiting or being prepared.")
            if preview_fingerprint(draft.documents.all()) != job.document_fingerprint:
                raise PreparationError("Your filing changed. Remove this upload and upload again.")
            job.status, job.attempts, job.error = "pending", 0, ""
            job.available_at = timezone.now()
            job.completed_at = None
        elif action == "cancel_preparation":
            job.status = "cancelled"
            job.claim_token = None
            job.lease_expires_at = None
            job.completed_at = timezone.now()
            transaction.on_commit(
                partial(cleanup_unreferenced_uploads, [source["key"] for source in job.originals]), robust=True
            )
        else:
            raise PreparationError("Choose retry or remove this upload.")
        job.save()

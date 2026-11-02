from functools import partial

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.db.models import Max

from efile.models import FilingDocument, FilingDraft, StoredUpload
from efile.services.document_extractions import queue_document_extraction
from efile.services.document_preparation import (
    PreparationError,
    PreparationUnavailable,
    cleanup_unreferenced_uploads,
    cleanup_uploads,
    store_prepared_document,
)
from efile.services.drafts import ACTIVE_DRAFT_STATUSES, read_upload_data
from efile.services.fee_quotes import invalidate_fee_quote
from efile.utils.s3_upload_handler import S3UploadHandler
from efile.workflow import WorkflowStepKey


def upload_files(
    draft,
    uploaded_files,
    jurisdiction,
    *,
    current_step=WorkflowStepKey.UPLOAD_DOCUMENTS,
    replace_document_id=None,
    _prepared_documents=None,
    _preparation_job_id=None,
    _stored_documents=None,
):
    """Prepare and store a whole batch, then queue analysis of the filing copy."""
    handler = S3UploadHandler()
    keys = []
    failure = None
    # Hold the draft lock BEFORE sending bytes to storage. Deletion either
    # includes the completed batch or freezes it before any upload can begin.
    with transaction.atomic():
        draft = FilingDraft.objects.select_for_update().get(pk=draft.pk)
        if draft.deletion_pending or draft.status not in ACTIVE_DRAFT_STATUSES:
            raise ValueError("This filing is no longer available to edit.")
        from efile.services.document_preparation_queue import unfinished_preparation

        pending = unfinished_preparation(draft)
        if pending is not None and pending.pk != _preparation_job_id:
            raise PreparationError("Your files are still being prepared. Return to uploads to check their progress.")
        try:
            with transaction.atomic():
                if _stored_documents is None and not handler._ensure_initialized():
                    raise ValueError("Document storage is not configured. Please try again later.")
                replacement = None
                old_keys = []
                if replace_document_id:
                    try:
                        replacement_id = int(replace_document_id)
                    except (ValueError, TypeError) as error:
                        raise ValueError("Choose a document in this draft to replace.") from error
                    replacement = draft.documents.filter(pk=replacement_id).first()
                    if replacement is None or len(uploaded_files) != 1:
                        raise ValueError("Choose one file to replace a document in this draft.")
                    old_keys = [replacement.s3_key, replacement.original_s3_key]
                prepared = list(_stored_documents or [])
                for index, file in enumerate(uploaded_files if _stored_documents is None else []):
                    try:
                        options = {}
                        if _prepared_documents is not None:
                            options = {
                                "prepared": _prepared_documents[index][0],
                                "original_key": _prepared_documents[index][1],
                            }
                        prepared.append(
                            store_prepared_document(handler, file, jurisdiction, "document", keys=keys, **options)
                        )
                    except ValueError as error:
                        raise ValueError(f"{file.name}: {error}") from error
                has_lead = draft.documents.filter(role=FilingDocument.Role.LEAD).exists()
                highest = draft.documents.filter(role=FilingDocument.Role.SUPPORTING).aggregate(
                    order=Max("sort_order")
                )["order"]
                order = 0 if highest is None else highest + 1
                for values in prepared:
                    if replacement is not None:
                        from efile.models import DocumentExtraction
                        from efile.services.handoff import record

                        DocumentExtraction.objects.filter(document=replacement).delete()
                        for field, value in values.items():
                            setattr(replacement, field, value)
                        replacement.preparation_reviewed_at = None
                        replacement.save()
                        record(draft, f"documents.{replacement.pk}", "document_replacement", {"source": "upload"})
                        if replacement.role == FilingDocument.Role.LEAD:
                            draft.extracted_guesses = {}
                            draft.extraction_review_fingerprint = ""
                            draft.filing_type_proposal = {}
                            transaction.on_commit(partial(queue_document_extraction, replacement), robust=True)
                        continue
                    is_lead = not has_lead
                    document = FilingDocument.objects.create(
                        draft=draft,
                        role=FilingDocument.Role.LEAD if is_lead else FilingDocument.Role.SUPPORTING,
                        sort_order=0 if is_lead else order,
                        **values,
                    )
                    if is_lead:
                        has_lead = True
                        draft.extracted_guesses = {}
                        transaction.on_commit(partial(queue_document_extraction, document), robust=True)
                    else:
                        order += 1
                draft.current_step = str(current_step)
                invalidate_fee_quote(draft, save=False)
                draft.save()
                transaction.on_commit(partial(cleanup_unreferenced_uploads, old_keys, handler), robust=True)
        except Exception as error:
            cleanup_uploads(handler, keys)
            # Commit ownership even after the batch savepoint rolled back. A
            # failed cleanup must remain discoverable by a later privacy request.
            StoredUpload.objects.bulk_create(
                [StoredUpload(draft=draft, key=key) for key in set(keys)], ignore_conflicts=True
            )
            failure = error
    if failure is not None:
        raise failure
    return read_upload_data(draft)


def prepare_stored_documents(draft, handler):
    """Bring legacy stored uploads through the same preparation and review gate."""
    keys = []
    try:
        with transaction.atomic():
            locked = FilingDraft.objects.select_for_update().get(pk=draft.pk)
            documents = list(locked.documents.filter(preparation=""))
            if not documents:
                return
            if locked.deletion_pending or locked.status not in ACTIVE_DRAFT_STATUSES:
                raise PreparationError("This filing is no longer available to edit.")
            if not handler._ensure_initialized() or handler.s3_client is None:
                raise PreparationUnavailable("Document storage is unavailable. Please try again later.")
            old_keys = []
            for document in documents:
                if not document.s3_key:
                    raise PreparationError("The stored upload is unavailable. Replace this document before continuing.")
                response = handler.s3_client.get_object(Bucket=handler.bucket_name, Key=document.s3_key)
                body = response["Body"]
                try:
                    content = body.read(settings.MAX_FILE_SIZE + 1)
                finally:
                    body.close()
                old_keys.extend([document.s3_key, document.original_s3_key])
                file = SimpleUploadedFile(document.original_filename or document.name or "document.pdf", content)
                prepared = store_prepared_document(handler, file, draft.jurisdiction, document.role, keys=keys)
                for field, value in prepared.items():
                    setattr(document, field, value)
                document.save()
                if document.role == FilingDocument.Role.LEAD:
                    locked.extracted_guesses = {}
                    transaction.on_commit(partial(queue_document_extraction, document), robust=True)
            invalidate_fee_quote(locked, save=False)
            locked.save()
            transaction.on_commit(partial(cleanup_unreferenced_uploads, old_keys, handler), robust=True)
    except Exception:
        cleanup_uploads(handler, keys)
        raise

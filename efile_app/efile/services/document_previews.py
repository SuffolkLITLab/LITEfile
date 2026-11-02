"""Track the preview step for the current filing bytes."""

import hashlib
import json

from django.db import transaction
from django.utils import timezone

from efile.models import FilingDocument, FilingDraft
from efile.services.drafts import ACTIVE_DRAFT_STATUSES


class DocumentReviewError(ValueError):
    """The displayed documents cannot be approved."""

    def __init__(self, message, *, status=422):
        super().__init__(message)
        self.status = status


def prepare_document_review(draft, handler):
    """Prepare stored uploads so the filer reviews the copies that will be filed."""
    from efile.services.document_preparation_queue import require_preparation_finished
    from efile.services.document_uploads import prepare_stored_documents

    require_preparation_finished(draft)
    prepare_stored_documents(draft, handler)


def approve_document_review(draft, fingerprint):
    """Approve only the current prepared copies, serialized against uploads."""
    with transaction.atomic():
        locked = FilingDraft.objects.select_for_update().get(pk=draft.pk)
        from efile.services.document_preparation_queue import require_preparation_finished

        try:
            require_preparation_finished(locked)
        except ValueError as error:
            raise DocumentReviewError(str(error), status=409) from error
        documents = list(locked.documents.order_by("role", "sort_order", "pk"))
        if locked.status not in ACTIVE_DRAFT_STATUSES or locked.deletion_pending:
            raise DocumentReviewError("This filing is no longer available to edit.", status=409)
        if not documents or any(not doc.preparation for doc in documents):
            raise DocumentReviewError("Your files are not ready. Reload this page or replace them.")
        if fingerprint != preview_fingerprint(documents):
            raise DocumentReviewError("Your files changed. Review these copies before you continue.")
        FilingDocument.objects.filter(draft=locked).update(preparation_reviewed_at=timezone.now())


def unreviewed_documents(draft):
    return draft.documents.filter(preparation_reviewed_at__isnull=True)


def preview_fingerprint(documents):
    return hashlib.sha256(
        json.dumps(
            sorted(
                (doc.pk, doc.s3_key, doc.original_s3_key, doc.preparation, doc.size, str(doc.updated_at))
                for doc in documents
            )
        ).encode()
    ).hexdigest()


def require_document_previews(draft):
    from efile.services.document_preparation_queue import require_preparation_finished

    require_preparation_finished(draft)
    if draft.documents.filter(preparation="").exists() or unreviewed_documents(draft).exists():
        raise ValueError("Review your PDFs before you submit.")


def document_storage_keys(document):
    return list(dict.fromkeys(key for key in (document.s3_key, document.original_s3_key) if key))

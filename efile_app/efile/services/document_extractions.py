"""Queue and process durable lead-document extraction jobs."""

import json
import logging
import re
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from time import perf_counter

from django.conf import settings
from django.db import connection, transaction
from django.db.models import F, Q
from django.utils import timezone
from docx2python import docx2python
from markitdown import MarkItDown
from pypdf import PdfReader, PdfWriter

from efile.models import DocumentExtraction, FilingDocument, FilingDraft
from efile.services.extraction_fields import (
    EXTRACTION_FIELDS,
    EXTRACTION_HINTS,
    display_extracted_fields,
    normalize_document_evidence,
)
from efile.services.taxonomy_classification import (
    ClassificationRun,
    HierarchicalDocumentClassifier,
    deterministic_form_identity,
    exact_form_crosswalk_matches,
    scan_document_for_form_identifiers,
    summarize_form_crosswalk_matches,
)
from efile.utils.llms import extract_fields_from_file, extract_fields_from_text, get_default_model
from efile.utils.prompt_config import prompt_version
from efile.utils.s3_upload_handler import S3UploadHandler

logger = logging.getLogger(__name__)


class ExtractionSuperseded(Exception):
    """The requested analysis changed; this is not a processing failure."""


# How long a filer waits on the upload page for the first file to be read.
# After that the filer may go on and enter the case details by hand; the
# analysis keeps running and its guesses are still offered if they arrive.
EXTRACTION_WAIT_LIMIT = timedelta(minutes=2)


def queue_document_extraction(document):
    """Create or reset the one background extraction job for a lead document."""
    if document.role != FilingDocument.Role.LEAD:
        raise ValueError("Only a lead document can be analyzed")
    job, _created = DocumentExtraction.objects.update_or_create(
        document=document,
        defaults={
            "status": DocumentExtraction.Status.PENDING,
            "attempts": 0,
            "claim_token": None,  # nosec B105: clear a concurrency claim, not a credential
            "lease_expires_at": None,
            "available_at": timezone.now(),
            "total_pages": None,
            "pages_analyzed": None,
            "evidence": {},
            "classification": {},
            "analysis_metadata": {},
            "error": "",
            "started_at": None,
            "completed_at": None,
            # Restart the filer's wait for a replaced lead document.
            "created_at": timezone.now(),
        },
    )
    return job


def extraction_is_waiting(extraction):
    """Whether the filer should still wait for this analysis before reviewing."""
    return (
        extraction is not None
        and extraction.status in {DocumentExtraction.Status.PENDING, DocumentExtraction.Status.PROCESSING}
        and extraction.created_at > timezone.now() - EXTRACTION_WAIT_LIMIT
    )


def extraction_for_document(document):
    try:
        return document.extraction
    except DocumentExtraction.DoesNotExist:
        return None


@contextmanager
def limited_pdf(source_path, max_pages):
    """Yield a PDF containing at most ``max_pages`` and its page counts."""
    reader = PdfReader(source_path)
    total_pages = len(reader.pages)
    pages_analyzed = min(total_pages, max_pages)
    if total_pages <= max_pages:
        yield source_path, total_pages, pages_analyzed
        return

    temp_path = None
    try:
        with NamedTemporaryFile(delete=False, suffix=".pdf") as limited_file:
            writer = PdfWriter()
            # append retains the AcroForm and only the widgets on selected pages.
            writer.append(reader, pages=(0, max_pages), import_outline=False)
            writer.write(limited_file)
            temp_path = limited_file.name
        yield temp_path, total_pages, pages_analyzed
    finally:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)


def _searchable_pdf_text(file_path):
    """Extract selectable text cheaply for the deterministic form-ID pass."""
    reader = PdfReader(file_path)
    return "\f".join(page.extract_text() or "" for page in reader.pages)


@contextmanager
def analysis_source(source_path, max_pages):
    """DOCX has no reliable page boundaries; its text is bounded separately."""
    if Path(source_path).suffix.lower() == ".docx":
        yield source_path, None, None
    else:
        with limited_pdf(source_path, max_pages) as source:
            yield source


def _source_text(file_path):
    """Convert the leading pages to text on this machine, sending nothing out."""
    if Path(file_path).suffix.lower() == ".docx":
        with docx2python(file_path) as document:
            text = document.text
        limit = max(1, settings.DOCUMENT_EXTRACTION_MAX_TEXT_CHARS)
        if len(text) > limit:
            text = text[:limit] + "\n[Remaining document text omitted.]"
        return text, None
    source_pages = max(1, settings.DOCUMENT_CLASSIFICATION_SOURCE_PAGES)
    with limited_pdf(file_path, source_pages) as (source_path, _total, pages_converted):
        return MarkItDown().convert(source_path).text_content + _pdf_form_values(source_path), pages_converted


def _pdf_form_values(file_path):
    """Read author-entered values independently of appearance streams."""
    fields = PdfReader(file_path).get_fields() or {}
    values = {
        name: {"value": field["/V"], "label": field.get("/TU", name)}
        for name, field in fields.items()
        if field.get("/FT") != "/Sig" and field.get("/V") not in (None, "", "/Off")
    }
    if not values:
        return ""
    text = json.dumps(values, ensure_ascii=False, default=str)
    limit = max(1, settings.DOCUMENT_EXTRACTION_MAX_TEXT_CHARS)
    return "\nStored PDF form values (document data):\n" + text[:limit]


def _source_metadata(file_path, text, pages):
    is_docx = Path(file_path).suffix.lower() == ".docx"
    return {
        "source_conversion": "docx2python" if is_docx else "markitdown",
        "source_pages": pages,
        "source_text_characters": len(text),
        "source_text_truncated": is_docx and text.endswith("\n[Remaining document text omitted.]"),
    }


def _form_identifier_pass(file_path, jurisdiction, source_text):
    """Look for registry form IDs printed in the document's own text.

    No model is involved: this is a keyword scan of text the document already
    carries, so it runs whether or not the filer allows AI.
    """
    scan_started = perf_counter()
    is_docx = Path(file_path).suffix.lower() == ".docx"
    searchable_text = source_text if is_docx else _searchable_pdf_text(file_path) + _pdf_form_values(file_path)
    scan = scan_document_for_form_identifiers(jurisdiction, searchable_text)
    scan_source = "docx2python" if is_docx else "pypdf"
    if scan["status"] == "unmatched" and source_text:
        markitdown_scan = scan_document_for_form_identifiers(jurisdiction, source_text)
        if markitdown_scan["status"] != "unmatched":
            scan = markitdown_scan
            scan_source = "markitdown"
    scan_ms = round((perf_counter() - scan_started) * 1000, 2)
    return scan, scan_source, scan_ms, searchable_text


# A label that introduces a case number, followed by the number itself. Courts
# and filers write the label many ways ("Case No.", "Docket Number", "Civil
# Action No."), and OCR turns it into things like "Docker number", so the label
# is matched loosely. The value is not: it is read as a short run of
# alphanumeric chunks, and anything that never shows a digit is discarded, so a
# blank line on an unfilled form is not mistaken for a case number.
_CASE_NUMBER = re.compile(
    r"\b(?:civil\s+action|dock\w*|case)[ \t]*(?:numbers?|no\b\.?|#)[ \t]*[:.\-]?[ \t]*(?:\r?\n[ \t]*){0,2}"
    r"(?P<value>[A-Za-z0-9][A-Za-z0-9./-]*(?:[ -][A-Za-z0-9][A-Za-z0-9./-]*){0,2})",
    re.IGNORECASE,
)


def _looks_like_case_number(candidate):
    """Whether a labelled value is a case number rather than the prose after a label.

    Court forms print the label in their instructions too ("Enter the Case
    Number given by the Circuit Clerk"), so the value has to look the part: it
    starts with a numbered chunk, carries at least two digits, and any further
    chunk is either numbered as well or a short division code, as in
    ``2024 SC 000456``.
    """

    chunks = candidate.split()
    if not chunks or not any(character.isdigit() for character in chunks[0]):
        return False
    if any(len(chunk) > 4 and not any(character.isdigit() for character in chunk) for chunk in chunks[1:]):
        return False
    return sum(character.isdigit() for character in candidate) >= 2


def keyword_case_number(text):
    """Read a printed case number out of labelled text, or return an empty string."""
    for match in _CASE_NUMBER.finditer(text or ""):
        chunks = match.group("value").split()
        # Whatever follows the number on the same line -- a party name, the
        # next label -- is not part of it.
        while chunks and not any(character.isdigit() for character in chunks[-1]):
            chunks.pop()
        candidate = " ".join(chunks).strip(" .-/")
        if _looks_like_case_number(candidate):
            return candidate
    return ""


def keyword_document_analysis(file_path, jurisdiction):
    """Identify a document without any AI, for a filer who opted out.

    Everything here reads the document locally: the printed form identifier is
    matched against the form registry, a printed case number is read from its
    label, and the form's own crosswalk entry supplies the court's category and
    type names when it names exactly one of each. Those are recommendations the
    filer still confirms on the review screen, exactly as the AI ones are.
    """
    source_text, pages_converted = _source_text(file_path)
    scan, scan_source, scan_ms, searchable_text = _form_identifier_pass(file_path, jurisdiction, source_text)

    evidence = {}
    matched_form = scan["deterministic_match"] if scan.get("deterministic") else None
    if matched_form:
        evidence["form identifier"] = matched_form["form_id"]
        if matched_form.get("form_name"):
            evidence["form name"] = matched_form["form_name"]
    case_number = keyword_case_number(searchable_text) or keyword_case_number(source_text)
    if case_number:
        evidence["docket number"] = case_number

    identity = deterministic_form_identity(jurisdiction, evidence)
    crosswalk = exact_form_crosswalk_matches(jurisdiction, evidence)
    crosswalk_summary = summarize_form_crosswalk_matches(crosswalk, identity_status=identity["status"])
    guesses = display_extracted_fields(evidence)
    for level, summary_key in (
        ("case category", "category"),
        ("case type", "case_type"),
        ("filing type", "filing_type"),
    ):
        if crosswalk_summary["level_status"].get(summary_key) == "resolved":
            guesses[level] = crosswalk_summary[f"{summary_key}_candidates"][0]

    return {
        "guesses": guesses,
        "evidence": evidence,
        # No live taxonomy selection is possible without a court, and choosing
        # one is the filer's job on the next screen. The names above are what
        # the review screen's dropdowns recommend from.
        "classification": {},
        "metadata": {
            "analysis_mode": "keyword",
            "ai_assistance": "opted_out",
            **_source_metadata(file_path, source_text, pages_converted),
            "form_identifier_scan": scan,
            "form_identifier_scan_source": scan_source,
            "form_identifier_scan_ms": scan_ms,
            "form_identity_status": identity["status"],
            "crosswalk_match_count": len(crosswalk),
            "form_crosswalk_summary": crosswalk_summary,
        },
    }


def analyze_document(file_path, jurisdiction, *, use_ai=True, before_outbound=None):
    """Extract evidence from original PDF bytes or DOCX text and classify it.

    ``use_ai=False`` is the filer's opt-out (issue #104): it takes the keyword
    path instead, which never sends the document to a model.
    """
    if not use_ai:
        return keyword_document_analysis(file_path, jurisdiction)

    source_text, pages_converted = _source_text(file_path)
    form_identifier_scan, scan_source, scan_ms, _searchable_text = _form_identifier_pass(
        file_path, jurisdiction, source_text
    )

    evidence_prompt = "document_evidence_extraction"
    evidence_version, _definition, evidence_config = prompt_version(evidence_prompt)
    evidence_diagnostics = {}
    strategy = settings.DOCUMENT_EVIDENCE_STRATEGY
    if strategy in {"terra_ensemble", "bedrock_ensemble"}:
        from efile.services.evidence_ensemble import extract_ensemble

        evidence_model = "bedrock_luna_haiku_terra_v1" if strategy == "bedrock_ensemble" else "terra_followup_v1"
        raw_evidence, evidence_diagnostics = extract_ensemble(
            file_path, jurisdiction, source_text, before_outbound=before_outbound
        )
    elif strategy == "single":
        evidence_model = getattr(settings, "DOCUMENT_EVIDENCE_MODEL", "") or get_default_model(
            evidence_config.get("preferred_model_tier", "small")
        )
        if before_outbound is not None:
            before_outbound()
        extraction_kwargs = {
            "llm_hint": EXTRACTION_HINTS.get(jurisdiction, EXTRACTION_HINTS["default"]),
            "model": evidence_model,
            "prompt_name": evidence_prompt,
            "prompt_version_name": evidence_version,
        }
        fields = EXTRACTION_FIELDS.get(jurisdiction, EXTRACTION_FIELDS["default"])
        if Path(file_path).suffix.lower() == ".docx":
            evidence_diagnostics["input_mode"] = "docx2python_text"
            raw_evidence = extract_fields_from_text(source_text, fields, **extraction_kwargs)
        else:
            raw_evidence = extract_fields_from_file(
                file_path,
                fields,
                diagnostics=evidence_diagnostics,
                supplemental_text=_pdf_form_values(file_path),
                **extraction_kwargs,
            )
    else:
        raise ValueError(f"Unknown document evidence strategy: {strategy}")
    evidence = normalize_document_evidence(raw_evidence)
    ai_form_identifier = evidence.get("form identifier")
    if form_identifier_scan.get("deterministic"):
        # The printed identifier found in the source text is stronger than an
        # AI transcription of the same field. Keep the AI value in metadata for
        # review and diagnostics, but classify from the deterministic value.
        evidence["form identifier"] = form_identifier_scan["deterministic_match"]["form_id"]
    if before_outbound is not None:
        before_outbound()
    classification = (
        ClassificationRun(selections={}, metadata={"classification_skipped": "content_filter"})
        if evidence_diagnostics.get("content_filtered")
        else HierarchicalDocumentClassifier().classify(jurisdiction, evidence, source_text)
    )
    guesses = display_extracted_fields(evidence)
    for level, selection in classification.selections.items():
        if selection.get("status") == "selected":
            guesses[level] = selection["name"]
    return {
        "guesses": guesses,
        "evidence": evidence,
        "classification": classification.selections,
        "metadata": {
            "analysis_mode": "ai",
            "evidence_prompt": evidence_prompt,
            "evidence_prompt_version": evidence_version,
            "evidence_model": evidence_model,
            "evidence_strategy": strategy,
            "evidence_diagnostics": evidence_diagnostics,
            "evidence_input_mode": evidence_diagnostics.get("input_mode", "unknown"),
            **_source_metadata(file_path, source_text, pages_converted),
            "form_identifier_scan": form_identifier_scan,
            "form_identifier_scan_source": scan_source,
            "form_identifier_scan_ms": scan_ms,
            "ai_form_identifier": ai_form_identifier,
            **classification.metadata,
        },
    }


def process_document_extraction(job_id, claim_token):
    """Download, page-limit, and analyze one job already claimed by a worker."""
    job = _current_claim(job_id, claim_token).select_related("document__draft").first()
    if job is None:
        return None
    document = job.document
    filing_key = document.s3_key
    original_key = document.original_s3_key
    original_suffix = Path(document.original_filename).suffix.lower()
    use_original = bool(original_key and original_suffix in {".pdf", ".docx"})
    source_key = original_key if use_original else filing_key
    source_suffix = original_suffix if use_original else ".pdf"
    source_kind = f"original_{source_suffix[1:]}" if use_original else "filing_pdf"
    handler = S3UploadHandler()

    with TemporaryDirectory(prefix="litefile-extraction-") as temp_dir:
        source_path = str(Path(temp_dir) / f"lead{source_suffix}")
        download = handler.download_file(source_key, source_path)
        if not download.get("success"):
            raise RuntimeError(download.get("error") or "Could not read the uploaded document")

        max_pages = max(1, settings.DOCUMENT_EXTRACTION_MAX_PAGES)
        with analysis_source(source_path, max_pages) as (analysis_path, total_pages, pages_analyzed):
            # Download/parsing may take time. Recheck the claim and preference
            # before starting analysis that can send the document upstream.
            if not _current_claim(job_id, claim_token).exists():
                return None
            document.draft.refresh_from_db()
            opted_out = document.draft.ai_assistance_opted_out

            def check_outbound_permission():
                if (
                    not _current_claim(job_id, claim_token)
                    .filter(
                        document__draft__ai_assistance_opted_out=False,
                        document__role=FilingDocument.Role.LEAD,
                        document__s3_key=filing_key,
                        document__original_s3_key=original_key,
                    )
                    .exists()
                ):
                    raise ExtractionSuperseded

            try:
                analysis = analyze_document(
                    analysis_path,
                    document.draft.jurisdiction,
                    use_ai=not opted_out,
                    before_outbound=check_outbound_permission,
                )
            except ExtractionSuperseded:
                _requeue_changed_preference(job_id, claim_token, opted_out)
                _requeue_changed_source(job_id, claim_token, filing_key, original_key)
                return None

    # Keep compatibility with extensions that still return the old flat shape.
    if "guesses" in analysis and isinstance(analysis.get("guesses"), dict):
        guesses = analysis["guesses"]
        evidence = analysis.get("evidence") if isinstance(analysis.get("evidence"), dict) else {}
        classification = analysis.get("classification") if isinstance(analysis.get("classification"), dict) else {}
        metadata = analysis.get("metadata") if isinstance(analysis.get("metadata"), dict) else {}
    else:
        guesses = analysis
        evidence = {}
        classification = {}
        metadata = {"pipeline": "legacy-flat-result"}
    metadata["analysis_source"] = source_kind

    with transaction.atomic():
        # Match the preference update's lock order: draft, then job.
        draft = FilingDraft.objects.select_for_update().filter(pk=document.draft_id).first()
        job = _current_claim(job_id, claim_token).select_for_update().first()
        if job is None:
            return None
        if draft is None:
            return None
        if draft.ai_assistance_opted_out != opted_out:
            _requeue_changed_preference(job_id, claim_token, opted_out)
            return None
        if not FilingDocument.objects.filter(pk=document.pk, s3_key=filing_key, original_s3_key=original_key).exists():
            queue_document_extraction(job.document)
            return None
        document = job.document
        # A filer can remove or replace the lead while this worker is running.
        # Never let the old document overwrite the new lead's extraction.
        is_current_lead = FilingDocument.objects.filter(
            pk=document.pk,
            draft=document.draft,
            role=FilingDocument.Role.LEAD,
        ).exists()
        if is_current_lead:
            draft.extracted_guesses = guesses
            # A filer may already have continued manually after the wait limit.
            # Keep extracted amounts in evidence until the filer confirms them
            # on the case-questions form; workers must not change fee inputs.
            draft.save(update_fields=["extracted_guesses", "updated_at"])
        job.status = DocumentExtraction.Status.COMPLETE
        job.total_pages = total_pages
        job.pages_analyzed = pages_analyzed
        job.evidence = evidence
        job.classification = classification
        job.analysis_metadata = metadata
        job.error = ""
        job.completed_at = timezone.now()
        job.lease_expires_at = None
        job.save(
            update_fields=[
                "status",
                "total_pages",
                "pages_analyzed",
                "evidence",
                "classification",
                "analysis_metadata",
                "error",
                "completed_at",
                "lease_expires_at",
                "updated_at",
            ]
        )
    return job


def claim_next_extraction(stale_after_minutes=15):
    """Atomically claim one pending or interrupted job for this worker."""
    max_attempts = settings.DOCUMENT_EXTRACTION_MAX_ATTEMPTS
    now = timezone.now()
    expired = Q(status=DocumentExtraction.Status.PROCESSING) & (
        Q(lease_expires_at__lte=now)
        | Q(lease_expires_at__isnull=True, started_at__lt=now - timedelta(minutes=stale_after_minutes))
        | Q(lease_expires_at__isnull=True, started_at__isnull=True)
    )
    # A process can die during its final attempt. Such jobs must not remain
    # PROCESSING forever simply because they are excluded from claim candidates.
    DocumentExtraction.objects.filter(expired, attempts__gte=max_attempts).update(
        status=DocumentExtraction.Status.FAILED,
        error="Extraction worker lease expired after the final attempt",
        claim_token=None,
        lease_expires_at=None,
        completed_at=now,
        updated_at=now,
    )
    candidates = DocumentExtraction.objects.filter(
        attempts__lt=max_attempts, document__draft__deletion_pending=False
    ).filter(Q(status=DocumentExtraction.Status.PENDING, available_at__lte=now) | expired)
    with transaction.atomic():
        job = candidates.order_by("created_at").first()
        if job is None:
            return None
        # Match deletion and completion lock order: draft, then extraction job.
        drafts = FilingDraft.objects.filter(pk=job.document.draft_id, deletion_pending=False)
        if connection.features.has_select_for_update_skip_locked:
            drafts = drafts.select_for_update(skip_locked=True)
        else:
            drafts = drafts.select_for_update()
        if drafts.first() is None:
            return None
        job = candidates.select_for_update().filter(pk=job.pk).first()
        if job is None:
            return None
        job.status = DocumentExtraction.Status.PROCESSING
        job.attempts += 1
        job.started_at = timezone.now()
        job.claim_token = uuid.uuid4()
        job.lease_expires_at = job.started_at + timedelta(minutes=stale_after_minutes)
        job.error = ""
        job.save(
            update_fields=["status", "attempts", "started_at", "claim_token", "lease_expires_at", "error", "updated_at"]
        )
        return job


def _current_claim(job_id, claim_token):
    if claim_token is None:
        return DocumentExtraction.objects.none()
    return DocumentExtraction.objects.filter(
        pk=job_id,
        claim_token=claim_token,
        status=DocumentExtraction.Status.PROCESSING,
        lease_expires_at__gt=timezone.now(),
        document__draft__deletion_pending=False,
    )


def renew_extraction_lease(job_id, claim_token):
    """An expired or superseded worker cannot revive its own claim."""
    return bool(_current_claim(job_id, claim_token).update(lease_expires_at=timezone.now() + timedelta(minutes=15)))


def _requeue_changed_source(job_id, claim_token, filing_key, original_key):
    """Restart analysis if a source was replaced while it was being read."""
    with transaction.atomic():
        job = _current_claim(job_id, claim_token).select_for_update().select_related("document").first()
        if job is not None and (job.document.s3_key != filing_key or job.document.original_s3_key != original_key):
            queue_document_extraction(job.document)


def _requeue_changed_preference(job_id, claim_token, opted_out):
    """Refund a superseded attempt without resetting earlier real failures."""
    now = timezone.now()
    changed_documents = FilingDocument.objects.filter(draft__ai_assistance_opted_out=not opted_out).values("pk")
    # Keep the claim predicates on the UPDATE itself, rather than inside a
    # joined-query subselect, so a concurrent new claim remains protected.
    return (
        _current_claim(job_id, claim_token)
        .filter(document_id__in=changed_documents)
        .update(
            status=DocumentExtraction.Status.PENDING,
            attempts=F("attempts") - 1,
            claim_token=None,
            lease_expires_at=None,
            available_at=now,
            started_at=None,
            completed_at=None,
            error="",
            updated_at=now,
        )
    )


def record_extraction_failure(job_id, claim_token, error):
    """Retry transient failures, then expose a manual-entry fallback."""
    with transaction.atomic():
        job = _current_claim(job_id, claim_token).select_for_update().first()
        if job is None:
            return None
        retry = job.attempts < settings.DOCUMENT_EXTRACTION_MAX_ATTEMPTS
        job.status = DocumentExtraction.Status.PENDING if retry else DocumentExtraction.Status.FAILED
        # Exceptions from PDF/model clients can contain document text, URLs,
        # or credentials. Persist a fixed category, never the exception body.
        job.error = "Document analysis failed. Please retry or enter the information manually."
        job.completed_at = None if retry else timezone.now()
        job.available_at = timezone.now() + timedelta(seconds=min(300, 10 * 2 ** min(job.attempts, 5)))
        job.claim_token = None
        job.lease_expires_at = None
        job.save(
            update_fields=[
                "status",
                "error",
                "completed_at",
                "available_at",
                "claim_token",
                "lease_expires_at",
                "updated_at",
            ]
        )
    return job

"""Prepare filing copies without replacing or rasterizing the original upload."""

from __future__ import annotations

import io
import logging
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import requests
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject

from efile.utils.config_loader import config_loader

logger = logging.getLogger(__name__)


class PreparationError(ValueError):
    """An actionable document failure safe to show to the filer."""


class PreparationUnavailable(PreparationError):
    """A temporary service/storage failure for which retry is appropriate."""


@dataclass(frozen=True)
class PreparedDocument:
    content: bytes
    filename: str
    operation: str
    upload_has_form_fields: bool | None = None


def requires_flattening(jurisdiction):
    config = config_loader.load_jurisdiction_config(jurisdiction)
    return config.get("document_preparation", {}).get("flatten_pdf_forms", True)


def inspect_pdf(content):
    try:
        if not content.startswith(b"%PDF-"):
            raise ValueError("Not a PDF")
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted or not reader.pages:
            raise ValueError("Encrypted or empty PDF")
        root = reader.trailer["/Root"]
        if not isinstance(root, DictionaryObject):
            raise ValueError("Invalid PDF catalog")
        form = root.get("/AcroForm")
        if form and form.get_object().get("/XFA"):
            raise PreparationError("We cannot use this PDF form. Print it to PDF and upload that copy.")
        # Force page and annotation parsing before accepting a filing copy.
        widgets = []
        for page in reader.pages:
            if "/Annots" not in page:
                continue
            annotations = page["/Annots"].get_object()
            if not isinstance(annotations, ArrayObject):
                raise ValueError("Invalid annotation array")
            for ref in annotations:
                annotation = ref.get_object()
                if not isinstance(annotation, DictionaryObject):
                    raise ValueError("Invalid annotation")
                if annotation.get("/Subtype") == "/Widget":
                    widgets.append(annotation)
        return reader, widgets
    except PreparationError:
        raise
    except Exception as error:
        raise PreparationError("This PDF could not be read. Upload a new copy without a password.") from error


def _word_format(content, suffix):
    if suffix == ".doc":
        if not content.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            raise PreparationError("Choose a Word file or save this file as a PDF.")
        return
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > 10000 or sum(entry.file_size for entry in entries) > 100 * 1024 * 1024:
                raise ValueError("Expanded document too large")
            if not {"[Content_Types].xml", "word/document.xml"}.issubset(archive.namelist()):
                raise ValueError("Not DOCX")
    except (ValueError, zipfile.BadZipFile) as error:
        raise PreparationError(
            "This Word file could not be read. Save a new copy as Word or PDF and upload it."
        ) from error


def _gotenberg(content, suffix, route, data=None):
    base_url = settings.GOTENBERG_URL.rstrip("/")
    if not base_url:
        raise PreparationUnavailable(
            "We could not prepare your file. Try again later, or print it to PDF and upload that copy."
        )
    limit = settings.MAX_FILE_SIZE
    deadline = time.monotonic() + settings.DOCUMENT_PREPARATION_TIMEOUT_SECONDS
    try:
        with requests.post(
            f"{base_url}{route}",
            auth=(settings.GOTENBERG_USERNAME, settings.GOTENBERG_PASSWORD),
            # Do not send user filenames to the conversion service.
            files={"files": (f"document{suffix}", content, "application/octet-stream")},
            data=data or {},
            timeout=(5, settings.DOCUMENT_PREPARATION_TIMEOUT_SECONDS),
            allow_redirects=False,
            stream=True,
        ) as response:
            if response.status_code >= 500 or response.status_code in {401, 403, 429}:
                raise PreparationUnavailable("We could not prepare your file. Try again later.")
            if response.status_code != 200:
                raise PreparationError("We could not make a PDF. Save your file as a PDF and upload it.")
            result = bytearray()
            for chunk in response.iter_content(64 * 1024):
                result.extend(chunk)
                if len(result) > limit:
                    raise PreparationError("This PDF is over 10 MB. Split it into smaller files and upload them.")
                if time.monotonic() > deadline:
                    raise requests.Timeout()
            return bytes(result)
    except requests.RequestException as error:
        logger.warning("Document preparation service unavailable (%s)", type(error).__name__)
        raise PreparationUnavailable(
            "We could not prepare your file. Try again later, or print it to PDF and upload that copy."
        ) from error


def _repair_text_appearances(content, source, widgets):
    """QPDF cannot generate multiline appearances. Use pypdf for stale/missing text APs.

    Keep existing correct appearance streams (including embedded fonts) intact.
    Turning off NeedAppearances after repair stops QPDF from replacing them.
    """
    form = source.trailer["/Root"].get("/AcroForm")
    needs_appearances = bool(form and getattr(form.get_object().get("/NeedAppearances"), "value", False))
    missing = False
    for widget in widgets:
        field = widget.get("/Parent", widget).get_object()
        if field.get("/FT") == "/Tx" and (not widget.get("/AP") or not widget["/AP"].get("/N")):
            missing = True
    if not (needs_appearances or missing):
        return content
    values = {
        name: str(field.get("/V") or "")
        for name, field in (source.get_fields() or {}).items()
        if field.get("/FT") == "/Tx"
    }
    try:
        writer = PdfWriter(clone_from=source)
        writer.update_page_form_field_values(None, values, auto_regenerate=False)
        output = io.BytesIO()
        writer.write(output)
        return output.getvalue()
    except Exception as error:
        raise PreparationError(
            "This PDF's filled-in answers could not be rendered safely. Save a printed PDF copy and upload it again."
        ) from error


def _draws_nothing(widget):
    appearance = widget.get("/AP")
    return not appearance or not appearance.get_object().get("/N")


def _drop_undrawn_widgets(content, reader, widgets):
    """Remove leftover widgets that have no appearance, such as blank signature lines.

    The flatten engine skips widgets with nothing to draw, so they survive as
    form fields. They print as nothing, so removing them changes no page.
    """
    if not widgets or not all(_draws_nothing(widget) for widget in widgets):
        return content
    writer = PdfWriter(clone_from=reader)
    for page in writer.pages:
        if "/Annots" not in page:
            continue
        annotations = page["/Annots"].get_object()
        if not isinstance(annotations, ArrayObject):
            continue
        kept = [
            ref
            for ref in annotations
            if not (ref.get_object().get("/Subtype") == "/Widget" and _draws_nothing(ref.get_object()))
        ]
        if kept:
            page[NameObject("/Annots")] = ArrayObject(kept)
        else:
            del page["/Annots"]
    writer._root_object.pop("/AcroForm", None)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _flatten(content):
    source, widgets = inspect_pdf(content)
    if not widgets:
        return content, False
    fields = source.get_fields() or {}
    if any(field.get("/FT") == "/Sig" and field.get("/V") for field in fields.values()):
        raise PreparationError("We cannot prepare this signed PDF. Print it to PDF and upload that copy.")
    content = _repair_text_appearances(content, source, widgets)
    result = _gotenberg(content, ".pdf", "/forms/pdfengines/flatten")
    result = _drop_undrawn_widgets(result, *inspect_pdf(result))
    output, remaining = inspect_pdf(result)
    if remaining or output.get_fields() or len(output.pages) != len(source.pages):
        raise PreparationError("We could not prepare this form. Print it to PDF and upload that copy.")
    # Engines can return 200 while dropping filled text. This is a conservative
    # check, not a guarantee of visual fidelity; the filer still previews it.
    visible_text = " ".join(" ".join(page.extract_text() or "" for page in output.pages).split())
    checked_fields = set()
    for widget in widgets:
        field = widget.get("/Parent", widget).get_object()
        name = field.get("/T")
        flags = int(field.get("/Ff", 0))
        annotation_flags = int(widget.get("/F", 0))
        rect = widget.get("/Rect", [0, 0, 0, 0])
        # Hidden/no-view widgets, passwords, combs, rich/formatted fields can
        # legitimately have a different appearance from their stored /V.
        plain_visible = (
            field.get("/FT") == "/Tx"
            and not (flags & ((1 << 13) | (1 << 24) | (1 << 25)))
            and not (annotation_flags & (1 | 2 | 32))
            and not field.get("/AA")
            and not widget.get("/AA")
            and rect[0] != rect[2]
            and rect[1] != rect[3]
        )
        if plain_visible and name not in checked_fields:
            checked_fields.add(name)
            value = str(field.get("/V") or "")
            if any(" ".join(line.split()) not in visible_text for line in value.splitlines() if line.strip()):
                raise PreparationError("Some answers are missing. Print your file to PDF and upload that copy.")
    return result, True


def prepare_document(uploaded_file, jurisdiction):
    suffix = Path(uploaded_file.name).suffix.lower()
    if suffix not in settings.ALLOWED_FILE_TYPES:
        raise PreparationError("Choose a PDF or Word file.")
    uploaded_file.seek(0)
    content = uploaded_file.read(settings.MAX_FILE_SIZE + 1)
    uploaded_file.seek(0)
    if not content or len(content) > settings.MAX_FILE_SIZE:
        raise PreparationError("Choose a file that is not empty and is up to 10 MB.")
    operation = "unchanged"
    upload_has_form_fields = bool(inspect_pdf(content)[1]) if suffix == ".pdf" else None
    filename = uploaded_file.name
    if suffix in {".doc", ".docx"}:
        _word_format(content, suffix)
        content = _gotenberg(
            content,
            suffix,
            "/forms/libreoffice/convert",
            {"exportFormFields": "false", "pdfua": "true", "losslessImageCompression": "true"},
        )
        filename = f"{Path(filename).stem}.pdf"
        operation = "converted"
    if requires_flattening(jurisdiction):
        content, flattened = _flatten(content)
        if flattened:
            operation = "converted_flattened" if operation == "converted" else "flattened"
    else:
        inspect_pdf(content)
    return PreparedDocument(content, filename, operation, upload_has_form_fields)


def store_prepared_document(handler, uploaded_file, jurisdiction, role, *, keys, metadata=None):
    """Store both copies. The caller cleans ``keys`` on any later failure."""
    prepared = prepare_document(uploaded_file, jurisdiction)
    original_key = ""
    if prepared.operation != "unchanged":
        uploaded_file.seek(0)
        original = handler.upload_file(uploaded_file, file_type="original", metadata=metadata)
        if not original.get("success"):
            raise PreparationUnavailable("The original could not be saved. Try uploading again.")
        original_key = original["key"]
        keys.append(original_key)
    filing = SimpleUploadedFile(prepared.filename, prepared.content, content_type="application/pdf")
    result = handler.upload_file(filing, file_type=role, metadata=metadata)
    if not result.get("success"):
        raise PreparationUnavailable("The filing copy could not be saved. Try uploading again.")
    keys.append(result["key"])
    return {
        "name": prepared.filename[:255],
        "original_filename": uploaded_file.name[:255],
        "original_s3_key": original_key,
        "preparation": prepared.operation,
        "upload_has_form_fields": prepared.upload_has_form_fields,
        "preparation_reviewed_at": None,
        "size": len(prepared.content),
        "content_type": "application/pdf",
        "s3_key": result["key"],
        "public_url": handler.get_public_url(result["key"]),
    }


def cleanup_uploads(handler, keys):
    for key in keys:
        try:
            result = handler.delete_file(key)
            if not result.get("success"):
                logger.warning("Could not remove an uncommitted document upload")
        except Exception:
            logger.exception("Could not remove an uncommitted document upload")


def cleanup_unreferenced_uploads(keys, handler=None):
    """Remove superseded copies after commit, preserving cross-draft references."""
    from django.db.models import Q

    from efile.models import FilingDocument
    from efile.utils.s3_upload_handler import S3UploadHandler

    unused = [
        key
        for key in dict.fromkeys(keys)
        if key and not FilingDocument.objects.filter(Q(s3_key=key) | Q(original_s3_key=key)).exists()
    ]
    if not unused:
        return
    handler = handler or S3UploadHandler()
    if handler._ensure_initialized():
        cleanup_uploads(handler, unused)

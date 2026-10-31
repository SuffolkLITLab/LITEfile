"""Analyze originals while preserving a separate PDF for filing and preview."""

import base64
import io
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, NameObject, TextStringObject

from efile.models import DocumentExtraction, FilingDocument, FilingDraft
from efile.services.document_extractions import (
    _pdf_form_values,
    _source_text,
    analyze_document,
    claim_next_extraction,
    limited_pdf,
    process_document_extraction,
    queue_document_extraction,
)
from efile.services.taxonomy_classification import ClassificationRun
from efile.tests.helpers import reviewed_document
from efile.tests.pdf_helpers import docx_bytes, pdf_bytes


@pytest.fixture
def extraction_draft(db, django_user_model):
    user = django_user_model.objects.create_user(username="original-analysis", tyler_jurisdiction="illinois")
    return FilingDraft.objects.create(user=user, jurisdiction="illinois", workflow_version=2)


def rich_docx():
    """A Word package with Unicode, a table, a header, and a footnote."""
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(docx_bytes("Case No. 24-CV-123"))) as source:
        with zipfile.ZipFile(output, "w") as target:
            for name in source.namelist():
                content = source.read(name)
                if name == "word/document.xml":
                    content = content.replace(
                        b"</w:body>",
                        b'<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Table answer: 1275</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:sectPr><w:headerReference w:type="default" r:id="rHeader"/></w:sectPr></w:body>',
                    )
                target.writestr(name, content)
            target.writestr(
                "word/_rels/document.xml.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rHeader" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/><Relationship Id="rNotes" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes" Target="footnotes.xml"/></Relationships>',
            )
            namespace = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
            target.writestr(
                "word/header1.xml",
                f"<w:hdr {namespace}><w:p><w:r><w:t>Synthetic court header</w:t></w:r></w:p></w:hdr>",
            )
            target.writestr(
                "word/footnotes.xml",
                f'<w:footnotes {namespace}><w:footnote w:id="1"><w:p><w:r><w:t>Footnote evidence</w:t></w:r></w:p></w:footnote></w:footnotes>',
            )
    return output.getvalue()


@pytest.mark.django_db
def test_docx_worker_sends_original_text_to_ai_and_classifier(extraction_draft, settings):
    settings.DOCUMENT_EVIDENCE_STRATEGY = "single"
    document = reviewed_document(
        draft=extraction_draft,
        role=FilingDocument.Role.LEAD,
        name="filing.pdf",
        s3_key="filing/prepared.pdf",
        original_filename="filing.DOCX",
        original_s3_key="original/source.docx",
        preparation="converted",
    )
    job = queue_document_extraction(document)
    claimed = claim_next_extraction()
    handler = MagicMock()

    def download(key, destination):
        assert key == document.original_s3_key
        Path(destination).write_bytes(rich_docx())
        return {"success": True}

    handler.download_file.side_effect = download
    with (
        patch("efile.services.document_extractions.S3UploadHandler", return_value=handler),
        patch(
            "efile.services.document_extractions.extract_fields_from_text", return_value={"docket number": "24-CV-123"}
        ) as text_ai,
        patch("efile.services.document_extractions.extract_fields_from_file") as file_ai,
        patch("efile.services.document_extractions.get_default_model", return_value="test-model"),
        patch("efile.services.document_extractions.HierarchicalDocumentClassifier") as classifier,
    ):
        classifier.return_value.classify.return_value = ClassificationRun(selections={}, metadata={})
        process_document_extraction(job.pk, claimed.claim_token)
    text = text_ai.call_args.args[0]
    for value in ["24-CV-123", "José García", "Table answer: 1275", "Synthetic court header", "Footnote evidence"]:
        assert value in text
    assert classifier.return_value.classify.call_args.args[2] == text
    file_ai.assert_not_called()
    job.refresh_from_db()
    assert job.status == DocumentExtraction.Status.COMPLETE
    assert job.total_pages is None and job.pages_analyzed is None
    assert job.analysis_metadata["analysis_source"] == "original_docx"
    assert job.analysis_metadata["evidence_input_mode"] == "docx2python_text"
    assert job.analysis_metadata["source_conversion"] == "docx2python"


@pytest.mark.django_db
def test_pdf_worker_sends_original_fields_even_without_appearances(extraction_draft, settings):
    settings.DOCUMENT_EVIDENCE_STRATEGY = "single"
    settings.DOCUMENT_EXTRACTION_MAX_PAGES = 1
    document = reviewed_document(
        draft=extraction_draft,
        role=FilingDocument.Role.LEAD,
        name="filing.pdf",
        s3_key="filing/flattened.pdf",
        original_filename="filled.pdf",
        original_s3_key="original/filled.pdf",
        preparation="flattened",
    )
    content = pdf_bytes(pages=3, form_value="Author answer\nSecond line", missing_appearance=True)
    job = queue_document_extraction(document)
    claimed = claim_next_extraction()
    handler = MagicMock()

    def download(key, destination):
        assert key == document.original_s3_key
        Path(destination).write_bytes(content)
        return {"success": True}

    def inspect_input(**request):
        content = request["input"][1]["content"]
        encoded = content[0]["file_data"].split(",", 1)[1]
        reader = PdfReader(io.BytesIO(base64.b64decode(encoded)))
        assert len(reader.pages) == 1
        form_fields = reader.get_fields()
        assert form_fields is not None
        assert form_fields["answers"]["/V"] == "Author answer\nSecond line"
        assert '"value": "Author answer\\nSecond line"' in content[1]["text"]
        return SimpleNamespace(output_text='{"document title": "Synthetic filing"}')

    handler.download_file.side_effect = download
    openai_client = MagicMock()
    openai_client.responses.create.side_effect = inspect_input
    with (
        patch("efile.services.document_extractions.S3UploadHandler", return_value=handler),
        patch("efile.utils.llms.client", openai_client),
        patch("efile.services.document_extractions.get_default_model", return_value="test-model"),
        patch("efile.services.document_extractions.HierarchicalDocumentClassifier") as classifier,
    ):
        classifier.return_value.classify.return_value = ClassificationRun(selections={}, metadata={})
        process_document_extraction(job.pk, claimed.claim_token)
    assert "Author answer" in classifier.return_value.classify.call_args.args[2]
    job.refresh_from_db()
    assert job.total_pages == 3 and job.pages_analyzed == 1
    assert job.analysis_metadata["analysis_source"] == "original_pdf"


def test_pdf_page_limit_retains_only_selected_page_fields(tmp_path):
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf_bytes(pages=2, form_value="Included"))))
    annotation = cast(Any, writer.pages[0]["/Annots"])[0].get_object()
    other = annotation.clone(writer, force_duplicate=True)
    other[NameObject("/T")] = TextStringObject("outside_limit")
    other[NameObject("/V")] = TextStringObject("Excluded")
    writer.pages[1][NameObject("/Annots")] = ArrayObject([writer._add_object(other)])
    cast(Any, writer._root_object["/AcroForm"])["/Fields"].append(other.indirect_reference)
    path = tmp_path / "form.pdf"
    writer.write(path)
    with limited_pdf(path, 1) as (limited, _, _):
        values = _pdf_form_values(limited)
        assert "Included" in values
        assert "Excluded" not in values


def test_docx_opt_out_reads_original_locally(tmp_path):
    path = tmp_path / "filing.docx"
    path.write_bytes(rich_docx())
    with (
        patch("efile.services.document_extractions.extract_fields_from_text") as text_ai,
        patch("efile.services.document_extractions.extract_fields_from_file") as file_ai,
        patch("efile.services.document_extractions.HierarchicalDocumentClassifier") as classifier,
    ):
        result = analyze_document(path, "illinois", use_ai=False)
    assert result["guesses"]["docket number"] == "24-CV-123"
    assert result["metadata"]["source_conversion"] == "docx2python"
    text_ai.assert_not_called()
    file_ai.assert_not_called()
    classifier.assert_not_called()


def test_docx_text_is_bounded_and_records_truncation(tmp_path, settings):
    settings.DOCUMENT_EXTRACTION_MAX_TEXT_CHARS = 100
    path = tmp_path / "long.docx"
    path.write_bytes(docx_bytes("A" * 1000))
    text, pages = _source_text(path)
    assert text == "A" * 100 + "\n[Remaining document text omitted.]"
    assert pages is None
    result = analyze_document(path, "illinois", use_ai=False)
    assert result["metadata"]["source_text_truncated"] is True


@pytest.mark.django_db
def test_binary_doc_analysis_uses_converted_pdf(extraction_draft):
    document = reviewed_document(
        draft=extraction_draft,
        role=FilingDocument.Role.LEAD,
        name="filing.pdf",
        s3_key="converted/filing.pdf",
        original_filename="filing.doc",
        original_s3_key="original/filing.doc",
        preparation="converted",
    )
    job = queue_document_extraction(document)
    claimed = claim_next_extraction()
    handler = MagicMock()

    def download(key, destination):
        assert key == document.s3_key
        Path(destination).write_bytes(pdf_bytes())
        return {"success": True}

    handler.download_file.side_effect = download
    with (
        patch("efile.services.document_extractions.S3UploadHandler", return_value=handler),
        patch("efile.services.document_extractions.analyze_document", return_value={"guesses": {}}),
    ):
        process_document_extraction(job.pk, claimed.claim_token)
    job.refresh_from_db()
    assert job.status == DocumentExtraction.Status.COMPLETE
    assert job.analysis_metadata["analysis_source"] == "filing_pdf"


@pytest.mark.django_db
@pytest.mark.parametrize("before_outbound", [False, True])
def test_replaced_source_cannot_commit_old_analysis(extraction_draft, before_outbound):
    document = reviewed_document(draft=extraction_draft, role=FilingDocument.Role.LEAD, s3_key="old.pdf")
    job = queue_document_extraction(document)
    claimed = claim_next_extraction()
    handler = MagicMock()

    def download(key, destination):
        Path(destination).write_bytes(pdf_bytes())
        return {"success": True}

    def replace_source(path, jurisdiction, **kwargs):
        FilingDocument.objects.filter(pk=document.pk).update(s3_key="new.pdf")
        if before_outbound:
            kwargs["before_outbound"]()
        return {"guesses": {"document title": "Obsolete answer"}}

    handler.download_file.side_effect = download
    with (
        patch("efile.services.document_extractions.S3UploadHandler", return_value=handler),
        patch("efile.services.document_extractions.analyze_document", side_effect=replace_source),
    ):
        process_document_extraction(job.pk, claimed.claim_token)
    job.refresh_from_db()
    extraction_draft.refresh_from_db()
    assert job.status == DocumentExtraction.Status.PENDING
    assert job.attempts == 0
    assert extraction_draft.extracted_guesses == {}

import io
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
import requests
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from pypdf import PdfReader

from efile.services.document_preparation import PreparationError, prepare_document, store_prepared_document
from efile.tests.pdf_helpers import docx_bytes, pdf_bytes


@pytest.fixture(autouse=True)
def configured_conversion_service(settings):
    # Mocked HTTP tests must not depend on the developer's .env.
    settings.GOTENBERG_URL = "https://conversion.example.invalid"
    settings.GOTENBERG_USERNAME = ""
    settings.GOTENBERG_PASSWORD = ""


def upload(data, name="form.pdf"):
    return SimpleUploadedFile(name, data)


def service_response(data, status=200):
    response = MagicMock()
    response.__enter__.return_value = response
    response.status_code = status
    response.iter_content.return_value = [data]
    return response


def test_static_pdf_is_byte_identical_even_when_service_is_unavailable():
    content = pdf_bytes(pages=2)
    with override_settings(GOTENBERG_URL=""), patch("efile.services.document_preparation.requests.post") as post:
        prepared = prepare_document(upload(content), "vermont")
    assert prepared.content == content
    assert prepared.operation == "unchanged"
    post.assert_not_called()


def test_missing_multiline_appearances_are_repaired_before_gotenberg():
    content = pdf_bytes(form_value="First line\nSecond line\nThird line", missing_appearance=True)
    output = pdf_bytes("First line Second line Third line")
    with patch("efile.services.document_preparation.requests.post", return_value=service_response(output)) as post:
        prepared = prepare_document(upload(content), "vermont")
    input_bytes = post.call_args.kwargs["files"]["files"][1]
    reader = PdfReader(io.BytesIO(input_bytes))
    form = cast(Any, reader.trailer["/Root"])["/AcroForm"]
    assert form["/NeedAppearances"].value is False
    appearance = cast(Any, reader.pages[0]["/Annots"])[0].get_object()["/AP"]["/N"].get_data()
    assert b"First line" in appearance and b"Second line" in appearance and b"Third line" in appearance
    assert prepared.operation == "flattened"
    assert prepared.content == output


def test_existing_appearances_are_preserved():
    content = pdf_bytes(form_value="First line\nSecond line")
    with patch(
        "efile.services.document_preparation.requests.post",
        return_value=service_response(pdf_bytes("First line Second line")),
    ) as post:
        prepare_document(upload(content), "vermont")
    assert post.call_args.kwargs["files"]["files"][1] == content


@pytest.mark.parametrize(
    "output",
    [
        pdf_bytes("Only first line"),
        pdf_bytes("First line Second line", pages=2),
        pdf_bytes(form_value="First line\nSecond line"),
        b"bad gateway",
    ],
)
def test_invalid_or_lossy_service_output_is_never_accepted(output):
    with patch("efile.services.document_preparation.requests.post", return_value=service_response(output)):
        with pytest.raises(PreparationError):
            prepare_document(upload(pdf_bytes(form_value="First line\nSecond line")), "vermont")


def test_word_uses_tagged_lossless_pdf_and_keeps_original():
    handler = MagicMock()
    handler.upload_file.side_effect = [
        {"success": True, "key": "original.docx"},
        {"success": True, "key": "filing.pdf"},
    ]
    handler.get_public_url.return_value = "https://storage.example/filing.pdf"
    keys = []
    original = docx_bytes()
    with patch(
        "efile.services.document_preparation.requests.post", return_value=service_response(pdf_bytes(pages=2))
    ) as post:
        result = store_prepared_document(handler, upload(original, "brief.DOCX"), "vermont", "lead", keys=keys)
    assert result["original_filename"] == "brief.DOCX"
    assert result["name"] == "brief.pdf"
    assert result["s3_key"] == "filing.pdf"
    assert result["original_s3_key"] == "original.docx"
    assert result["preparation_reviewed_at"] is None
    assert result["content_type"] == "application/pdf"
    assert keys == ["original.docx", "filing.pdf"]
    assert post.call_args.args[0].endswith("/forms/libreoffice/convert")
    assert post.call_args.kwargs["data"]["pdfua"] == "true"
    assert post.call_args.kwargs["data"]["exportFormFields"] == "false"
    assert post.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize(
    "data,name",
    [
        (b"fake PDF", "fake.pdf"),
        (b"not a zip", "bad.docx"),
        (b"not a Word file", "bad.doc"),
        (b"", "empty.pdf"),
        (b"html", "web.html"),
    ],
)
def test_bad_uploads_fail_before_outbound_conversion(data, name):
    with patch("efile.services.document_preparation.requests.post") as post:
        with pytest.raises(PreparationError):
            prepare_document(upload(data, name), "vermont")
    post.assert_not_called()


@pytest.mark.parametrize("error", [requests.Timeout(), requests.ConnectionError()])
def test_service_failure_has_retry_guidance_without_upstream_details(error):
    with patch("efile.services.document_preparation.requests.post", side_effect=error):
        with pytest.raises(PreparationError, match="Try again"):
            prepare_document(upload(docx_bytes(), "brief.docx"), "vermont")


def test_jurisdiction_can_keep_interactive_pdf_byte_identical():
    content = pdf_bytes(form_value="Keep these fields")
    with (
        patch(
            "efile.services.document_preparation.config_loader.load_jurisdiction_config",
            return_value={"document_preparation": {"flatten_pdf_forms": False}},
        ),
        patch("efile.services.document_preparation.requests.post") as post,
    ):
        assert prepare_document(upload(content), "illinois").content == content
    post.assert_not_called()


def test_oversize_prepared_output_is_rejected():
    with (
        override_settings(MAX_FILE_SIZE=4096),
        patch("efile.services.document_preparation.requests.post", return_value=service_response(b"x" * 4097)),
    ):
        with pytest.raises(PreparationError, match="over 10 MB"):
            prepare_document(upload(docx_bytes(), "brief.docx"), "vermont")


def test_indirect_annotations_are_resolved_before_inspection():
    from pypdf import PdfWriter
    from pypdf.generic import NameObject

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf_bytes(form_value="Filled answer"))))
    writer.pages[0][NameObject("/Annots")] = writer._add_object(writer.pages[0]["/Annots"])
    output = io.BytesIO()
    writer.write(output)
    with patch(
        "efile.services.document_preparation.requests.post", return_value=service_response(pdf_bytes("Filled answer"))
    ):
        assert prepare_document(upload(output.getvalue()), "vermont").operation == "flattened"


@pytest.mark.parametrize("kind", ["comb", "password", "hidden", "formatted"])
def test_special_field_appearances_do_not_require_literal_value_text(kind):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, NumberObject, TextStringObject

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf_bytes(form_value="123456"))))
    widget = cast(Any, writer.pages[0]["/Annots"])[0].get_object()
    if kind == "comb":
        widget[NameObject("/Ff")] = NumberObject(1 << 24)
    elif kind == "password":
        widget[NameObject("/Ff")] = NumberObject(1 << 13)
    elif kind == "hidden":
        widget[NameObject("/F")] = NumberObject(2)
    else:
        widget[NameObject("/AA")] = DictionaryObject(
            {
                NameObject("/F"): DictionaryObject(
                    {NameObject("/S"): NameObject("/JavaScript"), NameObject("/JS"): TextStringObject("formatNumber()")}
                )
            }
        )
    output = io.BytesIO()
    writer.write(output)
    with patch(
        "efile.services.document_preparation.requests.post", return_value=service_response(pdf_bytes("12/34/56"))
    ):
        assert prepare_document(upload(output.getvalue()), "vermont").operation == "flattened"


def test_blank_signature_lines_left_by_the_flattener_are_removed():
    from pypdf import PdfWriter
    from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject, NumberObject, TextStringObject

    # The flatten engine skips widgets with no appearance, so an unsigned
    # signature line comes back as a leftover form field that draws nothing.
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf_bytes("Filled answer"))))
    signature = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Widget"),
                NameObject("/FT"): NameObject("/Sig"),
                NameObject("/T"): TextStringObject("signature"),
                NameObject("/F"): NumberObject(4),
                NameObject("/Rect"): ArrayObject([FloatObject(n) for n in [50, 100, 300, 130]]),
            }
        )
    )
    writer.pages[0][NameObject("/Annots")] = ArrayObject([signature])
    output = io.BytesIO()
    writer.write(output)
    with patch("efile.services.document_preparation.requests.post", return_value=service_response(output.getvalue())):
        prepared = prepare_document(upload(pdf_bytes(form_value="Filled answer")), "vermont")
    assert prepared.operation == "flattened"
    reader = PdfReader(io.BytesIO(prepared.content))
    assert "/Annots" not in reader.pages[0]
    assert not reader.get_fields()
    assert "Filled answer" in reader.pages[0].extract_text()

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from efile.services import evidence_ensemble as ensemble
from efile.services.document_extractions import ExtractionSuperseded, analyze_document
from efile.services.taxonomy_classification import ClassificationRun


def test_followup_is_not_majority_and_preserves_agreement():
    first = {"court name": "Court", "docket number": "A", "document title": "Motion"}
    second = {**first, "docket number": "B"}
    third = {"court name": "Wrong", "docket number": "C", "document title": "Order"}
    assert ensemble.follow_up(first, second, third) == {**first, "docket number": "C"}


def test_merge_keeps_lead_parties_and_adds_excerpts():
    assert ensemble.merge_blocks(
        [
            {"plaintiff or petitioner names": "Lead", "classification evidence": ["Motion"]},
            {"plaintiff or petitioner names": "Exhibit", "classification evidence": ["Motion", "Continue"]},
        ]
    ) == {"plaintiff or petitioner names": "Lead", "classification evidence": ["Motion", "Continue"]}


@pytest.fixture
def configured(settings):
    for group in ("READERS", "TERRA"):
        setattr(settings, f"DOCUMENT_ENSEMBLE_{group}_BASE_URL", f"https://{group.lower()}.example/openai/v1/")
        setattr(settings, f"DOCUMENT_ENSEMBLE_{group}_API_KEY", "fake-key")
    with patch.object(ensemble, "OpenAI") as factory:
        factory.side_effect = lambda **kwargs: MagicMock()
        yield factory


def response(value=None, finish="stop", refusal=None):
    return SimpleNamespace(
        model="versioned-model",
        id="response-1",
        usage=SimpleNamespace(
            prompt_tokens=100, completion_tokens=20, model_dump=lambda: {"prompt_tokens": 100, "completion_tokens": 20}
        ),
        choices=[
            SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=value or "{}", refusal=refusal))
        ],
    )


def test_requires_both_endpoints_before_outbound(settings):
    settings.DOCUMENT_ENSEMBLE_READERS_BASE_URL = ""
    with pytest.raises(ImproperlyConfigured):
        ensemble.Readers("illinois", None)


def test_separate_clients_parameters_and_usage(configured):
    readers = ensemble.Readers("vermont", None)
    for client in readers.clients.values():
        client.chat.completions.create.return_value = response('{"docket number":"26-CV-1"}')
    readers.read("deepseek", "caption")
    readers.read("terra", "caption", docket_only=True)
    cheap = readers.clients["deepseek"].chat.completions.create.call_args.kwargs
    terra = readers.clients["terra"].chat.completions.create.call_args.kwargs
    assert cheap["max_tokens"] == 8192
    assert terra["max_completion_tokens"] == 8192
    assert terra["reasoning_effort"] == "low"
    assert readers.calls[1]["endpoint_host"] == "terra.example"
    assert readers.calls[1]["estimated_usd"] == pytest.approx(0.00022)
    assert all(call.kwargs["max_retries"] == 0 for call in configured.call_args_list)
    assert "fake-key" not in str(readers.calls)


def test_permission_checked_outside_error_handler(configured):
    def stopped():
        raise ExtractionSuperseded()

    readers = ensemble.Readers("illinois", stopped)
    with pytest.raises(ExtractionSuperseded):
        readers.read("luna", "private")
    readers.clients["luna"].chat.completions.create.assert_not_called()


@pytest.mark.parametrize("finish,refusal", [("content_filter", None), ("stop", "refused")])
def test_filter_stops_followup(configured, finish, refusal):
    readers = ensemble.Readers("illinois", None)
    readers.clients["luna"].chat.completions.create.return_value = response(finish=finish, refusal=refusal)
    assert readers.read("luna", "private") == {}
    assert readers.read("terra", "private") == {}
    assert readers.filtered and readers.review
    readers.clients["terra"].chat.completions.create.assert_not_called()


def test_partial_output_retains_valid_fields(configured):
    readers = ensemble.Readers("illinois", None)
    readers.clients["luna"].chat.completions.create.return_value = response(
        '{"court name":"Court", "docket number":42}'
    )
    assert readers.read("luna", "caption") == {"court name": "Court"}
    assert readers.review and readers.calls[0]["status"] == "partial"


def test_call_cap_prevents_unmetered_followups(configured, settings):
    settings.DOCUMENT_ENSEMBLE_MAX_CALLS = 0
    readers = ensemble.Readers("illinois", None)
    assert readers.read("terra", "caption") == {}
    readers.clients["terra"].chat.completions.create.assert_not_called()
    assert readers.review


@pytest.mark.parametrize("terra_value", ["C", None])
def test_terra_changes_only_docket_and_can_abstain(terra_value):
    reader = MagicMock()
    reader.filtered = False
    reader.review = False
    reader.calls = []
    base = {"court name": "Court", "plaintiff or petitioner names": "Alex", "document title": "Motion"}

    def read(role, *args, **kwargs):
        reader.calls.append(dict(role=role, status="complete", estimated_usd=0))
        if role == "terra":
            return {"docket number": terra_value} if terra_value else {}
        if kwargs.get("docket_only"):
            return {"docket number": "B"}
        return {**base, "docket number": "A" if role == "deepseek" else "B"}

    reader.read.side_effect = read
    with (
        patch.object(ensemble, "Readers", return_value=reader),
        patch.object(
            ensemble,
            "assets",
            return_value={
                "pages": ["image"],
                "texts": ["text"],
                "tiles": ["tile"],
                "native_lead": "",
                "raster": True,
            },
        ),
    ):
        evidence, meta = ensemble.extract_ensemble("x.pdf", "illinois", "text")
    assert all(evidence[k] == v for k, v in base.items())
    assert evidence.get("docket number") == terra_value
    assert [c["role"] for c in meta["calls"]] == ["deepseek", "luna", "glm", "glm", "terra"]
    reader.close.assert_called_once()


@override_settings(DOCUMENT_EVIDENCE_STRATEGY="terra_ensemble")
def test_application_nested_contract_and_classifier_preserved():
    callback = MagicMock()
    with (
        patch("efile.services.document_extractions._source_text", return_value=("caption", 1)),
        patch("efile.services.document_extractions._form_identifier_pass", return_value=({}, "pypdf", 0, "")),
        patch("efile.services.document_extractions._source_metadata", return_value={}),
        patch.object(
            ensemble, "extract_ensemble", return_value=({"court name": "Court"}, {"policy": ensemble.POLICY})
        ) as extract,
        patch("efile.services.document_extractions.HierarchicalDocumentClassifier") as classifier,
    ):
        classifier.return_value.classify.return_value = ClassificationRun(selections={}, metadata={})
        result = analyze_document("file.pdf", "massachusetts", before_outbound=callback)
    extract.assert_called_once_with("file.pdf", "massachusetts", "caption", before_outbound=callback)
    assert result["evidence"] == {"court name": "Court"}
    assert result["guesses"]["court"] == "Court"
    assert result["metadata"]["evidence_strategy"] == "terra_ensemble"
    classifier.return_value.classify.assert_called_once_with("massachusetts", {"court name": "Court"}, "caption")


def test_local_pdf_render_retains_multiline_widgets_and_page_limit(tmp_path, settings):
    import shutil

    from efile.tests.pdf_helpers import pdf_bytes

    if not all(shutil.which(tool) for tool in ("pdftoppm", "tesseract")):
        pytest.skip("Local PDF rendering requires Poppler and Tesseract")
    settings.DOCUMENT_EXTRACTION_MAX_PAGES = 1
    path = tmp_path / "form.pdf"
    path.write_bytes(pdf_bytes(pages=2, form_value="Author answer\nSecond line", missing_appearance=True))
    result = ensemble.assets(path, "", tmp_path)
    assert len(result["pages"]) == 1
    assert len(result["tiles"]) == 2
    assert "Author answer\\nSecond line" in result["texts"][0]
    assert "Author answer" not in result["native_lead"]


def test_docx_remains_text_only(tmp_path):
    result = ensemble.assets("original.docx", "Exact original text", tmp_path)
    assert result["texts"] == ["Exact original text"]
    assert result["pages"] == result["tiles"] == []


def test_filtered_evidence_does_not_reach_classifier():
    with (
        patch("efile.services.document_extractions._source_text", return_value=("caption", 1)),
        patch("efile.services.document_extractions._form_identifier_pass", return_value=({}, "pypdf", 0, "")),
        patch("efile.services.document_extractions._source_metadata", return_value={}),
        patch.object(ensemble, "extract_ensemble", return_value=({}, {"content_filtered": True})),
        patch("efile.services.document_extractions.HierarchicalDocumentClassifier") as classifier,
    ):
        result = analyze_document("file.pdf", "illinois")
    classifier.assert_not_called()
    assert result["classification"] == {}
    assert result["metadata"]["classification_skipped"] == "content_filter"


def test_ai_opt_out_never_constructs_ensemble():
    with (
        patch("efile.services.document_extractions.keyword_document_analysis", return_value={"guesses": {}}),
        patch.object(ensemble, "Readers") as readers,
    ):
        assert analyze_document("file.pdf", "illinois", use_ai=False) == {"guesses": {}}
    readers.assert_not_called()


def test_transport_failure_has_no_hidden_retry_or_sensitive_exception(configured):
    readers = ensemble.Readers("illinois", None)
    create = readers.clients["luna"].chat.completions.create
    create.side_effect = ConnectionError("secret-key and private document text")
    assert readers.read("luna", "private document text") == {}
    create.assert_called_once()
    assert readers.review
    assert readers.calls[0]["estimated_usd"] is None
    assert readers.calls[0]["error_type"] == "ConnectionError"
    assert "private" not in str(readers.calls)
    assert "secret" not in str(readers.calls)


def test_request_hash_and_receipt_are_retained_for_incomplete_output(configured):
    readers = ensemble.Readers("illinois", None)
    readers.clients["luna"].chat.completions.create.return_value = response(finish="length")
    assert readers.read("luna", "caption") == {}
    assert readers.calls[0]["status"] == "incomplete"
    assert readers.calls[0]["estimated_usd"] is not None
    assert len(readers.calls[0]["prompt_sha256"]) == 64
    assert readers.review


def test_per_role_provider_override_does_not_move_other_readers(configured, settings):
    import copy

    settings.DOCUMENT_ENSEMBLE_MODELS = copy.deepcopy(settings.DOCUMENT_ENSEMBLE_MODELS)
    settings.DOCUMENT_ENSEMBLE_MODELS["luna"].update(
        base_url="https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1/",
        api_key="bedrock-fake-key",
        deployment="us.openai.gpt-6-luna",
    )
    readers = ensemble.Readers("illinois", None)
    calls = configured.call_args_list
    assert calls[0].kwargs["base_url"] == settings.DOCUMENT_ENSEMBLE_READERS_BASE_URL
    assert calls[1].kwargs["base_url"].startswith("https://bedrock-runtime.")
    assert calls[1].kwargs["api_key"] == "bedrock-fake-key"
    assert calls[2].kwargs["base_url"] == settings.DOCUMENT_ENSEMBLE_READERS_BASE_URL
    readers.close()


def test_provider_override_requires_its_own_credential(configured, settings):
    import copy

    settings.DOCUMENT_ENSEMBLE_MODELS = copy.deepcopy(settings.DOCUMENT_ENSEMBLE_MODELS)
    settings.DOCUMENT_ENSEMBLE_MODELS["luna"].update(base_url="https://different-provider.example/v1/", api_key="")
    with pytest.raises(ImproperlyConfigured, match="both DOCUMENT_ENSEMBLE_LUNA"):
        ensemble.Readers("illinois", None)
    configured.assert_not_called()

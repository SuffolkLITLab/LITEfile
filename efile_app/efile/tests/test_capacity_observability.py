import io
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from efile.models import DocumentExtraction, DocumentPreparationJob, FilingDraft
from efile.tests.helpers import reviewed_document
from efile.utils.llms import chat_completion


def test_usage_receipts_preserve_the_request_and_result(settings):
    settings.DOCUMENT_ENSEMBLE_MODELS = {
        "test": {"deployment": "gpt-4o", "input_usd_per_million": 1, "output_usd_per_million": 2}
    }
    client = MagicMock()
    response = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{"answer":"unchanged"}'))],
        usage=SimpleNamespace(model_dump=lambda: {"prompt_tokens": 100, "completion_tokens": 20}),
    )
    client.chat.completions.create.return_value = response
    arguments = dict(
        system_message="Synthetic system",
        user_message="Synthetic input",
        model="gpt-4o",
        json_mode=True,
        openai_client=client,
    )
    before = chat_completion(**arguments)
    request_before = client.chat.completions.create.call_args
    receipts = []
    after = chat_completion(**arguments, usage_receipts=receipts)
    assert before == after == {"answer": "unchanged"}
    assert request_before == client.chat.completions.create.call_args
    assert receipts[0]["estimated_usd"] == pytest.approx(0.00014)
    assert receipts[0]["seconds"] >= 0
    assert "Synthetic" not in json.dumps(receipts)


def test_failed_call_and_unknown_price_are_not_reported_as_free(settings):
    settings.DOCUMENT_ENSEMBLE_MODELS = {}
    client = MagicMock()
    client.chat.completions.create.side_effect = RuntimeError("Synthetic provider failure")
    receipts = []
    with pytest.raises(RuntimeError):
        chat_completion(
            system_message="Synthetic",
            user_message="Synthetic",
            model="gpt-4o",
            openai_client=client,
            usage_receipts=receipts,
        )
    assert receipts[0]["status"] == "error"
    assert receipts[0]["estimated_usd"] is None
    assert "Synthetic provider failure" not in json.dumps(receipts)
    client.chat.completions.create.side_effect = None
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="same"))],
        usage=SimpleNamespace(model_dump=lambda: {"prompt_tokens": 100, "completion_tokens": 20}),
    )
    assert (
        chat_completion(
            system_message="Synthetic",
            user_message="Synthetic",
            model="gpt-4o",
            openai_client=client,
            usage_receipts=receipts,
        )
        == "same"
    )
    assert receipts[1]["estimated_usd"] is None


@pytest.mark.django_db
def test_health_is_read_only_aggregate_and_distinguishes_missing_cost(django_user_model):
    import uuid

    user = django_user_model.objects.create_user(username="private-capacity-user", email="private@example.invalid")
    draft = FilingDraft.objects.create(user=user, jurisdiction="illinois")
    now = timezone.now()
    for index, metadata in enumerate(
        [
            {
                "analysis_mode": "ai",
                "evidence_diagnostics": {"estimated_usd": 0.02, "cost_incomplete": False},
                "classification_usage": [{"estimated_usd": 0.01}],
            },
            {"analysis_mode": "ai", "evidence_diagnostics": {"estimated_usd": 0.02, "cost_incomplete": False}},
            {"analysis_mode": "keyword"},
        ]
    ):
        document = reviewed_document(
            draft=draft, role="lead" if index == 0 else "supporting", name=f"private-{index}.pdf", sort_order=index
        )
        DocumentExtraction.objects.create(
            document=document,
            status="complete",
            started_at=now - timedelta(seconds=30),
            completed_at=now,
            analysis_metadata=metadata,
        )
    job = DocumentPreparationJob.objects.create(
        draft=draft, request_token=uuid.uuid4(), fingerprint="x", document_fingerprint="x"
    )
    DocumentPreparationJob.objects.filter(pk=job.pk).update(created_at=now - timedelta(seconds=90))
    output = io.StringIO()
    with CaptureQueriesContext(connection) as captured:
        call_command("queue_health", stdout=output)
    report = json.loads(output.getvalue())
    assert report["preparation"]["counts"] == {"pending": 1}
    assert report["preparation"]["pending_oldest_seconds"] >= 90
    assert report["extraction"]["completed_last_24h"] == 3
    assert report["extraction"]["last_attempt_seconds_p95"] == 30
    cost = report["completed_pipeline_cost_sample"]
    assert cost["known_jobs"] == 2
    assert cost["unknown_or_incomplete_jobs"] == 1
    assert cost["estimated_usd"] == pytest.approx(0.03)
    assert cost["estimated_usd_per_known_job"] == pytest.approx(0.015)
    assert "private" not in output.getvalue()
    assert all(query["sql"].lstrip().upper().startswith("SELECT") for query in captured)


def test_broken_optional_telemetry_never_retries_a_successful_model_response(settings):
    client = MagicMock()
    usage = MagicMock()
    usage.model_dump.side_effect = TypeError("Synthetic incompatible usage")
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="answer retained"))], usage=usage
    )
    receipts = []
    assert (
        chat_completion(
            system_message="Synthetic",
            user_message="Synthetic",
            model="gpt-4o",
            openai_client=client,
            usage_receipts=receipts,
        )
        == "answer retained"
    )
    client.chat.completions.create.assert_called_once()
    assert receipts[0]["estimated_usd"] is None
    assert receipts[0]["usage_unavailable"]

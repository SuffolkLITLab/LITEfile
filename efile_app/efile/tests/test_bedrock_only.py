"""Contracts for LITEfile's SDK-based Bedrock clients; all requests are mocked."""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from botocore.credentials import Credentials

from efile.services import evidence_ensemble as ensemble
from efile.utils import llms
from efile.utils.bedrock_clients import BedrockMessages, WorkloadBearer, anthropic_content


@pytest.fixture
def synthetic_identity():
    identity = MagicMock()
    identity.get.return_value = Credentials("access", "secret", "session")
    with patch("efile.utils.bedrock_clients.credentials_for", return_value=identity):
        yield identity


def test_tokens_use_atomic_role_credentials(synthetic_identity):
    seen = []

    def issue(**kwargs):
        snapshot = kwargs["aws_credentials_provider"].load()
        seen.append((snapshot.access_key, snapshot.token, kwargs["expiry"].total_seconds()))
        return "temporary-token"

    with patch("efile.utils.bedrock_clients.provide_token", side_effect=issue):
        for messages_api in (True, False):
            auth = WorkloadBearer("role", "us-east-1", messages_api=messages_api)
            path = "/anthropic/v1/messages" if messages_api else "/openai/v1/responses"
            request = httpx.Request("POST", "https://bedrock-runtime.us-east-1.amazonaws.com" + path)
            output = next(auth.sync_auth_flow(request))
            if messages_api:
                assert output.headers["x-api-key"] == "temporary-token"
                assert "authorization" not in output.headers
            else:
                assert output.headers["authorization"] == "Bearer temporary-token"
                assert "x-api-key" not in output.headers
    assert seen == [("access", "session", 300), ("access", "session", 300)]


@pytest.mark.parametrize(
    "url",
    [
        "http://bedrock-runtime.us-east-1.amazonaws.com/openai/v1/responses",
        "https://example.com/openai/v1/responses",
        "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1/files",
        "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1/responses?token=bad",
        "https://bedrock-runtime.us-west-2.amazonaws.com/openai/v1/responses",
    ],
)
def test_auth_rejects_unapproved_operations_before_credentials(synthetic_identity, url):
    auth = WorkloadBearer("role", "us-east-1")
    with pytest.raises(ValueError):
        next(auth.sync_auth_flow(httpx.Request("POST", url)))
    synthetic_identity.get.assert_not_called()


def test_image_conversion_rejects_external_or_invalid_images():
    for url in ["https://example.com/page.jpg", "data:image/jpeg;base64,!!!"]:
        with pytest.raises(ValueError):
            anthropic_content([{"type": "image_url", "image_url": {"url": url}}])


def test_official_anthropic_sdk_contract_and_refusal(synthetic_identity):
    bodies = []

    def server(request):
        assert request.url.path == "/anthropic/v1/messages"
        assert request.headers["x-api-key"] == "temporary-token"
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "message-1",
                "type": "message",
                "role": "assistant",
                "model": "haiku-version",
                "content": [{"type": "text", "text": "{}"}],
                "stop_reason": "refusal",
                "stop_sequence": None,
                "usage": {"input_tokens": 17, "output_tokens": 2},
            },
        )

    transport = httpx.MockTransport(server)
    with (
        patch.object(httpx.Client, "_transport_for_url", return_value=transport),
        patch("efile.utils.bedrock_clients.provide_token", return_value="temporary-token"),
    ):
        reader = BedrockMessages("role", "us-east-1", "us.anthropic.claude-haiku-5-5")
        try:
            response = reader.create(
                messages=[
                    {"role": "system", "content": "Return JSON"},
                    {
                        "role": "user",
                        "content": [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,aW1hZ2U="}}],
                    },
                ]
            )
        finally:
            reader.close()
    assert bodies[0]["model"] == "us.anthropic.claude-haiku-5-5"
    assert bodies[0]["messages"][0]["content"][0]["source"] == {
        "type": "base64",
        "media_type": "image/jpeg",
        "data": "aW1hZ2U=",
    }
    assert response.choices[0].finish_reason == "content_filter"
    assert response.usage.total_tokens == 19


@pytest.mark.parametrize("failed_initial, failed_terra", [(False, False), (True, False), (False, True)])
def test_bedrock_followup_retains_agreement_resolves_conflicts(settings, failed_initial, failed_terra):
    settings.DOCUMENT_EVIDENCE_STRATEGY = "bedrock_ensemble"
    reader = MagicMock()
    reader.calls = []
    reader.review = False
    reader.filtered = False

    def read(role, *args, **kwargs):
        failed = (role == "luna" and failed_initial) or (role == "terra" and failed_terra)
        reader.calls.append({"role": role, "status": "error" if failed else "complete", "estimated_usd": 0})
        reader.review |= failed
        if failed:
            return {}
        return {
            "court name": "Agreed court" if role != "terra" else "Other court",
            "docket number": {"luna": "A", "haiku": "B", "terra": "C"}[role],
        }

    reader.read.side_effect = read
    with (
        patch.object(ensemble, "Readers", return_value=reader),
        patch.object(
            ensemble,
            "assets",
            return_value={"pages": ["image"], "texts": ["text"], "tiles": [], "native_lead": "", "raster": True},
        ),
    ):
        result, metadata = ensemble.extract_ensemble("fixture.pdf", "illinois", "")
    assert [c["role"] for c in reader.calls] == ["luna", "haiku", "terra"]
    assert metadata["policy"] == "bedrock_luna_haiku_terra_v1" and metadata["escalated"]
    if failed_terra:
        assert result == {"court name": "Agreed court"} and metadata["manual_review"]
    else:
        assert result["docket number"] == "C"
        assert result["court name"] == ("Other court" if failed_initial else "Agreed court")


def test_all_generic_ai_routes_reject_external_endpoint(settings):
    settings.AI_PROVIDER = "bedrock"
    with pytest.raises(ValueError, match="disabled"):
        llms.chat_completion(
            system_message="JSON", user_message="synthetic", openai_base_url="https://external.example/v1"
        )
    assert llms.get_default_model("small") == "us.openai.gpt-6-luna"
    assert llms.get_default_model("large") == "us.openai.gpt-6.1-terra"
    assert llms.is_thinking_model("us.openai.gpt-6-luna")
    assert llms.list_available_models() == ["us.openai.gpt-6-luna", "us.openai.gpt-6.1-terra"]

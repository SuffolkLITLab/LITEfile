import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from efile.services import evidence_ensemble as ensemble
from efile.utils import llms
from efile.utils.bedrock_auth import BedrockTokenAuth, MessagesClient


@pytest.mark.parametrize(
    "protocol,path,header", [("openai", "/openai/v1", "authorization"), ("anthropic", "/anthropic", "x-api-key")]
)
def test_fetch_token_protocol_refresh_and_destination(protocol, path, header):
    base = "https://bedrock-runtime.us-east-1.amazonaws.com" + path
    auth = BedrockTokenAuth(base, protocol, "arn:role", "us-east-1")
    with patch.object(auth, "_token", side_effect=["first", "second"]) as tokens:
        for expected in ["first", "second"]:
            r = next(auth.auth_flow(httpx.Request("POST", base + "/operation")))
            assert r.headers[header] == ("Bearer " + expected if protocol == "openai" else expected)
        with pytest.raises(ValueError):
            next(auth.auth_flow(httpx.Request("POST", "https://example.com/")))
        assert tokens.call_count == 2


@pytest.mark.parametrize(
    "url",
    [
        "http://bedrock-runtime.us-east-1.amazonaws.com/anthropic",
        "https://bedrock-runtime.us-east-1.amazonaws.com.evil/anthropic",
        "https://bedrock-runtime.us-east-1.amazonaws.com/anthropic?secret=x",
    ],
)
def test_fetch_token_rejects_untrusted_endpoint(url):
    with pytest.raises(ValueError):
        BedrockTokenAuth(url, "anthropic", "arn:role", "us-east-1")


def test_messages_image_wire_and_usage():
    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        assert request.url.path == "/anthropic/v1/messages"
        assert request.headers["x-api-key"] == "synthetic"
        assert request.headers["anthropic-version"] == "2023-06-01"
        return httpx.Response(
            200,
            json={
                "id": "receipt",
                "model": "haiku-version",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": '{"docket number":"2026 CV 1234"}'}],
                "usage": {"input_tokens": 12, "output_tokens": 8},
            },
        )

    c = MessagesClient("arn:role", "us-east-1", "us.anthropic.claude-haiku-5-5")
    c.client.close()
    auth = BedrockTokenAuth(
        "https://bedrock-runtime.us-east-1.amazonaws.com/anthropic", "anthropic", "arn:role", "us-east-1"
    )
    with patch.object(auth, "_token", return_value="synthetic"):
        c.client = httpx.Client(
            auth=auth, transport=httpx.MockTransport(handle), headers={"anthropic-version": "2023-06-01"}
        )
        r = c.create(
            messages=[
                {"role": "system", "content": "Extract JSON."},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Read"},
                        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,aW1hZ2U="}},
                    ],
                },
            ]
        )
    c.close()
    assert seen[0]["messages"][0]["content"][1]["source"] == {
        "type": "base64",
        "media_type": "image/jpeg",
        "data": "aW1hZ2U=",
    }
    assert r.usage.prompt_tokens == 12 and r.usage.completion_tokens == 8
    assert r.choices[0].finish_reason == "stop"


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

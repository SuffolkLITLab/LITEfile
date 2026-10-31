"""Bedrock SDK clients using isolated short-lived workload credentials.

API references:
https://docs.aws.amazon.com/bedrock/latest/userguide/inference-messages-api.html
https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys-generate.html

Implemented for LITEfile against AWS's documented interfaces; no FETCH code.
"""

import base64
import re
import time
from dataclasses import dataclass
from datetime import timedelta

import httpx
from anthropic import Anthropic
from aws_bedrock_token_generator import provide_token
from botocore.credentials import CredentialProvider, ReadOnlyCredentials
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from openai import OpenAI
from openai.types.chat import ChatCompletion

from efile.utils.bedrock_auth import credentials_for


@dataclass(frozen=True)
class CredentialSnapshot(CredentialProvider):
    """The token generator's provider interface, bound to one atomic snapshot."""

    credentials: ReadOnlyCredentials

    def load(self):
        return self.credentials


class WorkloadBearer(httpx.Auth):
    """Authorize only the specific inference operations this application uses."""

    def __init__(self, role_arn, region, *, messages_api=False):
        if not role_arn or not re.fullmatch(r"[a-z]{2}(?:-gov)?-[a-z]+-\d+", region):
            raise ImproperlyConfigured("Bedrock requires an application role and AWS region")
        self.region = region
        self.identity = credentials_for(role_arn, region)
        self.host = f"bedrock-runtime.{region}.amazonaws.com"
        self.messages_api = messages_api
        self.operations = (
            {"/anthropic/v1/messages"} if messages_api else {"/openai/v1/chat/completions", "/openai/v1/responses"}
        )

    def sync_auth_flow(self, request):
        destination = request.url
        if (
            request.method != "POST"
            or destination.scheme != "https"
            or destination.host != self.host
            or destination.port not in (None, 443)
            or destination.path not in self.operations
            or destination.query
            or destination.fragment
            or destination.username
            or destination.password
        ):
            raise ValueError("Request is not an approved Bedrock inference operation")
        frozen = self.identity.get().get_frozen_credentials()
        bearer = provide_token(
            region=self.region,
            expiry=timedelta(seconds=300),
            aws_credentials_provider=CredentialSnapshot(frozen),
        )
        if self.messages_api:
            request.headers.pop("authorization", None)
            request.headers["x-api-key"] = bearer
        else:
            request.headers.pop("x-api-key", None)
            request.headers["authorization"] = "Bearer " + bearer
        yield request


def workload_http(*, messages_api=False):
    return httpx.Client(
        auth=WorkloadBearer(
            settings.DOCUMENT_ENSEMBLE_BEDROCK_ROLE_ARN,
            settings.DOCUMENT_ENSEMBLE_BEDROCK_REGION,
            messages_api=messages_api,
        ),
        timeout=90,
        follow_redirects=False,
    )


def bedrock_openai_client():
    region = settings.DOCUMENT_ENSEMBLE_BEDROCK_REGION
    return OpenAI(
        base_url=f"https://bedrock-runtime.{region}.amazonaws.com/openai/v1/",
        api_key="workload-identity",
        http_client=workload_http(),
        max_retries=0,
        timeout=90,
    )


def anthropic_content(content):
    """Translate locally produced text/JPEG evidence to documented image blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    blocks = []
    for item in content:
        match item.get("type"):
            case "text":
                blocks.append({"type": "text", "text": item["text"]})
            case "image_url":
                prefix, separator, encoded = item["image_url"]["url"].partition(",")
                if prefix != "data:image/jpeg;base64" or not separator:
                    raise ValueError("Only locally encoded JPEG evidence is supported")
                base64.b64decode(encoded, validate=True)
                blocks.append(
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg", "data": encoded},
                    }
                )
            case _:
                raise ValueError("Unsupported evidence block")
    return blocks


class BedrockMessages:
    """Use the official Anthropic SDK and normalize its result for our readers."""

    def __init__(self, role_arn, region, model):
        # Explicit arguments also support isolated offline contract tests.
        self.model = model
        self.sdk = Anthropic(
            base_url=f"https://bedrock-runtime.{region}.amazonaws.com/anthropic",
            api_key="workload-identity",
            max_retries=0,
            timeout=90,
            http_client=httpx.Client(
                auth=WorkloadBearer(role_arn, region, messages_api=True), timeout=90, follow_redirects=False
            ),
        )

    def close(self):
        self.sdk.close()

    def create(self, *, messages, max_tokens=8192, **unused):
        system_text = "\n".join(m["content"] for m in messages if m["role"] == "system")
        conversation = [
            {"role": m["role"], "content": anthropic_content(m["content"])} for m in messages if m["role"] != "system"
        ]
        result = self.sdk.messages.create(
            model=self.model, system=system_text, messages=conversation, max_tokens=max_tokens
        )
        endings = {"end_turn": "stop", "stop_sequence": "stop", "refusal": "content_filter"}
        text = "".join(block.text for block in result.content if block.type == "text")
        usage = result.usage
        return ChatCompletion.model_validate(
            {
                "id": result.id,
                "created": int(time.time()),
                "object": "chat.completion",
                "model": result.model,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": endings.get(result.stop_reason, "length"),
                        "message": {"role": "assistant", "content": text},
                    }
                ],
                "usage": {
                    "prompt_tokens": usage.input_tokens,
                    "completion_tokens": usage.output_tokens,
                    "total_tokens": usage.input_tokens + usage.output_tokens,
                },
            }
        )

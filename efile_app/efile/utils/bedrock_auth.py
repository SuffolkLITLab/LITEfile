"""Short-lived Bedrock credentials from this Fly machine's OIDC identity.

Does not use or modify the application's separate S3 credential chain.
"""

import threading
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from urllib.parse import urlsplit

import boto3
import httpx
from botocore import UNSIGNED
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from botocore.credentials import Credentials


class FlyCredentials:
    def __init__(self, role_arn, region):
        self.role_arn = role_arn
        self.region = region
        self.lock = threading.Lock()
        self.cached = None
        self.expires = datetime.min.replace(tzinfo=UTC)

    def get(self):
        with self.lock:
            if self.cached is not None and self.expires > datetime.now(UTC) + timedelta(minutes=5):
                return self.cached
            with httpx.Client(transport=httpx.HTTPTransport(uds="/.fly/api"), timeout=15) as client:
                response = client.post("http://localhost/v1/tokens/oidc", json={"aud": "sts.amazonaws.com"})
                response.raise_for_status()
                # Fly returns a raw JWT even when content-type is application/json.
                token = response.text.strip()
            if len(token.split(".")) != 3:
                raise RuntimeError("Fly did not return an OIDC token")
            sts = boto3.client(
                "sts",
                region_name=self.region,
                config=Config(
                    signature_version=UNSIGNED, connect_timeout=10, read_timeout=15, retries={"total_max_attempts": 1}
                ),
            )
            try:
                result = sts.assume_role_with_web_identity(
                    RoleArn=self.role_arn,
                    RoleSessionName="litefile-bedrock",
                    WebIdentityToken=token,
                    DurationSeconds=3600,
                )["Credentials"]
            finally:
                sts.close()
            self.cached = Credentials(result["AccessKeyId"], result["SecretAccessKey"], result["SessionToken"])
            self.expires = result["Expiration"]
            return self.cached


@lru_cache(maxsize=4)
def credentials_for(role_arn, region):
    return FlyCredentials(role_arn, region)


class BedrockAuth(httpx.Auth):
    requires_request_body = True

    def __init__(self, role_arn, region):
        self.provider = credentials_for(role_arn, region)
        self.region = region

    def auth_flow(self, request):
        url = urlsplit(str(request.url))
        if (
            url.scheme != "https"
            or url.hostname != f"bedrock-runtime.{self.region}.amazonaws.com"
            or url.port not in (None, 443)
            or url.username
            or url.password
            or not url.path.startswith("/openai/v1/")
            or url.query
        ):
            raise ValueError("Bedrock credentials may only sign the configured runtime endpoint")
        # Proxies may remove hop-by-hop headers such as Connection. Sign only
        # stable headers and the body digest; botocore adds date/session headers.
        headers = {"Host": url.netloc, "Content-Type": request.headers.get("content-type", "application/json")}
        signed = AWSRequest(method=request.method, url=str(request.url), data=request.content, headers=headers)
        SigV4Auth(self.provider.get(), "bedrock", self.region).add_auth(signed)
        request.headers.update(dict(signed.headers.items()))
        yield request


class BedrockTokenAuth(httpx.Auth):
    """FETCH's short-lived bearer protocol, with LITEfile's isolated Fly role.

    Mirrors open-fetch/app/providers/bedrock_auth.py at 8c387c4e.
    This worker is synchronous, so credential refresh stays off the web event loop.
    """

    def __init__(self, base_url, protocol, role_arn, region):
        url = urlsplit(base_url)
        path = "/anthropic" if protocol == "anthropic" else "/openai/v1"
        if (
            protocol not in {"openai", "anthropic"}
            or url.scheme != "https"
            or url.hostname != f"bedrock-runtime.{region}.amazonaws.com"
            or url.port not in {None, 443}
            or url.path.rstrip("/") != path
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("Bedrock authentication requires the configured AWS endpoint")
        self.provider = credentials_for(role_arn, region)
        self.region = region
        self.base_url = base_url.rstrip("/") + "/"
        self.header = "x-api-key" if protocol == "anthropic" else "Authorization"
        self.lock = threading.Lock()

    def load(self):
        return self.provider.get().get_frozen_credentials()

    def _token(self):
        from aws_bedrock_token_generator import provide_token

        with self.lock:
            return provide_token(region=self.region, aws_credentials_provider=self, expiry=timedelta(minutes=5))

    def auth_flow(self, request):
        if not str(request.url).startswith(self.base_url):
            raise ValueError("Refusing AWS credentials outside the configured endpoint")
        token = self._token()
        request.headers[self.header] = token if self.header == "x-api-key" else f"Bearer {token}"
        yield request


class MessagesClient:
    """FETCH's Anthropic Messages wire protocol, adapted for document images."""

    def __init__(self, role_arn, region, model):
        self.model = model
        base = f"https://bedrock-runtime.{region}.amazonaws.com/anthropic"
        self.url = base + "/v1/messages"
        self.client = httpx.Client(
            auth=BedrockTokenAuth(base, "anthropic", role_arn, region),
            headers={"anthropic-version": "2023-06-01"},
            timeout=90,
            follow_redirects=False,
        )

    def close(self):
        self.client.close()

    def create(self, *, messages, max_tokens=8192, **unused):
        import time

        from openai.types.chat import ChatCompletion

        system = []
        converted = []
        for message in messages:
            if message["role"] == "system":
                system.append(message["content"])
                continue
            content = message["content"]
            if isinstance(content, str):
                parts = [{"type": "text", "text": content}]
            else:
                parts = []
                for part in content:
                    if part["type"] == "text":
                        parts.append({"type": "text", "text": part["text"]})
                    else:
                        url = part["image_url"]["url"]
                        if not url.startswith("data:image/jpeg;base64,"):
                            raise ValueError("Messages evidence must be locally encoded JPEG")
                        parts.append(
                            {
                                "type": "image",
                                "source": {"type": "base64", "media_type": "image/jpeg", "data": url.split(",", 1)[1]},
                            }
                        )
            converted.append({"role": message["role"], "content": parts})
        response = self.client.post(
            self.url,
            json={"model": self.model, "system": "\n".join(system), "messages": converted, "max_tokens": max_tokens},
        )
        response.raise_for_status()
        data = response.json()
        stop = data.get("stop_reason")
        finish = (
            "stop"
            if stop in {"end_turn", "stop_sequence"}
            else ("content_filter" if stop in {"refusal", "guardrail_intervened", "content_filtered"} else "length")
        )
        content = "".join(p.get("text", "") for p in data.get("content", []) if p.get("type") == "text")
        usage = data.get("usage", {})
        return ChatCompletion.model_validate(
            {
                "id": data.get("id", response.headers.get("x-amzn-requestid", "")),
                "object": "chat.completion",
                "created": int(time.time()),
                "model": data.get("model", self.model),
                "choices": [
                    {"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": content}}
                ],
                "usage": {
                    "prompt_tokens": usage.get("input_tokens", 0),
                    "completion_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                }
                if usage
                else None,
            }
        )


def bedrock_openai_client():
    """Only Suffolk workload identity; never use the separate S3 key chain."""
    from django.conf import settings
    from django.core.exceptions import ImproperlyConfigured
    from openai import OpenAI

    region = settings.DOCUMENT_ENSEMBLE_BEDROCK_REGION
    role = settings.DOCUMENT_ENSEMBLE_BEDROCK_ROLE_ARN
    if not role:
        raise ImproperlyConfigured("Bedrock AI requires an app-scoped workload role")
    base = f"https://bedrock-runtime.{region}.amazonaws.com/openai/v1/"
    return OpenAI(
        api_key="bedrock-role-auth",
        base_url=base,
        max_retries=0,
        timeout=90,
        http_client=httpx.Client(
            auth=BedrockTokenAuth(base, "openai", role, region), follow_redirects=False, timeout=90
        ),
    )

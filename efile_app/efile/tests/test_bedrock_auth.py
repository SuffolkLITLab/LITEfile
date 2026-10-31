from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import httpx
import pytest
from botocore import UNSIGNED
from botocore.credentials import Credentials

from efile.utils.bedrock_auth import BedrockAuth, FlyCredentials


def test_signs_runtime_request_without_bearer_key():
    auth = BedrockAuth("arn:role", "us-east-1")
    with patch.object(auth.provider, "get", return_value=Credentials("fake-access", "fake-secret", "fake-session")):
        request = httpx.Request(
            "POST",
            "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1/chat/completions",
            content=b'{"model":"us.openai.gpt-6-luna"}',
            headers={"Authorization": "Bearer sigv4", "Connection": "keep-alive"},
        )
        signed = next(auth.auth_flow(request))
    assert signed.headers["Authorization"].startswith("AWS4-HMAC-SHA256")
    assert signed.headers["X-Amz-Security-Token"] == "fake-session"
    assert signed.content == request.content
    assert "connection" not in signed.headers["Authorization"].lower()


@pytest.mark.parametrize(
    "url",
    [
        "https://unrelated.example/openai/v1/chat/completions",
        "http://bedrock-runtime.us-east-1.amazonaws.com/openai/v1/chat/completions",
        "https://bedrock-runtime.us-west-2.amazonaws.com/openai/v1/chat/completions",
        "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1/chat/completions?token=x",
    ],
)
def test_does_not_sign_wrong_endpoint(url):
    auth = BedrockAuth("arn:role", "us-east-1")
    with patch.object(auth.provider, "get") as get:
        with pytest.raises(ValueError):
            next(auth.auth_flow(httpx.Request("POST", url)))
    get.assert_not_called()


def test_oidc_refresh_is_independent_of_s3_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "s3-access")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s3-secret")
    provider = FlyCredentials("arn:role", "us-east-1")
    sts = MagicMock()
    sts.assume_role_with_web_identity.return_value = {
        "Credentials": {
            "AccessKeyId": "bedrock-access",
            "SecretAccessKey": "bedrock-secret",
            "SessionToken": "bedrock-session",
            "Expiration": datetime.now(UTC) + timedelta(hours=1),
        }
    }
    with (
        patch("efile.utils.bedrock_auth.httpx.Client") as local,
        patch("efile.utils.bedrock_auth.boto3.client", return_value=sts) as factory,
    ):
        local.return_value.__enter__.return_value.post.return_value.text = "header.payload.signature"
        assert provider.get().access_key == "bedrock-access"
        assert provider.get().access_key == "bedrock-access"
        assert sts.assume_role_with_web_identity.call_count == 1
        provider.expires = datetime.now(UTC)
        provider.get()
        assert sts.assume_role_with_web_identity.call_count == 2
    assert factory.call_args.kwargs["config"].signature_version is UNSIGNED
    assert sts.assume_role_with_web_identity.call_args.kwargs["RoleArn"] == "arn:role"
    assert sts.assume_role_with_web_identity.call_args.kwargs["WebIdentityToken"] == "header.payload.signature"

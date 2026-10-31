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

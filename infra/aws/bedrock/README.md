# Bedrock roles for LITEFile on Fly

These policies stage the Terra/Luna part of the selected ensemble in AWS account
132950722695. DeepSeek/GLM retain their Azure route. No provisioned endpoint or
static AWS API key is required. The existing OIDC provider is
`oidc.fly.io/suffolk-lit-lab`, with audience `sts.amazonaws.com`.

For a fresh setup, first verify `aws sts get-caller-identity --profile suffolk`.
Create each role with its matching checked-in trust document, then attach the
shared inline inference policy:

```bash
aws iam create-role --profile suffolk --role-name litefile-staging-bedrock \
  --assume-role-policy-document file://infra/aws/bedrock/litefile-staging-trust.json \
  --max-session-duration 3600
aws iam put-role-policy --profile suffolk --role-name litefile-staging-bedrock \
  --policy-name SelectedEnsembleModelsOnly \
  --policy-document file://infra/aws/bedrock/inference-policy.json
```

Repeat for `litefile-prod-bedrock` using `litefile-prod-trust.json`. Do not widen
an existing role or reuse another app's role. Verify each model's agreement and
region access using `get-foundation-model-availability`; model listing alone is
insufficient. Activate approved pay-as-you-go model access using the operator
account, never by granting subscription privileges to these app roles.

Stage the app-specific role ARN, region, Luna/Terra auth mode `fly_oidc`, runtime
base URL, profile IDs and dated prices using `fly secrets import --stage`.
Do not set global `AWS_ROLE_ARN` or change the existing S3 keys. The application
uses the local Fly socket explicitly for this separate credential path. Existing
classifier provider settings also remain unchanged. The application integration
notes contain the full setting names and rollout details.

Use `infra/scripts/check_bedrock_from_fly.py` from the application Python
environment to validate each app's role on an existing machine. This sends only
fabricated text and a generated caption image and preserves registration and
result receipts in the selected local output directory. Do not commit remote
identity tokens, session credentials or raw AWS signature-error messages.

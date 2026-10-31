# Terra evidence ensemble

The default evidence strategy is now `terra_ensemble`. The previous single-reader
implementation remains available through `DOCUMENT_EVIDENCE_STRATEGY=single`.
The nested `analyze_document()` contract and hierarchical filing-code classifier
remain intact. This change does not submit filings or deploy the application.

## Selected models and routing

| Stage | Deployment default | Input | Selection rule |
| --- | --- | --- | --- |
| Initial image reader | `deepseek-v4.1-flash` | Full-page images | Independent evidence |
| Initial text reader | `gpt-6-luna` | Native text, PDF widget values, local OCR | Independent evidence |
| Follow-up | `glm-5.3-flash` | Full-page images | Resolve disputed evidence; preserve initially agreed values |
| Caption follow-up | `glm-5.3-flash` | Overlapping caption tiles | Docket only; after disagreement or raster input |
| Docket adjudicator | `gpt-6.1-terra` | Independent caption tiles | Disputed docket only; no prior model answers supplied |

This is follow-up adjudication, not majority voting. Literal native-caption docket
evidence takes priority. Terra cannot change court, party or classification fields.
There is no Sol fallback. A successful Terra abstention removes the docket rather
than treating a missing value as an error. Reader errors preserve available evidence
and require review. Content filtering/refusal stops subsequent model readers and
skips taxonomy classification. `use_ai=False` bypasses the entire ensemble.

## Configuration and rollout

Configure **both** OpenAI-compatible API base URLs (including `/openai/v1/` for
Azure v1) and secret keys before deploying this default:

```text
DOCUMENT_EVIDENCE_STRATEGY=terra_ensemble
DOCUMENT_ENSEMBLE_READERS_BASE_URL=https://suffolk-ai-eastus2.openai.azure.com/openai/v1/
DOCUMENT_ENSEMBLE_READERS_API_KEY=<secret>
DOCUMENT_ENSEMBLE_TERRA_BASE_URL=https://workflowdocs.cognitiveservices.azure.com/openai/v1/
DOCUMENT_ENSEMBLE_TERRA_API_KEY=<separate secret>
```

Terra is on a different Azure account from the cheap readers. Missing configuration
fails explicitly before transmitting any content; the application never discovers
keys with `az` at runtime. Existing classifier credentials/settings are still used
for filing-code classification. Do not change `OPENAI_BASE_URL` globally to Terra.

Each role supports `DOCUMENT_ENSEMBLE_<ROLE>_MODEL`, `_INPUT_RATE`, and `_OUTPUT_RATE`
overrides (`DEEPSEEK`, `LUNA`, `GLM`, `TERRA`). Rates are dollars per million tokens.
Defaults are dated October 30 estimates from the corpus study, not guaranteed
billing rates. Verify deployment availability and rates when rolling out elsewhere.
Terra uses low reasoning, Luna none; both use `max_completion_tokens=8192`.
DeepSeek/GLM use `max_tokens=8192`. SDK automatic retries are disabled so hidden
retries cannot inflate unrecorded usage. Existing job-level failure handling remains.
`DOCUMENT_ENSEMBLE_MAX_CALLS` defaults to 32 per document. Reaching it requires review.

Docker installs Poppler and Tesseract; Pillow is pinned in `uv.lock`. Local rendering
is temporary and bounded to `DOCUMENT_EXTRACTION_MAX_PAGES`. Images are at most
3,200 pixels on the longest side; initial views are reduced to 1,600. Caption tiles
cover the top 70% and bottom 55% of each of the first two pages. Input batches are
six pages or 24,000 text characters. DOCX retains its original text path and does
not pretend to have image evidence. No provider Files API is used.

Every call invokes the existing permission/claim callback outside the provider error
handler. Calls are sequential to keep Django's database/claim checks on the worker
thread. Local conversion and each request have timeouts. Raw provider exceptions,
keys and document text are not put in diagnostics. API response IDs, returned model
names, prompt/input hashes, usage, finish status, latency, estimated cost and review
flags are persisted under `metadata.evidence_diagnostics`. Missing usage is flagged
as incomplete costing, not reported as known zero cost. The estimate covers evidence
reading only; classifier cost is additional. Docket candidates are sensitive document
evidence and remain in the same private extraction record.

## Evidence and reproducibility limits

The selection reference is the companion corpus repository at commit
`cdb9da8`, `experiments/il-full-terra-2026-10-30/results.json` and its protocol,
registration, source hashes and stored output bytes. On 8,060 previously examined
Illinois documents (4,760 with known field targets), normalized field F1 was
96.62%, exact F1 95.96%, docket F1 96.71%. Comparative estimated cost was
$13.36/1,000 documents. This includes the historical workflow; it is not a measured
cost for this application adapter. Zero final Terra errors coexisted with 121
inherited baseline flags. The role taxonomy in that study is not a validated
filing-code benchmark.

This adapter is versioned `terra_followup_v1` and requests richer application
fields using LITEFile's evidence prompt. It merges leading field values and later
classification excerpts across blocks; the research implementation separately
composed old and revised paths. Model disagreement on additional application
fields can trigger extra GLM calls. Full application accuracy, clean-run cost and
latency require a separately registered evaluation. Massachusetts, Vermont and
DOCX performance are not established by the Illinois PDF experiment. No frozen
research outputs or targets are edited by this change.

## Validation

Offline tests cover agreed/disputed fields, docket-only replacement and abstention,
lead-page merging, partial JSON, separate endpoints and token parameters, usage
accounting, filters/refusals, callback propagation, AI opt-out, call caps, multiline
widgets without appearances, page limits, DOCX handling, and the nested extraction
contract with the existing classifier. Legacy integration tests explicitly select
`single` where they assert the old provider request format.

## Bedrock staging and per-model routing

Each role also accepts `DOCUMENT_ENSEMBLE_<ROLE>_BASE_URL` and `_API_KEY`. For API-key authentication, both
must be supplied together. A role override never borrows a credential from the
shared readers endpoint. With no override, DeepSeek/Luna/GLM continue using the
`READERS` pair; Terra uses the `TERRA` pair. Receipts identify the endpoint host
without persisting API keys or URL query parameters.

AWS documents an OpenAI-compatible Chat Completions endpoint at
`https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1/`. The documented US
inference IDs are `us.openai.gpt-6-luna` and `us.openai.gpt-6.1-terra`. These need
account-specific access verification before use; a model card is not proof of
access. Geographic cross-region inference stays within its documented US routing.
The Luna US rates are $0.11 input/$0.55 output per million tokens; Terra rates are
$1.10/$5.50 for short context. Override the corresponding rate settings when
switching providers; Azure benchmark costs must not be relabeled as Bedrock costs.

As checked October 31, the public DeepSeek catalog lists V3.2, V3.1 and R1;
the Z.AI catalog lists GLM 5.3 but not GLM 5.3 Flash. Do not silently substitute
these for the benchmark models. Verify the account catalog, then use explicit
mixed-provider routing or preregister and evaluate a new Bedrock-only ensemble.

The requested staging targets are Fly `litefile-prod` and its existing
`litefile-staging` environment. Use `fly secrets import --stage` with secrets piped
through stdin from memory; do not put them in command arguments, files, notebooks,
or Git. Staging does not restart machines, but staged secrets take effect on the
next deployment. Stage complete model/endpoint/credential/rate settings together,
and preserve the existing S3 and classifier credentials. A Bedrock serverless API
does not require provisioning a GPU or an always-on inference endpoint.

After the user refreshed the AWS session, the account inventory confirmed Terra
and Luna US profiles. The DeepSeek/GLM alternatives in this region were text-only
and did not match the benchmark variants. The complete hybrid configuration is
now staged (not deployed) on both Fly apps. Existing S3 and classifier settings
were not modified. CI's dependency audit was fixed by upgrading Pillow to 13.0.0
and pypdf to 6.21.4; no audit exceptions were added.

Sources:

- [AWS Chat Completions API](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-chat-completions.html)
- [AWS Terra model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-6-1-terra.html)
- [AWS Luna model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-6-luna.html)
- [AWS DeepSeek catalog](https://docs.aws.amazon.com/bedrock/latest/userguide/model-cards-deepseek.html)
- [AWS Z.AI catalog](https://docs.aws.amazon.com/bedrock/latest/userguide/model-cards-zai.html)
- [AWS API key lifecycle](https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys.html)


## Fly workload identity for Bedrock

Luna and Terra use `DOCUMENT_ENSEMBLE_<ROLE>_AUTH=fly_oidc`, their Bedrock runtime
base URL and US profile ID. API keys for these two roles must be absent. The
staged `DOCUMENT_ENSEMBLE_BEDROCK_ROLE_ARN` is app-specific; the region is
`us-east-1`. DeepSeek and GLM retain their Azure API-key route.

`efile/utils/bedrock_auth.py` fetches a machine OIDC token from the local Fly
socket, exchanges it via unsigned STS `AssumeRoleWithWebIdentity`, and signs
Bedrock requests with SigV4. Credentials last one hour and are refreshed five
minutes before expiration. Neither tokens nor temporary keys are written to disk.
The existing S3 credential chain is not used or changed. Only the configured
HTTPS Bedrock runtime host/path can be signed; redirects are disabled. Signing
excludes hop-by-hop headers, which proxies may change.

The existing organization OIDC provider is reused. New roles
`litefile-staging-bedrock` and `litefile-prod-bedrock` trust only machines belonging
to their corresponding app, with `sts.amazonaws.com` as audience. Checked-in
policies under `infra/aws/bedrock/` allow `bedrock:InvokeModel` for Luna/Terra US
profiles, their three regional foundation-model ARNs, and the default project.
No new IAM user, long-lived AWS API key, compute instance or provisioned endpoint
was created. The role references are configuration values, not bearer secrets.

`infra/scripts/check_bedrock_from_fly.py` writes a preregistration and probes two
fabricated captions from a specified existing Fly machine. It preserves inputs,
request code hashes and usage/output receipts without logging token-bearing AWS
errors. This tests endpoint/authentication compatibility, not corpus accuracy.
Run it in the application environment with `--app`, `--machine`, `--role`, and a
new `--out` directory. The first diagnostic attempt failed before inference
because a proxy changed the signed Connection header; the corrected staging
probe passed for both models.

A Fly secret staging operation alone does not restart machines. These settings
will take effect on the next deployment, which must include this OIDC adapter.
The hybrid configuration still incurs Azure usage for DeepSeek/GLM; it is not a
Bedrock-only replacement for the benchmarked ensemble.

## AWS-only deployment revision

The personal Azure resource used in research is not Suffolk-owned. Its key and
endpoint were removed from both apps' staged secrets. The historical hybrid
configuration above is withdrawn, not a production recommendation.

`AI_PROVIDER=bedrock` and `DOCUMENT_EVIDENCE_STRATEGY=bedrock_ensemble` select
`bedrock_luna_haiku_terra_v1`: independent Luna and Haiku image readers, followed
by a full Terra read on disagreement or initial reader failure. Agreed fields
are retained; Terra resolves disputed fields. If initial readers fail, a complete
Terra result is used with a review flag. Failed adjudication retains only agreed
fields and flags review. Content filtering stops later calls. This new policy
is not the historical docket-only blend or majority voting.

The provider implementation mirrors `open-fetch` revision
`8c387c4e475f6b527eae9b62b5193a8cc6e7a817`: five-minute bearer tokens from
`aws-bedrock-token-generator`, OpenAI Chat Completions and Anthropic Messages
(`/anthropic/v1/messages`). LITEfile's synchronous extraction worker uses a
synchronous HTTP client. Its explicit app-scoped Fly OIDC credential provider
is retained so existing S3 static credentials cannot override the AI role.
Tokens are never persisted. Endpoint validation and redirects-disabled behavior
prevent credential forwarding. SDK retries remain disabled for observable usage.

The generic LLM helper, classifier, and single-reader rollback also use the
same Bedrock-only client factory. No public OpenAI or Azure fallback is allowed.
Small tasks use `us.openai.gpt-6-luna`; higher tiers use
`us.openai.gpt-6.1-terra`. The independent reader is
`us.anthropic.claude-haiku-5-5`. IAM permits those profiles and their routed
foundation models, plus short-lived bearer calls. It does not grant Marketplace
subscription management or storage access.

The October 31 AWS agreement offer for Haiku reports USE1_input_tokens_standard
as “Million Input Tokens Standard” at $0.11 and USE1_output_tokens_standard as
“Million Response Tokens Standard” at $0.55. Recorded rates remain estimates;
reported usage and incomplete-cost indicators are retained. Calls stay below
Haiku's long-context threshold through page/text block limits.

`infra/scripts/check_bedrock_only.py --out /tmp/unique-canary-directory` checks
all three image endpoints, real ensemble assembly on a fabricated PDF, and the
generic classifier client without submitting a filing. Local-only calibration
can pass `--aws-profile suffolk`; Fly canaries must omit that override and use
the actual machine identity. Production rollout follows passing offline checks,
staging deployment/canary, then production deployment/canary. Existing live
filing and storage settings must remain unchanged.

The first local canary passed all three endpoint probes but caught an application
prompt ambiguity: “any visible prefix” caused a docket value to include “Case
No.”. The Bedrock adapter now explicitly distinguishes identifier prefixes from
printed field labels. The expected canary values were preserved, not relaxed;
this correction precedes deployment and is captured by the prompt/source hashes.

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

Each role also accepts `DOCUMENT_ENSEMBLE_<ROLE>_BASE_URL` and `_API_KEY`. Both
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

Both the default and `suffolk` AWS CLI sessions were expired during preparation;
Fly authentication succeeded. AWS provisioning, account access checks, model smoke
calls and secret staging remain pending `aws login --profile suffolk`. Nothing
has been changed in the live Fly environments by this preparation.

Sources:

- [AWS Chat Completions API](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-chat-completions.html)
- [AWS Terra model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-6-1-terra.html)
- [AWS Luna model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-6-luna.html)
- [AWS DeepSeek catalog](https://docs.aws.amazon.com/bedrock/latest/userguide/model-cards-deepseek.html)
- [AWS Z.AI catalog](https://docs.aws.amazon.com/bedrock/latest/userguide/model-cards-zai.html)
- [AWS API key lifecycle](https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys.html)

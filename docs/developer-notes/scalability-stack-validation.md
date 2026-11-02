# Scalability stack validation and operating implications

The stack improves failure handling on modest resources. It does not certify a
month of production traffic. Production is unchanged: pooling and asynchronous
preparation still default off, and no new Fly or Azure deployment was made.
The [external evidence bundle](https://gist.github.com/nonprofittechy/d503c4a4e3402120ca8342e404021b14)
contains raw measurements, exact cohorts, code provenance, crash checks, and
Claude's reviews. The [first-run report](issue-270-capacity.md) remains historical.

## What the experiments establish

| Experiment | Observed benefit and limit |
| --- | --- |
| Existing-size web app with bounded pooling | Earlier same-resource tests improved synchronized upload journeys from 50–54/150 to 150/150 and prepared submission journeys from 57/150 to 150/150. Local PostgreSQL, simulated court; not production certification. |
| PDF inspection reuse | Equivalent output and converter requests; about 5.6% aggregate CPU reduction for the interactive packet. It removes redundant parsing, not conversion itself. |
| Adaptive polling | At least 59% fewer checks in the deterministic two-minute waiting experiment. Each poll still refreshes the session; expiry semantics are preserved. Completion detection may be up to ten seconds later. |
| Heavy preparation, equal 2 CPU / 2 GB application budget | Synchronous web: 30/150 acknowledged; 36 complete batches persisted, including six with lost acknowledgements. Web plus preparation worker: 150/150 acknowledged in 31.0 seconds, all 150 complete, no partial batches; completion p95 21.7 minutes. |
| Journaled preparation, smaller pool | At commit `39141d7`, 150/150 simultaneous three-interactive-PDF batches acknowledged in 31.2 seconds, p95 29.7 seconds. Four connections per web process, down from eight; sampled database connections peaked at 11 rather than 19. Exact accepted IDs, drainage and restart results are in the bundle. |
| Hard process exit immediately after real storage writes | Committed ownership survived both original and filing-copy writes in LocalStack. No partial documents appeared. The filing-copy job recovered on attempt two and published one document. |
| Locked oldest extraction job/draft | Before: unrelated work was not claimed. After: the available job was claimed in about 20 ms on PostgreSQL; competing workers claimed a single job only once. |
| Actual staging Supavisor session endpoint | Two pools of four accepted 150 synthetic SQL jobs in 6.87 seconds; 150 unique completions, zero rolled-back rows or stale updates, ten expired claims recovered, and reconnection after terminating our own connection. Disposable schema removed. This tests SQL/pool primitives, not Django HTTP or production. |

The heavy comparison keeps PostgreSQL (2 CPU / 1 GB, 60 direct connections),
Gotenberg (2 CPU / 1 GB), LocalStack (2 GB) and gateway (1 GB) fixed. The web-only
case receives 2 CPU / 2 GB; the queued case divides that into a 1 CPU / 1 GB web
machine and one 1 CPU / 1 GB preparation worker. All court traffic is simulated;
no AI worker consumes the local test queue. There are 1,200 synthetic sessions,
120,000 sparse historical drafts and 360,000 historical document rows. The final
journal burst peaked at about 844 MiB web memory, so memory headroom is finite.
The host has eight CPUs / 16 GB; regression tests also ran on the host during
part of the journal run. These are container ceilings, not dedicated-cloud CPU
performance guarantees.

An exploratory 128 MB gateway worker ran out of memory; that run is retained but
excluded from the primary comparison. Earlier reports' eight- or fifteen-slot
per-process pools are also not valid production recipes. The effective
production Supavisor session pool has only **15 backend slots**, distinct from
PostgreSQL's 60-connection limit. See the [connection and rollout budget](preparation-storage-journal.md).

## What stays the same for filers

Tests cover real Gotenberg conversion, PDF.js previews, retained filled answers,
Word uploads, mobile preview, invalid-file recovery, replacement preservation,
ownership, AI opt-out and the actual submission guard. Both synchronous and
queued browser flows use the same assertions. Model receipt collection preserves
request arguments and returned answers, and telemetry errors cannot trigger a
paid retry. Court payload construction and ambiguous-submission handling are
unchanged; the stress test never sends a real court filing.

The deliberate workflow differences are saved background progress, resumable
preparation, retry/removal controls, a ten-file/50 MB batch limit, and slower
status checks. Invalid files can become a saved failed batch rather than an
immediate HTTP error. Preparation still must finish and PDFs must be reviewed
before submission. **An accepted upload is not a filed court document.** A
near-deadline burst therefore needs a preparation-wait budget as well as durable
storage. The existing two-minute manual AI fallback cannot bypass preparation
or preview requirements.

Claude reviewed the stack, then reviewed fixes twice. The initial review found
a real crash/privacy ownership window. The follow-up journal commits ownership
before PUTs, preserves leases across cancellation, renews original-upload leases
before each file, cleans unused retry copies after success, and normalizes PDF
content types. The final focused review reported no new actionable bugs. It
noted that failed best-effort cleanup after successful publication retains
tracked copies until privacy cleanup; this is not untracked storage loss.

## Measured AI cost and queue planning

Three synthetic Illinois documents exercised the current complete Bedrock
analysis pipeline on the existing staging extraction machine: 24 successful
model calls, 24.3–45.8 seconds per document, peak observed RSS about 392 MB.
Read-only taxonomy calls were real; no draft, job, storage or court mutation was
performed. Measured module hashes match deployed main. The local burst did not
run this paid pipeline 150 times.

The configured-price estimate was $0.107 for the three pipelines. Applying the
reported OpenAI cache-write premium raises that to approximately **$0.128**, or
**4.28 cents per completed pipeline**. The calculation uses the published
[GPT-6 Luna](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-6-luna.html)
and [GPT-6.1 Terra](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-6-1-terra.html)
US geographic prices. Haiku retains the configured $0.11/$0.55 per-million rates;
its three calls reported no cache reads/writes. These are estimates, not invoice
reconciliation, and exclude failed attempts, infrastructure and storage.

At that small-sample mean, 10,000 pipelines/month cost about **$428**; 4,000/day
for 22 weekdays cost about **$3,768**. Actual document mix, caching, opt-outs,
retries and provider billing can change these substantially. AI may dominate
the service budget before worker machines do. This stack does not quietly
remove ensemble calls, change models or alter inference quality to lower cost.
Such changes need a separate quality experiment. Manual AI fallback does not
cancel an already queued job and must not be counted as a token-cost saving.

The executable FIFO model assumes 11 seconds per heavy preparation batch and
cycles the three measured AI service times. Launch traffic places 25% of the day
in each of two 15-minute peaks, one hour apart (Eastern and Central), with the
rest spread over nine hours. Illinois puts 25% into one Central-time peak. Thus
a 1,000-filing busy launch day has two 250-filing peaks; a 4,000-filing Illinois
day has a 1,000-filing/15-minute peak. The 150-at-once HTTP burst is a harsher
instantaneous arrival pattern, but smaller than the statewide peak volume.

| Modeled day | Preparation / AI workers | Preparation p95 | AI p95 after preparation | Last AI completion, hours after 9 a.m. |
| --- | ---: | ---: | ---: | ---: |
| 455 filings (10,000 / 22 weekdays) | 1 / 1 | 6.4 min | 59.0 min | 10.25 |
| 1,000-filing busy launch day | 1 / 1 | 30.6 min | 202.4 min | 13.26 |
| 4,000 Illinois filings | 1 / 2 | 219.5 min | 404.7 min | 19.33 |
| 4,000 Illinois filings | 2 / 4 | 69.8 min | 48.1 min | 10.40 |

These are illustrative queue calculations, not measured tail forecasts. Do not
add the two p95 columns: they need not describe the same filer. The model omits
retries, throttling, provider contention, AI process startup, seasonal variation
and multi-day carry. One AI worker needs about 38.6 service hours for 4,000 jobs,
so one cannot sustainably handle that weekday volume. Two can drain overnight
under the sampled mean; four shorten waits at greater infrastructure cost.
Those extra workers also require a new database connection budget and quota
qualification. Do not buy dozens of permanently running workers merely to hide
a finite queue when the agreed service policy permits waiting.

## Deployment decision and remaining qualification

For the launch target, qualify the existing small web allocation with a bounded
pool and one preparation worker first. Keep the current single AI worker if its
measured daily completion capacity and allowed waits suffice; add concurrency
only when oldest-job age fails to recover after peaks or completion falls behind
arrivals. Maintain visible retry/failure counts, memory headroom, queue age,
completed-job cost coverage and Supavisor limits. PostgreSQL backend counts alone
do not reveal every pooler client constraint. The [queue health command](capacity-observability.md)
provides aggregate observations without document text or user identifiers.

Before production enablement, verify the aggregate 15-slot budget including
release commands and old/new machine overlap, migrate, start the worker, and
smoke-test synthetic uploads through the actual deployment. Rollback turns off
new queue acceptance while workers drain; removing queue-aware code early would
strand work. The full managed application, public ingress, maximum-size mixed
workload, provider quotas/tails and real court acceptance remain unqualified.
Issues #371–#375 should not be closed as if those operational checks happened.

No Azure endpoints were necessary and Azure spend was $0. Paid inference for
this experiment is estimated at $0.128, plus Claude review usage on the existing
account (about $5 API-equivalent). The $300 temporary-resource cap was ample.

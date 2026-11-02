# LITEfile capacity test and Illinois growth plan

> Follow-up: the effective production Supavisor session pool has 15 slots.
> Use the [reviewed connection budget and storage journal](preparation-storage-journal.md)
> before enabling these changes; larger local pool experiments below are historical.

Test date: November 1, 2026 (Eastern). Code: `0ff456b8247ed5459d9cab3f426955d09572cbda`.
`main` was fetched and already matched the merged remote branch. No production
application settings were changed and no filings were sent to a court.

The [reproducible harness](../../benchmarking/capacity/README.md) and
[archived raw results](https://gist.github.com/nonprofittechy/90ab33a3c611ea992db55d29b88d797a) accompany this report.

The ordinary web workload supports the 10,000/month launch target in these
short local tests, including 150 mixed active users. **The unchanged current
deployment still produces errors under synchronized bursts.**
The baseline exhausts database connections under coordinated arrivals; heavier
interactive PDFs expose preparation latency even after scaling. Current Bedrock
extraction throughput remains unmeasured. This is production-informed capacity
evidence, not a claim that the live Supabase system passed an end-to-end test.

## Reliability and cost take priority

After reviewing the results, the service owner clarified that delays are much
more acceptable than failures and that operating cost is a major constraint.
Accordingly, the original 2-second page / 5-second upload / 10-second submission
thresholds below are responsiveness reference points, **not release gates**.
Latency-only misses are degraded service, not correctness failures. Explicit
HTTP errors, failed uploads, timeouts without a recoverable outcome, lost data,
and duplicate filings remain failures. Tables retain the original thresholds
so readers can see what changed in the interpretation rather than the evidence.

This materially favors the **2 CPU / 2 GB, bounded-pool candidate** over buying
four or six web cores just to lower p95. It completed all 7,200 mixed-user actions
with no errors while requests backed up, and all 150 simultaneous prepared
submission journeys succeeded (25.82-second p95, 27.14 seconds to finish the
burst). Those are useful budget-oriented results. Its 600-user run offered
60 actions/second for two minutes and took 176.5 seconds to finish: about
40.8 actions/second overall. It can absorb that finite burst with waiting, but
cannot sustain 60 actions/second indefinitely. Queue drainage and the next
Eastern/Central spike must be considered together.

The cheapest launch option remains the existing small web/database allocation
for ordinary traffic, but synchronized errors mean it needs reliability work
before claiming burst tolerance. Evaluate a bounded pool on one CPU first;
that combination was not tested here. The tested two-core pooled candidate is
a practical next qualification target, not a required statewide baseline or a
guaranteed production configuration. Keep Supabase Micro until measured pressure
justifies a larger tier. No production settings were changed by this work.

For expensive uploads and AI, prefer durable background jobs with visible
progress, resumable status, idempotent retries, and bounded concurrency. This
lets inexpensive workers drain a burst after it ends without tying up request
workers and database connections. Some of that architecture already exists for
AI; synchronous PDF preparation still needs attention. Raising HTTP or pool
timeouts alone is not equivalent to durable queueing and was not validated here.
Avoid automatic submission retries after an ambiguous court response; reconcile
the outcome first. The existing two-minute manual AI fallback can help contain
cost if filers can successfully proceed without immediate extraction.

The worker counts later in this report assume 70% utilization and short queues;
they are **not minimum purchases**. A smaller pool can be valid if its daily
throughput exceeds arrivals and its worst queue drains within the accepted wait.
For example, at 30 seconds/job, five workers can process 600/hour, above the
444/hour statewide daytime average. A 1,000-job / 15-minute spike leaves about
850 queued, needing roughly 5.5 hours to drain while average arrivals continue.
Ten such workers leave 700 and drain in about 56 minutes. At 60 seconds/job,
those capacities halve. These are planning calculations, not measurements of
the new Bedrock pipeline; measure its duration, request count, and token cost
before paying for workers or changing provider quotas. Dollar-per-completed-filing
and oldest-job age should accompany error rate on the operating dashboard.

## Measured baseline results

The five-minute launch arrival-rate run completed 84 upload journeys and 1,596
HTTP requests with zero errors. The maximum ordinary endpoint p95 was 1.03
seconds; three-PDF upload p95 was 1.12 seconds. Sampled web CPU peaked at 27%
and memory at 381 MiB; PostgreSQL connections peaked at three.

The 150-session cohort completed 2,700 requests with zero errors, but upload-page
p95 was 4.35 seconds and draft-read p95 2.40 seconds, exceeding the proposed
2-second ordinary-request objective. The cohort staggers users across ten
seconds but follows the same sequence of actions, deliberately concentrating
navigation onto the same screen. Web CPU reached one full core and sampled
database connections reached 46. This is evidence of limited coordinated-burst
headroom even when the monthly arrival-rate target is comfortable.

The statewide arrival-rate run also passed its HTTP objectives: 334 journeys,
6,346 responses, no failures, and 1.38-second upload p95. Web CPU peaked near
96%, so this is a capacity observation with little spare CPU, not ample
production headroom.

| Baseline experiment | Completed / offered | Key p95 | Result |
| --- | ---: | ---: | --- |
| 1,000/hour upload journeys, 5 minutes | 84 / 84 | Upload 1.12 s | Pass |
| 150 coordinated active sessions, 3 minutes | 2,700 / 2,700 requests | Upload page 4.35 s | No errors; delayed completion |
| 4,000/hour upload journeys, 5 minutes | 334 / 334 | Upload 1.38 s | Pass, little CPU headroom |
| 150 simultaneous upload journeys | 51 / 150 | Upload 15.58 s | Fail: connection exhaustion |
| Recovery at 1,000/hour, 2 minutes | 34 / 34 | Upload 1.14 s | Pass without app restart |
| Three 5 MiB PDFs, 1,000/hour, 2 minutes | 34 / 34 | Upload 4.93 s | Pass, near upload latency threshold |
| Prepared submission journeys at 4,000/hour | 200 / 200 | Journey 4.22 s | Pass with simulated court |
| 150 simultaneous prepared submission journeys | 48 / 150 | Journey 16.05 s | Fail: connection exhaustion |

A submission journey includes obtaining a CSRF cookie from the upload page and
then submitting an already reviewed fixture. Most synchronized failures occurred
on that page before the final POST. This is a resume-and-submit burst, not 150
bare submission POSTs. Submission timing includes that page and the simulated
two-second court response. Percentiles include failed attempts; a low error
response time is never a pass.

The first 600-session run also exhausted the local gateway's file descriptors,
so its 8,135/10,800 failed-action count is **not** used as a production error-rate
estimate. Its raw evidence is retained with a qualification. The gateway limit
was raised from 1,024 to 16,384 before repeating the case. The 150-at-once cases
were below the old gateway descriptor limit and showed Django/PostgreSQL errors.

Ordinary mixed activity is different from a synchronized cohort. At 150 users,
the baseline completed all 900 actions in one minute, with every endpoint p95
below 0.48 seconds and no errors. In the corrected 600-user cohort repeat,
2,622 of 3,600 actions failed; PostgreSQL again reached its connection ceiling,
while the corrected gateway showed no descriptor errors.

## Measured scaling comparisons

The experimental changes are confined to the benchmark configuration; nothing
was deployed. PostgreSQL remained 2 CPU / 1 GB with 60 connections throughout.
The candidate image adds psycopg-pool 3.3.0 while retaining `CONN_MAX_AGE=0`,
using [Django's supported pooling option](https://docs.djangoproject.com/en/5.2/ref/databases/#connection-pool).

| Web configuration | Workload | Errors | Highest endpoint p95 | Interpretation |
| --- | --- | ---: | ---: | --- |
| Original 1 CPU / 1 GB, two workers, no app pool | 150 mixed users, 15 actions/s | 0 / 900 | 0.48 s | Launch concurrent-use case passes |
| 2 CPUs / 2 GB, two workers, 15 pooled connections each | 600 mixed users, 60 actions/s | 0 / 7,200 | 55.39 s | No errors; finite burst drains slowly |
| 4 CPUs / 2 GB, four workers, 8 pooled connections each | 600 mixed users, 60 actions/s | 0 / 7,200 | 1.31 s | Busy-hour web target passes |

Session timing includes lateness against the intended action schedule. The
2-core run accumulated 46.7 seconds of p99 scheduling delay; the 4-core run's
p99 delay was 4.4 milliseconds. Avoiding errors while silently serving a much
lower offered rate is not counted as success. A four-worker pool limit of eight
means **32 total web connections**, not eight for the whole deployment.

The original-size app plus a local 2-CPU / 1 GB Gotenberg service also completed
17 interactive-form upload journeys at 1,000/hour with zero errors and 4.52-second
upload p95. These use the same 4-, 7-, and 2-page Illinois forms with 45, 81, and
45 fillable fields, rather than padded blank documents.

A later two-core upload burst encountered LocalStack's accumulated temporary-file
descriptor limit. That storage-error rate is excluded from production inference.
The emulator's descriptor ceiling was raised to 65,536 in place; the final
four-core comparisons use the corrected setting. The emulator also received
2 GB RAM to keep a growing synthetic object collection from imposing an
artificial S3 limit. Neither adjustment changes the measured app/database sizes. The initial latency
proxy also reached its 0.5-CPU ceiling during the four-core synchronized burst;
that burst was repeated with one CPU for the proxy. The corrected proxy limit
is in the harness. The raw exploratory runs and qualifications are retained.

The final burst and heavy-document comparisons were:

| Candidate and workload | Successful journeys | Key p95 | Result |
| --- | ---: | ---: | --- |
| 4 CPUs, 150 simultaneous ordinary uploads, proxy with headroom | 150 / 150 | Upload 14.37 s | No errors; delayed completion |
| 4 CPUs, 150 simultaneous prepared submissions | 150 / 150 | Journey 22.08 s | No errors; delayed completion |
| 4 CPUs, interactive forms at 4,000/hour for 60 seconds | 53 / 67 | Upload 29.41 s | 14 request timeouts |
| 6 CPUs / 3 GB, six workers with four connections each, interactive forms at 4,000/hour | 66 / 67 | Upload 25.72 s | One pool timeout; latency fails |

The six-core failure was a 20-second application-pool acquisition timeout on
an extraction-status request. Reducing total pool capacity from 32 to 24 while
adding workers makes this a different capacity tradeoff, not a controlled
CPU-only comparison. Four-core interactive preparation saturated the app CPU;
the converter was not saturated. Moving expensive synchronous PDF preparation
out of request workers and shortening connection occupancy deserve priority
over simply buying more web CPUs. Revalidate pool size across the whole deployment.
The final six-core run shares an eight-core host with all dependencies, so it
cannot establish the capacity of six dedicated cloud cores.

The corrected four-core ordinary upload burst was slower than its earlier
9.26-second upload p95 run; the repeat is retained rather than selecting the
better result. These short runs show variability and neither meets the five-second
upload objective. The 600-user success is a separate browsing/editing workload,
not simultaneous proof of 600 users plus maximum uploads, conversion, and AI.

The final database audit found 550 successfully submitted synthetic fixtures,
200 unsubmitted fixtures, zero submissions stuck in flight, and zero saved-title
ownership mismatches. The 1,298 pending extraction jobs are expected because no
AI worker ran. Concurrent duplicate submission returned one success and one
conflict, with one accepted submission. These checks do not certify every possible
integrity invariant.

## Demand assumptions

[Issue #270](https://github.com/SuffolkLITLab/LITEfile/issues/270) proposes
10,000/month, 1,000/day, and 150 simultaneous users. These are engineering test
targets, not all averages and not confirmed partner SLAs. Its partner comment
reports 93,601 suspected Illinois SRL filings in September 2026 and 3,961 on
September 1; the commenter explicitly calls the SRL classification uncertain.
The same comment reports only 145 monthly filings from the softly launched
ILAO Appearance integration. Planning for all 4,000/day is therefore a growth
ceiling, not a prediction of immediate LITEfile adoption.

Use 22 weekdays/month and a nine-hour window, 9 a.m. Eastern through 5 p.m.
Central. This deliberately concentrates all traffic into weekdays and daytime.
Eastern and Central overlap by most of the day; they do not provide two
independent eight-hour capacity windows. Deadline traffic can also occur at
night. The same burst allowance must cover an evening deadline, with workers
kept available then. An Eastern deadline and a Central deadline can produce
two spikes only an hour apart; an AI backlog may still be draining when the
second arrives. The 4,000/day Illinois ceiling is Central-time demand, not
traffic divided equally between the two zones.

| Scenario | Filings/hour | Starts/second | Active 20-minute sessions |
| --- | ---: | ---: | ---: |
| 10,000/month, weekday daytime average | 50.5 | 0.0140 | 17 |
| 1,000-filing launch day, daytime average | 111.1 | 0.0309 | 37 |
| Launch busy hour: 20% of 1,000/day | 200 | 0.0556 | 67 |
| Launch deadline/recovery: 25% of day in 15 minutes | 1,000 | 0.2778 | 333 if sustained |
| Illinois 4,000/day, daytime average | 444.4 | 0.1235 | 148 |
| Illinois busy hour: 20% of day | 800 | 0.2222 | 267 |
| Illinois deadline/recovery: 25% of day in 15 minutes | 4,000 | 1.1111 | 1,333 if sustained |

Session estimates use Little's law: arrival rate × time in the system. A finite
15-minute burst does not instantly create the steady-state session number in
the last column. Abandoned attempts, repeat visits, and browsing can add load
without producing filings: a 2× allowance makes 150 active sessions reasonable
for launch and roughly 600 for an Illinois busy hour.

A plausible harsh event is an outage ending shortly before a deadline, followed
by a legal-aid reminder directing hundreds of people to resume together. The
suite therefore includes both a sustained 4,000/hour upload arrival rate and
150 simultaneous upload journeys or final-submit attempts. The latter is an
intentional synchronization test, not a claim that all ordinary users click
at the exact same instant. Large-file testing adds three 5 MiB PDFs per journey.
No finite scenario is an absolute worst case; a longer outage or many-page
scanned files can exceed it.

At 4,000/hour, even the 19 dynamic requests in this harness imply approximately
21 requests/second. A fuller 60-request visit would imply 67 requests/second,
before static assets and polling. Hence the separate 600-session, 60-action/sec
scenario. Polling every two seconds while 150 people wait adds 75 requests/sec
on its own; poll backoff and an extraction queue matter as much as monthly volume.

## Observed deployment and test fidelity

Read-only CLI/API inspection found:

- Production Fly region `ord` (Chicago), one web machine, one extraction worker,
  and one code-index worker. Each has one shared CPU and 1 GB RAM. The web
  process has two Gunicorn/Uvicorn workers. The deployed commit matches `main`.
- Supabase Micro in `us-east-2` (Ohio), PostgreSQL 17.11, 1 GB RAM, two shared
  CPU cores, 60 direct database connections, and 200 pooler client slots. The
  database reports 256 MiB shared buffers and 3,500 kB work memory.
- Production also points document conversion at `gotenberg-dev.fly.dev`: one
  started 2-shared-CPU / 1 GB machine in Newark and one stopped machine were
  observed. It is a separate service whose capacity must be included.
- Django connects to the Supabase session pooler on port 5432 with
  `CONN_MAX_AGE=0`; it also saves the session on every request.
- Ten low-rate, read-only connection/`SELECT 1` probes from the actual web
  machine measured median connection setup of 72.3 ms and SELECT transaction
  time of 22.3 ms. The local delay proxy measured 97.9 ms and 25.5 ms, respectively.
  This makes the tested network overhead somewhat conservative at low load.

The local app uses the real production settings and locked dependencies,
PostgreSQL 17, real migrations, CSRF and authenticated synthetic sessions,
120,000 historical drafts, and 360,000 historical document records. This is
roughly a year of launch volume or 30 Illinois weekdays; the historical rows
are sparse synthetic metadata, not a full year of statewide extracted content. CPU and RAM
are constrained to the observed sizes. The application, database, object
storage, and court simulator have no external network route. Only a localhost
HTTP gateway is exposed.

This is stronger evidence than SQLite tests, a health-page benchmark, or a
calculation from monthly totals. It still does not reproduce Supabase's
Supavisor queueing, TLS, competing service connections, storage IOPS, Fly shared
CPU contention, wide-area upload bandwidth, or the public ingress path. Local
60-connection PostgreSQL exhaustion is a warning, not a precise prediction of
the session pooler's behavior. Production reserves connections for Supabase
services; 200 pooler client slots do not imply 200 available database connections.
The management API returned null configured pool-size overrides, so an effective
backend pool size was not independently established. Historical data
has synthetic shape and no historical object bytes or realistic code-index
cardinality. No external court, payment, or AI capacity was stressed.

## AI extraction and the future Illinois workload

The deployed extraction command claims and processes one job at a time, waiting
for its child process before claiming another. Two web workers do not create
two extraction workers. Each uploaded lead creates a durable queued job.

Production had no extraction jobs when inspected. Staging had six AI completions
in the previous seven days: median 29.0 seconds and p95 53.1 seconds, plus one
keyword-only completion at 8.7 seconds. **Those six AI jobs used the previous
`gpt-6-luna` path, not the newly merged Bedrock ensemble.** They are useful
sensitivity inputs, not a benchmark of the current model pipeline. Current
Bedrock/OCR/model fan-out, token demand, throttling, and processing costs still
need a representative measured sample.

Use 30 and 60 seconds per lead as planning scenarios, including processing and
provider time. For a target 70% worker utilization:

`workers = ceil(filings_per_second × mean_service_seconds / 0.70)`

| Sustained demand | Workers at 30 seconds | Workers at 60 seconds |
| --- | ---: | ---: |
| Launch weekday average, 50.5/hour | 1 | 2 |
| Launch busy hour, 200/hour | 3 | 5 |
| Illinois daytime average, 444.4/hour | 6 | 11 |
| Illinois busy hour, 800/hour | 10 | 20 |
| Illinois recovery burst rate, 4,000/hour | 48 | 96 |

The last row is the concurrency needed to keep up while the burst lasts, not a
recommendation to run that many machines continuously. A queue and deliberate
latency budget can trade peak worker count for waiting time. For example, ten
30-second workers can complete 300 jobs during a 15-minute arrival burst of
1,000 jobs. About 700 remain; they take another 35 minutes to drain with **no
new arrivals**. With continuing normal Illinois arrivals of 444/hour, the net
drain rate is only about 756/hour, so draining takes about 56 minutes. At
60 seconds per job, the same ten workers leave about 850 pending and ongoing
normal arrivals leave only 156/hour of spare capacity: more than five hours
to recover. These are deterministic fluid approximations, not measured queue
latencies or guarantees; they also assume worker claims and model quotas scale
without additional contention. Test that before provisioning dozens of workers.

A single worker at 30 seconds has theoretical capacity 120/hour; at 60 seconds,
60/hour. At the 4,000/day Illinois average it is overloaded under either
assumption. Even launch's 200/hour busy hour exceeds both. Fast web responses
therefore cannot establish timely AI assistance on the current one-worker
configuration. The app already lets a filer continue manually after two minutes;
an AI backlog does not necessarily prevent filing, but it changes the user
experience and the amount of help the filer receives.

## Storage and bandwidth are separate budgets

The three representative flattened PDFs total 1,421,151 bytes and 13 pages.
At that mix, 10,000/month adds about 14.2 GB of filing-copy bytes/month;
4,000 × 22 weekdays adds about 125 GB/month. Originals, corrected copies,
previews, backups, and retention multiply those totals. The interactive
versions total 3.6 MB before conversion. A three-by-5-MiB filing mix instead
adds about 1.38 TB/month at statewide volume. Historical database rows in this
test do not simulate that stored-object footprint.

At 4,000/hour, uploading and then viewing each 1.42 MB packet once moves about
11.4 GB/hour, around 25 Mbps across both directions, before originals, retries,
static assets, and court downloads. That is ordinary application traffic, but
it is enough to make storage/egress and preview streaming worth measuring.

## What to change as traffic grows

For launch, keep the inexpensive Micro database until measured pool pressure,
CPU, or I/O warrants an upgrade. Add monitoring of oldest pending extraction,
queue depth, extraction duration and failure rate, PostgreSQL connections,
web p95/p99, and upload/submission errors. Use age of the oldest queued job and
arrival rate to drive extraction capacity; autoscaling web machines alone
will not clear that queue. Validate the existing two-minute manual fallback with filers, and define the
acceptable AI waiting-time objective during a backlog.

For statewide Illinois, plan separately for web replicas, extraction processes,
and conversion/OCR resources. Load-test with representative scanned and
interactive PDFs, document sizes and page counts, live-sized code indexes,
multiple jurisdictions, and realistic session durations. Keep each worker's
memory and lease settings intact when increasing concurrency. Match model
request/token quotas to measured calls per filing; a multi-model ensemble is
not one provider request per filing. Use bounded queues and backpressure so
provider throttling does not turn into unbounded retries.

Budget database connections across all web replicas, extraction workers,
indexing, and Supabase services before scaling. Per-request connections plus
session saves make connection establishment and database round trips material.
The local pooled candidates demonstrate bounded application connections;
validate their interaction with the actual Supabase pooler before deploying,
and assess session write frequency. Do not simply enable
persistent Django connections for this ASGI deployment or raise PostgreSQL's
connection limit without memory accounting. Count both an extraction supervisor
and its active child when budgeting worker connections. The observed idle
production snapshot had eight client backends, including the inspection query;
that is a snapshot, not a guaranteed permanent reserve.

For statewide growth, 32 web connections plus roughly 20 parent/child connections
for ten extraction workers and Supabase/indexing headroom nearly exhaust a
60-connection Micro instance. The account's observed catalog lists Small at
2 GB / 90 database connections, approximately $15/month compute versus Micro's
approximately $10/month; Medium lists 4 GB / 120 connections at approximately
$60/month. These exclude plan fees, storage, egress, and AI usage. A Small or
Medium instance is a connection/memory budgeting option, not an upgrade applied
by this test. The [captured catalog in the evidence bundle](https://gist.github.com/nonprofittechy/90ab33a3c611ea992db55d29b88d797a)
records the prices and limits observed during this work. Supabase documents the distinction
between database connections and pooler clients in its
[pooling guide](https://supabase.com/docs/guides/database/connecting-to-postgres/pooling-and-limits).

## Cost and abuse prevention

Incremental Azure/Supabase resources created: **none**. Incremental cloud test
spend: **$0**. The $300 cap was not a constraint for these local experiments.
The disposable local containers, networks, database, and generated authentication
fixtures were removed after the final audit. Raw JSON results are archived externally; the harness and findings
remain in the repository. Ruff, Compose configuration validation, and JSON
parsing checks passed.
Only small read-only metadata/timing queries reached existing cloud resources.
The HTTP test traffic stayed on the machine; court traffic, paid AI traffic,
and user emails were isolated.

If a later managed-environment qualification run is needed, use an isolated
clone with synthetic data, one bounded application-level generator, explicit
rate ceilings, a short fixed duration, and resource cleanup. Azure offers
[Azure Load Testing](https://learn.microsoft.com/en-us/azure/load-testing/quickstart-create-and-run-load-test)
for that purpose. A packet flood or a DDoS-mitigation test is a different
activity: Microsoft's [testing rules](https://learn.microsoft.com/en-us/azure/security/fundamentals/pen-testing)
prohibit ordinary DoS tests and direct DDoS simulations to approved partners.
There is no reason to use a packet-level attack to answer this capacity question.

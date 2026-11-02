# Capacity test for issue #270

This is an isolated HTTP test of the actual LITEfile application and PostgreSQL,
with synthetic data and local replacements for S3 and the court API. It is not
permission to load-test a court, payment provider, or production database.

See [the capacity report](../../docs/developer-notes/issue-270-capacity.md) for
assumptions, measured results, and the limits of the production inference.

Generated run output belongs in the ignored `results/` directory, which the
harness creates when needed. The repository keeps reusable scripts and findings,
not telemetry snapshots. The [archived evidence bundle](https://gist.github.com/nonprofittechy/90ab33a3c611ea992db55d29b88d797a) contains the
102 original result files for the first run and follow-up experiments. Download
the gist ZIP or clone it for the full JSON; the web preview may be truncated.

## Reproduce the test

Run from the repository root. Docker needs about 5 GB of available memory, plus
space for the application image and generated database. The web container gets
1 CPU / 1 GB; PostgreSQL gets 2 CPUs / 1 GB and 60 connections. These are resource
ceilings, not a claim that local dedicated CPU time equals Fly or Supabase shared
CPU scheduling.

```bash
# Only committed source enters the application image.
git archive HEAD > /tmp/litefile-capacity-source.tar
docker build -t litefile-capacity:270 - < /tmp/litefile-capacity-source.tar

docker compose -f benchmarking/capacity/compose.yml up -d
# Wait for the web process to finish starting before continuing.
curl --fail http://127.0.0.1:8270/about/
docker compose -f benchmarking/capacity/compose.yml exec -T web \
  .venv/bin/python manage.py migrate --noinput
docker compose -f benchmarking/capacity/compose.yml exec -T web \
  .venv/bin/python /bench/capacity/seed.py
docker compose -f benchmarking/capacity/compose.yml exec -T web \
  .venv/bin/python /bench/capacity/seed_submissions.py
uv run --with aiohttp==3.14.5 python benchmarking/capacity/load.py \
  --burst 1 --output /tmp/capacity-smoke.json
python benchmarking/capacity/run_suite.py

# Remove only this disposable test stack and its database volume.
docker compose -f benchmarking/capacity/compose.yml down --volumes --remove-orphans
```

The suite must run sequentially. The synthetic sessions are reused across
scenarios; overlapping runs would simulate multiple tabs changing the same
session rather than independent users. `seed.py` refuses an already seeded
database. Recreate this stack before repeating the whole suite. The submission
scenarios consume distinct prepared drafts; rerunning them without reseeding
correctly produces duplicate-submission rejections.

The generator requires Python and `uv`. Its aiohttp version is pinned in the
suite. Application dependencies come from the repository lockfile. Existing
images with a broken Docker build cache may require `docker build --no-cache`.

## Focused follow-up experiments

After the baseline suite finishes, run:

```bash
python benchmarking/capacity/followups.py
python benchmarking/capacity/scale4.py
python benchmarking/capacity/scale6.py
python benchmarking/capacity/summarize.py
```

This repeats the coordinated 600-session case with the corrected gateway,
checks mixed 150-user activity, adds real Gotenberg conversion of three
interactive Illinois PDFs, and tests a separate candidate with 2 CPUs / 2 GB
for the web process and a bounded psycopg pool of 15 connections per worker
(30 across the two workers). `Dockerfile.pool` adds psycopg-pool 3.3.0 only to
the experimental image; it does not change production dependencies or settings.
The existing S3 simulator is raised to a 2 GB ceiling in place to avoid an
emulator memory constraint as synthetic objects accumulate. The database
remains unchanged. See `compose-scaled.yml` and `compose-conversion.yml`.

The four- and six-core experiments need an eight-core host with roughly 16 GB
RAM. They test four workers with eight pooled connections each, then six workers
with four each. They retain the same small database. The final committed harness
includes the corrected proxy CPU and storage file-descriptor limits, so it should
not recreate the known emulator failures preserved in exploratory results.

The final checks send a duplicate concurrent submission and audit persisted
database outcomes. These scripts consume more distinct prepared drafts; use a
fresh stack to repeat them. The final report distinguishes baseline observations
from this candidate rather than presenting scaled results as current production.

The initial 600-user exploratory result is retained with its gateway-limit
qualification; it must not be used as a production error-rate estimate. The
committed gateway configuration contains the correction.

## What is exercised

- Real Gunicorn/Uvicorn, production Django settings, middleware, CSRF, database
  sessions, authentication, ownership checks, ORM queries, and migrations.
- 1,200 synthetic authenticated users; 120,000 historical submitted drafts and
  360,000 document metadata records. Historical document bytes are not stored.
- Nineteen requests per upload journey: page rendering, new draft, multipart
  upload of three PDFs, six extraction polls, three saves, three draft reads,
  document metadata, and three private PDF downloads. The default uses the
  repository's flattened, synthetically filled Illinois forms IL-01/02/03.
- A separate active-session workload makes one action every ten seconds per
  user, with staggered starts. Its default cohort follows the same screen
  sequence; `--mixed` distributes users across different action phases. It includes the draft list page. Timing includes
  lateness relative to the planned action time, so overload cannot hide behind
  a slower request loop.
- The submission workload starts from prepared, reviewed, fee-quoted synthetic
  drafts. It first obtains a CSRF cookie from the upload page, then runs the real
  validation, disclaimer check, payment-account lookup,
  atomic claim, payload preparation, external HTTP call, and finalization. The
  local court simulator returns after two seconds; it is not a measurement of
  Tyler. User registration, actual payment authorization, and real acceptance
  by a court are not tested.
- Real private object upload/download through boto3 and LocalStack. AI jobs are
  enqueued, but no AI worker runs during the HTTP suite. Pending-job growth is
  deliberately measured and must not be interpreted as completed filings.

The 15 MB scenario uploads three 5 MiB valid PDFs with padding. That scenario
measures payload handling, not the CPU cost of complex scanned documents. Word
conversion, browser JavaScript, live taxonomy/code
search, external interview handoffs, and cold-start availability are separate
coverage gaps.

## Isolation and limits

The application, database, S3 simulator, and court simulator share an internal
Docker network with no external route. A small Nginx gateway publishes only
`127.0.0.1:8270`. The scripts have a fixed localhost target. No production
credentials enter the stack; generated session files are ignored by Git.

`settings.py` imports production settings, then changes hosts, local HTTP cookie
security, database TLS, and external service endpoints for this isolated stack.
The real production connection lifetime (`CONN_MAX_AGE=0`) is preserved.
`db_proxy.py` adds 40 ms per connection and 5 ms per forwarded chunk/direction.
Measured connection and SELECT timings are recorded alongside the production
read-only probe. This approximates latency; it does not emulate TLS, Supavisor,
provider throttling, network loss, shared-host contention, or storage IOPS.

`monitor.py` records Docker CPU/memory and PostgreSQL connections, locks,
deadlocks, temporary bytes, database size, and queued extractions about every
five seconds. Brief peaks between samples may be missed. The generator records
all endpoint latencies and explicit HTTP/business failures, with no retries.
A 30-second per-request timeout bounds upload/session load; submission requests
have 45 seconds. The suite bounds each scenario subprocess to 12 minutes.

Use the JSON's explicit error and success counts to evaluate a capacity test;
a successful harness process exit alone does not mean the scenario passed.
Results contain only synthetic values and aggregate production metadata.

## Pass criteria

The original responsiveness reference points for these experiments are below.
The owner subsequently prioritized successful completion and cost over latency.
Treat latency-only misses as degraded service; errors and incorrect outcomes
remain failures. A finite backlog that drains is different from a sustained
arrival rate exceeding capacity. See the report's reliability and cost section.

The recorded reference points are:

- No unexpected HTTP/business errors or missing/incorrect uploaded files.
- Ordinary page/API p95 below 2 seconds; three-document upload p95 below
  5 seconds; completed simulated submission p95 below 10 seconds.
- No OOM termination, database deadlocks, or accumulating request scheduling lag.
- Recovery after overload without manual service restart.

These are proposed engineering acceptance criteria, not a partner-approved SLA.
AI completion has a separate queue/latency requirement and cannot pass based on
HTTP response time alone.

## Same-resource pooling experiment

On a fresh stack built from the pooling PR, build the small experimental image
and apply `compose-pool-micro.yml`. It retains one CPU, 1 GB RAM, and two web
workers. It mounts the checkout's Python application code; record the checkout
revision and do not edit application files while a comparison runs.

```bash
docker build -t litefile-capacity:pool-micro \
  -f benchmarking/capacity/Dockerfile.micro benchmarking/capacity
docker compose -f benchmarking/capacity/compose.yml \
  -f benchmarking/capacity/compose-pool-micro.yml up -d
# Migrate and seed as above. Setup can bypass simulated network delay:
docker compose -f benchmarking/capacity/compose.yml exec -T \
  -e DATABASE_URL=postgres://capacity:local-capacity-only@db:5432/capacity \
  web .venv/bin/python /bench/capacity/seed_submissions.py
python benchmarking/capacity/pool_micro.py
```

Run the submission seeder only once after the user/history seeder. Timed web
requests still use the latency proxy. The comparison toggles only
`DB_POOL_MAX_SIZE` between zero and eight per worker, runs mixed activity and
synchronized upload/submission journeys, and repeats upload bursts after both
variants have warmed the database. It uses the actual application configuration
helper and the lockfile's psycopg-pool version. Do not run other load generators
against the shared synthetic sessions concurrently.

## Durable preparation follow-up

The [stack validation report](../../docs/developer-notes/scalability-stack-validation.md)
compares equal total application resources, reports accepted versus completed
uploads, and documents the actual 15-slot Supavisor constraint. Use
`compose-queue.yml` with `compose.yml` and `compose-conversion.yml` for the
opt-in queue candidate. Its web pool defaults to four per process. Run
`queue_burst.py` with an unused session offset, then `queue_audit.py` with that
burst's `--accepted-results` file and `--expected` count. The audit requires exact
accepted job IDs; it cannot count prior successful cohorts as the current run.

`queue_model.py` accepts measured `--bedrock-results`, a conservative
`--preparation-seconds` value, and optional `--pipeline-cost-usd` for externally
repriced estimates. Its output is a scenario calculation, not a live provider
load test. Use local synthetic dependencies for bursts and keep any managed
qualification explicitly bounded. Current production's connection budget must
include every worker, release command, and overlapping deployment machine.

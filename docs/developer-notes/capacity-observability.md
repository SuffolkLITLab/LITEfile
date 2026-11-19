# Queue and cost observability

Run `uv run python manage.py queue_health` in the application environment for a
read-only JSON snapshot. It contains queue counts, oldest pending age, expired
claims, completions in the last day, bounded timing samples, retry counts, and
visible PostgreSQL connection counts. It contains no user identifiers, filenames,
document text, prompts or storage keys. Unknown connection states remain labeled
unknown. Database backends are not a substitute for monitoring Supavisor client
limits and rejection metrics; this command itself needs one connection.

Classification now retains optional usage receipts alongside the existing
evidence receipts. Tests compare both model request arguments and returned
answers with and without receipt collection. Optional telemetry failures cannot
turn a successful paid response into a retry. Unknown prices and missing or
malformed historical metadata remain unknown, rather than being counted as free.

Cost summaries are configured-price estimates for a bounded sample of completed
jobs. They exclude previous failed attempts, failed jobs, infrastructure and
storage, and do not adjust for every provider cache or long-context price tier.
They are not invoice totals. Keyword-only jobs are known zero-model-cost jobs.
Keep the unknown count visible when using these numbers for budgeting.

The reusable capacity scripts add synchronized heavy-upload arrivals, explicit
queue-drain cohort audits, and an illustrative FIFO model using measured AI
service times. The queue audit uses exactly the accepted job IDs, so an older run
cannot accidentally satisfy a newer run's completion requirement. Generated
receipts, telemetry, model outputs and screenshots stay outside Git.

Validation includes unchanged model requests/results, unknown pricing, failed
calls, non-fatal telemetry, read-only aggregate queries, and sensitive-data
exclusion. The PostgreSQL safety suite additionally checks bounded provider
throttling retries and confirms that pending, processing and failed replacements
all block the actual submission endpoint before any court request.

## Staging document workers that sleep when idle

The normal Fly configuration keeps both document workers running. The optional
`fly.staging-autoscale.toml` profile adds a small coordinator to staging. Its
durable mode in PostgreSQL defaults to warm. In autoscale mode a worker exits
between jobs after five idle minutes. The coordinator reads committed work and
short upload wake reservations, then starts existing Machines through Fly. It
never chooses a running worker for scale-down. It can try another configured
Machine or restart an unresponsive one once there are no live claims to interrupt.

Before a new upload is stored, both supervisors must report after its wake
request. A failure returns HTTP 503 with the selected files still in the form.
Retries of an already accepted upload return the stored job. The web app
independently checks the age of work and returns a delayed message on affected
drafts. The top-level Fly probe polls `/health/document-workers/` independently
of the coordinator and does not route web traffic away from the app.

### Enable a staging pilot

1. Keep the worker configuration warm while preparing the pilot. Verify that
   there are two independently startable Machines in each document process group.
   Record their IDs in preference order in the staging
   `DOCUMENT_WORKER_MACHINES` JSON secret, keyed by `preparation_worker` and
   `extraction_worker`. Fly-managed hardware standbys have their own failover
   behavior and should stay out of this list.
2. Set `DOCUMENT_WORKER_FLY_TOKEN` as a short-lived, app-scoped deploy token for
   `litefile-staging`. The app-wide Fly secret is available to its web Machine,
   so the token must never have access to production or the organization.
   The coordinator is the only process that uses it. Renew it before it expires.
3. Deploy the pilot configuration to staging with `--ha=false` so it has one
   coordinator. With the database mode still warm, verify health with
   `fly checks list -a litefile-staging` and check that both workers are started.
   Verify that the independent probe reports an unhealthy response when presented
   with stale coordinator state in the test environment. Connect the failing
   probe to an operator alert and verify delivery before enabling idle exits.
4. On the staging coordinator, run
   `uv run python manage.py coordinate_document_workers --mode autoscale`.
   After both workers have slept, upload a synthetic document and time it through
   preparation, analysis and review. Compare this with the 79-second warm
   measurement and the two-minute confirmation wait. Use three cold runs,
   each with both groups initially stopped. Require both worker heartbeats
   within 20 seconds and the upload marked Ready within 120 seconds on each
   run. Verify primary start
   failure, in-flight job recovery, and upload admission failure using an
   isolated test or a mock.
5. Enable the `LITEFILE_STAGING_WORKER_AUTOSCALING` repository variable for
   subsequent staging deploys only after the cold-start and outage validation.
   The deployment checks the live coordinator, the queue-health response and
   both Machine pools. Production continues to use its warm worker configuration.

### Return to warm workers

On the staging coordinator, run
`uv run python manage.py coordinate_document_workers --mode warm`. Confirm that
one worker in each group has started. Turn off
`LITEFILE_STAGING_WORKER_AUTOSCALING`, then deploy the normal staging
`fly.toml` or the previous warm release. Verify the live worker check after
the deploy. If the coordinator itself is unavailable, first start one worker
in each group manually and verify their processes before rolling back.

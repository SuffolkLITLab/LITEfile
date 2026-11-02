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

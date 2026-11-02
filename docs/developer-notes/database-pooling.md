# Bounded database pooling

Related work: #371, under #270. This change adds an opt-in pool; it does not
change production sizing, session expiry, polling, filing validation, document
contents, or court submission behavior.

## Configuration and connection budget

Production and staging accept `DB_POOL_MAX_SIZE` (default `0`, disabled) and
`DB_POOL_TIMEOUT` (default `30` seconds). A positive size enables Django's
supported psycopg pool with zero eagerly opened connections. `CONN_MAX_AGE`
remains zero for ASGI: ending a request returns the connection to the pool.
Health checks prevent reuse of a connection known to be broken. TLS options
remain intact. SQLite image-build settings do not receive pool options.

The maximum applies **per process**. Two web workers with a size of eight can
hold 16 database connections. Count every replica, extraction supervisor and
child, code-index worker, management command, and Supabase's own services in
the overall budget. A zero minimum reduces idle startup allocation; it does
not remove the need to budget peak usage. Do not use the pool's default sizes
without an explicit deployment budget.

Keep the existing Supabase session-pooler endpoint for the initial rollout.
This PR does not switch to transaction pooling or change prepared statements,
server-side cursors, transaction boundaries, or retries. A pool is a concurrency
limit, not a durable queue. If demand exceeds capacity for long enough, its
finite acquisition timeout can still fail requests. Never automatically retry
an ambiguous court submission as a connection-error recovery mechanism.

## Qualification and rollback

Compare disabled and enabled pooling at the same web CPU/memory/process count
before attributing benefits to pooling. Exercise mixed users, synchronized
uploads, prepared submissions, duplicate clicks, and recovery. Record pool
wait, errors, elapsed time to drain, and database connection counts separately
from response percentiles. Longer completion can be acceptable, but lost work
or a backlog that never drains is not.

Before enabling on production, qualify the chosen budget with the actual
Supabase pooler in an isolated deployment, including a database reconnect and
background-worker workloads. The local experiment is evidence, not that cloud
qualification. Enable in staging first; monitor errors and queue age. Roll back
by setting `DB_POOL_MAX_SIZE=0` and restarting affected processes. This restores
per-request connection establishment without a schema or data migration.

[Django documents the ASGI and pooling requirements](https://docs.djangoproject.com/en/5.2/ref/databases/#connection-pool).

## Behavioral regression coverage

The isolated PostgreSQL configuration in
`benchmarking/capacity/regression_settings.py` exercises the real pool with
fixed loopback services. With `DB_POOL_MAX_SIZE=8`, 92 targeted tests passed,
including real Chromium navigation/payment/waiver/document previews, real local
Gotenberg conversion, new filing flows across Illinois/Massachusetts/Vermont,
durable drafts, security boundaries, submission tracking, rollback across
request boundaries, and recovery after terminating an idle pooled connection.
Court/payment responses and object storage are test doubles in the browser
suite; these are not live court or provider acceptance tests.

An earlier SQLite browser run passed six scenarios and failed one payment
scenario with `database table is locked`. The same scenario passed on
PostgreSQL. We retain that limitation rather than reporting the SQLite run as
successful. The ordinary baseline Python suite passed 2,389 tests with 13 opt-in
skips, and JavaScript unit tests passed.

## Same-resource micro-experiment

The comparison uses one app CPU, 1 GB RAM, two web workers, the same local
PostgreSQL limits, delayed network proxy, and synthetic history on both sides.
The candidate uses eight pooled connections per worker (16 web connections),
30-second acquisition timeout, and health checks. No CPU or memory upgrade is
included. Both variants use the same image/library and application code; the
pool-size setting is the switch.

| Workload | Pool disabled | Pool enabled |
| --- | --- | --- |
| 150 mixed users, one minute | 900/900 actions | 900/900 actions |
| 150 simultaneous ordinary upload journeys | 50/150 completed | 150/150 completed |
| 150 simultaneous prepared submission journeys | 57/150 completed | 150/150 completed |

The pooled upload journeys took 135.91 seconds to drain, with upload p95
25.61 seconds. Prepared submission p95 was 41.16 seconds, with the whole burst
finished in 43.80 seconds. That is within the existing browser submission
budget (330 seconds), which this change does not alter. The test court responds
after two seconds; a real court can take longer. The ordinary upload browser
uses fetch without the generic API utility's 30-second timer.

This supports evaluating pooling on the existing small machine before buying
more CPU. It does not establish unlimited burst tolerance. Web CPU saturates,
memory approaches its limit, and a finite pool timeout can still fail a larger
or longer burst. The small database's pooler and all real background workers
still require staging qualification. App instances restart between variants;
the database, cache and synthetic history persist. Mixed-user timing therefore
is not a controlled cold-cache speed comparison. Upload bursts are repeated
with pooling disabled and enabled after both initial variants.

These are short local observations, not an SLA or a production sizing promise.

The repeated upload burst completed 54/150 journeys with pooling disabled and
150/150 with pooling enabled. The latter drained in 117.11 seconds. Sampled
connections peaked at 60 without pooling in the submission run and at 17
with pooling (16 web plus monitoring). The final audit found 208 submitted
fixtures, no submissions stuck in flight, and no saved-title ownership
mismatches; the double click accepted exactly one filing. Other local regression
tests ran on the host, so absolute timing should not be treated as dedicated
cloud-core performance. See `pool-micro-summary.json` and the raw result files in the
[external evidence bundle](https://gist.github.com/nonprofittechy/90ab33a3c611ea992db55d29b88d797a).

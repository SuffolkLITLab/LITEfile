# Preparation storage journal and review fixes

Claude's review found a crash window in the first durable preparation change:
S3 could finish a write before the database committed ownership of its key.
A killed process could therefore leave an object that privacy cleanup could not
find. The opt-in queue now commits server-generated keys and their ownership
before sending bytes, both for originals and prepared filing copies. Publication
is a separate short transaction. PostgreSQL connections are returned to the pool
while storage and conversion run; each web process permits four concurrent
original-upload batches. Journaled writes use single S3 PUTs, without background
multipart transfer threads.

An interrupted original upload is never acknowledged as accepted or passed to a
preparation worker. Its page offers removal and re-upload. Accepted originals
remain durable across worker death; fencing prevents an old worker publishing.
Cancellation retains the storage lease, so cleanup and privacy deletion wait
until in-flight writes have stopped. Original upload leases renew to 30 minutes before each file;
preparation leases allow the worker's hard time limit plus 60 seconds. S3 uses
5-second connect and 60-second read timeouts, with at most three attempts.
These are operational bounds, not a transactional guarantee from S3: a storage
service that completes a timed-out write after the grace period would require
later reconciliation. Ownership records remain available for privacy cleanup.

Preparation permits at most ten files and 50 MB per batch, with the existing
10 MB per-file limit. Its child deadline is `max(300, 30 + 120 × files)` seconds,
with a 768 MB address-space limit. A deadline failure asks the filer to split the
batch; it does not repeat the same expensive work three times. Transient errors
retain the bounded three-attempt policy.

Replacement guards compare the original document's storage identity, size and
preparation state. Unrelated filing-choice edits no longer invalidate queued
work. Publication reads the latest document and preserves its filing choices.
Existing queued rows retain the previous fingerprint version for migration
compatibility. Cancellation includes the job ID and reports already-completed
work honestly, preventing a stale tab cancelling a newer upload.

## Connection budget

The effective production Supavisor pool has **15 backend slots**, although
PostgreSQL itself permits more connections. The production URL uses its session
endpoint. Historical local experiments with eight or fifteen pooled connections
per process are not a production configuration recommendation.

A starting budget for one two-process web machine is four connections per web
process (eight total), up to two each for extraction and preparation supervisors
and children, and one for code indexing: thirteen slots, leaving two spare.
Confirm all other database consumers before enabling this. An extra web machine
requires rebudgeting, including overlapping old/new machines during deployment
and the migration release process. Use a stop/start or otherwise explicitly
budgeted rollout; the thirteen-slot example is a steady-state budget. `DB_POOL_MAX_IDLE=60` retires idle pool connections sooner;
it does not replace the aggregate budget. Pooling and asynchronous preparation
remain disabled by default. Deploy migrations and worker support before enabling
preparation; drain jobs before rolling back to code without the queue.

## Validation

Tests interrupt execution immediately after storage writes, verify committed
ownership and no partial publication, exercise cancellation during work, verify
privacy blockers for active and cancelled leases, preserve edited filing
choices, reject changed source bytes, and cover the actual submission gate.
PostgreSQL and Chromium/Gotenberg runs exercise both synchronous and queued
flows. Raw run output and the review transcript belong in the external evidence
archive, not this repository.

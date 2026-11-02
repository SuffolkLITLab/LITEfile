# Queue claim fairness

An extraction worker previously selected the oldest eligible job, tried to lock
its draft with `skip_locked`, and returned no work if that draft was busy. Every
worker could therefore idle behind one busy draft while unrelated work waited.

Claiming now scans at most 64 eligible jobs, ordered by creation and primary key,
and skips both locked drafts and locked jobs. It keeps the existing draft-then-
job lock order, rechecks eligibility while locking, and retains attempt limits,
lease fencing and privacy-deletion checks. Preparation uses the same lock
behavior. If all 64 candidates are busy, the worker still returns to its normal
poll interval; this is bounded scanning, not an unbounded queue sweep.

A real PostgreSQL experiment held the oldest draft lock on another connection.
The previous implementation returned no claim even though the second job was
available. With the change, that same test claims the second job in about 20 ms
on the local test database, then claims the first after releasing its lock.
Holding the job row instead of the draft also passes. Two simultaneous workers
cannot claim the same job. This demonstrates avoided worker idling under
contention; it does not make an individual model call faster.

All 31 PostgreSQL fairness/extraction tests pass. Three existing management-command
tests now use real transaction boundaries because those commands explicitly
close connections; a Django test-owned atomic block otherwise fails on pooled
PostgreSQL while SQLite masks the problem. Application behavior is unchanged by
that test-fixture correction.

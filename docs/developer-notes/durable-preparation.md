# Durable document preparation

> Follow-up: the effective production Supavisor session pool has 15 slots.
> Use the [reviewed connection budget and storage journal](preparation-storage-journal.md)
> before enabling these changes; larger local pool experiments below are historical.

This opt-in change addresses the remaining queue work in #372. Parsing and
conversion move out of the upload request and out of the database transaction.
The request saves every original, its hash and ownership record, and one durable
batch job before returning HTTP 202. The browser shows saved progress, resumes
after navigation, and offers retry or removal for a failed batch.

## Safety boundaries

A request UUID and content fingerprint make retries idempotent. Only one
unfinished batch per draft is allowed. A worker claims a bounded lease with a
fresh token; expired or superseded tokens cannot publish. Three automatic
attempts and exponential retry delays bound failures. A supervisor runs one
child at a time, with a 768 MB address-space limit and a five-minute timeout;
a six-minute lease allows restart recovery after the supervisor itself dies.
A final-attempt crash becomes a visible failure instead of remaining stuck.

Originals are retained through preparation failures. Conversion occurs outside
transactions. Publication locks the draft, checks the lease and document
fingerprint again, and atomically publishes the whole batch. A replacement
keeps the old copy until publication. Existing review and submission guards
also block unfinished or failed batches, so the old copy cannot be submitted
while its replacement is pending. Prepared copies still require preview
approval. Cancellation invalidates the claim. Privacy freezing uses the same
draft lock and tracked original keys; late workers cannot recreate deleted
filings or upload new copies after the freeze. Failed storage cleanup retains
ownership for subsequent privacy deletion.

The upload batch limit is 50 MB, with the existing 10 MB limit per file. Larger
sets can be added in successive batches. The feature is disabled by default;
synchronous callers such as handoff retain their existing contract. Converting
an invalid file now produces a saved, actionable batch failure rather than an
immediate upload error when the feature is enabled. These are intentional
workflow changes; court payloads, PDF validation, original provenance, AI
opt-out, and preview requirements remain the same.

## Rollout and rollback

Apply migrations first. Start a dedicated `python manage.py
process_document_preparations` process on a modest worker with enough memory
for the 768 MB child plus its supervisor (start with 1 GB and observe RSS).
Keep one worker initially. Set `DOCUMENT_PREPARATION_ASYNC=true` only after
confirming the worker can reach storage and Gotenberg and can drain a synthetic
job. No production machine or setting is changed by this PR.

To stop new asynchronous acceptance, turn the flag off. Keep the worker running
until existing batches are complete or explicitly cancelled; pending batches
continue to block unsafe submission even with the flag off. Do not remove the
migration or stop the worker with queued work outstanding. Multiple supervisors
can claim separate drafts; skip-locked claiming avoids idling behind a busy
draft, while database constraints and claim fencing prevent duplicate publish.

## Validation

The initial component run passed 65 preparation, extraction and status tests.
Real PostgreSQL and browser validation passed the queue tests and both versions
of the existing Gotenberg/PDF.js workflow: synchronous and asynchronous. The
same filled answers, Word conversion, preview approval, mobile preview and
invalid-file recovery assertions run in both modes. Additional failure injection
covers expired claims, final-attempt crashes, cancellation, privacy freezing,
replacement preservation, no partial batch publication, and retained ownership
when cleanup fails. Generated browser screenshots stay outside Git.

These checks establish behavior and failure handling. They do not by themselves
establish burst capacity or reduced total CPU: queueing changes when work runs,
and spawning a fresh bounded child has overhead. Acceptance latency and total
queue drain time must be reported separately in the capacity experiment.

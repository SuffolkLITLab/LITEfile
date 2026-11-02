# Status polling under load

Fourth change in the capacity stack, following #378; addresses #373.

The status endpoint now reads the explicitly selected draft without creating a
new draft when that pointer is absent. Ownership and jurisdiction checks stay in
`get_current_draft`. Django's rolling session expiry is unchanged: each poll
still writes its session. Reducing the number of polls reduces those writes.

The browser starts at 2.5 seconds, backs off with jitter to 8–10 seconds, resets
on a status transition, and caps the next delay at the server's remaining manual
entry wait. The server rounds that remaining time upward to a whole second;
that rounding can add less than one second. Terminal responses stop polling,
and a slow request never starts overlapping polls. Network failures retain the
existing retry and fallback behavior. Completed analysis can become visible up
to ten seconds later, a deliberate tradeoff for lower request and database load.

A deterministic two-minute waiting experiment with random values 0, 0.5 and 1
compares the actual exported scheduling function with the previous fixed
2.5-second interval. See the JavaScript test for the executable midpoint case.
The new schedule needs at most 20 checks rather than 49 (at least 59% fewer),
including the initial request. This is a scheduling experiment, not a claim
that complete application throughput improves by 59%.

Validation: 30 Django status/extraction tests and all 93 JavaScript unit tests
pass. Tests verify no unexpected draft creation, no draft mutation, retained
session refresh, bounded scheduling, no overlapping requests, and stopping on a
terminal response. No production setting or session-expiry policy was changed.

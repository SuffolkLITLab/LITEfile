# Reuse PDF inspection within one preparation

Related work: #372 under #270. This is a small CPU optimization, not the durable
preparation queue proposed in that issue. Profiling/inspection found repeated
parsing of identical input and converter-output bytes within one request.

`prepare_document` now passes its inspected input to `_flatten`, and `_flatten`
reuses its parsed converter output when blank-widget cleanup returns the same
bytes object. If cleanup rewrites the PDF, the new bytes are inspected again.
There is no cross-request cache or retained reader shared between filers.

The original validation still runs: encrypted/empty/malformed PDFs and XFA are
rejected; filled signatures cannot be flattened; filled answers must survive;
leftover fields and changed page counts are rejected; blank widgets are removed;
originals, filenames, operation metadata, and jurisdiction-specific flattening
choices are preserved. Word conversions still validate the converted PDF.

## Micro-experiment

`benchmarking/capacity/pdf_micro.py` compares the baseline implementation from
commit `0ff456b` to the candidate. For each synthetic Illinois form, it obtains
one real local Gotenberg response and then fixes that response during timed
runs, isolating Python preparation CPU from converter/network variability.
It alternates baseline/candidate order, discards warmup, records six timings,
and asserts identical converter request bytes/arguments, prepared bytes,
filename, preparation operation, and original-form-field metadata.

The three flat inputs go from two inspections to one. The three interactive
inputs go from four inspections to two. This saves parsing; it deliberately
does not remove appearance repair, text extraction, or safety checks, which
still account for most work on complex forms. Reduced parsing alone is not a
solution to the statewide interactive-PDF burst failures.

## Behavioral verification

The existing preparation/preview tests cover invalid and lossy conversion,
filled answers, signatures, hidden/special widgets, blank-widget cleanup,
per-jurisdiction behavior, original storage, failed preparation, and review
requirements. The PostgreSQL/browser suite also exercises real local Gotenberg
and Chromium/PDF.js for multiline forms and Word files, then navigation,
review acknowledgments, payments, and draft persistence. Multi-state end-to-end
HTTP flows reach simulated submission. Court/payment responses, S3, and AI are
isolated test boundaries; this does not certify real provider behavior.

No workflow, session expiration, polling cadence, UI timeout, or automatic
submission retry is changed. The intended difference is less CPU spent parsing
the same document. Timing percentages are short local measurements, not a
production throughput guarantee.

## Recorded CPU results

Times are median process CPU milliseconds per file; converter time is excluded.

| Input | Baseline | Candidate | CPU reduction |
| --- | ---: | ---: | ---: |
| flattened IL-01.pdf | 2.87 ms | 1.67 ms | 41.7% |
| flattened IL-02.pdf | 3.95 ms | 2.21 ms | 44.1% |
| flattened IL-03.pdf | 1.71 ms | 0.87 ms | 49.3% |
| interactive IL-01.pdf | 243.59 ms | 245.07 ms | -0.6% |
| interactive IL-02.pdf | 782.38 ms | 732.14 ms | 6.4% |
| interactive IL-03.pdf | 353.67 ms | 324.79 ms | 8.2% |

The sum of per-file medians for the interactive packet fell about 5.6%; the
first interactive form was effectively unchanged (0.6% slower). Flat-PDF parsing
fell roughly 42–49%, but saves only milliseconds per file. These modest results
justify removing redundant work, not a claim that preparation is no longer a
bottleneck. Raw six-round CPU samples and equality assertions are recorded in
`pdf-inspection-micro.json` in the [external evidence bundle](https://gist.github.com/nonprofittechy/90ab33a3c611ea992db55d29b88d797a).

All 92 targeted PostgreSQL/browser checks passed with both fixes. The isolated
regression settings supply dummy storage credentials for locally generated
signed URLs; no real S3 account is needed. A temporary-checkout full-suite run
passed 2,396 tests and failed three credential-dependent live-profile tests
(401), with 15 skips. The normal-checkout full run passed **2,399 tests with 15 skips**
in 405.51 seconds, resolving those environment-dependent profile failures.
GitHub CI also passed on both code-change commits. Ruff, type checks, Bandit,
migration checks, and JavaScript unit tests passed. No unintended behavior
change was found in the covered workflows; live court/model acceptance remains
outside the isolated end-to-end tests. All temporary containers, synthetic
authentication fixtures, and the validation worktree were removed. Incremental
cloud spend remained $0.

## Reproduce the focused checks

From the repository root, with Docker, the development Python dependencies,
Node dependencies, and Playwright Chromium installed:

```bash
docker run -d --name litefile-micro-browser-db --cpus 1 --memory 1g \
  -e POSTGRES_USER=capacity -e POSTGRES_PASSWORD=local-capacity-only \
  -e POSTGRES_DB=capacity -p 127.0.0.1:8543:5432 postgres:17
docker run -d --name litefile-micro-converter --cpus 2 --memory 1g \
  -p 127.0.0.1:8300:3000 gotenberg/gotenberg:8
# Wait until both local services are ready.
DB_POOL_MAX_SIZE=8 PYTHONPATH="$PWD/benchmarking" \
  CORE_FILING_UX_BROWSER_TESTS=1 DOCUMENT_PREPARATION_BROWSER_TESTS=1 \
  PAYMENT_FLOW_BROWSER_TESTS=1 WAIVER_HIERARCHY_BROWSER_TESTS=1 \
  uv run --directory efile_app pytest --ds=capacity.regression_settings -q \
  efile/tests/test_database_pool.py efile/tests/test_core_filing_ux_browser.py \
  efile/tests/test_document_preparation_browser.py efile/tests/test_payment_flow_browser.py \
  efile/tests/test_end_to_end_new_flow_states.py efile/tests/test_durable_drafts.py \
  efile/tests/test_security_boundaries.py efile/tests/test_submission_tracker.py
DJANGO_SETTINGS_MODULE=efile.settings_dev PYTHONPATH="$PWD/efile_app" \
  uv run --project efile_app python benchmarking/capacity/pdf_micro.py \
  --output /tmp/pdf-inspection-micro.json
docker rm -fv litefile-micro-browser-db litefile-micro-converter
```

The fixed loopback regression settings deliberately use dummy S3 credentials
for URL signing. No production database credentials belong in this setup.
Snapshots/screenshots created by browser tests stay under temporary test paths,
not in the repository. The full ordinary suite uses `cd efile_app && uv run pytest -q`;
its credential-dependent live-profile tests have different environmental
requirements from these isolated regressions.

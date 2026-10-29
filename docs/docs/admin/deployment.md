---
id: deployment
title: Production deployment & cloud setup
sidebar_label: Deployment guide
sidebar_position: 4
---

# Production deployment & cloud setup <span className="wip-badge">WIP</span>

This guide explains how to deploy LITEfile to cloud infrastructure using **Docker**, **Fly.io**, and **AWS S3**.

---

## Production and staging

Production runs at **https://app.litefile.org** on the Fly app `litefile-prod`,
using `fly.production.toml`, `efile.settings_prod`, and a separate Supabase
project named `litefile-prod`. Fly runs in Chicago (`ord`), near the database
in AWS Ohio (`us-east-2`). Cloudflare provides DNS-only A and AAAA records;
Fly terminates HTTPS with an automatically renewed certificate.

A push or merge to `main` runs CI. After CI succeeds, the **Deploy production**
workflow deploys that exact commit, runs database migrations, and checks the
public site. Deployments are serialized. The GitHub `production` environment
records deployments. `FLY_PRODUCTION_API_TOKEN` is restricted to the production
Fly app; renew it before October 29, 2027.

The **Deploy staging** workflow can be run manually for a selected branch. It
uses `fly.toml`, `efile.settings_staging`, and the existing `FLY_API_TOKEN`.
For a manual production deployment, run **Deploy production** on `main`, or:

```bash
fly deploy --config fly.production.toml --remote-only
```

Production initially reuses the existing OpenAI, S3, Gotenberg, handoff, and
**test EFSP** credentials. Its Django secret and application database are
separate. Staging users, sessions, and filings are not copied. S3 storage is
currently shared with staging. The database uses the Supabase session pooler
(port 5432) so worker advisory locks remain valid. Client roles `anon` and
`authenticated` have no access to the Django `public` schema; preserve this
restriction if recreating the project.

Before accepting real filings, update `SUFFOLK_EFILE_API_KEY`, `EFSP_URL`, and
`EFSP_CODES_DATABASE_URL` on `litefile-prod` together to use the production
EFSP. Change `FILING_CODE_SYNC_TIME` in `fly.production.toml` from `21:30` to
`04:30`, then rebuild the filing-code index for the new EFSP source. Verify the
production login and filing flow after that switch.

## 1. Docker build & image optimization

LITEfile includes a production-ready `Dockerfile` using multi-stage caching and [Astral uv](https://astral.sh/uv).

### Preventing documentation from deploying with the Docker build

Documentation files (`docs/`, `node_modules/`, `.docusaurus/`) are strictly excluded from the Docker build context via `.dockerignore`. This ensures:
- Docker image size remains minimal (~200 MB).
- Fast build times and reliable layer caching.
- Documentation site is deployed independently (e.g. via GitHub Pages or static CDN) and is not packaged or deployed to Fly.io.

```dockerfile
# Dockerfile snippet
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /app/efile_app
COPY efile_app/pyproject.toml efile_app/uv.lock* ./
RUN uv sync --frozen --no-install-project

COPY . /app
RUN uv sync --frozen

# Static asset collection
RUN DJANGO_SETTINGS_MODULE=efile.settings_staging \
    DJANGO_SECRET_KEY=build-static-collect-key \
    DATABASE_URL=sqlite:////tmp/build-collectstatic.sqlite3 \
    uv run python manage.py collectstatic --noinput

EXPOSE 8000
CMD ["uv", "run", "gunicorn", "efile.asgi:application", "-k", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8000"]
```

---

## 2. Deploying to Fly.io

LITEfile uses `fly.toml` for staging and production hosting on [Fly.io](https://fly.io):

```toml
# fly.toml
app = 'litefile-staging'
primary_region = 'lax'

[env]
  DJANGO_SETTINGS_MODULE = "efile.settings_staging"
  PYTHONUNBUFFERED = "1"
  FILING_CODE_SYNC_MODE = "database"
  # Staging uses the test proxy; production should copy after its own refresh.
  FILING_CODE_SYNC_TIME = "21:30"
  FILING_CODE_SYNC_TIMEZONE = "America/New_York"

[deploy]
  # Run database migrations before each release
  release_command = "uv run python manage.py migrate --noinput --fake-initial"
  release_command_timeout = "30m"

[processes]
  app = "uv run gunicorn efile.asgi:application -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000 --workers 2 --timeout 60 --access-logfile - --error-logfile - --log-level debug"
  extraction_worker = "uv run python manage.py process_document_extractions"
  code_index_worker = "uv run python manage.py refresh_filing_code_index --daily --retry-interval 900"

[http_service]
  internal_port = 8000
  force_https = true
  auto_stop_machines = 'stop'
  auto_start_machines = true
  min_machines_running = 1
  processes = ['app']

[[vm]]
  memory = '1gb'
  cpu_kind = 'shared'
  cpus = 1
```

### Document extraction worker

Document analysis runs outside the web request in the `extraction_worker` process group. The web process stores the upload and queues a durable database job; the worker downloads the original lead document from S3 and records the extracted details on the filing draft. PDFs retain their stored form values for extraction. DOCX files are read locally with `docx2python`, and their text is supplied to analysis. Older binary DOC files use the converted PDF. Keep at least one worker Machine running so queued documents are analyzed.

By default, LITEfile sends only the first 20 PDF pages for analysis. Set `DOCUMENT_EXTRACTION_MAX_PAGES` to a positive integer to change that cap. DOCX text is limited to the first 100,000 characters with `DOCUMENT_EXTRACTION_MAX_TEXT_CHARS`; Word files have no reliable page boundaries. Review identifies when either limit omitted part of a document. `DOCUMENT_EXTRACTION_MAX_ATTEMPTS` controls how many times a failed job is tried before the filer is sent to manual review.

### Filing code search index

For routine health checks, recovery choices, ownership and escalation, use the
[filing-code operating checklist](./filing-code-operations.md). The setup details
below remain the reference for provisioning.

Filing codes come straight from the EFSP proxy's codes database. The proxy
updates its codes from Tyler once a day (production at 02:13, test at 19:35
Eastern). Each day at `FILING_CODE_SYNC_TIME` in `FILING_CODE_SYNC_TIMEZONE`, the
`code_index_worker` copies every court whose codes changed into LITEfile's own
database, then rebuilds the search index for those courts. Set the time well
after the proxy's update: Fly staging, which uses the test EFSP, runs at 21:30
Eastern. Production should run at about 04:30. Keep one Machine running in the
`code_index_worker` process group.

LITEfile connects with `EFSP_CODES_DATABASE_URL` and only ever reads. The court
list and every court's tables are read in one `READ ONLY`, `REPEATABLE READ`
transaction, so the copy is a consistent snapshot and the database refuses any
write. Supabase's connection pooler ignores connection-level settings, which is
why read-only is set per transaction. The queries match the proxy's own
filing-catalog export: courts with all three installed code lists, non-criminal
case categories, and filings that aren't court-use only.

LITEfile connects as its own role, `litefile_codes_reader`, never with the
proxy's credentials. The proxy keeps user data in the same database, so the role
can read only the five code tables. It is also read only at the server, which
holds even through Supabase's pooler. The test EFSP's database already has it.
For another environment's database, run this once as its `postgres` role,
with a new random password:

```sql
CREATE ROLE litefile_codes_reader LOGIN PASSWORD '<random>'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS
  CONNECTION LIMIT 5;
ALTER ROLE litefile_codes_reader SET default_transaction_read_only = on;
ALTER ROLE litefile_codes_reader SET statement_timeout = '10min';
GRANT CONNECT ON DATABASE postgres TO litefile_codes_reader;
GRANT USAGE ON SCHEMA public TO litefile_codes_reader;
GRANT SELECT ON public.location, public.installedversion, public.casecategory,
  public.casetype, public.filing TO litefile_codes_reader;
```

Through Supabase's pooler, the user name is `litefile_codes_reader.<project ref>`:
`postgresql://litefile_codes_reader.<project ref>:<password>@<pooler host>:5432/postgres?sslmode=require`.
The proxy's nightly update reloads these tables' rows without dropping them, so
the grants persist. If a proxy schema migration ever recreates one of them,
grant `SELECT` on it again. Postgres lets every role create session-private
temporary tables (a `PUBLIC` default that can't be revoked from one role). Those
can't touch shared data, and the role's read-only default refuses them unless a
client turns read-only off, which LITEfile never does.

A court whose revision is unchanged is not read again. A court that suddenly
has no filing types usually means the proxy's update is half finished, so the
copy stops and keeps the old one. Failed states are retried every 15 minutes
(`--retry-interval 900`), then the schedule returns to daily. On startup, the
worker syncs immediately if any jurisdiction has no current index. That is the
case on a fresh deployment, or after a search-rules change.

#### Resync and rebuild from the staff tools

Superusers see a **Filing codes** page in the staff tools (at the private `LITEFILE_STAFF_PATH`). It shows
each jurisdiction's copy and index, recent runs, and two buttons, for one
jurisdiction or all of them:

- **Resync codes** copies changed courts from the EFSP now, then updates their
  part of the index.
- **Rebuild code search indexes** rebuilds the whole index from the copy already
  in LITEfile, without contacting the EFSP. Use it after changing search rules
  in `filing_code_search.yaml`.

The buttons queue a job, which the code index worker starts within a minute.
Each request is recorded in the staff audit log.

#### From the command line

Run these from `efile_app/` in the target environment:

```bash
uv run python manage.py migrate --noinput
uv run python manage.py refresh_filing_code_index                          # resync every state now
uv run python manage.py refresh_filing_code_index --jurisdiction vermont   # one state
uv run python manage.py refresh_filing_code_index --rebuild                # rebuild from the local copy
uv run python manage.py refresh_filing_code_index --dry-run --jurisdiction vermont  # report changes only
uv run python manage.py refresh_filing_code_index --daily                  # what the worker runs
```

On the test EFSP, Vermont (22 courts, about 1.1 million search paths) copies and
indexes in about two minutes. A run with nothing changed takes seconds.

`FILING_CODE_SYNC_MODE=bulk` (the proxy's HTTP `filing_catalog` export) and
`legacy` (the full list-API crawler, `--legacy-crawl`) remain available as
fallbacks. Neither needs database access. `--cache-dir` applies only to the
legacy crawler.

Local Docker Compose and `./run_all.sh` start the same daily worker. They read
`EFSP_CODES_DATABASE_URL` and `FILING_CODE_SYNC_TIME` from `efile_app/.env`.

Before the first successful sync, the modal explains that search is unavailable
and the usual court lists remain available. The dialog does not trigger indexing,
but retries an unavailable search automatically while it stays open.
Results older than two days carry an out-of-date notice. Every selected path is
checked against the live court lists before it updates the form. A changed
`EFSP_URL` or thesaurus revision requires a fresh index, so test-server codes and
incompatible search terms cannot leak into another environment.

### Setting Fly.io production secrets:
```bash
fly secrets set \
  DJANGO_SECRET_KEY="generate-a-strong-random-key" \
  DATABASE_URL="postgres://..." \
  AWS_ACCESS_KEY_ID="AKIA..." \
  AWS_SECRET_ACCESS_KEY="..." \
  AWS_S3_BUCKET_NAME="litefile-production-documents" \
  AWS_S3_REGION_NAME="us-east-1" \
  OPENAI_API_KEY="sk-..." \
  GOTENBERG_URL="https://..." \
  EFSP_CODES_DATABASE_URL="postgresql://<read-only role>:...@<host>:5432/postgres?sslmode=require" \
  GOTENBERG_USERNAME="..." \
  GOTENBERG_PASSWORD="..."
```

---

## 3. AWS S3 storage setup

LITEfile stores uploaded PDFs in a private S3 bucket and generates presigned URLs for e-file proxy document ingestion. Set `AWS_S3_REGION_NAME` to the bucket's actual region. Keep all four S3 Block Public Access settings enabled, use "Bucket owner enforced" object ownership, and do not add a public bucket policy or object ACLs. The proxy downloads each presigned URL with an HTTP GET; CORS is not needed for that server-to-server request.

### IAM policy

Attach this policy to the application IAM principal, replacing `YOUR-BUCKET-NAME`:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListDocumentsForConnectionCheck",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::YOUR-BUCKET-NAME",
      "Condition": {"StringLike": {"s3:prefix": "efile-documents/*"}}
    },
    {
      "Sid": "ManageDocuments",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject", "s3:AbortMultipartUpload"],
      "Resource": "arn:aws:s3:::YOUR-BUCKET-NAME/efile-documents/*"
    }
  ]
}
```

For a bucket configured with an older `PublicReadGetObject` policy, remove that statement and verify all four public access blocks. If the bucket uses a customer-managed KMS key, grant the application principal the required `kms:GenerateDataKey` and `kms:Decrypt` permissions for that key as well. After deploying, upload a test PDF, verify that its unsigned object URL returns 403 and its fresh presigned URL downloads successfully, then delete the test object. Keep presigned URLs out of logs and issue reports because they grant temporary access.

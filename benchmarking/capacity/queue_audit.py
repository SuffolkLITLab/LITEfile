"""Wait for local durable jobs to drain, then verify persisted batch outcomes."""

import argparse
import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COMPOSE = ["docker", "compose", "-f", str(ROOT / "compose.yml")]


def sql(query):
    return json.loads(
        subprocess.check_output(
            COMPOSE + ["exec", "-T", "db", "psql", "-U", "capacity", "-Atc", query],
            text=True,
            timeout=15,
        )
    )


parser = argparse.ArgumentParser()
parser.add_argument("--expected", type=int, default=150)
parser.add_argument("--accepted-results", type=Path, required=True)
parser.add_argument("--max-seconds", type=int, default=1800)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
assert 1 <= args.expected <= 150 and 1 <= args.max_seconds <= 3600
accepted = json.loads(args.accepted_results.read_text())
job_ids = sorted({row.get("job_id") for row in accepted["rows"] if row.get("accepted")})
assert len(job_ids) == args.expected and all(
    isinstance(pk, int) and pk > 0 for pk in job_ids
)
cohort = ",".join(str(pk) for pk in job_ids)
started = time.monotonic()
samples = []
while True:
    sample = sql(f"""select json_build_object('pending',count(*) filter(where status='pending'),
    'processing',count(*) filter(where status='processing'),'complete',count(*) filter(where status='complete'),
    'failed',count(*) filter(where status='failed'),'retried',count(*) filter(where attempts>1),
    'oldest_pending_seconds',extract(epoch from now()-(min(created_at) filter(where status='pending'))),
    'accepted_to_complete_p95_seconds',percentile_cont(.95) within group(order by extract(epoch from completed_at-created_at)) filter(where status='complete'))
    from efile_documentpreparationjob where id in ({cohort})""")
    sample["observed_seconds"] = time.monotonic() - started
    samples.append(sample)
    if (
        sample["complete"] + sample["failed"] >= args.expected
        or time.monotonic() - started > args.max_seconds
    ):
        break
    time.sleep(5)
# The queue job's draft must contain exactly the three prepared, unreviewed copies.
audit = sql(f"""select json_build_object('jobs',count(*),'valid_batches',count(*) filter(where status='complete' and docs=3 and leads=1 and prepared=3 and originals=3 and reviewed=0)) from
(select j.id,j.status,count(d.id) docs,count(d.id) filter(where d.role='lead') leads,
count(d.id) filter(where d.preparation in ('flattened','converted','converted_flattened')) prepared,
count(d.id) filter(where d.original_s3_key<>'') originals,count(d.id) filter(where d.preparation_reviewed_at is not null) reviewed
from efile_documentpreparationjob j left join efile_filingdocument d on d.draft_id=j.draft_id where j.id in ({cohort}) group by j.id,j.status) batches""")
result = {
    "samples": samples,
    "audit": audit,
    "elapsed_seconds": time.monotonic() - started,
    "expected": args.expected,
    "job_ids": job_ids,
}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(result, indent=2))
print(
    json.dumps(
        {
            "last_sample": samples[-1],
            "audit": audit,
            "elapsed_seconds": result["elapsed_seconds"],
        }
    )
)
raise SystemExit(
    0 if audit["valid_batches"] == args.expected and samples[-1]["failed"] == 0 else 1
)

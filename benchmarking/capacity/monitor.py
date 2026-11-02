"""Run a bounded scenario while collecting container and PostgreSQL telemetry."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

compose = ["docker", "compose", "-f", str(Path(__file__).with_name("compose.yml"))]
output = Path(sys.argv[1])
output.parent.mkdir(parents=True, exist_ok=True)
command = sys.argv[2:]
process = subprocess.Popen(
    command,
    stdout=output.with_suffix(".log").open("w"),
    stderr=subprocess.STDOUT,
    start_new_session=True,
)


def stop(signum, frame):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
    raise SystemExit(128 + signum)


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
container_ids = subprocess.check_output(compose + ["ps", "-q"], text=True).split()
samples = []
query = """select json_build_object(
 'connections', (select count(*) from pg_stat_activity where datname='capacity'),
 'active', (select count(*) from pg_stat_activity where datname='capacity' and state='active'),
 'lock_waiters', (select count(*) from pg_stat_activity where datname='capacity' and wait_event_type='Lock'),
 'deadlocks', (select deadlocks from pg_stat_database where datname='capacity'),
 'temp_bytes', (select temp_bytes from pg_stat_database where datname='capacity'),
 'database_bytes', pg_database_size('capacity'),
 'pending_extractions', (select count(*) from efile_documentextraction where status='pending'))"""


if os.getenv("CAPACITY_MONITOR_PREPARATION") == "1":
    query = (
        query[:-1]
        + """, 'pending_preparations',
      (select count(*) from efile_documentpreparationjob where status='pending'),
      'processing_preparations', (select count(*) from efile_documentpreparationjob where status='processing'),
      'complete_preparations', (select count(*) from efile_documentpreparationjob where status='complete'),
      'failed_preparations', (select count(*) from efile_documentpreparationjob where status='failed'))"""
    )


def write_snapshot(exit_code):
    temporary = output.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {"command": command, "exit_code": exit_code, "samples": samples}, indent=2
        )
        + "\n"
    )
    temporary.replace(output)


while process.poll() is None:
    entry = {"time": time.time()}
    try:
        stats = subprocess.check_output(
            [
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{json .}}",
                *container_ids,
            ],
            timeout=10,
            text=True,
        )
        entry["containers"] = [json.loads(line) for line in stats.splitlines()]
        db = subprocess.check_output(
            compose + ["exec", "-T", "db", "psql", "-U", "capacity", "-Atc", query],
            timeout=10,
            text=True,
            stderr=subprocess.STDOUT,
        )
        entry["postgres"] = json.loads(db)
    except Exception as exc:
        entry["monitor_error"] = str(exc)
    samples.append(entry)
    write_snapshot(None)
    time.sleep(3)
write_snapshot(process.wait())
sys.exit(process.returncode)
